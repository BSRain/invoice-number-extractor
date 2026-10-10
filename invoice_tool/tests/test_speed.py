import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from invoice_tool.core import TextBox,InvoiceRecord
from invoice_tool.reader import InvoiceReader


class FastModeTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB',(1000,700))
        self.detector = [[[100,100],[500,100],[500,120],[100,120]]]
        self.boxes = [TextBox('3100214130',(.1,.2,.35,.24)),
                      TextBox('Invoice No: 00123456',(.6,.2,.98,.24))]

    def test_header_coordinates_map_back_to_full_page(self):
        reader = InvoiceReader(single_invoice=True)
        with patch.object(reader,'engine') as engine,patch.object(reader,'ocr_boxes',return_value=self.boxes):
            engine.return_value.return_value=(self.detector,[.1])
            row = reader.recognize_image(self.image)[0]
        self.assertEqual(row.invoice_number,'00123456')
        self.assertEqual(row.invoice_code,'3100214130')
        self.assertAlmostEqual(row.bbox[1],.08)
        self.assertEqual(row.rotation,0)

    def test_low_quality_and_missing_code_fall_back_to_full_page(self):
        for crop in ([TextBox('Invoice No: 00123456',(.6,.2,.98,.24))],
                     [TextBox('Invoice No: 00123456',(.6,.2,.98,.24),.7)]):
            reader = InvoiceReader(single_invoice=True)
            with patch.object(reader,'engine') as engine,patch.object(reader,'ocr_boxes',side_effect=[crop,self.boxes]) as ocr:
                engine.return_value.return_value=(self.detector,[.1])
                row = reader.recognize_image(self.image)[0]
            self.assertEqual(ocr.call_count,2)
            self.assertEqual(row.invoice_code,'3100214130')
            self.assertEqual(row.bbox[1],.2)

    def test_multiple_header_numbers_fall_back_and_retain_both(self):
        crop=[TextBox('Invoice No: 00123456',(.6,.08,.98,.12)),
              TextBox('Invoice No: 87654321',(.6,.5,.98,.54))]
        reader=InvoiceReader(single_invoice=True)
        with patch.object(reader,'engine') as engine,patch.object(reader,'ocr_boxes',side_effect=[crop,crop]) as ocr:
            engine.return_value.return_value=(self.detector,[.1])
            rows=reader.recognize_image(self.image)
        self.assertEqual(ocr.call_count,2)
        self.assertEqual([r.invoice_number for r in rows],['00123456','87654321'])

    def test_vertical_detection_selects_rotated_header(self):
        reader=InvoiceReader(single_invoice=True)
        vertical=[[[100,100],[120,100],[120,500],[100,500]]]
        with patch.object(reader,'engine') as engine,patch.object(reader,'ocr_boxes',return_value=self.boxes) as ocr:
            engine.return_value.return_value=(vertical,[.1])
            row=reader.recognize_image(self.image)[0]
        self.assertEqual(row.rotation,90)
        self.assertEqual(ocr.call_args.args[0].size,(700,400))

    def test_crop_failure_does_not_turn_readable_page_into_failure(self):
        reader=InvoiceReader(single_invoice=True)
        with patch.object(reader,'fast_header',side_effect=RuntimeError('crop failed')),patch.object(reader,'ocr_boxes',return_value=self.boxes):
            row=reader.recognize_image(self.image)[0]
        self.assertEqual(row.invoice_number,'00123456')

    def test_default_mode_does_not_use_header_crop(self):
        reader=InvoiceReader()
        with patch.object(reader,'fast_header') as fast,patch.object(reader,'ocr_boxes',return_value=self.boxes):
            reader.recognize_image(self.image)
        fast.assert_not_called()


class ContentCacheTests(unittest.TestCase):
    def test_content_cache_remaps_source_and_isolated_from_manual_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            one,two=Path(folder)/'one.txt',Path(folder)/'two.txt'
            one.write_text('发票号码：00123456',encoding='utf-8')
            two.write_bytes(one.read_bytes())
            reader=InvoiceReader()
            with patch.object(reader,'_read_uncached',wraps=reader._read_uncached) as actual:
                first=list(reader.read(one))[0]
                first.invoice_number='87654321'
                first.candidates.append('87654321')
                second=list(reader.read(two))[0]
                self.assertEqual(actual.call_count,1)
                self.assertEqual(second.source,str(two.resolve()))
                self.assertEqual(second.invoice_number,'00123456')
                self.assertEqual(second.candidates,['00123456'])
                stamp=two.stat()
                two.write_text('发票号码：00000001',encoding='utf-8')
                os.utime(two,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
                self.assertEqual(list(reader.read(two))[0].invoice_number,'00000001')
                self.assertEqual(actual.call_count,2)

    def test_modes_are_separate_and_failed_inputs_are_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.txt'
            source.write_text('发票号码：00123456',encoding='utf-8')
            reader=InvoiceReader()
            list(reader.read(source))
            reader.single_invoice=True
            list(reader.read(source))
            self.assertEqual(reader.cache_hits,0)
            broken=Path(folder)/'broken.pdf'
            broken.write_bytes(b'broken')
            list(reader.read(broken))
            list(reader.read(broken))
            self.assertEqual(reader.cache_hits,0)

    def test_cancelled_read_does_not_cache_partial_pages(self):
        import pymupdf as fitz
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.pdf'
            with fitz.open() as doc:
                for number in ('00123456','87654321'):
                    doc.new_page().insert_text((320,50),'Invoice No: '+number)
                doc.save(source)
            reader=InvoiceReader()
            stream=reader.read(source)
            next(stream)
            reader.stop.set()
            self.assertEqual(list(stream),[])
            self.assertEqual(len(reader._cache),0)

    def test_old_entries_are_evicted_at_cache_limit(self):
        with tempfile.TemporaryDirectory() as folder,patch('invoice_tool.reader.MAX_CACHE_FILES',2):
            reader=InvoiceReader()
            files=[]
            for index in range(3):
                source=Path(folder)/f'{index}.txt'
                source.write_text(f'发票号码：{index:08d}',encoding='utf-8')
                files.append(source)
                list(reader.read(source))
            self.assertEqual(len(reader._cache),2)
            with patch.object(reader,'_read_uncached',wraps=reader._read_uncached) as actual:
                list(reader.read(files[0]))
                self.assertEqual(actual.call_count,1)

    def test_oversized_file_retains_all_results_without_filling_cache(self):
        with tempfile.TemporaryDirectory() as folder,patch('invoice_tool.reader.MAX_CACHE_RECORDS',2):
            source=Path(folder)/'source.txt'
            source.write_text('placeholder',encoding='utf-8')
            reader=InvoiceReader()
            records=[InvoiceRecord(invoice_number=f'{index:08d}') for index in range(5)]
            with patch.object(reader,'_read_uncached',return_value=iter(records)):
                self.assertEqual(len(list(reader.read(source))),5)
            self.assertEqual(len(reader._cache),0)

    def test_file_changed_during_read_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.txt'
            source.write_text('发票号码：00123456',encoding='utf-8')
            reader=InvoiceReader()
            def changed(path):
                source.write_text('发票号码：87654321',encoding='utf-8')
                yield InvoiceRecord(source=str(source),invoice_number='00123456')
            with patch.object(reader,'_read_uncached',side_effect=changed):
                list(reader.read(source))
            self.assertEqual(len(reader._cache),0)
            self.assertEqual(list(reader.read(source))[0].invoice_number,'87654321')


if __name__=='__main__':
    unittest.main()
