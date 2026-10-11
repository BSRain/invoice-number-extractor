"""Two reusable spawn workers; bounded streaming, ordered rows and cancellation."""
import multiprocessing as mp
import os
import time
from collections import deque
from pathlib import Path
from queue import Empty

from .reader import InvoiceReader

BUFFER_ROWS = 8
FILE_WINDOW = 4


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
        results.put(('complete',not stop.is_set()))


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

    @property
    def pids(self):
        return tuple(process.pid for process in self._processes)

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
            scheduled = index = 0
            progress_index = -1
            while index < len(paths):
                if stop is not None and stop.is_set():
                    self._stop.set()
                # Refill whichever process finishes first, without allowing
                # unbounded look-ahead behind a slow first file.
                for slot in range(2):
                    if (slot in active or self._stop.is_set()
                            or scheduled >= min(len(paths),index+FILE_WINDOW)):
                        continue
                    self._commands[slot].put((paths[scheduled],single_invoice))
                    active[slot] = scheduled
                    buffers[scheduled] = deque()
                    scheduled += 1
                if index >= scheduled:
                    break
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
                        finished[job] = value
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
        for channel in self._commands+self._results:
            channel.cancel_join_thread()
            channel.close()
        self._commands.clear()
        self._results.clear()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
