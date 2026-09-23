# -*- coding: utf-8 -*-
"""
标签系统: 受控词表(tags) + 节点关联(node_tags)
- 标签是纯本地库概念, 115 云端不可见; 移动目录 cid 不变 => 标签自动跟随
- source 区分打标来源: manual=手动 / rule=规则引擎(tag_rules.py) / ai=AI(预留)
- 打标对象必须是已入库(tree_nodes)的 cid; 目录删除时 server._remove_subtree 同步清理关联
- SQLite 999 变量上限: 所有 IN 查询/写入按 500 分块
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

import config
import db
import logbus

CATEGORIES = ["类型", "分辨率", "字幕", "国家", "画质", "音频", "状态", "来源", "自定义"]
TYPE_CATEGORY = "类型"   # AI 审核落标使用的分类(内容类型, 规则判不了)

# 种子词表(仅首启时创建, 之后在网页上自由增删; 颜色为 6 位 hex, 前端叠加透明度用)
SEED_TAGS = [
    # 类型: 规则判不了, 由 AI 分类审核通过后落标(source='ai')
    ("电影",         "类型", "#0052d9"),
    ("剧集",         "类型", "#7a5af8"),
    ("动漫",         "类型", "#e37318"),
    ("纪录片",       "类型", "#2ba471"),
    ("综艺",         "类型", "#b0812b"),
    ("演唱会·音乐",  "类型", "#0aa5a5"),
    ("音乐",         "类型", "#e34d59"),
    ("体育",         "类型", "#5b5b6e"),
    # 分辨率(载体/编码格式)
    ("4K",       "分辨率", "#7a5af8"),
    ("1080P",    "分辨率", "#8a919c"),
    ("720P",     "分辨率", "#5b5b6e"),
    ("480P",     "分辨率", "#9a5b2c"),
    ("Remux",    "分辨率", "#0aa5a5"),
    ("Web-DL",   "分辨率", "#0052d9"),
    ("HDTV",     "分辨率", "#8a919c"),
    ("BluRay",   "分辨率", "#7a5af8"),
    ("原盘",     "分辨率", "#e37318"),
    ("Encode",   "分辨率", "#5b5b6e"),
    ("HEVC",     "分辨率", "#7a5af8"),
    ("H.264",    "分辨率", "#8a919c"),
    ("10bit",    "分辨率", "#2ba471"),
    # 字幕
    ("中字",     "字幕", "#0052d9"),
    ("国语",     "字幕", "#e34d59"),
    ("粤语",     "字幕", "#e37318"),
    ("日语",     "字幕", "#7a5af8"),
    ("英语",     "字幕", "#2ba471"),
    ("双语",     "字幕", "#0aa5a5"),
    ("多音轨",   "字幕", "#b0812b"),
    # 国家/地区
    ("华语",     "国家", "#e34d59"),
    ("日本",     "国家", "#7a5af8"),
    ("韩国",     "国家", "#0052d9"),
    ("欧美",     "国家", "#2ba471"),
    ("港台",     "国家", "#e37318"),
    ("印度",     "国家", "#b0812b"),
    # 画质(HDR/动态范围)
    ("HDR",      "画质", "#2ba471"),
    ("HDR10",    "画质", "#0052d9"),
    ("HDR10+",   "画质", "#0aa5a5"),
    ("Dolby Vision", "画质", "#e34d59"),
    ("HLG",      "画质", "#0052d9"),
    ("SDR",      "画质", "#9a5b2c"),
    # 音频格式
    ("DTS-HD",   "音频", "#0052d9"),
    ("DTS-HD MA","音频", "#3585f7"),
    ("TrueHD",   "音频", "#7a5af8"),
    ("Atmos",    "音频", "#e37318"),
    ("DTS",      "音频", "#8a919c"),
    ("DTS-X",    "音频", "#5b5b6e"),
    ("AC3",      "音频", "#2ba471"),
    ("AAC",      "音频", "#0aa5a5"),
    ("FLAC",     "音频", "#e34d59"),
    ("LPCM",     "音频", "#b0812b"),
    ("DSD",      "音频", "#0aa5a5"),
    # 状态
    ("未看",     "状态", "#e37318"),
    ("在看",     "状态", "#0052d9"),
    ("看完可删", "状态", "#e34d59"),
    ("待人工",   "状态", "#b0812b"),
    # 来源
    ("分享转存", "来源", "#4a5568"),
    ("自购",     "来源", "#0052d9"),
    ("录制",     "来源", "#7a5af8"),
]

# AI 建议的载体(format) -> 落标用的标签名; 空/未知不落
FORMAT_TAG_NAMES = {"原盘": "原盘", "remux": "Remux", "web-dl": "Web-DL", "hdtv": "HDTV"}

_CHUNK = 500


def get_conn() -> sqlite3.Connection:
    """兼容旧接口, 委托给 db.get_conn()"""
    return db.get_conn()


def ensure_tables():
    """表已在 db.py 中创建, 此函数保留兼容接口"""
    pass


def ensure_seed_tags():
    """启动时检查种子标签，自动补上缺失的（不覆盖用户已删除的旧种子）"""
    con = get_conn()
    try:
        # 收集用户曾手动删除的标签名（不应重新创建）
        row = con.execute("SELECT value FROM app_settings WHERE key='deleted_seed_tags'").fetchone()
        deleted = set()
        if row:
            try:
                deleted = set(json.loads(row["value"]))
            except Exception:
                pass
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added = 0
        cat_seq = {}   # 分类内序号: 按种子设计顺序排(4K→1080P→720P…), 不再掉进字母乱序
        for name, cat, color in SEED_TAGS:
            if name in deleted:
                continue
            cat_seq[cat] = cat_seq.get(cat, -1) + 1
            cur = con.execute(
                "INSERT OR IGNORE INTO tags(name,category,color,sort_order,created_at)"
                " VALUES(?,?,?,?,?)",
                (name, cat, color, cat_seq[cat], now))
            if cur.rowcount > 0:
                added += 1
        if added:
            logbus.pub("标签", f"新增 {added} 个种子标签", lv="ok")
        _normalize_seed_order(con)   # 没被手动排过的分类, 按种子设计顺序排
        con.commit()
    finally:
        con.close()


def _normalize_seed_order(con):
    """把"没被用户手动排过"的分类按种子设计顺序排(4K→1080P→720P…)。
    为什么不能只按 id 排: 老库当年就是按字母序创建的, id 顺序=字母顺序, 治不了本。
    用户在编辑模式拖过序的分类会被写进 app_settings:tag_sort_manual 标记,
    本函数永不改动带标记的分类(拖过 = 以用户为准)。每分类内: 种子按设计顺序在前,
    用户/规则建的标签按创建顺序接在后面。"""
    manual = set()
    row = con.execute("SELECT value FROM app_settings WHERE key='tag_sort_manual'").fetchone()
    if row:
        try:
            manual = set(json.loads(row["value"]))
        except Exception:
            pass
    cats = {r["category"] for r in con.execute("SELECT DISTINCT category FROM tags")}
    for cat in cats - manual:
        seq = 0
        seeded = set()
        for name, c, _color in SEED_TAGS:
            if c != cat:
                continue
            seeded.add(name)
            con.execute("UPDATE tags SET sort_order=? WHERE name=? AND category=?",
                        (seq, name, cat))
            seq += 1
        for r in con.execute("SELECT id, name FROM tags WHERE category=? ORDER BY id", (cat,)):
            if r["name"] not in seeded:
                con.execute("UPDATE tags SET sort_order=? WHERE id=?", (seq, r["id"]))
                seq += 1


def mark_seed_deleted(tag_name: str):
    """记录用户删除的种子标签名，防止重启后重新创建"""
    con = get_conn()
    try:
        row = con.execute("SELECT value FROM app_settings WHERE key='deleted_seed_tags'").fetchone()
        deleted = []
        if row:
            try:
                deleted = json.loads(row["value"])
            except Exception:
                pass
        if tag_name not in deleted:
            deleted.append(tag_name)
            con.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('deleted_seed_tags',?)",
                        (json.dumps(deleted, ensure_ascii=False),))
            con.commit()
    finally:
        con.close()


# ---------------- 标签 CRUD ----------------
def list_tags() -> list:
    """全部标签 + 已打数量, 按自定义排序 → 分类 → 名称"""
    con = get_conn()
    try:
        rows = con.execute("""
            SELECT t.id, t.name, t.category, t.color, count(n.cid) AS n
            FROM tags t LEFT JOIN node_tags n ON n.tag_id=t.id
            GROUP BY t.id ORDER BY t.category, t.sort_order, t.name""").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def reorder_tags(tag_ids: list):
    """保存标签拖拽排序: tag_ids 按用户当前顺序排列, 依次写入 sort_order
    (拖拽仅限同分类内, 这批 id 属于同一分类)。保存后给该分类打"手动排过"标记,
    之后启动时的种子顺序修正永不再动它 —— 拖过就以用户为准。"""
    if not tag_ids:
        return
    con = get_conn()
    try:
        ids = [int(t) for t in tag_ids]
        for i, tid in enumerate(ids):
            con.execute("UPDATE tags SET sort_order=? WHERE id=?", (i, tid))
        qm = ",".join("?" * len(ids))
        cats = {r["category"] for r in con.execute(
            f"SELECT DISTINCT category FROM tags WHERE id IN ({qm})", ids)}
        manual = set()
        row = con.execute("SELECT value FROM app_settings WHERE key='tag_sort_manual'").fetchone()
        if row:
            try:
                manual = set(json.loads(row["value"]))
            except Exception:
                pass
        manual |= cats
        con.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('tag_sort_manual',?)",
                    (json.dumps(sorted(manual), ensure_ascii=False),))
        con.commit()
    finally:
        con.close()


def create_tag(name: str, category: str = "自定义", color: str = "") -> int:
    name = (name or "").strip()[:30]
    if not name:
        raise ValueError("标签名不能为空")
    category = (category or "自定义").strip() or "自定义"
    con = get_conn()
    try:
        # 新标签排到本分类末尾(sort_order 全 0 会显示成字母乱序)
        nxt = con.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM tags WHERE category=?",
            (category,)).fetchone()[0]
        cur = con.execute(
            "INSERT INTO tags(name,category,color,sort_order,created_at) VALUES(?,?,?,?,?)",
            (name, category, (color or "").strip(), nxt,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"标签「{name}」已存在")
    finally:
        con.close()


def get_or_create(name: str, category: str = "自定义", color: str = "") -> int:
    con = get_conn()
    try:
        row = con.execute("SELECT id FROM tags WHERE name=?", ((name or "").strip(),)).fetchone()
        if row:
            return row["id"]
        nxt = con.execute(
            "SELECT COALESCE(MAX(sort_order),0)+1 FROM tags WHERE category=?",
            (category,)).fetchone()[0]
        cur = con.execute(
            "INSERT OR IGNORE INTO tags(name,category,color,sort_order,created_at) VALUES(?,?,?,?,?)",
            ((name or "").strip(), category, color, nxt,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        if cur.lastrowid:
            return cur.lastrowid
        return con.execute("SELECT id FROM tags WHERE name=?", ((name or "").strip(),)).fetchone()["id"]
    finally:
        con.close()


def update_tag(tag_id: int, name=None, category=None, color=None):
    """部分更新: 传 None 的字段保持不变"""
    con = get_conn()
    try:
        sets, args = [], []
        if name is not None:
            name = name.strip()[:30]
            if not name:
                raise ValueError("标签名不能为空")
            ex = con.execute("SELECT id FROM tags WHERE name=? AND id<>?", (name, tag_id)).fetchone()
            if ex:
                raise ValueError(f"标签「{name}」已存在")
            sets.append("name=?"); args.append(name)
        if category is not None:
            sets.append("category=?"); args.append(category.strip() or "自定义")
        if color is not None:
            sets.append("color=?"); args.append(color.strip())
        if not sets:
            return
        args.append(tag_id)
        con.execute(f"UPDATE tags SET {', '.join(sets)} WHERE id=?", args)
        con.commit()
    finally:
        con.close()


def delete_tag(tag_id: int) -> str:
    con = get_conn()
    try:
        row = con.execute("SELECT name FROM tags WHERE id=?", (tag_id,)).fetchone()
        if not row:
            raise ValueError("标签不存在")
        tag_name = row["name"]
        con.execute("DELETE FROM node_tags WHERE tag_id=?", (tag_id,))
        con.execute("DELETE FROM tags WHERE id=?", (tag_id,))
        con.commit()
        # 记录删除的标签名，防止种子标签重启后重新创建
        mark_seed_deleted(tag_name)
        return tag_name
    finally:
        con.close()


# ---------------- 打标 / 摘标 ----------------
def _filter_existing(con, cids: list) -> list:
    """只保留已入库的 cid(未扫描/在线浏览的节点无法打标)"""
    out = []
    for i in range(0, len(cids), _CHUNK):
        chunk = [str(c) for c in cids[i:i + _CHUNK] if str(c).strip()]
        if not chunk:
            continue
        qm = ",".join("?" * len(chunk))
        out.extend(r["cid"] for r in con.execute(
            f"SELECT cid FROM tree_nodes WHERE cid IN ({qm})", chunk))
    return out


def assign(tag_id: int, cids: list, source: str = "manual") -> int:
    """给一批 cid 打标签; 已有的关联保持原 source 不被覆盖"""
    if not cids:
        return 0
    con = get_conn()
    try:
        if not con.execute("SELECT id FROM tags WHERE id=?", (tag_id,)).fetchone():
            raise ValueError("标签不存在")
        valid = _filter_existing(con, cids)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        src = source if source in ("manual", "rule", "ai") else "manual"
        before = con.total_changes
        for i in range(0, len(valid), _CHUNK):
            chunk = valid[i:i + _CHUNK]
            con.executemany(
                "INSERT OR IGNORE INTO node_tags(cid,tag_id,source,created_at) VALUES(?,?,?,?)",
                [(c, tag_id, src, now) for c in chunk])
        n = con.total_changes - before
        con.commit()
        return n
    finally:
        con.close()


def unassign(tag_id: int, cids: list) -> int:
    if not cids:
        return 0
    con = get_conn()
    try:
        n = 0
        for i in range(0, len(cids), _CHUNK):
            chunk = [str(c) for c in cids[i:i + _CHUNK]]
            qm = ",".join("?" * len(chunk))
            cur = con.execute(f"DELETE FROM node_tags WHERE tag_id=? AND cid IN ({qm})",
                              [tag_id] + chunk)
            n += cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


def tags_for_cids(cids: list) -> dict:
    """{cid: [{id,name,category,color}]} — 树浏览/打标弹窗共用"""
    con = get_conn()
    out = {}
    try:
        for i in range(0, len(cids), _CHUNK):
            chunk = [str(c) for c in cids[i:i + _CHUNK]]
            if not chunk:
                continue
            qm = ",".join("?" * len(chunk))
            for r in con.execute(f"""
                    SELECT n.cid, t.id, t.name, t.category, t.color
                    FROM node_tags n JOIN tags t ON t.id=n.tag_id
                    WHERE n.cid IN ({qm}) ORDER BY t.category, t.name""", chunk):
                out.setdefault(r["cid"], []).append(
                    {"id": r["id"], "name": r["name"],
                     "category": r["category"], "color": r["color"]})
        return out
    finally:
        con.close()


def nodes_for_tag(tag_id: int, limit: int = 500):
    """打了某标签的节点清单(内联 JOIN 保证只返回仍在树上的)"""
    con = get_conn()
    try:
        total = con.execute("""
            SELECT count(*) FROM node_tags n JOIN tree_nodes t ON t.cid=n.cid
            WHERE n.tag_id=?""", (tag_id,)).fetchone()[0]
        rows = con.execute("""
            SELECT t.cid, t.pid, t.root, t.name, t.is_dir, t.size
            FROM node_tags n JOIN tree_nodes t ON t.cid=n.cid
            WHERE n.tag_id=?
            ORDER BY t.is_dir DESC, t.size DESC LIMIT ?""", (tag_id, int(limit))).fetchall()
        return [dict(r) for r in rows], total
    finally:
        con.close()


def nodes_for_tags(tag_ids: list, op: str = "and", limit: int = 500):
    """多标签组合筛选 + 每个标签在当前结果中的命中数

    op='and': 交集——cid 必须同时拥有全部 tag_ids
    op='or':  并集——cid 拥有 tag_ids 中任一即可
    返回:
        rows, total, per_tag_count
      - rows: 节点清单(同 nodes_for_tag 字段)
      - total: 组合后的命中总数(不截断的总数)
      - per_tag_count: {tag_id: 该标签在主结果中的命中数}, 用来在 UI 上展示每个标签的贡献
    """
    if not tag_ids:
        return [], 0, {}
    op = (op or "and").lower()
    if op not in ("and", "or"):
        op = "and"
    con = get_conn()
    try:
        ids = [int(t) for t in tag_ids]
        ph = ",".join(["?"] * len(ids))
        ids_params = tuple(ids)  # 仅 ids, 长度 = ph 个
        # 构造"命中条件"的子查询(共享给 total/detail/per_tag 三处)
        if op == "and":
            cond_sql = f"""
                SELECT n.cid FROM node_tags n
                JOIN tree_nodes t ON t.cid = n.cid
                WHERE n.tag_id IN ({ph})
                GROUP BY n.cid
                HAVING COUNT(DISTINCT n.tag_id) = ?
            """
            cond_params = (*ids_params, len(ids))
        else:  # or
            cond_sql = f"""
                SELECT n.cid FROM node_tags n
                JOIN tree_nodes t ON t.cid = n.cid
                WHERE n.tag_id IN ({ph})
            """
            cond_params = ids_params
        # 1) 总数(无截断)
        total = con.execute(f"SELECT COUNT(*) FROM ({cond_sql})", cond_params).fetchone()[0]
        # 2) 详情(按目录优先 + 大小降序), 仍用 JOIN 过滤孤儿节点
        detail_sql = f"""
            SELECT t.cid, t.pid, t.root, t.name, t.is_dir, t.size
            FROM tree_nodes t
            WHERE t.cid IN ({cond_sql})
            ORDER BY t.is_dir DESC, t.size DESC LIMIT ?
        """
        rows = con.execute(detail_sql, (*cond_params, int(limit))).fetchall()
        # 3) 每个 tag 在主结果中的命中数
        per_tag_sql = f"""
            SELECT n.tag_id, COUNT(DISTINCT n.cid)
            FROM node_tags n
            WHERE n.tag_id IN ({ph})
              AND n.cid IN ({cond_sql})
            GROUP BY n.tag_id
        """
        per_tag_rows = con.execute(per_tag_sql, (*ids_params, *cond_params)).fetchall()
        per_tag = {int(r[0]): int(r[1]) for r in per_tag_rows}
        # 任何输入标签如果没在结果里出现, 补 0
        for tid in ids:
            per_tag.setdefault(tid, 0)
        return [dict(r) for r in rows], total, per_tag
    finally:
        con.close()


def cleanup_orphans() -> int:
    """清理指向已不存在节点的关联(安全网, 正常删除路径会同步清理)"""
    con = get_conn()
    try:
        cur = con.execute("DELETE FROM node_tags WHERE cid NOT IN (SELECT cid FROM tree_nodes)")
        con.commit()
        return cur.rowcount
    finally:
        con.close()


# ---------------- AI 建议桥接(审核通过 -> 自动落标) ----------------
AI_MANAGED_CATEGORIES = ["类型", "分辨率", "字幕", "国家", "画质", "音频"]


def _ai_managed_tag_ids(con) -> list:
    """AI 落标管理的标签 id: 类型/分辨率/字幕/国家/画质/音频 分类下的全部标签"""
    ids = []
    for cat in AI_MANAGED_CATEGORIES:
        ids.extend(r["id"] for r in con.execute("SELECT id FROM tags WHERE category=?", (cat,)))
    return ids


def assign_ai_suggestion(cid: str, category: str, resolution: str = "",
                         subtitle: str = "", country: str = "",
                         quality: str = "", audio: str = "") -> int:
    """AI 建议通过后落标: 类型 + 分辨率 + 字幕 + 国家 + 画质 + 音频, source='ai'
    其他·待定/未知/空 不打; 已有 rule/manual 同名关联时不覆盖"""
    cid = str(cid)
    n = 0
    category = (category or "").strip()
    if category and category != "其他·待定":
        n += assign(get_or_create(category, category=TYPE_CATEGORY), [cid], source="ai")
    # 分辨率/字幕/国家/画质/音频：可选维度，有值就落标
    for val, cat_name in [(resolution, "分辨率"), (subtitle, "字幕"),
                          (country, "国家"), (quality, "画质"), (audio, "音频")]:
        val = (val or "").strip()
        if val and val != "未知":
            n += assign(get_or_create(val, category=cat_name), [cid], source="ai")
    return n


def revoke_ai_suggestion(cids: list) -> int:
    """摘掉 AI 来源的类型/载体标签(只删 source='ai', 不动 rule/manual 关联)"""
    if not cids:
        return 0
    con = get_conn()
    try:
        ids = _ai_managed_tag_ids(con)
        if not ids:
            return 0
        n = 0
        for i in range(0, len(cids), _CHUNK):
            cchunk = [str(c) for c in cids[i:i + _CHUNK]]
            for j in range(0, len(ids), _CHUNK):
                ichunk = ids[j:j + _CHUNK]
                qm_i = ",".join("?" * len(ichunk))
                qm_c = ",".join("?" * len(cchunk))
                cur = con.execute(
                    f"DELETE FROM node_tags WHERE source='ai' AND tag_id IN ({qm_i})"
                    f" AND cid IN ({qm_c})", ichunk + cchunk)
                n += cur.rowcount
        con.commit()
        return n
    finally:
        con.close()
