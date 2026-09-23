# -*- coding: utf-8 -*-
"""停止/暂停语义的回归测试(曾对排队中的任务假成功: 接口回 stopped:true 任务照跑)"""
import os
import sys
import unittest

os.environ["TREE_DB"] = "/tmp/t_stop.db"
for p in ("/tmp/t_stop.db", "/tmp/t_stop.db-wal", "/tmp/t_stop.db-shm"):
    if os.path.exists(p):
        os.remove(p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, scanner, transfer_worker, ai_worker   # noqa: E402


class TestStopSemantics(unittest.TestCase):
    def setUp(self):
        self.scan = object.__new__(scanner.ScanManager); self.scan._stop_events = {}
        self.tf = object.__new__(transfer_worker.TransferManager); self.tf._stop_events = {}
        self.ai = object.__new__(ai_worker.AiManager); self.ai._stop_events = {}

    def _job(self, status):
        con = db.get_conn()
        cur = con.execute("INSERT INTO scan_jobs(target_cid,target_name,status,created_at)"
                          " VALUES('c','t',?,'x')", (status,))
        con.commit(); con.close(); return cur.lastrowid

    def _task(self, status):
        con = db.get_conn()
        cur = con.execute("INSERT INTO transfer_tasks(name,target_cid,target_name,status,created_at)"
                          " VALUES('t','c','t',?,'x')", (status,))
        con.commit(); con.close(); return cur.lastrowid

    def _batch(self, status):
        con = db.get_conn()
        cur = con.execute("INSERT INTO ai_batches(scope_cid,scope_name,status,created_at)"
                          " VALUES('c','t',?,'x')", (status,))
        con.commit(); con.close(); return cur.lastrowid

    def _status(self, sql):
        con = db.get_conn()
        r = con.execute(sql).fetchone()[0]
        con.close(); return r

    def test_scan_stop_queued_really_stops(self):
        jid = self._job("queued")
        self.assertTrue(self.scan.stop(jid))
        self.assertEqual(self._status(f"SELECT status FROM scan_jobs WHERE id={jid}"), "stopped")

    def test_scan_pause_queued(self):
        jid = self._job("queued")
        self.assertTrue(self.scan.stop(jid, final_status="paused"))
        self.assertEqual(self._status(f"SELECT status FROM scan_jobs WHERE id={jid}"), "paused")

    def test_scan_stop_running_without_event(self):
        jid = self._job("running")     # 极端: 无 stop_event 也不得崩
        self.assertTrue(self.scan.stop(jid))
        self.assertEqual(self._status(f"SELECT status FROM scan_jobs WHERE id={jid}"), "stopped")

    def test_finished_jobs_untouched(self):
        jid = self._job("done")
        self.assertFalse(self.scan.stop(jid))
        self.assertEqual(self._status(f"SELECT status FROM scan_jobs WHERE id={jid}"), "done")

    def test_transfer_stop_queued(self):
        tid = self._task("queued")
        self.assertTrue(self.tf.stop(tid))
        self.assertEqual(self._status(f"SELECT status FROM transfer_tasks WHERE id={tid}"), "stopped")

    def test_ai_stop_queued(self):
        bid = self._batch("queued")
        self.assertTrue(self.ai.stop(bid))
        self.assertEqual(self._status(f"SELECT status FROM ai_batches WHERE id={bid}"), "stopped")

    def test_ai_finished_untouched(self):
        bid = self._batch("done")
        self.assertFalse(self.ai.stop(bid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
