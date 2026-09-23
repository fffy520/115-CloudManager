# -*- coding: utf-8 -*-
"""查重引擎: 平移 tree_html_115.py 的算法, 读 tree_nodes 出重复组"""
import os
import re
import sqlite3
from collections import defaultdict

import config
import db

CUT = re.compile(
    r'[._\s-]*(?:2160p|1080[pi]|720p|4320p|WEB[-_. ]?DL|WEBRip|WEB|Blu-?ray|BluRay|BDrip|'
    r'HDTV|Remux|HDR10?|DoVi|DV\b|HEVC|AVC|x264|x265|H\.?26[45]|AAC|DD[P5]?[ .]?[25]?\.?[01]?|'
    r'Atmos|60FPS|50FPS|30FPS|AAC2\.0|DDP5\.1|Complete|V2)\b.*$',
    re.IGNORECASE,
)


def norm(n: str) -> str:
    s = CUT.sub("", n).strip("._ ")
    s = re.sub(r'[._\s]+$', '', s)
    return s.lower() or n.lower()


def fmt_size(b: int) -> str:
    if b >= 1024 ** 4: return f"{b/1024**4:.2f} TB"
    if b >= 1024 ** 3: return f"{b/1024**3:.2f} GB"
    if b >= 1024 ** 2: return f"{b/1024**2:.1f} MB"
    return f"{max(b//1024,0)} KB"


def get_duplicates(scope_root: str = None):
    """返回 {groups: [{key, names:[{cid,pid,path,name,fc,sz}], exact, sizes}], meta}
    scope_root: 限定某个根目录内查重; None=全部一级目录互查"""
    con = db.get_conn()
    # 一级目录(pid=某root 且自身是目录) 按范围取; 附带父目录名用于展示完整路径
    if scope_root:
        roots = con.execute(
            "SELECT t.cid, t.pid, t.name,"
            " (SELECT name FROM tree_nodes WHERE cid=t.pid) AS pname"
            " FROM tree_nodes t WHERE t.pid=? AND t.is_dir=1 AND t.cid<>t.pid"
            " ORDER BY t.name", (scope_root,)).fetchall()
    else:
        # HiveWeb转存 的 cid 已知为 root 锚点; 通用做法: 取既是目录、pid 等于自身 root 的顶层
        roots = con.execute("""
            SELECT DISTINCT t.cid, t.pid, t.name,
              (SELECT name FROM tree_nodes WHERE cid=t.pid) AS pname
            FROM tree_nodes t
            JOIN tree_nodes p ON t.pid = p.cid AND p.pid = p.cid
            WHERE t.is_dir=1 AND t.cid<>t.pid ORDER BY t.name""").fetchall()

    stats = _member_stats(con, [r["cid"] for r in roots])
    con.close()

    groups_map = defaultdict(list)
    for r in roots:
        cid = r["cid"]
        fc, sz = stats.get(cid, [0, 0])
        groups_map[norm(r["name"])].append({
            "cid": cid, "pid": r["pid"], "name": r["name"], "fc": fc, "sz": sz,
            "path": f'{r["pname"] or "?"} / {r["name"]}',
        })

    out = []
    for key, members in groups_map.items():
        if len(members) < 2:
            continue
        bysig = defaultdict(list)
        for m in members:
            bysig[(m["fc"], m["sz"])].append(m)
        exact = [v for v in bysig.values() if len(v) >= 2]
        out.append({
            "key": members[0]["name"],
            "names": sorted(members, key=lambda x: x["name"].lower()),
            "exact": exact,
            "sizes": sorted({m["sz"] for m in members}),
        })
    out.sort(key=lambda g: (-len(g["names"]), g["key"].lower()))
    return {"groups": out}


def _member_stats(con, member_ids) -> dict:
    """算出每个成员(一级目录)子树的 [文件数, 大小]。
    注意: tree_nodes.root 是"扫描根"而非"一级目录", 不能当聚合键 —— 旧版按 root
    聚合却拿一级目录 cid 去查, 几乎全部查成 (0, 0): 显示 0 文件/0B, 且"精确重复"
    按 (0,0) 签名聚类整体失真。正确做法:
      ① SQL 按父目录聚合"直属文件"(重活让数据库干);
      ② 只对目录(约21万行)沿 pid 链向上累加到最近的成员为止(记忆化防重复爬链)。
    """
    direct = defaultdict(lambda: [0, 0])      # 目录cid -> [直属文件数, 直属文件大小]
    for r in con.execute(
            "SELECT pid, count(*) AS c, sum(size) AS s FROM tree_nodes"
            " WHERE is_dir=0 GROUP BY pid"):
        direct[r["pid"]] = [r["c"], r["s"] or 0]
    # 目录 -> 父目录(排除锚点 pid==cid 的自环, 向上走到锚点即止)
    parent = {r["cid"]: r["pid"] for r in con.execute(
        "SELECT cid, pid FROM tree_nodes WHERE is_dir=1 AND cid<>pid")}

    stats = {m: [0, 0] for m in member_ids}   # 成员cid -> [子树文件数, 子树大小]
    memo = {}                                 # 目录cid -> 归属成员cid(None=不属于)
    for dir_cid, (fc, sz) in direct.items():
        owner, chain, cur, seen = None, [], dir_cid, set()
        while cur and cur not in seen:
            seen.add(cur)
            if cur in memo:
                owner = memo[cur]
                break
            chain.append(cur)
            if cur in stats:
                owner = cur
                break
            cur = parent.get(cur)
        for c in chain:                       # 本链路各层归属一致, 记忆化免得重复爬
            memo[c] = owner
        if owner:
            stats[owner][0] += fc
            stats[owner][1] += sz
    return stats


def get_stats():
    """总览统计"""
    con = db.get_conn()
    meta = {}
    meta["nodes"] = con.execute("SELECT count(*) c FROM tree_nodes").fetchone()["c"]
    meta["files"] = con.execute("SELECT count(*) c FROM tree_nodes WHERE is_dir=0").fetchone()["c"]
    meta["dirs"] = con.execute("SELECT count(*) c FROM tree_nodes WHERE is_dir=1").fetchone()["c"]
    meta["scanned"] = con.execute("SELECT count(*) c FROM scan_state WHERE status='done'").fetchone()["c"]
    meta["total_size"] = con.execute(
        "SELECT coalesce(sum(size),0) s FROM tree_nodes WHERE is_dir=0").fetchone()["s"]
    # 一级目录数(顶层)
    meta["roots"] = con.execute("""
        SELECT count(*) c FROM tree_nodes t
        JOIN tree_nodes p ON t.pid = p.cid AND p.pid = p.cid
        WHERE t.is_dir=1""").fetchone()["c"]
    con.close()
    return meta
