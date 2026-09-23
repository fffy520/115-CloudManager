# -*- coding: utf-8 -*-
"""worker 停工/复工 + 备份恢复 的回归测试(曾每恢复一次泄漏 2 条线程、带连接换库文件)。
注意: 本文件会 import server(启动调度器与 worker, 均为守护线程, 测试进程退出即消)。
全部接口调用已打桩, 不碰真实 115。"""
import os
import shutil
import sqlite3
import threading
import unittest

ROOT = "/tmp/t_restore"
shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT + "/backups")
os.environ["TREE_DB"] = ROOT + "/115_tree.db"
os.environ["BACKUP_DIR"] = ROOT + "/backups"
os.environ["COOKIE_FILE"] = ROOT + "/cookie.txt"
os.environ["AI_CONFIG"] = ROOT + "/ai.json"
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api115   # noqa: E402
api115.load_cookie = lambda: "fake"
api115.list_children_paged = lambda *a, **k: []
import server, db   # noqa: E402


def workers():
    return [t for t in threading.enumerate()
            if t.name in ("scan-worker", "transfer-worker", "ai-worker") and t.is_alive()]


class TestShutdownResume(unittest.TestCase):
    def test_lifecycle(self):
        mgrs = [server.scan_mgr, server.transfer_mgr, server.ai_mgr]
        self.assertEqual(len(workers()), 3)
        for m in mgrs:
            self.assertTrue(m.shutdown(timeout=10))
        self.assertEqual(len(workers()), 0)
        for m in mgrs:
            m.resume()
            m.resume()   # 重复复工不得泄漏线程
        self.assertEqual(len(workers()), 3)

    def test_stuck_running_requeued_on_resume(self):
        con = db.get_conn()
        cur = con.execute("INSERT INTO scan_jobs(target_cid,target_name,status,created_at)"
                          " VALUES('c','t','running','x')")
        con.commit(); jid = cur.lastrowid; con.close()
        for m in (server.scan_mgr, server.transfer_mgr, server.ai_mgr):
            m.shutdown(timeout=10)
        # 屏蔽启动器, 单独验证"退回排队"这一步(否则 worker 会立刻抢走任务)
        for m in (server.scan_mgr, server.transfer_mgr, server.ai_mgr):
            m._start_worker, saved = (lambda: None), m._start_worker
            m.resume()
            m._start_worker = saved
        con = db.get_conn()
        self.assertEqual(con.execute("SELECT status FROM scan_jobs WHERE id=?", (jid,)).fetchone()[0],
                         "queued")
        con.close()
        for m in (server.scan_mgr, server.transfer_mgr, server.ai_mgr):
            m.resume()


class TestRestore(unittest.TestCase):
    def test_restore_twice_no_leak_no_corruption(self):
        con = db.get_conn()
        con.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('marker','current')")
        for i in range(200):   # 制造一批未落盘 WAL 写入
            con.execute("INSERT INTO search_history(q,scope,n,ts) VALUES(?,'',1,0)", (f"x{i}",))
        con.commit(); con.close()
        # 备份文件与真实备份同源(SQLite backup API), 内含 marker=backup + 卡 running 的任务
        src = sqlite3.connect(ROOT + "/backups/115_tree-test.db")
        cur = sqlite3.connect(ROOT + "/115_tree.db")
        cur.backup(src); cur.close()
        src.execute("UPDATE app_settings SET value='backup' WHERE key='marker'")
        src.execute("DELETE FROM search_history")
        src.execute("INSERT INTO scan_jobs(target_cid,target_name,status,created_at)"
                    " VALUES('c','恢复的任务','running','x')")
        src.commit(); src.close()

        before = len(workers())
        for _ in range(2):
            server._restore_backup("115_tree-test.db")
        self.assertEqual(len(workers()), before)            # 零线程泄漏

        con = db.get_conn()
        self.assertEqual(con.execute("SELECT value FROM app_settings WHERE key='marker'").fetchone()[0],
                         "backup")
        self.assertEqual(con.execute("SELECT count(*) FROM search_history").fetchone()[0], 0)  # 旧WAL不混入
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
