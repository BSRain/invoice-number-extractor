"""Task persistence against real Tk state, with local synthetic records only."""
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from copy import deepcopy

from invoice_tool.gui import InvoiceApp
from invoice_tool.core import InvoiceRecord
from invoice_tool.task_file import load_task


class TaskUITests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=Path(self.folder.name)/'task.invtask'
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=InvoiceApp(self.root)
        self.app.add_rows([InvoiceRecord(source=str(Path(self.folder.name)/'missing.pdf'),
                           invoice_number='00123456',amount='10.00',amount_status='已识别')])
        self.root.update()
        self.app.select()

    def tearDown(self):
        self.app.tasks.writer.close()
        self.app.parallel_reader.close()
        for timer in self.root.tk.call('after','info'):
            self.root.after_cancel(timer)
        self.root.destroy()
        self.folder.cleanup()

    def wait_result(self):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            self.app.tasks.poll()
            if self.app.tasks.recovered_revision==self.app.tasks.revision:
                return
            time.sleep(.01)
        self.fail(self.app.tasks.status.get())

    def test_save_reopen_keeps_unconfirmed_input_and_pending_region(self):
        item={'value':'113.00','confidence':.8,'bbox':(.5,.7,.9,.8)}
        self.app.accept_region_candidate(self.app.rows[0],[item],item)
        self.app.number.set('unfinished')
        self.assertTrue(self.app.tasks.save(self.path))
        self.assertEqual(self.app.rows[0].amount,'10.00')
        self.assertFalse(self.app.tasks.needs_warning())
        self.app.tasks.apply(load_task(self.path),self.path)
        self.root.update()
        self.assertEqual(self.app.number.get(),'unfinished')
        self.assertEqual(self.app.amount.get(),'113.00')
        self.assertTrue(self.app.form_changed())
        self.assertIs(self.app._region_pending[0],self.app.rows[0])
        self.assertFalse(self.app.tasks.needs_warning())
        self.app.number.set('87654321')
        self.assertTrue(self.app.tasks.needs_warning())

    def test_invalid_task_leaves_current_rows_draft_and_path_unchanged(self):
        self.app.amount.set('12.')
        before=deepcopy(self.app.rows)
        self.path.write_text('{}',encoding='utf-8')
        with patch('invoice_tool.task_ui.messagebox.showerror'):
            self.assertFalse(self.app.tasks.open(self.path))
        self.assertEqual(self.app.rows,before)
        self.assertEqual(self.app.amount.get(),'12.')
        self.assertIsNone(self.app.tasks.path)

    def test_open_cancelled_save_does_not_discard_current_task(self):
        self.app.tasks.save(self.path)
        self.app.amount.set('12.')
        with patch('invoice_tool.task_ui.messagebox.askyesnocancel',return_value=True), \
             patch('invoice_tool.task_ui.save_task',side_effect=OSError('full')), \
             patch('invoice_tool.task_ui.messagebox.showerror'):
            self.assertFalse(self.app.tasks.open(self.path))
        self.assertEqual(self.app.amount.get(),'12.')

    def test_missing_original_still_restores_and_exports_records(self):
        self.app.tasks.save(self.path)
        self.app.tasks.apply(load_task(self.path),self.path)
        self.assertIsNone(self.app.preview)
        self.assertEqual(self.app.rows[0].invoice_number,'00123456')
        with patch('invoice_tool.gui.filedialog.asksaveasfilename',return_value=str(self.path.with_suffix('.xlsx'))), \
             patch('invoice_tool.gui.export_excel') as export:
            self.app.export()
        self.assertEqual(len(export.call_args.args[0]),1)

    def test_disabled_recovery_creates_no_files_and_cancel_stays_disabled(self):
        self.assertFalse(self.app.tasks.enabled.get())
        self.app.tasks.poll()
        self.assertIsNone(self.app.tasks.writer.thread)
        with patch('invoice_tool.task_ui.filedialog.askdirectory',return_value=''):
            self.app.tasks.enabled.set(True)
            self.app.tasks.toggle()
        self.assertFalse(self.app.tasks.enabled.get())
        self.assertEqual(list(Path(self.folder.name).glob('*.invtask')),[])

    def test_recovery_is_separate_and_restores_as_unsaved_task(self):
        self.app.tasks.save(self.path)
        original=self.path.read_bytes()
        with patch('invoice_tool.task_ui.filedialog.askdirectory',return_value=self.folder.name):
            self.app.tasks.enabled.set(True)
            self.app.tasks.toggle()
        self.app.amount.set('12.')
        self.app.tasks.submit_recovery()
        self.wait_result()
        recovery=load_task(self.app.tasks.recovery_path)
        self.assertTrue(recovery.is_recovery)
        self.assertEqual(recovery.recovery_of,str(self.path))
        self.assertEqual(self.path.read_bytes(),original)
        recovery_path=self.app.tasks.recovery_path
        self.app.tasks.apply(recovery,recovery_path)
        self.root.update()
        self.assertIsNone(self.app.tasks.path)
        self.assertTrue(self.app.tasks.needs_warning())
        self.assertEqual(self.app.amount.get(),'12.')
        self.assertIn(recovery_path.name,self.app.tasks.status.get())
        self.assertIn('原任务 task.invtask',self.app.tasks.status.get())
        self.assertIn(recovery.saved_at,self.app.tasks.status.get())

    def test_stale_completion_cannot_mark_new_task_recovered(self):
        generation=self.app.tasks.generation
        self.app.tasks.reset()
        self.app.tasks.writer.results.put((generation,999,None))
        self.app.tasks.poll()
        self.assertIsNone(self.app.tasks.recovered_revision)

    def test_final_close_waits_for_latest_draft_write(self):
        with patch('invoice_tool.task_ui.filedialog.askdirectory',return_value=self.folder.name):
            self.app.tasks.enabled.set(True)
            self.app.tasks.toggle()
        self.app.amount.set('12.')
        finished=[]
        self.app.tasks.finish(lambda:finished.append(True))
        self.assertFalse(finished)
        self.wait_result()
        self.assertEqual(finished,[True])
        self.assertEqual(load_task(self.app.tasks.recovery_path).draft['amount'],'12.')

    def test_recovery_failure_preserves_work_and_keeps_window_open(self):
        self.app.tasks.enabled.set(True)
        self.app.tasks.recovery_path=Path(self.folder.name)/'missing-dir'/'backup.invtask'
        self.app.amount.set('12.')
        finished=[]
        with patch('invoice_tool.task_ui.messagebox.showerror') as error:
            self.app.tasks.finish(lambda:finished.append(True))
            deadline=time.monotonic()+5
            while self.app.tasks.enabled.get() and time.monotonic()<deadline:
                self.app.tasks.poll()
                time.sleep(.01)
        self.assertFalse(finished)
        self.assertEqual(self.app.amount.get(),'12.')
        self.assertFalse(self.app.tasks.enabled.get())
        error.assert_called_once()

    def test_confirm_after_task_save_marks_task_modified(self):
        self.app.tasks.save(self.path)
        self.app.confirm()
        self.assertTrue(self.app.tasks.needs_warning())

    def test_saving_before_opening_same_task_loads_the_new_save(self):
        self.app.tasks.save(self.path)
        self.app.amount.set('12.')
        with patch('invoice_tool.task_ui.messagebox.askyesnocancel',return_value=True):
            self.assertTrue(self.app.tasks.open(self.path))
        self.root.update()
        self.assertEqual(self.app.amount.get(),'12.')
        self.assertFalse(self.app.tasks.needs_warning())

    def test_filter_order_options_and_completed_sources_roundtrip(self):
        self.app.add_rows([InvoiceRecord(source='粘贴文字',method='text',invoice_number='87654321',
                                         amount_status='待复核')])
        self.app.paths_seen={self.app.rows[0].source.casefold()}
        self.app.single_invoice.set(True)
        self.app.parallel.set(True)
        self.app.review_filter.set('金额待处理')
        self.app.apply_filter()
        self.root.update()
        self.app.tasks.save(self.path)
        self.app.tasks.apply(load_task(self.path),self.path)
        self.root.update()
        self.assertEqual(self.app.tree.get_children(),('1',))
        self.assertEqual(self.app.selected,1)
        self.assertEqual([r.invoice_number for r in self.app.rows],['00123456','87654321'])
        self.assertTrue(self.app.single_invoice.get() and self.app.parallel.get())
        self.assertEqual(self.app.paths_seen,{self.app.rows[0].source.casefold()})
        self.assertFalse(self.app.tasks.needs_warning())

    def test_autosave_deadline_is_not_postponed_by_continuous_changes(self):
        self.app.tasks.enabled.set(True)
        with patch('invoice_tool.task_ui.time.monotonic',return_value=100):
            self.app.tasks.changed()
        with patch('invoice_tool.task_ui.time.monotonic',return_value=104):
            self.app.tasks.changed()
        self.assertEqual(self.app.tasks.due,105)
        with patch('invoice_tool.task_ui.time.monotonic',return_value=106), \
             patch.object(self.app.tasks,'submit_recovery') as submit:
            self.app.tasks.poll()
        submit.assert_called_once()
