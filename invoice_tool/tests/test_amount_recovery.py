import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from invoice_tool.core import TextBox, extract_invoices
from invoice_tool.reader import InvoiceReader
from invoice_tool.uppercase import uppercase_money


def box(text, x, y, width=.15, height=.025, score=1):
    return TextBox(text, (x,y,x+width,y+height), score)


HEADER = box('Invoice No: 00123456', .65, .1)


class AmountRecoveryTests(unittest.TestCase):
    def test_financial_uppercase_parser_is_strict_and_supports_fractions(self):
        cases = {'零元整':'0.00','肆拾万圆整':'400000.00','壹佰壹拾叁元整':'113.00',
                 '负壹佰零壹元伍角':'-101.50','壹万元零伍分':'10000.05',
                 '壹亿零壹万零壹元整':'100010001.00','壹元贰角叁分':'1.23'}
        for text,value in cases.items():
            self.assertEqual(uppercase_money(text),value,text)
        for text in ('壹佰壹元整','壹拾壹方零捌佰捌拾圆整','壹壹元整','壹元','伍万万圆整',
                     '零伍元整','壹元整 113.00','壹拾仟元整','壹元零角伍分',
                     '拾万壹元整','一佰壹元整','1壹元整'):
            self.assertIsNone(uppercase_money(text),text)

    def test_unclear_uppercase_cannot_promote_unclear_digits(self):
        row = extract_invoices([HEADER,box('价税合计（大写）',.1,.7),
                                box('壹佰壹拾叁元整',.3,.7,score=.94),
                                box('小写',.65,.7),box('￥113.00',.8,.7,score=.94)],'ocr')[0]
        self.assertEqual((row.amount,row.amount_status),('','待复核'))

    def test_clear_uppercase_can_verify_lower_score_digits(self):
        boxes = [HEADER,box('价税合计（大写）',.1,.7),box('壹佰壹拾叁元整',.3,.7),
                 box('小写',.65,.7),box('￥113.00',.8,.7,score=.94)]
        row = extract_invoices(boxes,'ocr')[0]
        self.assertEqual((row.amount,row.amount_status),('113.00','已识别'))
        boxes[-1].confidence = .8
        self.assertEqual(extract_invoices(boxes,'ocr')[0].amount,'')

    def test_uppercase_disagreement_prevents_automatic_acceptance(self):
        row = extract_invoices([HEADER,box('价税合计（大写）',.1,.7),box('壹佰壹拾捌元整',.3,.7),
                                box('小写',.65,.7),box('￥113.00',.8,.7)],'ocr')[0]
        self.assertEqual((row.amount,row.amount_status,row.amount_candidates),('','待复核',['113.00']))

    def test_uppercase_without_digits_is_not_used_as_the_output(self):
        row = extract_invoices([HEADER,box('价税合计（大写）壹佰壹拾叁元整',.1,.7)],'ocr')[0]
        self.assertEqual((row.amount,row.amount_candidates),('',[]))
    def test_clear_total_and_digits_are_not_rejected_by_small_label(self):
        row = extract_invoices([HEADER, box('价税合计（大写）', .1, .7),
                                box('（小写）', .65, .7, score=.9353),
                                box('￥113.00', .8, .7, score=.9656)], 'ocr')[0]
        self.assertEqual((row.amount,row.amount_status), ('113.00','已识别'))

    def test_digit_and_total_scores_still_require_high_confidence(self):
        for total_score, digit_score in ((.8,.99), (.99,.95)):
            row = extract_invoices([HEADER, box('价税合计', .1, .7, score=total_score),
                                    box('（小写）', .65, .7),
                                    box('￥113.00', .8, .7, score=digit_score)], 'ocr')[0]
            self.assertEqual((row.amount,row.amount_status), ('','待复核'))

    def test_split_total_and_small_labels_are_joined_spatially(self):
        row = extract_invoices([HEADER, box('价税', .1, .7, width=.055),
                                box('合计', .16, .7, width=.055),
                                box('小', .65, .7, width=.015),
                                box('写', .669, .7, width=.015),
                                box('￥113.00', .8, .7)], 'ocr')[0]
        self.assertEqual(row.amount, '113.00')

    def test_total_label_on_previous_line_can_match_wrapped_amount(self):
        row = extract_invoices([HEADER, box('价税合计', .65, .7),
                                box('￥113.00', .66, .738)], 'ocr')[0]
        self.assertEqual(row.amount, '113.00')

    def test_unanchored_small_label_is_only_a_review_candidate(self):
        row = extract_invoices([HEADER, box('（小写）', .65, .7),
                                box('￥113.00', .8, .7)], 'ocr')[0]
        self.assertEqual((row.amount,row.amount_status,row.amount_candidates),
                         ('','待复核',['113.00']))

    def test_previous_tax_row_and_remote_label_parts_are_not_joined(self):
        row = extract_invoices([HEADER, box('价税', .1, .6, width=.05),
                                box('合计', .7, .6, width=.05),
                                box('税额 ￥13.00', .8, .67),
                                box('（小写）', .65, .7)], 'ocr')[0]
        self.assertEqual(row.amount_candidates, [])

    def test_unanchored_small_label_cannot_steal_another_invoice_total(self):
        other = box('Invoice No: 87654321', .7, .1)
        rows = extract_invoices([box('Invoice No: 00123456', .2, .1), other,
                                 box('小写', .7, .7), box('￥113.00', .85, .7)], 'ocr')
        self.assertTrue(all(not r.amount for r in rows))


class AmountRereadTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new('RGB',(1000,700))
        self.boxes = [HEADER,box('价税合计',.1,.7),box('小写',.65,.7),
                      box('￥113.00',.8,.7,score=.91)]
        self.recovered = [TextBox('￥113.00',(.05,.25,.95,.75),.99)]

    def test_local_reread_recovers_amount_and_preserves_number(self):
        reader = InvoiceReader()
        with patch.object(reader,'ocr_boxes',side_effect=[self.boxes,self.recovered]) as ocr:
            row = reader.recognize_image(self.image)[0]
        self.assertEqual((row.invoice_number,row.amount,row.amount_status),
                         ('00123456','113.00','已识别'))
        self.assertEqual(ocr.call_count,2)
        self.assertLess(ocr.call_args.args[0].width,self.image.width)
        self.assertTrue(0 <= row.amount_bbox[0] < row.amount_bbox[2] <= 1)

    def test_retry_failure_keeps_original_candidates(self):
        reader = InvoiceReader()
        with patch.object(reader,'ocr_boxes',side_effect=[self.boxes,RuntimeError('retry failed')]):
            row = reader.recognize_image(self.image)[0]
        self.assertEqual((row.invoice_number,row.amount_status,row.amount_candidates),
                         ('00123456','待复核',['113.00']))

    def test_clear_amount_does_not_trigger_reread(self):
        self.boxes[-1].confidence = .99
        reader = InvoiceReader()
        with patch.object(reader,'ocr_boxes',return_value=self.boxes) as ocr:
            row = reader.recognize_image(self.image)[0]
        self.assertEqual(row.amount,'113.00')
        self.assertEqual(ocr.call_count,1)

    def test_retry_conflict_keeps_both_candidates_for_review(self):
        reader = InvoiceReader()
        recovered = [TextBox('￥118.00',(.05,.25,.95,.75),.99)]
        with patch.object(reader,'ocr_boxes',side_effect=[self.boxes,recovered]):
            row = reader.recognize_image(self.image)[0]
        self.assertEqual((row.amount,row.amount_status),('','待复核'))
        self.assertEqual(set(row.amount_candidates),{'113.00','118.00'})

    def test_cancel_during_reread_does_not_emit_rows(self):
        reader = InvoiceReader()
        def recognize(image):
            if image.size != self.image.size:
                reader.stop.set()
                return self.recovered
            return self.boxes
        with patch.object(reader,'ocr_boxes',side_effect=recognize):
            self.assertEqual(reader.recognize_image(self.image),[])

    @staticmethod
    def hybrid_pdf(folder):
        import pymupdf as fitz
        path = Path(folder)/'hybrid.pdf'
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((300,60),'Invoice No: 00123456')
            # Number text is selectable, gross-total region is an image.
            page.insert_image(fitz.Rect(30,550,550,700),stream=fitz.Pixmap(
                fitz.csRGB,fitz.IRect(0,0,10,10),False).tobytes('png'))
            doc.save(path)
        return path

    def test_hybrid_pdf_supplements_amount_without_replacing_native_number(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.hybrid_pdf(folder)
            reader = InvoiceReader()
            amounts = [box('价税合计 ￥113.00',.1,.7)]
            with patch.object(reader,'ocr_boxes',return_value=amounts) as ocr:
                row = list(reader.read(path))[0]
            self.assertEqual((row.invoice_number,row.method,row.status,row.amount),
                             ('00123456','pdf','已识别','113.00'))
            ocr.assert_called_once()

    def test_parallel_native_preflight_does_not_skip_missing_image_amount(self):
        from invoice_tool.pipeline import ParallelReader
        with tempfile.TemporaryDirectory() as folder:
            path = self.hybrid_pdf(folder)
            with ParallelReader() as pool, patch.object(pool,'_start',side_effect=RuntimeError('dispatch OCR')) as start:
                with self.assertRaisesRegex(RuntimeError,'dispatch OCR'):
                    list(pool.read([path]))
                start.assert_called_once()

    def test_detail_page_with_only_header_qr_does_not_trigger_amount_ocr(self):
        import pymupdf as fitz
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'details.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((300,60),'Invoice No: 00123456')
                page.insert_text((100,300),'Subtotal CNY 100.00; tax CNY 13.00')
                page.insert_image(fitz.Rect(20,20,70,70),stream=fitz.Pixmap(
                    fitz.csRGB,fitz.IRect(0,0,10,10),False).tobytes('png'))
                doc.save(path)
            reader = InvoiceReader()
            with patch.object(reader,'ocr_boxes') as ocr:
                row = list(reader.read(path))[0]
            ocr.assert_not_called()
            self.assertEqual((row.invoice_number,row.amount,row.amount_candidates),('00123456','',[]))

    def test_amount_already_read_in_fast_header_is_not_erased(self):
        row = extract_invoices([HEADER,box('价税合计 ￥113.00',.1,.2)],'ocr')[0]
        reader = InvoiceReader(single_invoice=True)
        with patch.object(reader,'fast_header',return_value=[row]), patch.object(reader,'ocr_boxes') as ocr:
            result = reader.recognize_image(self.image)[0]
        self.assertEqual(result.amount,'113.00')
        ocr.assert_not_called()

    def test_fast_header_total_above_lower_crop_can_be_recovered(self):
        reader = InvoiceReader(single_invoice=True)
        header = [box('Invoice No: 26000000000000000001',.6,.1),box('价税合计',.1,.22)]
        row = reader.accept_header(header,0,'test')
        recovered = [TextBox('￥113.00',(.7,.25,.95,.5),.99)]
        with patch.object(reader,'fast_header',return_value=row), patch.object(reader,'ocr_boxes',side_effect=[[],recovered]):
            result = reader.recognize_image(self.image)[0]
        self.assertEqual(result.amount,'113.00')

    def test_small_wide_amount_bitmap_near_header_is_not_treated_as_qr(self):
        import pymupdf as fitz
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'top-amount.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                page.insert_text((300,60),'Invoice No: 00123456')
                page.insert_image(fitz.Rect(100,140,300,170),stream=fitz.Pixmap(
                    fitz.csRGB,fitz.IRect(0,0,10,10),False).tobytes('png'))
                doc.save(path)
            reader = InvoiceReader()
            with patch.object(reader,'ocr_boxes',return_value=[box('价税合计 ￥113.00',.1,.2)]) as ocr:
                row = list(reader.read(path))[0]
            ocr.assert_called_once()
            self.assertEqual(row.amount,'113.00')
