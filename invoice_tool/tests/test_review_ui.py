"""Actual Tk review state, with only the expensive OCR/viewer boundaries mocked."""
import unittest
from copy import deepcopy
from unittest.mock import patch
import tkinter as tk
import time
from threading import Event
from PIL import Image
from invoice_tool.core import InvoiceRecord, TextBox
from invoice_tool.gui import InvoiceApp
from invoice_tool.review_dialog import RegionDialog


class ReviewUITests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=InvoiceApp(self.root)
        self.rows=[InvoiceRecord(source='one.png',invoice_number='00123456',amount='10.00',
                                 amount_status='已识别',bbox=(.6,.1,.9,.2),amount_bbox=(.6,.7,.9,.8)),
                   InvoiceRecord(source='two.png',invoice_number='87654321',amount_status='待复核')]
        self.preview=patch('invoice_tool.gui.preview_image',return_value=Image.new('RGB',(1000,600)))
        self.preview.start()
        self.app.add_rows(deepcopy(self.rows))
        self.root.update()
        self.app.select()

    def tearDown(self):
        self.preview.stop()
        self.app.parallel_reader.close()
        for timer in self.root.tk.call('after','info'):
            self.root.after_cancel(timer)
        self.root.destroy()

    def test_filter_hides_rows_and_export_still_contains_all_records(self):
        self.app.review_filter.set('金额待处理')
        self.app.apply_filter()
        self.root.update()
        self.assertEqual(self.app.tree.get_children(),('1',))
        self.assertEqual(len(self.app.rows),2)
        self.assertEqual(self.app.selected,1)
        with patch('invoice_tool.gui.filedialog.asksaveasfilename',return_value='all.xlsx'), \
             patch('invoice_tool.gui.export_excel') as export:
            self.app.export()
        self.assertEqual(len(export.call_args.args[0]),2)

    def test_filter_cannot_discard_unsaved_amount(self):
        self.app.amount.set('999.99')
        self.app.review_filter.set('金额待处理')
        with patch('invoice_tool.gui.messagebox.showinfo') as notice:
            self.app.apply_filter()
        notice.assert_called_once()
        self.assertEqual(self.app.review_filter.get(),'全部记录')
        self.assertEqual(self.app.amount.get(),'999.99')
        self.assertEqual(self.app.selected,0)

    def test_confirm_removes_completed_row_from_pending_view(self):
        self.app.review_filter.set('金额待处理')
        self.app.apply_filter()
        self.root.update()
        self.app.amount.set('20.00')
        self.app.confirm(advance=True)
        self.root.update()
        self.assertEqual(self.app.rows[1].amount_status,'已确认')
        self.assertEqual(self.app.tree.get_children(),())
        self.assertIsNone(self.app.selected)

    def test_amount_zoom_leaves_form_and_record_unchanged(self):
        self.app.amount.set('999.99')
        before=deepcopy(self.app.rows)
        with patch('invoice_tool.review_dialog.show_amount') as zoom:
            self.app.zoom_amount()
        zoom.assert_called_once()
        self.assertEqual(self.app.rows,before)
        self.assertEqual(self.app.amount.get(),'999.99')

    def test_clear_deletes_detached_rows_and_old_filter(self):
        self.app.review_filter.set('金额待处理')
        self.app.apply_filter()
        self.root.update()
        with patch('invoice_tool.gui.messagebox.askyesno',return_value=True):
            self.app.clear()
        self.assertFalse(self.app.tree.exists('0'))
        self.assertFalse(self.app.tree.exists('1'))
        self.assertEqual(self.app.review_filter.get(),'全部记录')

    def test_adoption_preserves_confirmed_record_until_explicit_save(self):
        row=self.app.rows[0]
        row.amount_status='已确认'
        before=deepcopy(row)
        candidates=[{'value':'113.00','confidence':.8,'bbox':(.6,.7,.9,.8)},
                    {'value':'118.00','confidence':.99,'bbox':(.6,.7,.9,.8)}]
        self.app.accept_region_candidate(row,candidates,candidates[0])
        self.assertEqual(row,before)
        self.assertTrue(self.app.form_changed())
        self.app.confirm()
        self.assertEqual((row.amount,row.amount_status),('113.00','已确认'))
        self.assertEqual(row.amount_candidates,['113.00','118.00'])
        self.assertEqual(row.amount_bbox,(.6,.7,.9,.8))

    def test_discarding_region_edit_does_not_mutate_record(self):
        before=deepcopy(self.app.rows)
        candidate={'value':'113.00','confidence':.8,'bbox':(.6,.7,.9,.8)}
        self.app.accept_region_candidate(self.app.rows[0],[candidate],candidate)
        with patch('invoice_tool.gui.messagebox.askyesnocancel',return_value=False):
            self.app.tree.selection_set('1')
            self.root.update()
        self.assertEqual(self.app.rows,before)
        self.assertIsNone(self.app._region_pending)

    def test_invalid_confirmation_does_not_commit_region_evidence(self):
        before=deepcopy(self.app.rows)
        candidate={'value':'113.00','confidence':.8,'bbox':(.6,.7,.9,.8)}
        self.app.accept_region_candidate(self.app.rows[0],[candidate],candidate)
        self.app.number.set('invalid')
        with patch('invoice_tool.gui.messagebox.showerror'):
            self.app.confirm()
        self.assertEqual(self.app.rows,before)

    def test_saving_on_row_switch_keeps_region_candidates_and_location(self):
        candidate={'value':'113.00','confidence':.8,'bbox':(.6,.75,.9,.85)}
        row=self.app.rows[0]
        self.app.accept_region_candidate(row,[candidate],candidate)
        with patch('invoice_tool.gui.messagebox.askyesnocancel',return_value=True):
            self.app.tree.selection_set('1')
            self.root.update()
        self.assertEqual(row.amount_candidates,['113.00'])
        self.assertEqual(row.amount_bbox,candidate['bbox'])
        self.assertEqual((row.amount,row.amount_status),('113.00','已确认'))

    def test_compact_window_can_scroll_to_confirmation(self):
        self.root.deiconify()
        self.root.geometry('980x640')
        self.root.update()
        panel=self.app.review_panel
        panel.canvas.yview_moveto(1)
        self.root.update()
        def find(widget):
            for child in widget.winfo_children():
                if child.winfo_class()=='TButton' and child.cget('text')=='保存并确认':
                    return child
                nested=find(child)
                if nested is not None:
                    return nested
        button=find(panel.content)
        self.assertIsNotNone(button)
        self.assertGreaterEqual(button.winfo_rooty(),panel.canvas.winfo_rooty())
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                             panel.canvas.winfo_rooty()+panel.canvas.winfo_height())
        self.assertLessEqual(self.app.progress.winfo_rooty()+self.app.progress.winfo_height(),
                             self.root.winfo_rooty()+self.root.winfo_height())
        def buttons(widget):
            result=[widget] if widget.winfo_class()=='TButton' else []
            return result+[button for child in widget.winfo_children() for button in buttons(child)]
        for control in buttons(panel.content):
            self.assertLessEqual(control.winfo_rootx()+control.winfo_width(),
                                 panel.canvas.winfo_rootx()+panel.canvas.winfo_width())

    def dialog(self):
        self.root.deiconify()
        dialog=RegionDialog(self.app,Image.new('RGB',(1000,600)),self.app.rows[0])
        self.root.update()
        dialog.draw()
        return dialog

    def pump(self, finished):
        deadline=time.monotonic()+4
        while not finished() and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(finished(),'Review dialog did not settle')

    def test_dialog_cancel_preserves_unsaved_form_and_record(self):
        before=deepcopy(self.app.rows)
        self.app.amount.set('999.99')
        dialog=self.dialog()
        dialog.cancel()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.amount.get(),'999.99')
        self.assertEqual(self.app.rows,before)

    def test_immediate_cancel_removes_queued_dialog_draw(self):
        dialog=RegionDialog(self.app,Image.new('RGB',(1000,600)),self.app.rows[0])
        timers=set(self.root.tk.call('after','info'))
        draws={timer for timer in timers if str(self.root.tk.call('after','info',timer)[0]).endswith('draw')}
        self.assertTrue(draws)
        dialog.cancel()
        self.assertFalse(draws.intersection(self.root.tk.call('after','info')))

    def test_dialog_requires_candidate_choice_and_explicit_save(self):
        before=deepcopy(self.app.rows)
        dialog=self.dialog()
        dialog.bounds=(.5,.5,.9,.8)
        with patch.object(self.app.reader,'ocr_boxes',return_value=[TextBox('￥113.00',(0,0,1,1),.8)]):
            dialog.read()
            self.pump(lambda:not dialog.busy)
        self.assertEqual(dialog.choice.current(),-1)
        dialog.adopt()
        self.assertEqual(self.app.rows,before)
        self.assertEqual(self.app.amount.get(),'10.00')
        dialog.choice.current(0)
        dialog.adopt()
        self.assertEqual(self.app.rows,before)
        self.assertEqual(self.app.amount.get(),'113.00')
        self.assertFalse(self.app.busy)

    def test_cancel_during_ocr_waits_then_closes_without_changes(self):
        started,release=Event(),Event()
        before=deepcopy(self.app.rows)
        dialog=self.dialog()
        dialog.bounds=(.5,.5,.9,.8)
        def work(_):
            started.set()
            release.wait(3)
            return [TextBox('￥113.00',(0,0,1,1))]
        with patch.object(self.app.reader,'ocr_boxes',side_effect=work):
            dialog.read()
            self.assertTrue(started.wait(1))
            dialog.cancel()
            release.set()
            self.pump(lambda:not self.app.busy)
        self.assertEqual(self.app.rows,before)
        self.assertFalse(dialog.window.winfo_exists())

    def test_dialog_error_keeps_original_and_allows_retry(self):
        before=deepcopy(self.app.rows)
        dialog=self.dialog()
        dialog.bounds=(.5,.5,.9,.8)
        with patch.object(self.app.reader,'ocr_boxes',side_effect=RuntimeError('OCR unavailable')):
            dialog.read()
            self.pump(lambda:not dialog.busy)
        self.assertIn('OCR unavailable',dialog.status.get())
        self.assertEqual(self.app.rows,before)
        self.assertEqual(str(dialog.read_button['state']),'normal')
        dialog.cancel()


if __name__=='__main__':
    unittest.main()
