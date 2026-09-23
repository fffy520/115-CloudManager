# -*- coding: utf-8 -*-
"""转存"快照差集对账"的回归测试:
曾用分享侧 fid 占位 + 按名字校准, 重名/(1)后缀/校准失败都会留下云端查无的幽灵节点。"""
import os
import sys
import unittest

os.environ["TREE_DB"] = "/tmp/t_xfer.db"
for p in ("/tmp/t_xfer.db", "/tmp/t_xfer.db-wal", "/tmp/t_xfer.db-shm"):
    if os.path.exists(p):
        os.remove(p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, scanner, transfer_worker, api115   # noqa: E402


class TestSnapshotDiff(unittest.TestCase):
    def test_sync_registers_only_real_new_items(self):
        con = db.get_conn()
        con.execute("INSERT INTO tree_nodes VALUES('T','T','T','转存目标',1,0)")
        con.execute("INSERT INTO tree_nodes VALUES('OLD1','T','T','海报.jpg',0,10)")
        con.execute("INSERT INTO transfer_tasks(name,target_cid,target_name,status,total,created_at)"
                    " VALUES('测试','T','转存目标','queued',1,'2026-01-01')")
        con.execute("INSERT INTO transfer_items(task_id,share_code,receive_code,title,status)"
                    " VALUES(1,'abc','','测试条目','pending')")
        con.commit(); con.close()

        BEFORE = [{"cid": "OLD1", "pid": "T", "name": "海报.jpg", "is_dir": 0, "size": 10, "pick_code": ""}]
        AFTER = BEFORE + [
            {"cid": "NEW1", "pid": "T", "name": "海报 (1).jpg", "is_dir": 0, "size": 20, "pick_code": ""},
            {"cid": "NEW2", "pid": "T", "name": "正片.mkv", "is_dir": 0, "size": 30, "pick_code": ""},
            {"cid": "NEW3", "pid": "T", "name": "花絮", "is_dir": 1, "size": 0, "pick_code": ""},
        ]
        calls = {"n": 0}

        def fake_list(cid, cookie, **kw):
            calls["n"] += 1
            return list(BEFORE) if calls["n"] == 1 else list(AFTER)   # 1次=快照, 之后=对账

        api115.list_children_paged = fake_list
        transfer_worker.TransferManager._transfer_one = \
            lambda self, *a, **k: ("success", "1 个对象 / 3 个文件")
        submitted = []

        class FakeMgr:
            def submit(self, cid, name, rescan=False, priority=0):
                submitted.append((cid, name, priority)); return 99

        scanner.get_manager = lambda: FakeMgr()

        mgr = object.__new__(transfer_worker.TransferManager); mgr._stop_events = {}
        mgr._run_task(1)

        con = db.get_conn()
        rows = [dict(r) for r in con.execute(
            "SELECT cid,name,is_dir,size FROM tree_nodes WHERE pid='T' AND cid<>'T' ORDER BY cid")]
        con.close()
        self.assertEqual(sorted(r["cid"] for r in rows), ["NEW1", "NEW2", "NEW3", "OLD1"])
        self.assertEqual(next(r["name"] for r in rows if r["cid"] == "NEW1"), "海报 (1).jpg")
        self.assertEqual(next(r["size"] for r in rows if r["cid"] == "OLD1"), 10)
        self.assertFalse([r for r in rows if r["cid"].startswith("FID")])   # 零占位/幽灵
        self.assertEqual(submitted, [("NEW3", "花絮", 1)])                   # 新文件夹自动排扫描


if __name__ == "__main__":
    unittest.main(verbosity=2)
