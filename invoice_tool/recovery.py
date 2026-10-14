"""One local writer, one pending snapshot, and bounded completion messages."""
from queue import Queue, Empty, Full
from threading import Event, Thread
from .task_file import save_task


def _latest(queue,value):
    try:
        queue.put_nowait(value)
    except Full:
        try:
            queue.get_nowait()
        except Empty:
            pass
        queue.put_nowait(value)


class RecoveryWriter:
    def __init__(self):
        self.pending=Queue(maxsize=1)
        self.results=Queue(maxsize=1)
        self.stopped=Event()
        self.thread=None

    def submit(self,path,state,generation,revision):
        if self.stopped.is_set():
            raise RuntimeError('恢复写入器已关闭。')
        _latest(self.pending,(path,state,generation,revision))
        if self.thread is None:
            self.thread=Thread(target=self._run,daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                path,state,generation,revision=self.pending.get(timeout=.1)
            except Empty:
                continue
            error=None
            try:
                save_task(path,state)
            except Exception as exc:
                error=str(exc)
            _latest(self.results,(generation,revision,error))

    def discard_pending(self):
        try:
            self.pending.get_nowait()
        except Empty:
            pass

    def close(self):
        self.stopped.set()
        self.discard_pending()
        if self.thread is not None:
            self.thread.join(timeout=.1)
