# -*- coding: utf-8 -*-
"""AI 解析策略的回归测试:
- reason 曾反向改判分类("音乐纪录片" → "音乐"), 现分类以 category 为准
- subtitle 曾解析后不入库(「字幕」标签永远打不上)"""
import json
import os
import sys
import unittest

os.environ["TREE_DB"] = "/tmp/t_ai.db"
for p in ("/tmp/t_ai.db", "/tmp/t_ai.db-wal", "/tmp/t_ai.db-shm"):
    if os.path.exists(p):
        os.remove(p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, tags, ai_worker   # noqa: E402

TAX = {"types": ["电影", "剧集", "动漫", "纪录片", "综艺", "演唱会·音乐", "音乐", "体育"],
       "resolutions": ["4K", "1080P"], "subtitles": ["中字"], "countries": ["华语"],
       "qualities": ["HDR"], "audios": ["DTS"]}


def run(reason, category, confidence=0.9):
    content = json.dumps({"items": [{"cid": "1", "category": category, "resolution": "4K",
        "subtitle": "中字", "country": "华语", "quality": "HDR", "audio": "DTS",
        "confidence": confidence, "reason": reason, "suggested_name": ""}]}, ensure_ascii=False)
    return ai_worker.parse_ai_items(content, ["1"], TAX)["1"]


class TestCategoryPolicy(unittest.TestCase):
    def test_reason_cannot_override(self):
        # 核心回归: 「音乐纪录片」不得被改成 音乐
        r = run("讲述一支乐队的音乐纪录片", "电影")
        self.assertEqual(r["category"], "电影")
        self.assertEqual(r["confidence"], 0.8)              # 存疑降置信度
        self.assertTrue(r["reason"].startswith("⚠分类存疑"))

    def test_reason_fallback_when_missing(self):
        self.assertEqual(run("一部音乐纪录片", "")["category"], "纪录片")   # 最长匹配

    def test_no_conflict_no_flag(self):
        r = run("剧情精彩表演出色", "电影")
        self.assertEqual((r["category"], r["confidence"]), ("电影", 0.9))
        self.assertFalse(r["reason"].startswith("⚠"))


class TestSubtitlePersist(unittest.TestCase):
    def test_subtitle_end_to_end(self):
        con = db.get_conn()
        con.execute("INSERT INTO tree_nodes VALUES('1','1','1','测试根',1,0)")
        con.execute("INSERT INTO tree_nodes VALUES('3','1','1','Test Movie.mkv',0,1000)")
        con.commit(); con.close()
        tags.ensure_seed_tags()
        canned = json.dumps({"items": [{"cid": "3", "category": "电影", "resolution": "4K",
            "subtitle": "中字", "country": "华语", "quality": "HDR", "audio": "DTS",
            "confidence": 0.9, "reason": "测试", "suggested_name": ""}]}, ensure_ascii=False)
        ai_worker.chat_json = lambda messages, max_tokens=6000: (canned, 10, 20)
        con = db.get_conn()
        cur = con.execute("INSERT INTO ai_batches(scope_cid,scope_name,status,created_at)"
                          " VALUES('1','测试','queued','2026-01-01 00:00:00')")
        con.commit(); bid = cur.lastrowid; con.close()
        mgr = object.__new__(ai_worker.AiManager); mgr._stop_events = {}
        mgr._run_batch(bid)
        con = db.get_conn()
        row = dict(con.execute("SELECT * FROM ai_suggestions WHERE cid='3'").fetchone())
        con.close()
        self.assertEqual(row.get("subtitle"), "中字")       # 曾在这里丢失
        tags.assign_ai_suggestion(row["cid"], row["category"], row["resolution"],
                                  row["subtitle"], row["country"], row["quality"], row["audio"])
        names = {t["name"] for lst in tags.tags_for_cids(["3"]).values() for t in lst}
        self.assertIn("中字", names)                          # 审核通过后「中字」标签要落上


if __name__ == "__main__":
    unittest.main(verbosity=2)
