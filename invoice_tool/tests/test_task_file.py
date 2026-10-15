import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from invoice_tool.core import InvoiceRecord
from invoice_tool.task_file import TaskState, encode_task, decode_task, save_task, load_task


class TaskFileTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'核对任务.invtask'
        self.source=str(Path(self.folder.name)/'原件 不打包.pdf')
        row=InvoiceRecord(source=self.source,invoice_number='00123456',invoice_code='3100214130',
                          status='已确认',amount='-101.50',amount_status='已确认',
                          amount_candidates=['-101.50','-118.00'],amount_bbox=(.5,.7,.9,.8),
                          bbox=(.6,.1,.9,.2),rotation=90)
        self.state=TaskState(rows=[row],completed_paths={self.source.casefold()},selected=0,
                             draft={'index':0,'number':'unfinished','code':'','amount':'12.', 'region':None})

    def test_roundtrip_preserves_all_fields_draft_options_and_order(self):
        self.state.rows.append(InvoiceRecord(source='粘贴文字',method='text',status='待复核'))
        self.state.single_invoice=True
        before=deepcopy(self.state)
        save_task(self.path,self.state)
        loaded=load_task(self.path)
        for name in ('rows','completed_paths','selected','draft','single_invoice','parallel','review_filter'):
            self.assertEqual(getattr(loaded,name),getattr(before,name),name)
        self.assertTrue(loaded.saved_at)
        self.assertEqual(self.state,before)

    def test_original_need_not_exist_and_is_not_embedded(self):
        encoded=encode_task(self.state)
        self.assertFalse(Path(self.source).exists())
        self.assertEqual(decode_task(encoded).rows,self.state.rows)
        self.assertNotIn(b'%PDF',encoded)

    def test_recovery_metadata_is_explicit_and_local_only(self):
        self.state.is_recovery=True
        self.state.recovery_of=str(self.path)
        restored=decode_task(encode_task(self.state))
        self.assertTrue(restored.is_recovery)
        self.assertEqual(restored.recovery_of,str(self.path))
        self.state.recovery_of='https://example.com/task.invtask'
        with self.assertRaises(ValueError):
            encode_task(self.state)

    def test_region_draft_keeps_conflicts_and_chosen_location(self):
        item={'value':'113.00','confidence':.8,'bbox':(.6,.7,.9,.8)}
        self.state.draft['region']={'candidates':[item], 'chosen':item}
        self.state.draft['amount']='113.00'
        loaded=decode_task(encode_task(self.state))
        self.assertEqual(loaded.draft['region']['chosen']['bbox'],item['bbox'])
        self.assertEqual(loaded.rows[0].amount,'-101.50')

    def test_corruption_wrong_version_and_unknown_fields_rejected(self):
        data=json.loads(encode_task(self.state))
        data['payload']['rows'][0]['amount']='999.00'
        with self.assertRaises(ValueError):
            decode_task(json.dumps(data).encode())
        data=json.loads(encode_task(self.state))
        data['version']=99
        with self.assertRaises(ValueError):
            decode_task(json.dumps(data).encode())
        self.state.rows[0].amount_status='自动忽略错误'
        with self.assertRaises(ValueError):
            encode_task(self.state)

    def test_duplicate_json_keys_nonfinite_values_and_bad_boxes_rejected(self):
        for content in (b'{"version":1,"version":2}',b'{"number":NaN}'):
            with self.assertRaises(ValueError):
                decode_task(content)
        self.state.rows[0].amount_bbox=(0,0,float('nan'),1)
        with self.assertRaises(ValueError):
            encode_task(self.state)
        self.state.rows[0].amount_bbox=(0,0,2,1)
        with self.assertRaises(ValueError):
            encode_task(self.state)

    def test_invalid_index_page_and_completed_path_rejected(self):
        for field,value in [('selected',3),('completed_paths',{'C:\\unrelated.pdf'})]:
            state=deepcopy(self.state)
            setattr(state,field,value)
            with self.assertRaises(ValueError):
                encode_task(state)
        self.state.rows[0].page=True
        with self.assertRaises(ValueError):
            encode_task(self.state)

    def test_network_or_url_sources_are_not_opened_from_task_files(self):
        for source in ('\\\\server\\share\\invoice.pdf','https://example.com/invoice.pdf'):
            self.state.rows[0].source=source
            self.state.completed_paths=set()
            with self.assertRaises(ValueError):
                encode_task(self.state)

    def test_failed_replace_retains_previous_success_and_cleans_temporary(self):
        save_task(self.path,self.state)
        old=self.path.read_bytes()
        self.state.rows[0].amount='200.00'
        with patch('invoice_tool.task_file.os.replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                save_task(self.path,self.state)
        self.assertEqual(self.path.read_bytes(),old)
        self.assertEqual(list(self.path.parent.glob('.invoice-task-*')),[])

    def test_invalid_extension_or_oversized_file_cannot_replace_original(self):
        original=Path(self.source)
        original.write_bytes(b'%PDF synthetic fixture')
        with self.assertRaises(ValueError):
            save_task(original,self.state)
        self.assertEqual(original.read_bytes(),b'%PDF synthetic fixture')
        with patch('invoice_tool.task_file.MAX_BYTES',20):
            with self.assertRaises(ValueError):
                decode_task(b' '*21)


if __name__=='__main__':
    unittest.main()
