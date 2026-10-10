import tempfile
import unittest
from pathlib import Path
from threading import Event

from invoice_tool.reader import InvoiceReader, collect_inputs, parse_text
from invoice_tool.core import TextBox


class ReaderTests(unittest.TestCase):
    def test_low_quality_number_does_not_retry_all_orientations(self):
        from PIL import Image
        from unittest.mock import patch
        boxes = [TextBox('Invoice No:',(.5,.1,.7,.13),.99),
                 TextBox('00123456',(.72,.1,.94,.13),.7),
                 TextBox('description',(.1,.4,.6,.43)),
                 TextBox('quantity',(.1,.5,.6,.53))]
        reader = InvoiceReader()
        with patch.object(reader,'ocr_boxes',return_value=boxes) as ocr:
            rows = reader.recognize_image(Image.new('RGB',(1000,700)))
        self.assertEqual(ocr.call_count,1)
        self.assertEqual(rows[0].invoice_number,'00123456')
        self.assertEqual(rows[0].status,'待复核')

    def test_full_page_retains_lower_invoice_during_early_stop(self):
        from PIL import Image
        from unittest.mock import patch
        boxes = [TextBox('Invoice No: 00123456',(.5,.1,.94,.13),.99),
                 TextBox('Invoice No: 87654321',(.5,.6,.94,.63),.7),
                 TextBox('VAT invoice',(.1,.04,.6,.07))]
        reader = InvoiceReader()
        with patch.object(reader,'ocr_boxes',return_value=boxes) as ocr:
            rows = reader.recognize_image(Image.new('RGB',(1000,700)))
        self.assertEqual(ocr.call_count,1)
        self.assertEqual([r.invoice_number for r in rows],['00123456','87654321'])
        self.assertEqual(rows[1].status,'待复核')

    def test_text_labels_and_bare_numbers(self):
        rows = parse_text('发票号码：00123456\n发票代码：3100214130')
        self.assertEqual(rows[0].invoice_number, '00123456')
        self.assertEqual(rows[0].invoice_code, '3100214130')
        rows = parse_text('00123456\n26000000000000000001')
        self.assertEqual([r.invoice_number for r in rows], ['00123456','26000000000000000001'])
        self.assertTrue(all(r.status == '待复核' for r in rows))

    def test_native_multipage_pdf_and_page_provenance(self):
        import pymupdf as fitz
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'发票.pdf'
            with fitz.open() as doc:
                for number in ['00123456', '26000000000000000001']:
                    page = doc.new_page()
                    page.insert_text((320,50), 'Invoice No: '+number, fontsize=10)
                doc.save(path)
            rows = list(InvoiceReader().read(path))
            self.assertEqual([r.invoice_number for r in rows], ['00123456','26000000000000000001'])
            self.assertEqual([r.page for r in rows], [1,2])
            self.assertTrue(all(r.source == str(path.resolve()) for r in rows))

    def test_bad_file_keeps_a_failure_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'损坏.pdf'
            path.write_bytes(b'not a pdf')
            rows = list(InvoiceReader().read(path))
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0].status, '失败')
            self.assertIn('损坏.pdf',rows[0].source)

    def test_cancelled_reader_does_not_process_more_pages(self):
        import pymupdf as fitz
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'a.pdf'
            with fitz.open() as doc:
                for _ in range(2):
                    doc.new_page().insert_text((320,50),'Invoice No: 12345678')
                doc.save(path)
            stop = Event()
            stream = InvoiceReader(stop).read(path)
            self.assertEqual(next(stream).page,1)
            stop.set()
            self.assertEqual(list(stream),[])

    def test_recursive_input_deduplicates_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'子目录').mkdir()
            one = root/'子目录'/'a.PDF'
            one.touch()
            (root/'b.jpg').touch()
            (root/'结果.xlsx').touch()
            self.assertEqual(len(collect_inputs([root,one])),2)

    def test_rotated_scan_recovers_code_and_number_in_upright_orientation(self):
        import pymupdf as fitz
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'rotated.png'
            with fitz.open() as doc:
                page = doc.new_page(width=800,height=500)
                page.insert_text((260,45),'VAT INVOICE',fontsize=22)
                page.insert_text((65,85),'3100214130',fontsize=20)
                page.insert_text((485,85),'Invoice No: 00123456',fontsize=20)
                for i in range(5):
                    page.insert_text((65,170+i*40),'Invoice item description and quantity',fontsize=18)
                pix=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
                Image.frombytes('RGB',(pix.width,pix.height),pix.samples).rotate(270,expand=True).save(path)
            row = next(InvoiceReader().read(path))
            self.assertEqual(row.invoice_number,'00123456')
            self.assertEqual(row.invoice_code,'3100214130')
            self.assertEqual(row.rotation,90)


if __name__ == '__main__':
    unittest.main()
