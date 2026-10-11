import multiprocessing as mp
from pathlib import Path
import tempfile
from threading import Event
import unittest

from invoice_tool.core import InvoiceRecord
from invoice_tool.pipeline import ParallelReader


class ProbeReader:
    """Spawnable probe: rendezvous proves overlap without timing assertions."""
    def __init__(self, stop, threads, cache_files, cache_records):
        self.stop = stop
        self._cache = {}
        self.parameters = (threads,cache_files,cache_records)

    def read(self, path):
        import os
        import time
        path = Path(path)
        if path.name.startswith('dispatch'):
            path.with_suffix('.started').touch()
            if path.stem == 'dispatch0':
                deadline = time.monotonic()+10
                while not (path.parent/'dispatch2.started').exists():
                    if time.monotonic()>deadline:
                        raise RuntimeError('Idle worker was not refilled')
                    time.sleep(.01)
        if path.name.startswith('overlap'):
            path.with_suffix('.started').touch()
            other = path.parent / ('overlap1.started' if path.stem == 'overlap0' else 'overlap0.started')
            deadline = time.monotonic()+10
            while not other.exists() and not self.stop.is_set():
                if time.monotonic() > deadline:
                    raise RuntimeError('Workers did not overlap')
                time.sleep(.01)
            if path.stem == 'overlap0':
                while not other.with_suffix('.finished').exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError('Second file did not finish first')
                    time.sleep(.01)
            else:
                path.with_suffix('.finished').touch()
        if path.name.startswith('fail'):
            raise ValueError('probe failure')
        if path.name.startswith('crash'):
            os._exit(7)
        count = 100 if path.name.startswith('many') else 1
        for index in range(count):
            if index == 8 and path.name.startswith('many'):
                path.with_suffix('.full').touch()
            if self.stop.is_set():
                break
            yield InvoiceRecord(source=str(path),page=index+1,invoice_number=str(os.getpid()),
                                evidence=str(self.parameters))


class ParallelTests(unittest.TestCase):
    def test_idle_worker_gets_next_file_while_first_is_still_running(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=ProbeReader) as pool:
            paths = [Path(folder)/f'dispatch{i}.txt' for i in range(3)]
            rows = [v for k,v in pool.read(paths) if k == 'row']
            self.assertEqual([r.source for r in rows],list(map(str,paths)))
            self.assertTrue(all(r.status == '已识别' for r in rows))

    def test_overlaps_preserves_order_and_reuses_two_processes(self):
        with tempfile.TemporaryDirectory() as directory, ParallelReader(reader_factory=ProbeReader) as pool:
            paths = [Path(directory)/f'overlap{i}.txt' for i in range(2)]
            first = list(pool.read(paths))
            rows = [value for kind,value in first if kind == 'row']
            self.assertEqual([r.source for r in rows],list(map(str,paths)))
            self.assertEqual(len({r.invoice_number for r in rows}),2)
            self.assertTrue(all(r.evidence.endswith(', 16, 100)') for r in rows))
            next_rows = [value for kind,value in pool.read(paths) if kind == 'row']
            self.assertEqual([r.invoice_number for r in next_rows],[r.invoice_number for r in rows])
            self.assertEqual([v for k,v in first if k == 'complete'],paths)

    def test_worker_exception_is_a_row_and_other_files_continue(self):
        with ParallelReader(reader_factory=ProbeReader) as pool:
            events = list(pool.read([Path('fail.txt'),Path('ok.txt'),Path('third.txt')]))
            rows = [v for k,v in events if k == 'row']
            self.assertEqual(len(rows),3)
            self.assertEqual(rows[0].status,'失败')
            self.assertIn('probe failure',rows[0].reason)
            self.assertEqual(len([v for k,v in events if k == 'complete']),3)

    def test_cancel_with_full_buffers_drains_and_can_restart(self):
        stop = Event()
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=ProbeReader) as pool:
            import time
            paths = [Path(folder)/name for name in ('many0.txt','many1.txt','not-started.txt')]
            events = pool.read(paths,stop=stop)
            self.assertEqual(next(events)[0],'progress')
            deadline = time.monotonic()+10
            while not all(p.with_suffix('.full').exists() for p in paths[:2]):
                if time.monotonic()>deadline:
                    self.fail('Workers did not fill the bounded buffers')
                time.sleep(.01)
            stop.set()
            observed = []
            for kind,value in events:
                observed.append((kind,value))
            self.assertTrue(any(k == 'row' for k,v in observed))
            self.assertFalse(any(k == 'row' and Path(v.source).name == 'not-started.txt' for k,v in observed))
            self.assertFalse(any(k == 'complete' and v.name == 'many0.txt' for k,v in observed))
            rows = [v for k,v in pool.read([Path('new0.txt'),Path('new1.txt')]) if k == 'row']
            self.assertEqual(len(rows),2)

    def test_stopped_batch_does_not_start_processes(self):
        stop = Event()
        stop.set()
        with ParallelReader(reader_factory=ProbeReader) as pool:
            self.assertEqual(list(pool.read([Path('a.txt'),Path('b.txt')],stop=stop)),[])
            self.assertEqual(pool.pids,())

    def test_crashed_worker_reports_error_and_cleans_up(self):
        with ParallelReader(reader_factory=ProbeReader) as pool:
            with self.assertRaisesRegex(RuntimeError,'意外退出'):
                list(pool.read([Path('crash.txt'),Path('ok.txt')]))
            self.assertEqual(pool.pids,())

    def test_close_reaps_processes_even_after_generator_is_abandoned(self):
        pool = ParallelReader(reader_factory=ProbeReader)
        iterator = pool.read([Path('many0.txt'),Path('many1.txt')])
        next(iterator)
        pids = pool.pids
        iterator.close()
        pool.close()
        self.assertFalse(set(pids) & {child.pid for child in mp.active_children()})


if __name__ == '__main__':
    mp.freeze_support()
    unittest.main()
