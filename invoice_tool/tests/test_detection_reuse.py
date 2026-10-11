import unittest
from unittest.mock import Mock,patch
from PIL import Image
from invoice_tool.reader import InvoiceReader

class DetectedBoxReuseTests(unittest.TestCase):
    def setUp(self):
        self.reader=InvoiceReader(single_invoice=True)
        self.image=Image.new('RGB',(2000,1000))
        self.polygons=[[[100,90],[500,90],[500,120],[100,120]],
                       [[1300,90],[1900,90],[1900,120],[1300,120]],
                       [[200,700],[700,700],[700,740],[200,740]]]
        self.engine=Mock()
        self.engine.get_crop_img_list.side_effect=lambda image,boxes:[image[90:120,100:500].copy() for _ in boxes]
        self.engine.text_cls.side_effect=lambda crops:(crops,[],.01)
        self.engine.text_rec.return_value=([('3100214130',.99),('Invoice No: 00123456',.99)],.01)
        self.reader._engine=self.engine

    def test_reuse_filters_body_and_returns_full_page_coordinates_without_detection(self):
        boxes=self.reader.recognize_detected_boxes(self.image,self.polygons,0,.25)
        self.assertEqual([b.text for b in boxes],['3100214130','Invoice No: 00123456'])
        self.assertAlmostEqual(boxes[1].bbox[1],.09)
        self.assertEqual(len(self.engine.get_crop_img_list.call_args.args[1]),2)
        self.engine.assert_not_called()
        self.engine.text_cls.assert_called_once()
        self.engine.text_rec.assert_called_once()

    def test_rotated_polygon_matches_upright_preview(self):
        # Source 1000 x 2000 rotated CCW -> 2000 x 1000.
        source=[[[880,1300],[910,1300],[910,1900],[880,1900]]]
        self.engine.text_rec.return_value=([('Invoice No: 00123456',.99)],.01)
        box=self.reader.recognize_detected_boxes(self.image,source,90,.25)[0]
        self.assertAlmostEqual(box.bbox[0],.65)
        self.assertAlmostEqual(box.bbox[1],.09)
        self.assertAlmostEqual(box.bbox[2],.95)
        self.assertAlmostEqual(box.bbox[3],.12)

    def test_crossing_crop_boundary_is_not_partially_recognized(self):
        self.assertEqual(self.reader.recognize_detected_boxes(self.image,[[[100,240],[500,240],[500,270],[100,270]]],0,.25),[])
        self.engine.text_rec.assert_not_called()

    def test_invalid_polygon_and_mismatched_recognizer_output_are_rejected(self):
        with self.assertRaises(ValueError): self.reader.recognize_detected_boxes(self.image,[[[float('nan'),100]]*4],0,.25)
        self.engine.text_rec.return_value=([('Invoice No: 00123456',.99)],.01)
        with self.assertRaises(ValueError): self.reader.recognize_detected_boxes(self.image,self.polygons,0,.25)

    def test_cancel_between_classification_and_recognition_stops_work(self):
        def cancel(crops):
            self.reader.stop.set();return crops,[],.01
        self.engine.text_cls.side_effect=cancel
        self.assertEqual(self.reader.recognize_detected_boxes(self.image,self.polygons,0,.25),[])
        self.engine.text_rec.assert_not_called()

    def test_successful_reuse_needs_only_direction_detector(self):
        self.engine.return_value=(self.polygons,[.1])
        with patch.object(self.reader,'ocr_boxes',side_effect=AssertionError('duplicate detector')):
            rows=self.reader.fast_header(self.image)
        self.assertEqual(rows[0].invoice_number,'00123456')
        self.assertEqual(rows[0].invoice_code,'3100214130')
        self.assertIn('复用检测框',rows[0].reason)
        self.engine.assert_called_once()

    def test_adapter_error_restores_original_header_reader(self):
        self.engine.return_value=(self.polygons,[.1])
        self.engine.text_rec.side_effect=RuntimeError('batch contract')
        with patch.object(self.reader,'header_boxes',return_value=[]) as original:
            self.assertIsNone(self.reader.fast_header(self.image))
        self.assertEqual(original.call_count,2)

    def test_opencv_crop_error_restores_original_header_reader(self):
        import cv2
        self.engine.return_value=(self.polygons,[.1])
        self.engine.get_crop_img_list.side_effect=cv2.error('invalid crop')
        with patch.object(self.reader,'header_boxes',return_value=[]) as original:
            self.assertIsNone(self.reader.fast_header(self.image))
        self.assertEqual(original.call_count,2)

    def test_cancel_during_cropping_skips_classifier(self):
        def cancel(image,boxes):
            self.reader.stop.set();return [image[:10,:10].copy() for _ in boxes]
        self.engine.get_crop_img_list.side_effect=cancel
        self.assertEqual(self.reader.recognize_detected_boxes(self.image,self.polygons,0,.25),[])
        self.engine.text_cls.assert_not_called()

if __name__=='__main__': unittest.main()
