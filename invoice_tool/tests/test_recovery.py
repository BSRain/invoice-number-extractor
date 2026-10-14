import time
import unittest
from threading import Event
from unittest.mock import patch
from invoice_tool.task_file import TaskState
from invoice_tool.recovery import RecoveryWriter


class RecoveryTests(unittest.TestCase):
    def wait_for(self,condition):
        deadline=time.monotonic()+3
        while not condition() and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(condition())

    def test_requests_are_bounded_and_only_latest_pending_snapshot_is_written(self):
        started,release=Event(),Event()
        calls=[]
        def write(path,state):
            calls.append(state.selected)
            if len(calls)==1:
                started.set()
                release.wait(2)
        with patch('invoice_tool.recovery.save_task',side_effect=write):
            writer=RecoveryWriter()
            self.addCleanup(writer.close)
            writer.submit('one.invtask',TaskState(selected=1),4,1)
            self.assertTrue(started.wait(1))
            writer.submit('one.invtask',TaskState(selected=2),4,2)
            writer.submit('one.invtask',TaskState(selected=3),4,3)
            self.assertEqual(writer.pending.qsize(),1)
            release.set()
            self.wait_for(lambda:len(calls)==2)
            self.wait_for(lambda:not writer.results.empty())
            self.assertEqual(calls,[1,3])
            result=writer.results.get(timeout=1)
            if result[1]!=3:
                result=writer.results.get(timeout=1)
            self.assertEqual(result,(4,3,None))

    def test_failure_is_reported_and_worker_can_write_next_snapshot(self):
        with patch('invoice_tool.recovery.save_task',side_effect=[OSError('disk unavailable'),None]):
            writer=RecoveryWriter()
            self.addCleanup(writer.close)
            writer.submit('one.invtask',TaskState(),1,1)
            self.wait_for(lambda:not writer.results.empty())
            self.assertIn('disk unavailable',writer.results.get()[2])
            writer.submit('one.invtask',TaskState(),1,2)
            self.wait_for(lambda:not writer.results.empty())
            self.assertEqual(writer.results.get(),(1,2,None))

    def test_discard_pending_never_routes_old_state_to_a_new_path(self):
        started,release=Event(),Event()
        calls=[]
        def write(path,state):
            calls.append(str(path))
            if len(calls)==1:
                started.set()
                release.wait(2)
        with patch('invoice_tool.recovery.save_task',side_effect=write):
            writer=RecoveryWriter()
            self.addCleanup(writer.close)
            writer.submit('old.invtask',TaskState(),1,1)
            self.assertTrue(started.wait(1))
            writer.submit('old.invtask',TaskState(),1,2)
            writer.discard_pending()
            writer.submit('new.invtask',TaskState(),2,1)
            release.set()
            self.wait_for(lambda:len(calls)==2)
            self.assertEqual(calls,['old.invtask','new.invtask'])


if __name__=='__main__':
    unittest.main()
