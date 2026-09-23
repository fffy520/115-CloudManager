# -*- coding: utf-8 -*-
"""
幽灵节点大扫除: 找出本地清单(tree_nodes)里"云端查无此项"的脏记录, 报告后可清理。

背景: 旧版转存同步是"先用分享侧 fid 占位、再按名字校准", 校准没命中的占位记录会永久
残留, 变成云端查无此项的幽灵节点; 另外在 115 App 里直接删除/移动的文件, 本地清单
也不会自动过期。本模块三步走:

  1. find_suspects(con)          纯本地找嫌疑(不联网、只读), 秒级
  2. verify(rows, cookie)        按父目录分组联网核对(每个父目录只列一次目录)
  3. sweep(con, ghosts, reason)  清理: 先留底 JSON 再删, 只动本地清单, 绝不碰云端

铁律:
  - 只清本地账本, 云端一根汗毛都不动;
  - 拿不准的一律留下(核对失败/接口异常 => 标记"未核实", 不删);
  - 删前必留底, 留底文件可查可恢复。

用法(命令行):
    python ghosts.py --report     # 第1+2步: 找嫌疑并联网核对, 只打印报告
    python ghosts.py --sweep      # 第3步: 按报告清理(先留底再删)
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime

import config
import db

# 名字里的补丁后缀, 如 "海报 (1).jpg" —— 同名转存被 115 改名的典型痕迹
SUFFIX_RE = re.compile(r"\(\d+\)\s*(?:\.[A-Za-z0-9]{1,8})?$")


def find_suspects(con=None, broad: bool = False) -> list:
    """纯本地找嫌疑条目, 返回 [{cid, pid, name, why}]。
    幽灵节点只可能是"转存同步"写下的占位记录, 而它一律写在转存目标目录下, 所以:
      a. (默认)曾经的转存目标目录下的全部直接子项 —— 精准主战场;
      b. (broad=True 才启用)同父重名 / "(N)" 补丁后缀 —— 用于顺带体检"过时记录",
         但音乐/演唱会库天然有海量重名和补丁名, 误报极多, 不做默认。
    """
    own = con is None
    if own:
        con = db.get_conn()
    try:
        targets = {r["cid"] for r in con.execute(
            "SELECT DISTINCT target_cid AS cid FROM transfer_tasks")}
        suspects = {}

        def mark(row, why):
            key = row["cid"]
            if key in suspects:
                whys = set(suspects[key]["why"].split("+"))
                whys.add(why)
                suspects[key]["why"] = "+".join(sorted(whys))
            else:
                suspects[key] = {"cid": row["cid"], "pid": row["pid"],
                                 "name": row["name"], "why": why}

        # a. 转存目标目录的全部直接子项(排除锚点行)
        for t in targets:
            for r in con.execute(
                    "SELECT cid, pid, name FROM tree_nodes WHERE pid=? AND cid<>pid", (t,)):
                mark(r, "转存目标子项")
        if broad:
            # b. 同父重名
            for r in con.execute("""
                    SELECT a.cid, a.pid, a.name FROM tree_nodes a
                    JOIN tree_nodes b ON a.pid=b.pid AND a.name=b.name AND a.cid<b.cid
                    WHERE a.cid<>a.pid"""):
                mark(r, "同父重名")
            # c. "(N)" 补丁后缀
            for r in con.execute(
                    "SELECT cid, pid, name FROM tree_nodes WHERE cid<>pid"):
                if r["name"] and SUFFIX_RE.search(r["name"]):
                    mark(r, "补丁后缀")
        return sorted(suspects.values(), key=lambda x: (x["pid"], x["name"]))
    finally:
        if own:
            con.close()


def verify(rows: list, cookie: str) -> dict:
    """联网核对嫌疑: 按父目录分组, 每个父目录只列一次, cid 不在云端清单里的=幽灵。
    返回 {ghosts:[...], kept:[...], unverified:[...], checked_dirs:int}
    任何一目录核对失败 => 该目录全部嫌疑标"未核实", 一律不删(宁留勿删)。"""
    import api115
    by_pid = {}
    for r in rows:
        by_pid.setdefault(r["pid"], []).append(r)
    ghosts, kept, unverified = [], [], []
    checked = 0
    for pid, items in sorted(by_pid.items()):
        try:
            children = api115.list_children_paged(pid, cookie)
            cloud_cids = {it["cid"] for it in children}
            checked += 1
            for r in items:
                (ghosts if r["cid"] not in cloud_cids else kept).append(r)
        except Exception as e:
            for r in items:
                r = dict(r)
                r["err"] = str(e)[:120]
                unverified.append(r)
        time.sleep(1.5)   # 对 115 温柔一点
    return {"ghosts": ghosts, "kept": kept, "unverified": unverified,
            "checked_dirs": checked}


def sweep(con=None, ghosts: list = None, reason: str = "manual") -> dict:
    """清理确认的幽灵条目: 先留底 JSON, 再删本地记录(含子树/标签/扫描状态)。
    只动本地清单, 绝不调用任何云端接口。"""
    ghosts = ghosts or []
    own = con is None
    if own:
        con = db.get_conn()
    try:
        # 留底: 万一清错, 有据可查可手工恢复
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = os.path.join(config.BACKUP_DIR, f"ghost_sweep-{ts}-{reason}.json")
        with open(backup_path, "w", encoding="utf-8") as f:
            json.dump({"swept_at": ts, "reason": reason, "ghosts": ghosts},
                      f, ensure_ascii=False, indent=2)

        removed = 0
        for g in ghosts:
            subs = _collect_subtree(con, g["cid"])
            for i in range(0, len(subs), 500):
                chunk = subs[i:i + 500]
                qm = ",".join("?" * len(chunk))
                con.execute(f"DELETE FROM tree_nodes WHERE cid IN ({qm})", chunk)
                con.execute(f"DELETE FROM scan_state WHERE cid IN ({qm})", chunk)
                con.execute(f"DELETE FROM node_tags WHERE cid IN ({qm})", chunk)
                removed += len(chunk)
        con.commit()
        return {"removed": removed, "backup": backup_path, "ghosts": len(ghosts)}
    finally:
        if own:
            con.close()


def _collect_subtree(con, cid: str) -> list:
    """收集 cid 及其全部后代(幽灵目录下若被扫过, 子孙同样是假账)"""
    out, frontier = [cid], [cid]
    while frontier:
        kids = []
        for i in range(0, len(frontier), 500):
            chunk = frontier[i:i + 500]
            qm = ",".join("?" * len(chunk))
            kids.extend(r["cid"] for r in con.execute(
                f"SELECT cid FROM tree_nodes WHERE pid IN ({qm}) AND cid<>pid", chunk))
        out.extend(kids)
        frontier = kids
    return out


def report_text(res: dict) -> str:
    """把核对结果排成给人看的报告"""
    lines = [
        f"核对了 {res['checked_dirs']} 个目录:",
        f"  · 坐实幽灵(云端查无此项): {len(res['ghosts'])} 条",
        f"  · 冤枉了, 实为真记录:    {len(res['kept'])} 条",
        f"  · 未能核实(一律不删):    {len(res['unverified'])} 条",
    ]
    for title, rows in (("幽灵", res["ghosts"]), ("未核实", res["unverified"])):
        if rows:
            lines.append(f"--- {title}明细(最多列30条) ---")
            for r in rows[:30]:
                lines.append(f"  [{r['why']}] {r['name'][:50]}  (cid={r['cid']}, pid={r['pid']})"
                             + (f"  错误: {r['err']}" if r.get("err") else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import api115
    rows = find_suspects()
    print(f"第1步 本地找嫌疑: {len(rows)} 条")
    if "--report" in sys.argv or "--sweep" not in sys.argv:
        cookie = api115.load_cookie()
        res = verify(rows, cookie)
        print("第2步 联网核对:")
        print(report_text(res))
    if "--sweep" in sys.argv:
        cookie = api115.load_cookie()
        res = verify(rows, cookie)
        out = sweep(ghosts=res["ghosts"], reason="cli")
        print(f"第3步 清理完成: 删 {out['removed']} 行, 留底 {out['backup']}")
