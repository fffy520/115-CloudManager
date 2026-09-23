# -*- coding: utf-8 -*-
"""查重统计(子树聚合)与精确重复判定的回归测试
(曾按"扫描根"聚合却拿"一级目录"去查, 25/27 统计成 0 且精确重复整体误判)"""
import os
import sys
import unittest

os.environ["TREE_DB"] = "/tmp/t_dedup.db"
for p in ("/tmp/t_dedup.db", "/tmp/t_dedup.db-wal", "/tmp/t_dedup.db-shm"):
    if os.path.exists(p):
        os.remove(p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config   # noqa: E402
import db, dedup   # noqa: E402


def bfs_stats(con, cid):
    """独立算法(自上而下 BFS)——与实现的"自底向上归属"写法完全不同, 用来交叉验证"""
    fc, sz, frontier = 0, 0, [cid]
    while frontier:
        rows = []
        for i in range(0, len(frontier), 500):
            chunk = frontier[i:i + 500]
            qm = ",".join("?" * len(chunk))
            rows += list(con.execute(
                f"SELECT cid,is_dir,size FROM tree_nodes WHERE pid IN ({qm}) AND cid<>pid", chunk))
        frontier = [r["cid"] for r in rows if r["is_dir"]]
        for r in rows:
            if not r["is_dir"]:
                fc += 1
                sz += r["size"] or 0
    return [fc, sz]


def fresh_db(name):
    for p in (f"/tmp/{name}.db", f"/tmp/{name}.db-wal", f"/tmp/{name}.db-shm"):
        if os.path.exists(p):
            os.remove(p)
    os.environ["TREE_DB"] = f"/tmp/{name}.db"
    import importlib
    importlib.reload(config)   # config.TREE_DB 在导入时定死, 换库必须连它一起重载
    importlib.reload(db)
    importlib.reload(dedup)


class TestMemberStats(unittest.TestCase):
    def test_subtree_stats_cross_check(self):
        fresh_db("t_dedup1")   # 各用例独立库, 互不干扰
        con = db.get_conn()
        con.executemany("INSERT INTO tree_nodes VALUES(?,?,?,?,?,?)", [
            ("A", "A", "A", "根1", 1, 0), ("B", "B", "B", "根2", 1, 0),
            ("m1", "A", "A", "专辑甲", 1, 0), ("m2", "B", "B", "专辑乙", 1, 0),
            ("d1", "m1", "A", "CD1", 1, 0),
            ("f1", "d1", "A", "01.flac", 0, 100), ("f2", "d1", "A", "02.flac", 0, 200),
            ("f3", "m1", "A", "cover.jpg", 0, 5),
            ("f4", "m2", "B", "01.flac", 0, 999),
        ])
        con.commit()
        stats = dedup._member_stats(con, ["m1", "m2"])
        self.assertEqual(stats["m1"], [3, 305])
        self.assertEqual(stats["m2"], [1, 999])
        # 交叉验证: 独立算法(BFS)结果必须一致
        self.assertEqual(stats["m1"], bfs_stats(con, "m1"))
        self.assertEqual(stats["m2"], bfs_stats(con, "m2"))
        con.close()

    def test_exact_duplicate_judgement(self):
        fresh_db("t_dedup2")
        con = db.get_conn()
        con.executemany("INSERT INTO tree_nodes VALUES(?,?,?,?,?,?)", [
            ("A", "A", "A", "根1", 1, 0), ("B", "B", "B", "根2", 1, 0),
            ("q1", "A", "A", "沙丘2", 1, 0), ("q2", "B", "B", "沙丘2", 1, 0),
            ("g1", "q1", "A", "a.mkv", 0, 100), ("g2", "q2", "B", "a.mkv", 0, 50),
        ])
        con.commit(); con.close()
        res = dedup.get_duplicates()
        self.assertEqual(len(res["groups"]), 1)
        self.assertEqual(len(res["groups"][0]["exact"]), 0)   # 大小不同 → 不得误判"精确重复"
        con = db.get_conn()
        con.execute("UPDATE tree_nodes SET size=100 WHERE cid='g2'")
        con.commit(); con.close()
        res = dedup.get_duplicates()
        self.assertEqual(len(res["groups"][0]["exact"]), 1)   # 内容相同 → 判精确重复


if __name__ == "__main__":
    unittest.main(verbosity=2)
