import unittest
from copy import deepcopy
from PIL import Image
from invoice_tool.core import InvoiceRecord, TextBox
from invoice_tool.review import visible_indices, next_review_index, crop_region, display_region, region_candidates


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.rows = [InvoiceRecord(amount='10.00', amount_status='已识别'),
                     InvoiceRecord(status='待复核', amount='20.00', amount_status='已确认'),
                     InvoiceRecord(amount_status='待复核'),
                     InvoiceRecord(amount_status='已确认', duplicate_of='1')]

    def test_filters_keep_original_indices_and_every_record(self):
        before = deepcopy(self.rows)
        for name, expected in [('全部记录',[0,1,2,3]), ('号码待处理',[1]),
                               ('金额待处理',[2]), ('任一待处理',[1,2]), ('重复记录',[3])]:
            self.assertEqual(visible_indices(self.rows,name),expected)
        self.assertEqual(self.rows,before)

    def test_next_review_respects_filter_wraps_and_handles_empty(self):
        self.assertEqual(next_review_index(self.rows,2,'全部记录'),1)
        self.assertEqual(next_review_index(self.rows,1,'金额待处理'),2)
        self.assertIsNone(next_review_index(self.rows,None,'重复记录'))
        self.assertIsNone(next_review_index([],None,'全部记录'))

    def test_region_on_centered_letterboxed_image_and_reverse_drag(self):
        self.assertEqual(display_region((400,250),(200,150),(100,100,400,200)),(.25,.25,.75,.75))
        self.assertEqual(display_region((200,150),(900,900),(100,100,400,200)),(.25,.25,1,1))
        with self.assertRaises(ValueError):
            display_region((20,20),(400,250),(100,100,400,200))

    def test_crop_is_bounded_and_invalid_regions_rejected(self):
        image=Image.new('RGB',(1000,500))
        self.assertEqual(crop_region(image,(.1,.2,.3,.4)).size,(200,100))
        for bounds in ((0,0,0,1),(-.1,0,1,1),(0,0,float('nan'),1),(0,0,2,1)):
            with self.assertRaises(ValueError):
                crop_region(image,bounds)

    def test_region_candidates_preserve_conflicts_scores_and_coordinates(self):
        boxes=[TextBox('￥113.00',(.1,.1,.8,.8),.93),TextBox('￥118.00',(.2,.2,.9,.9),.99)]
        candidates=region_candidates(boxes,(.5,.5,1,1))
        self.assertEqual([c['value'] for c in candidates],['113.00','118.00'])
        self.assertEqual(candidates[0]['bbox'],(.55,.55,.9,.9))
        self.assertEqual(candidates[0]['confidence'],.93)

    def test_region_does_not_guess_digits_accept_foreign_currency_or_bank_numbers(self):
        boxes=[TextBox('1O.00 USD 99.00 1234567890123456789',(0,0,1,1))]
        self.assertEqual(region_candidates(boxes,(0,0,1,1)),[])


if __name__=='__main__':
    unittest.main()
