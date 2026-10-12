"""Two reusable spawn workers; bounded streaming, ordered rows and cancellation."""
import multiprocessing as mp
import os
import time
from collections import OrderedDict, deque
from pathlib import Path
from queue import Empty

from .core import extract_invoices
from .reader import InvoiceReader

BUFFER_ROWS = 8
FILE_WINDOW = 4
OWNER_LIMIT = 32
PAGE_BATCH = 2
NATIVE_ROWS = 100


def _worker(commands, results, stop, factory, threads):
    reader = factory(stop=stop,threads=threads,cache_files=16,cache_records=100)
    if isinstance(reader,InvoiceReader):
        import cv2
        # OpenCV otherwise creates a full CPU-sized pool in each process,
        # in addition to ONNX's model threads. Keep preprocessing modest.
        cv2.setNumThreads(1)
    while True:
        command = commands.get()
        if command is None:
            return
        path, single_invoice, page_range = command
        reader.single_invoice = single_invoice
        try:
            stream = (reader.read_pdf_pages(path,*page_range) if page_range is not None
                      else reader.read(path))
            for row in stream:
                # The coordinator drains both bounded streams even on cancel.
                results.put(('row',row))
        except Exception as exc:
            results.put(('row',InvoiceReader.failure(path,page_range[0]+1 if page_range else 1,exc)))
        # Confirm actual cache ownership, including evictions, without sending
        # OCR data or guessing whether a partial/failed file was cached.
        results.put(('complete',(not stop.is_set(),tuple(reader._cache),getattr(reader,'cache_hits',0))))


class ParallelReader:
    """Owned by one coordinator at a time; OCR models survive later batches."""
    def __init__(self, reader_factory=InvoiceReader):
        self._context = mp.get_context('spawn')
        self._stop = self._context.Event()
        self._factory = reader_factory
        self._processes = []
        self._commands = []
        self._results = []
        self._busy = False
        # Only routing hints, never OCR data. Readers still hash the actual
        # content independently before serving their own bounded caches.
        self._owners = OrderedDict()
        self._cache_hits = [0,0]

    @property
    def pids(self):
        return tuple(process.pid for process in self._processes)

    @property
    def cache_hits(self):
        return sum(self._cache_hits)

    def _start(self):
        if self._processes:
            return
        threads = max(1,min(4,(os.cpu_count() or 2)//2))
        try:
            for _ in range(2):
                commands = self._context.Queue(maxsize=1)
                results = self._context.Queue(maxsize=BUFFER_ROWS)
                process = self._context.Process(target=_worker,
                    args=(commands,results,self._stop,self._factory,threads),daemon=True)
                self._commands.append(commands)
                self._results.append(results)
                process.start()
                self._processes.append(process)
        except Exception:
            self.close()
            raise

    def read(self, paths, stop=None, single_invoice=False):
        paths = list(map(Path,paths))
        if not paths or (stop is not None and stop.is_set()):
            return
        if self._busy:
            raise RuntimeError('并行读取器正在处理上一批文件。')
        self._busy = True
        exhausted = False
        try:
            page_count = 0
            pdf_digest = None
            native_rows = None
            if len(paths) == 1 and paths[0].suffix.lower() == '.pdf':
                import pymupdf as fitz
                try:
                    with fitz.open(paths[0]) as doc:
                        if not doc.needs_pass:
                            page_count = len(doc) if len(doc)>1 else 0
                            if self._factory is InvoiceReader:
                                # Short text-only PDFs can finish before spawn
                                # startup. Bound this preflight, close the PDF
                                # before yielding, and never cache these rows.
                                native_rows = []
                                native_reader = InvoiceReader()
                                for page in doc:
                                    if stop is not None and stop.is_set():
                                        raise InterruptedError()
                                    boxes = native_reader.native_boxes(page)
                                    rows = extract_invoices(boxes,'pdf')
                                    if (not rows or not all(r.status == '已识别' for r in rows)
                                            or native_reader.needs_amount_ocr(page,rows,boxes)
                                            or len(native_rows)+len(rows)>NATIVE_ROWS):
                                        native_rows = None
                                        break
                                    for row in rows:
                                        row.source,row.page = str(paths[0].resolve()),page.number+1
                                    native_rows.extend(rows)
                            if page_count and native_rows is None:
                                pdf_digest = InvoiceReader.fingerprint(paths[0],stop)
                                # A whole-file cache from a prior multi-file batch
                                # is cheaper than subdividing already-read content.
                                key = ('.pdf',pdf_digest,single_invoice)
                                if key in self._owners:
                                    page_count = 0
                except InterruptedError:
                    exhausted = True  # No work dispatched; keep prior idle models.
                    return
                except (OSError,ValueError,RuntimeError):
                    native_rows = None
                    page_count = 0
                    pass  # Normal reader emits the existing failure record.
            if native_rows:
                yield 'progress',(0,1,paths[0].name)
                for row in native_rows:
                    if stop is not None and stop.is_set():
                        break
                    yield 'row',row
                if stop is None or not stop.is_set():
                    yield 'complete',paths[0]
                exhausted = True
                return
            batch = PAGE_BATCH if page_count >= 4 else 1
            count = (page_count+batch-1)//batch if page_count else len(paths)
            def job_path(job):
                return paths[0] if page_count else paths[job]
            def job_range(job):
                return (job*batch,min((job+1)*batch,page_count)) if page_count else None
            self._start()
            self._stop.clear()
            active = {}
            buffers = {}
            finished = {}
            waiting = OrderedDict()
            prepared = index = 0
            progress_index = -1
            complete_parts = 0
            while index < count:
                if stop is not None and stop.is_set():
                    self._stop.set()
                # Fingerprint at most the bounded look-ahead window. Only
                # confirmed cache hits wait for an owner; misses remain parallel.
                while not self._stop.is_set() and prepared < min(count,index+FILE_WINDOW):
                    path = job_path(prepared)
                    try:
                        digest = pdf_digest if page_count else InvoiceReader.fingerprint(
                            path,stop if stop is not None else self._stop)
                        key = (path.suffix.lower(),digest,single_invoice)
                        if page_count:
                            key += (job_range(prepared),)
                    except InterruptedError:
                        self._stop.set()
                        break
                    except OSError:
                        key = None
                    waiting[prepared] = key
                    buffers[prepared] = deque()
                    prepared += 1
                # Refill whichever process finishes first, without allowing
                # unbounded look-ahead behind a slow first file.
                for slot in range(2):
                    if slot in active or self._stop.is_set():
                        continue
                    job = next((job for job,key in waiting.items()
                                if key is None or self._owners.get(key,slot) == slot),None)
                    if job is None:
                        continue
                    waiting.pop(job)
                    self._commands[slot].put((job_path(job),single_invoice,job_range(job)))
                    active[slot] = job
                if index >= prepared:
                    break
                if index in waiting and self._stop.is_set():
                    del waiting[index],buffers[index]
                    index += 1
                    continue
                if progress_index != index:
                    progress_index = index
                    label = job_path(index).name
                    if page_count:
                        start,end = job_range(index)
                        label += f'（第 {start+1}–{end} 页 / 共 {page_count} 页）'
                    yield 'progress',(index,count,label)
                    continue
                if buffers[index]:
                    yield 'row',buffers[index].popleft()
                    continue
                if index in finished:
                    if finished.pop(index):
                        complete_parts += 1
                        if not page_count or complete_parts == count:
                            yield 'complete',job_path(index)
                    del buffers[index]
                    index += 1
                    continue
                received = False
                for slot,job in list(active.items()):
                    if len(buffers[job]) >= BUFFER_ROWS:
                        continue
                    try:
                        kind,value = self._results[slot].get_nowait()
                    except Empty:
                        if not self._processes[slot].is_alive():
                            raise RuntimeError('并行识别进程意外退出，请重试本批文件。')
                        continue
                    received = True
                    if kind == 'row':
                        buffers[job].append(value)
                    else:
                        complete,keys,hits = value
                        self._cache_hits[slot] = hits
                        finished[job] = complete
                        for key in list(self._owners):
                            if self._owners[key] == slot and key not in keys:
                                del self._owners[key]
                        for key in keys:
                            self._owners[key] = slot
                            self._owners.move_to_end(key)
                        while len(self._owners) > OWNER_LIMIT:
                            self._owners.popitem(last=False)
                        del active[slot]
                if not received:
                    time.sleep(.02)
            exhausted = True
        finally:
            self._busy = False
            if not exhausted:
                # An abandoned iterator cannot leave blocked producers behind.
                self.close()

    def close(self):
        self._stop.set()
        for process in self._processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            process.close()
        self._processes.clear()
        self._owners.clear()
        self._cache_hits = [0,0]
        for channel in self._commands+self._results:
            channel.cancel_join_thread()
            channel.close()
        self._commands.clear()
        self._results.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
