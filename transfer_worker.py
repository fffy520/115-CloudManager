# -*- coding: utf-8 -*-
"""
转存引擎: 链接解析(粘贴/上传txt|csv|xlsx) + 后台批量转存线程
- 任务表 transfer_tasks / transfer_items 存于 115_tree.db
"""
import io
import os
import random
import re
import sqlite3
import threading
import time
import traceback
from datetime import datetime

import api115
import config
import logbus
import scanner

BASE_DIR = config.PROJECT_DIR
TREE_DB = config.TREE_DB
DELAY = 4.0  # 每条之间的基础间隔(秒)

SLUG_RE = re.compile(r"(?:115(?:cdn)?|anxia)\.com/s/([a-z0-9]+)", re.I)
PW_RE = re.compile(r"password=([^&#\s]+)", re.I)

_init_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(TREE_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    with _init_lock:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS transfer_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, target_cid TEXT NOT NULL, target_name TEXT NOT NULL,
            status TEXT DEFAULT 'queued',  -- queued/running/done/stopped/error
            total INTEGER DEFAULT 0, processed INTEGER DEFAULT 0,
            n_success INTEGER DEFAULT 0, n_repeat INTEGER DEFAULT 0,
            n_expired INTEGER DEFAULT 0, n_failed INTEGER DEFAULT 0,
            err TEXT, created_at TEXT, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS transfer_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL, share_code TEXT NOT NULL,
            receive_code TEXT DEFAULT '', title TEXT DEFAULT '',
            status TEXT DEFAULT 'pending', message TEXT DEFAULT '',
            processed_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_titems_task ON transfer_items(task_id);
        """)
        con.commit()
    return con


# ---------------- 链接解析 ----------------

def parse_text(text: str) -> list:
    """从任意文本中解析 115 分享链接
    返回 [{share_code, receive_code, title, one_click}]
    title 取链接前的同行文字(若有)，或下一行文字（原生115分享格式）"""
    out, seen = [], set()
    lines = text.splitlines() or [text]
    i = 0
    while i < len(lines):
        line = lines[i]
        m = SLUG_RE.search(line)
        if not m:
            i += 1
            continue
        slug = m.group(1).lower()
        if slug in seen:
            i += 1
            continue
        seen.add(slug)
        url_part = line.split("#")[0]
        pw = ""
        pm = PW_RE.search(url_part)
        if pm:
            pw = pm.group(1).strip()
        # 标题: 链接之外的文字(去掉纯符号)
        title = SLUG_RE.sub("", line).strip()
        title = re.sub(r"https?://\S*", "", title).strip(" \t,-|:：")
        # 原生115分享: 链接在第一行，标题和访问码在后续行
        if not title and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            # 下一行不是链接也不是空行 → 当作标题
            if next_line and not SLUG_RE.search(next_line):
                title = next_line.strip(" \t,-|:：")
                i += 1
        # 访问码兜底: 下一行含 "访问码：xxx" 且URL里没密码
        if not pw and i + 1 < len(lines):
            pw_line = lines[i + 1].strip()
            pw_match = re.match(r"访问码[：:]\s*(\S+)", pw_line)
            if pw_match:
                pw = pw_match.group(1).strip()
                i += 1
        if len(title) > 120:
            title = title[:120]
        out.append({
            "share_code": slug,
            "receive_code": pw,
            "title": title,
            "one_click": f"https://115.com/s/{slug}?password={pw}" if pw else f"https://115.com/s/{slug}",
        })
        i += 1
    return out


def parse_upload(filename: str, content: bytes) -> list:
    """上传文件解析: 支持 .txt/.csv/.xlsx
    xlsx 按单元格识别: 含 /s/ 的是链接列, 纯短字母数字串是访问码, 其余拼为标题"""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        items, seen = [], set()
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if not cells:
                    continue
                link_idx = next((i for i, c in enumerate(cells) if "/s/" in c.lower()), None)
                if link_idx is None:
                    continue
                link = cells[link_idx]
                m = SLUG_RE.search(link)
                if not m:
                    continue
                slug = m.group(1).lower()
                if slug in seen:
                    continue
                seen.add(slug)
                pw = ""
                pm = PW_RE.search(link.split("#")[0])
                if pm:
                    pw = pm.group(1).strip()
                pw_idx = -1
                if not pw:
                    # 访问码兜底: 同行其他单元格里的短字母数字串
                    for i, c in enumerate(cells):
                        if i != link_idx and re.fullmatch(r"[a-zA-Z0-9]{3,8}", c):
                            pw = c
                            pw_idx = i
                            break
                title = " ".join(c for i, c in enumerate(cells)
                                 if i != link_idx and i != pw_idx
                                 and not re.fullmatch(r"[a-zA-Z0-9]{3,8}", c))
                title = re.sub(r"https?://\S*", "", title).strip(" \t,-|:：")[:120]
                items.append({
                    "share_code": slug, "receive_code": pw, "title": title,
                    "one_click": f"https://115.com/s/{slug}?password={pw}" if pw else f"https://115.com/s/{slug}",
                })
        wb.close()
        return items
    # txt / csv / 其他文本
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return parse_text(content.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_text(content.decode("utf-8", errors="ignore"))


# ---------------- 转存任务 ----------------

class TransferManager:
    def __init__(self):
        # 服务重启时, 上次卡在 running 的任务重置为 queued(断点续传)
        con = get_conn()
        con.execute("UPDATE transfer_tasks SET status='queued' WHERE status='running'")
        con.commit()
        con.close()
        self.worker = None
        self.stop_event = threading.Event()
        if not scanner._is_reloader_process():
            self._start_worker()

    def create_task(self, name: str, items: list, target_cid: str, target_name: str) -> int:
        con = get_conn()
        cur = con.execute(
            "INSERT INTO transfer_tasks(name,target_cid,target_name,status,total,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (name or f"转存 {datetime.now():%m-%d %H:%M}", target_cid, target_name,
             "queued", len(items), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        task_id = cur.lastrowid
        con.executemany(
            "INSERT INTO transfer_items(task_id,share_code,receive_code,title) VALUES(?,?,?,?)",
            [(task_id, it["share_code"], it.get("receive_code", ""), it.get("title", ""))
             for it in items])
        con.commit()
        con.close()
        return task_id

    def stop(self, task_id: int) -> bool:
        con = get_conn()
        row = con.execute("SELECT status FROM transfer_tasks WHERE id=?", (task_id,)).fetchone()
        con.close()
        if row and row["status"] == "running":
            self.stop_event.set()
            return True
        return False

    def _start_worker(self):
        self.worker = threading.Thread(target=self._loop, daemon=True, name="transfer-worker")
        self.worker.start()

    def _loop(self):
        while True:
            try:
                con = get_conn()
                task = con.execute(
                    "SELECT * FROM transfer_tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                con.close()
                if not task:
                    time.sleep(3)
                    continue
                self.stop_event.clear()
                self._run_task(task["id"])
            except Exception:
                traceback.print_exc()
                time.sleep(10)

    def _run_task(self, task_id: int):
        con = get_conn()
        t = con.execute("SELECT * FROM transfer_tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            con.close()
            return
        con.execute("UPDATE transfer_tasks SET status='running' WHERE id=?", (task_id,))
        con.commit()
        target_cid = t["target_cid"]
        stats = {"success": 0, "repeat": 0, "expired": 0, "failed": 0}
        try:
            cookie = api115.load_cookie()
            rows = con.execute(
                "SELECT * FROM transfer_items WHERE task_id=? AND status='pending' ORDER BY id",
                (task_id,)).fetchall()
            logbus.pub("转存", f"任务 #{task_id}「{t['name']}」开始: 待处理 {len(rows)} 条"
                       f" → 「{t['target_name']}」", lv="ok")
            for idx, r in enumerate(rows, 1):
                if self.stop_event.is_set():
                    con.execute("UPDATE transfer_tasks SET status='stopped', finished_at=? WHERE id=?",
                                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
                    con.commit()
                    logbus.pub("转存", f"任务 #{task_id} 已停止(已处理 {idx-1}/{len(rows)})", lv="warn")
                    con.close()
                    return
                status, msg, snap_items = self._transfer_one(r["share_code"], r["receive_code"], target_cid, cookie)
                stats[status] += 1
                _lv = {"success": "ok", "repeat": "warn", "expired": "warn"}.get(status, "error")
                _st = {"success": "成功", "repeat": "已存在", "expired": "已失效"}.get(status, "失败")
                logbus.pub("转存", f"#{task_id} [{idx}/{len(rows)}] {r['title'] or r['share_code']}"
                           f": {_st} — {msg[:80]}", lv=_lv)
                # 转存成功后同步到本地 tree_nodes，并自动触发高优先级扫描
                if status == "success":
                    logbus.pub("转存", f"#{task_id} 转存成功, snap_items={len(snap_items) if snap_items else 0} 个", lv="info")
                    if snap_items:
                        try:
                            self._sync_to_tree(con, snap_items, target_cid, cookie)
                            # 对新转存的文件夹创建高优先级扫描任务
                            import scanner
                            for item in snap_items:
                                is_dir = item.get("fc", 1) == 0  # fc=0 表示文件夹
                                if is_dir:
                                    # 使用 _sync_to_tree 校准后的真实cid
                                    name = item.get("n") or item.get("name") or ""
                                    # 从数据库获取校准后的真实cid
                                    real_row = con.execute(
                                        "SELECT cid FROM tree_nodes WHERE pid=? AND name=?",
                                        (target_cid, name)).fetchone()
                                    if real_row:
                                        real_cid = real_row["cid"]
                                        logbus.pub("转存", f"#{task_id} 创建扫描任务: cid={real_cid}, name={name}", lv="info")
                                        job_id = scanner.get_manager().submit(real_cid, name, priority=1)
                                        logbus.pub("转存", f"#{task_id} 自动创建高优先级扫描任务 #{job_id}: {name}", lv="ok")
                        except Exception as sync_err:
                            logbus.pub("转存", f"#{task_id} 同步/扫描失败: {sync_err}", lv="error")
                con.execute(
                    "UPDATE transfer_items SET status=?, message=?, processed_at=? WHERE id=?",
                    (status, msg[:200], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
                con.execute(
                    "UPDATE transfer_tasks SET processed=processed+1,"
                    " n_success=n_success+?, n_repeat=n_repeat+?, n_expired=n_expired+?, n_failed=n_failed+?"
                    " WHERE id=?",
                    (1 if status == "success" else 0, 1 if status == "repeat" else 0,
                     1 if status == "expired" else 0, 1 if status == "failed" else 0, task_id))
                con.commit()
                time.sleep(DELAY + random.uniform(0, 2))
            con.execute("UPDATE transfer_tasks SET status='done', finished_at=? WHERE id=?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
            con.commit()
            logbus.pub("转存", f"任务 #{task_id} 完成: 成功 {stats['success']} / 已存在 "
                       f"{stats['repeat']} / 已失效 {stats['expired']} / 失败 {stats['failed']}", lv="ok")
        except Exception as e:
            con.execute("UPDATE transfer_tasks SET status='error', err=?, finished_at=? WHERE id=?",
                        (f"{e}"[:300], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
            con.commit()
            logbus.pub("转存", f"任务 #{task_id} 异常终止: {str(e)[:120]}", lv="error")
            traceback.print_exc()
        finally:
            con.close()

    def _transfer_one(self, share_code: str, receive_code: str, target_cid: str, cookie: str):
        """单条转存, 返回 (status, message, snap_items)"""
        try:
            snap = api115.share_snap(share_code, receive_code, cookie)
            if not snap.get("state"):
                msg = snap.get("error", "分享无效或访问码错误")
                return api115.classify_transfer_error(msg), msg, []
            data = snap.get("data") or {}
            snap_items = data.get("list") or []
            fids = []
            for f in snap_items:
                fid = f.get("fid")
                if not fid or str(fid) == "0":
                    fid = f.get("cid")
                if fid and str(fid) != "0":
                    fids.append(str(fid))
            if not fids:
                return "failed", "分享内容为空", []
            total = 0
            for i in range(0, len(fids), 900):
                part = fids[i:i + 900]
                res = api115.share_receive(share_code, receive_code, part, target_cid, cookie)
                if not res.get("state"):
                    msg = res.get("error", "转存失败")
                    st = api115.classify_transfer_error(msg)
                    if st == "repeat":
                        return "repeat", msg, []
                    return "failed", msg, []
                total += int((res.get("data") or {}).get("recv_file_count", 0))
            return "success", f"{len(fids)} 个对象 / {total} 个文件", snap_items
        except Exception as e:
            return "failed", f"异常: {e}", []

    def _sync_to_tree(self, con, snap_items, target_cid, cookie):
        """转存后同步新项到 tree_nodes: 乐观写入 + 单层查询校准"""
        # 查 target_cid 的 root（若已扫描过）
        row = con.execute(
            "SELECT root FROM tree_nodes WHERE cid=?", (target_cid,)).fetchone()
        root = (row["root"] if row else None) or target_cid

        # 乐观写入：用 snap 的 name/size，cid 暂用 fid（转存后会变）
        for item in snap_items:
            name = item.get("n") or item.get("name") or "?"
            size = item.get("s", 0) or 0
            is_dir = 1 if item.get("fc", 1) == 0 else 0
            fid = str(item.get("fid") or item.get("cid") or "")
            if not fid or fid == "0":
                continue
            # 验证: 跳过明显的根目录cid（pid==cid 表示根目录）
            if fid == target_cid:
                logbus.pub("转存", f"跳过无效fid={fid} (与target_cid相同), name={name}", lv="warn")
                continue
            con.execute(
                "INSERT OR REPLACE INTO tree_nodes(cid,pid,root,name,is_dir,size)"
                " VALUES(?,?,?,?,?,?)",
                (fid, target_cid, root, name, is_dir, size))

        # 单层查询校准：获取真实 cid 替换占位值
        try:
            real_items = api115.list_children(target_cid, cookie, offset=0, limit=2000)
            name_map = {}
            for ri in real_items.get("items", []):
                name_map.setdefault(ri["name"], ri)
            for item in snap_items:
                name = item.get("n") or item.get("name") or "?"
                fid = str(item.get("fid") or item.get("cid") or "")
                ri = name_map.get(name)
                if ri and ri["cid"] != fid:
                    con.execute("DELETE FROM tree_nodes WHERE cid=?", (fid,))
                    con.execute(
                        "INSERT OR REPLACE INTO tree_nodes(cid,pid,root,name,is_dir,size)"
                        " VALUES(?,?,?,?,?,?)",
                        (ri["cid"], target_cid, root, ri["name"], ri["is_dir"], ri["size"]))
        except Exception as e:
            logbus.pub("转存", f"cid校准失败: {e}", lv="warn")

        con.commit()
