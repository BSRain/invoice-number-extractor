"""Two reusable spawn workers; bounded streaming, ordered rows and cancellation."""
import multiprocessing as mp
import os
import time
from collections import OrderedDict, deque
from pathlib import Path
from queue import Empty

from .reader import InvoiceReader

BUFFER_ROWS = 8
FILE_WINDOW = 4
OWNER_LIMIT = 32


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
        path, single_invoice = command
        reader.single_invoice = single_invoice
        try:
            for row in reader.read(path):
                # The coordinator drains both bounded streams even on cancel.
                results.put(('row',row))
        except Exception as exc:
            results.put(('row',InvoiceReader.failure(path,1,exc)))
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
            self._start()
            self._stop.clear()
            active = {}
            buffers = {}
            finished = {}
            waiting = OrderedDict()
            prepared = index = 0
            progress_index = -1
            while index < len(paths):
                if stop is not None and stop.is_set():
                    self._stop.set()
                # Fingerprint at most the bounded look-ahead window. Only
                # confirmed cache hits wait for an owner; misses remain parallel.
                while not self._stop.is_set() and prepared < min(len(paths),index+FILE_WINDOW):
                    path = paths[prepared]
                    try:
                        key = (path.suffix.lower(),InvoiceReader.fingerprint(
                            path,stop if stop is not None else self._stop),single_invoice)
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
                    self._commands[slot].put((paths[job],single_invoice))
                    active[slot] = job
                if index >= prepared:
                    break
                if index in waiting and self._stop.is_set():
                    del waiting[index],buffers[index]
                    index += 1
                    continue
                if progress_index != index:
                    progress_index = index
                    yield 'progress',(index,len(paths),paths[index].name)
                    continue
                if buffers[index]:
                    yield 'row',buffers[index].popleft()
                    continue
                if index in finished:
                    if finished.pop(index):
                        yield 'complete',paths[index]
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
