import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest

from invoice_tool.core import InvoiceRecord
from invoice_tool.pipeline import ParallelReader
from invoice_tool.reader import InvoiceReader


class CachedProbe(InvoiceReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def _read_uncached(self, path):
        self.calls += 1
        content = path.read_text(encoding='utf-8')
        path.with_suffix('.started').touch()
        if content == 'wait':
            deadline = time.monotonic()+10
            while not (path.parent/'unique.started').exists() and not self.stop.is_set():
                if time.monotonic()>deadline:
                    raise RuntimeError('Duplicate blocked unrelated work')
                time.sleep(.01)
        count = 200 if content == 'many' else 1
        for index in range(count):
            if self.stop.is_set():
                return
            yield InvoiceRecord(source=str(path),page=index+1,invoice_number='00123456',
                status='失败' if content == 'failure' else '已识别',
                reason=f'{os.getpid()}:{self.calls}',evidence=content)


class ChangingProbe(CachedProbe):
    def read(self, path):
        if Path(path).name == 'changed.txt':
            Path(path).write_text('other',encoding='utf-8')
        yield from super().read(path)


def rows(pool, paths, **kwargs):
    return [value for kind,value in pool.read(paths,**kwargs) if kind == 'row']


class AffinityTests(unittest.TestCase):
    def test_worker_rehashes_if_file_changes_after_routing(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=ChangingProbe) as pool:
            seed,changed = [Path(folder)/name for name in ('seed.txt','changed.txt')]
            for path in (seed,changed):
                path.write_text('same',encoding='utf-8')
            first = rows(pool,[seed])[0]
            second = rows(pool,[changed])[0]
            self.assertEqual(second.evidence,'other')
            self.assertNotEqual(first.reason,second.reason)

    def test_cached_copies_use_one_read_and_keep_every_source(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = [Path(folder)/f'{i}.txt' for i in range(4)]
            for path in paths:
                path.write_text('same',encoding='utf-8')
            rows(pool,paths[:1])
            result = rows(pool,paths[1:])
            self.assertEqual([r.source for r in result],list(map(str,paths[1:])))
            self.assertEqual(len({r.reason for r in result}),1)
            self.assertFalse(paths[1].with_suffix('.started').exists())

    def test_reordered_copies_reuse_owner_across_batches_and_edits_are_isolated(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = [Path(folder)/f'{i}.txt' for i in range(4)]
            for path,content in zip(paths,('first','second','second','first')):
                path.write_text(content,encoding='utf-8')
            first = rows(pool,paths[:2])
            tokens = [r.reason for r in first]
            first[0].invoice_number = '87654321'
            copied = rows(pool,paths[2:])
            self.assertEqual([r.reason for r in copied],tokens[::-1])
            self.assertTrue(all(r.invoice_number == '00123456' for r in copied))

    def test_waiting_duplicate_does_not_block_an_unrelated_file(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = [Path(folder)/name for name in ('first.txt','copy.txt','unique.txt')]
            seed = Path(folder)/'seed.txt'
            seed.write_text('same',encoding='utf-8')
            seeded = rows(pool,[seed])[0]
            for path,content in zip(paths,('wait','same','other')):
                path.write_text(content,encoding='utf-8')
            result = rows(pool,paths)
            self.assertEqual([r.source for r in result],list(map(str,paths)))
            self.assertTrue(all(r.status == '已识别' for r in result))
            self.assertEqual(result[1].reason,seeded.reason)

    def test_changed_content_and_mode_do_not_reuse_stale_results(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            source = Path(folder)/'source.txt'
            source.write_text('first',encoding='utf-8')
            first = rows(pool,[source])[0]
            source.write_text('other',encoding='utf-8')
            changed = rows(pool,[source])[0]
            self.assertEqual(changed.evidence,'other')
            self.assertNotEqual(changed.reason,first.reason)
            switched = rows(pool,[source],single_invoice=True)[0]
            self.assertNotEqual(switched.reason,changed.reason)

    def test_failures_are_retried_and_all_rows_retained(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = [Path(folder)/f'{i}.txt' for i in range(2)]
            for path in paths:
                path.write_text('failure',encoding='utf-8')
            result = rows(pool,paths)
            self.assertEqual(len(result),2)
            self.assertTrue(all(r.status == '失败' for r in result))
            self.assertNotEqual(result[0].reason,result[1].reason)

    def test_cancel_does_not_start_waiting_cached_copy_and_partial_file_can_retry(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = [Path(folder)/f'{i}.txt' for i in range(2)]
            seed = Path(folder)/'seed.txt'
            seed.write_text('same',encoding='utf-8')
            rows(pool,[seed])
            for path,content in zip(paths,('many','same')):
                path.write_text(content,encoding='utf-8')
            stop = Event()
            iterator = pool.read(paths,stop=stop)
            for kind,value in iterator:
                if kind == 'row':
                    stop.set()
                    break
            list(iterator)
            self.assertFalse(paths[1].with_suffix('.started').exists())
            self.assertEqual(len(rows(pool,[paths[0]])),200)

    def test_first_reads_and_oversized_files_remain_parallel_and_uncached(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = [Path(folder)/f'{i}.txt' for i in range(2)]
            for path in paths:
                path.write_text('many',encoding='utf-8')
            result = rows(pool,paths)
            self.assertEqual(len(result),400)
            self.assertEqual(len({r.reason.split(':')[0] for r in result}),2)
            self.assertEqual(len(pool._owners),0)

    def test_routing_hints_follow_cache_evictions_and_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder, ParallelReader(reader_factory=CachedProbe) as pool:
            paths = []
            for i in range(40):
                path = Path(folder)/f'{i}.txt'
                path.write_text(str(i),encoding='utf-8')
                paths.append(path)
            self.assertEqual(len(rows(pool,paths)),40)
            self.assertLessEqual(len(pool._owners),32)
            key = ('.txt',InvoiceReader.fingerprint(paths[0]),False)
            self.assertNotIn(key,pool._owners)


if __name__ == '__main__':
    unittest.main()
