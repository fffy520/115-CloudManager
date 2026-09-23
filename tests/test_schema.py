# -*- coding: utf-8 -*-
"""建表 / 旧库迁移 / 种子标签排序 的回归测试(全新安装与老库升级两条路都要通)"""
import os
import sqlite3
import sys
import traceback
import unittest

os.environ["TREE_DB"] = "/tmp/t_schema.db"
for p in ("/tmp/t_schema.db", "/tmp/t_schema.db-wal", "/tmp/t_schema.db-shm"):
    if os.path.exists(p):
        os.remove(p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config   # noqa: E402
import db, tags, tag_rules   # noqa: E402
import importlib   # noqa: E402


def switch_db(path):
    """换测试库必须连 config 一起重载: config.TREE_DB 在导入时就定死了"""
    os.environ["TREE_DB"] = path
    importlib.reload(config)
    importlib.reload(db)


class TestFreshInstall(unittest.TestCase):
    """全新空库: 建表与代码字段必须一致(曾因 assigned_at/created_at、缺 sort 列整体崩)"""

    def test_tag_pipeline_on_fresh_db(self):
        con = db.get_conn()
        con.execute("INSERT INTO tree_nodes VALUES('1','1','1','Test.2160p.HEVC',1,0)")
        con.commit(); con.close()
        tid = tags.get_or_create("4K")
        self.assertEqual(tags.assign(tid, ["1"], source="rule"), 1)
        rep = tag_rules.run_rules(clean=False, dry_run=False)
        self.assertGreaterEqual(rep["assigned"], 0)
        self.assertGreaterEqual(len(tag_rules.list_rules()["rules"]), 1)

    def test_run_rules_idempotent_and_clean(self):
        # 反复跑 + clean 路径不得撞锁(曾有双连接写锁 "database is locked")
        tag_rules.run_rules(clean=False, dry_run=False)
        tag_rules.run_rules(clean=True, dry_run=False)

    def test_seed_order(self):
        res = tags.list_tags()
        order = [t["name"] for t in res if t["category"] == "分辨率"]
        self.assertEqual(order[:4], ["4K", "1080P", "720P", "480P"])

    def test_create_appends_to_category_end(self):
        tags.create_tag("测试尾部甲", "分辨率")
        tags.create_tag("测试尾部乙", "分辨率")
        order = [t["name"] for t in tags.list_tags() if t["category"] == "分辨率"]
        self.assertEqual(order[-2:], ["测试尾部甲", "测试尾部乙"])

    def test_manual_order_protected(self):
        # 用户拖过序的分类, 启动修正不得再动
        tags.ensure_seed_tags()
        cur = [t for t in tags.list_tags() if t["category"] == "状态"]
        tags.reorder_tags([t["id"] for t in cur][::-1])
        before = [t["name"] for t in tags.list_tags() if t["category"] == "状态"]
        tags.ensure_seed_tags()   # 模拟重启
        after = [t["name"] for t in tags.list_tags() if t["category"] == "状态"]
        self.assertEqual(before, after)


class TestLegacyMigration(unittest.TestCase):
    """老库(node_tags 只有 assigned_at, tag_rules 无 sort)自动升级"""

    def test_migrate(self):
        os.environ["TREE_DB"] = "/tmp/t_schema_legacy.db"
        for p in ("/tmp/t_schema_legacy.db", "/tmp/t_schema_legacy.db-wal", "/tmp/t_schema_legacy.db-shm"):
            if os.path.exists(p):
                os.remove(p)
        con = sqlite3.connect("/tmp/t_schema_legacy.db")
        con.executescript("""
            CREATE TABLE node_tags (cid TEXT NOT NULL, tag_id INTEGER NOT NULL,
                source TEXT DEFAULT 'manual', assigned_at TEXT, PRIMARY KEY (cid, tag_id));
            CREATE TABLE tag_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, tag_name TEXT NOT NULL,
                dir_pattern TEXT NOT NULL, file_pattern TEXT DEFAULT '', on_files INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1, note TEXT DEFAULT '', created_at TEXT);
            CREATE TABLE tree_nodes (cid TEXT PRIMARY KEY, pid TEXT NOT NULL, root TEXT NOT NULL,
                name TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER DEFAULT 0);
            INSERT INTO node_tags(cid,tag_id,source,assigned_at) VALUES('1',1,'manual','2026-01-01 00:00:00');
            INSERT INTO tag_rules(tag_name,dir_pattern,created_at) VALUES('4K','2160p','2026-01-01 00:00:00');
        """)
        con.commit(); con.close()
        switch_db("/tmp/t_schema_legacy.db")
        con = db.get_conn()
        cols = [r[1] for r in con.execute("PRAGMA table_info(node_tags)")]
        self.assertIn("created_at", cols)
        self.assertEqual(con.execute("SELECT created_at FROM node_tags").fetchone()[0],
                         "2026-01-01 00:00:00")   # 旧时间戳要保留
        cols = [r[1] for r in con.execute("PRAGMA table_info(tag_rules)")]
        self.assertIn("sort", cols)
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
