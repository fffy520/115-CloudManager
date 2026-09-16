# -*- coding: utf-8 -*-
"""
扫描引擎: 后台线程 + 任务表 + 断点续扫
- 一次只跑一个扫描任务(全局串行, 保护 115 WAF 限额)
- 复用 115_tree.db 的 tree_nodes / scan_state 表
- 新增 scan_jobs 表记录任务, schedules 表记录定时配置
"""
import os
import random
import sqlite3
import threading
import time
import traceback
from datetime import datetime

import api115
import config
import logbus

INTERVAL = (config.SCAN_INTERVAL_MIN, config.SCAN_INTERVAL_MAX)
RETRY_WAIT = config.SCAN_RETRY_WAIT
MAX_AUTO_RETRY = config.SCAN_RETRY_MAX

_init_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(config.TREE_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    with _init_lock:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tree_nodes (
            cid TEXT PRIMARY KEY, pid TEXT NOT NULL, root TEXT NOT NULL,
            name TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_nodes_pid ON tree_nodes(pid);
        CREATE INDEX IF NOT EXISTS idx_nodes_root ON tree_nodes(root);
        CREATE TABLE IF NOT EXISTS scan_state (
            cid TEXT PRIMARY KEY, status TEXT DEFAULT 'pending',
            err TEXT, scanned_at TEXT, node_count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS scan_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_cid TEXT NOT NULL, target_name TEXT NOT NULL,
            status TEXT DEFAULT 'queued',   -- queued/running/retry_wait/done/error/stopped
            rescan INTEGER DEFAULT 0,       -- 1=强制重扫(清除旧记录)
            priority INTEGER DEFAULT 0,    -- 0=普通, 1=高优先级(转存后自动触发)
            total INTEGER DEFAULT 0, done_count INTEGER DEFAULT 0,
            err TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_cid TEXT NOT NULL, target_name TEXT NOT NULL,
            hour INTEGER NOT NULL, minute INTEGER NOT NULL,
            enabled INTEGER DEFAULT 1, last_run TEXT, created_at TEXT);
        """)
        # 旧库迁移: scan_jobs 补 retry_count 列
        cols = [r[1] for r in con.execute("PRAGMA table_info(scan_jobs)")]
        if "retry_count" not in cols:
            con.execute("ALTER TABLE scan_jobs ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "priority" not in cols:
            con.execute("ALTER TABLE scan_jobs ADD COLUMN priority INTEGER DEFAULT 0")
        con.commit()
    return con


class ScanManager:
    """全局单例: 管理扫描线程与任务队列"""

    def __init__(self):
        # 服务重启时, 上次卡在 running 的任务重置为 queued(断点续扫)
        # 注意: stopped 状态的任务不要重置，保持原状
        con = get_conn()
        con.execute("UPDATE scan_jobs SET status='queued' WHERE status='running'")
        con.commit()
        con.close()
        self.worker = None
        self.stop_event = threading.Event()
        # 始终启动扫描线程。
        # 本项目启动方式均为单进程直跑(server.py 用 uvicorn.run(app, ...) 且未开 reload;
        # run.py 亦显式 reload=False)。uvicorn 的 reload/workers 分支要求以 import string
        # 传入 app, 本项目从未使用, 因此根本不存在"reloader 主进程"。
        # 旧代码在 __main__.__file__ 以 server.py 结尾时跳过启动, 导致 python server.py
        # 场景下扫描线程永不创建, 所有扫描任务永久停留在 queued。
        self._start_worker()

    # ---------- 对外 ----------
    def submit(self, target_cid: str, target_name: str, rescan: bool = False, priority: int = 0) -> int:
        con = get_conn()
        # 同一 target 排队中/运行中的任务不重复提交
        row = con.execute(
            "SELECT id FROM scan_jobs WHERE target_cid=? AND status IN ('queued','running')",
            (target_cid,)).fetchone()
        if row:
            con.close()
            return row["id"]
        cur = con.execute(
            "INSERT INTO scan_jobs(target_cid,target_name,status,rescan,priority,total,created_at)"
            " VALUES(?,?,?,?,?,0,?)",
            (target_cid, target_name, "queued", 1 if rescan else 0, priority,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        jid = cur.lastrowid
        con.close()
        return jid

    def stop(self, job_id: int) -> bool:
        """请求停止正在运行/等待重试/排队中的任务"""
        con = get_conn()
        row = con.execute("SELECT status FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
        con.close()
        if row and row["status"] in ("running", "retry_wait", "queued"):
            self.stop_event.set()
            return True
        return False

    # ---------- 内部 ----------
    def _start_worker(self):
        self.worker = threading.Thread(target=self._loop, daemon=True, name="scan-worker")
        self.worker.start()

    def _loop(self):
        while True:
            try:
                con = get_conn()
                # 优先取高优先级任务，同优先级按ID顺序
                job = con.execute(
                    "SELECT * FROM scan_jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1").fetchone()
                # paused 任务不自动恢复, 需用户手动点"继续"(scan_resume 端点重新入队)
                con.close()
                if not job:
                    time.sleep(3)
                    continue
                self.stop_event.clear()
                self._run_job(job["id"])
            except Exception:
                traceback.print_exc()
                time.sleep(10)

    def _run_job(self, job_id: int):
        con = get_conn()
        job = con.execute("SELECT * FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            con.close()
            return
        target_cid, target_name = job["target_cid"], job["target_name"]
        con.execute("UPDATE scan_jobs SET status='running', started_at=? WHERE id=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id))
        con.commit()
        logbus.pub("扫描", f"任务 #{job_id} 开始:「{target_name}」", lv="ok")
        try:
            cookie = api115.load_cookie()
            # 目标自身入树。
            # 关键: 若该目录已在树中, 必须保留它原有的 pid/root 归属, 只更新名称。
            # 旧代码用 INSERT OR REPLACE 无条件写成 pid=root=自身, 会把"别人树里的
            # 子目录"从父目录的浏览列表中摘掉 —— 例如扫描 最近接收/自动转存 之后,
            # 自动转存 就再也无法从 最近接收 里点开(数据没丢, 但入口消失)。
            existed = con.execute(
                "SELECT pid FROM tree_nodes WHERE cid=?", (target_cid,)).fetchone()
            if existed:
                con.execute("UPDATE tree_nodes SET name=?, is_dir=1 WHERE cid=?",
                            (target_name, target_cid))
            else:
                con.execute(
                    "INSERT INTO tree_nodes(cid,pid,root,name,is_dir,size) VALUES(?,?,?,?,1,0)",
                    (target_cid, target_cid, target_cid, target_name))
            if job["rescan"]:
                con.execute("DELETE FROM scan_state WHERE cid=?", (target_cid,))
            con.commit()

            # 收集任务涉及的目录(种子=目标自身)
            stack = [target_cid]
            total_done = 0
            skipped = 0
            while stack:
                if self.stop_event.is_set():
                    # 检查当前状态：如果是用户手动暂停（paused），保持paused状态
                    current_status = con.execute("SELECT status FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
                    if current_status and current_status["status"] == "paused":
                        # 用户手动暂停，保持paused状态，不更新为stopped
                        logbus.pub("扫描", f"任务 #{job_id} 已暂停(用户请求, 已扫 {total_done})", lv="warn")
                        con.close()
                        return
                    else:
                        # 系统停止或其他情况，更新为stopped
                        con.execute("UPDATE scan_jobs SET status='stopped', finished_at=? WHERE id=?",
                                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id))
                        con.commit()
                        logbus.pub("扫描", f"任务 #{job_id} 已停止(用户请求, 已扫 {total_done})", lv="warn")
                        con.close()
                        return
                # 检查是否有高优先级任务需要暂停当前任务
                if job["priority"] == 0:  # 普通任务才检查
                    high_job = con.execute(
                        "SELECT id FROM scan_jobs WHERE status='queued' AND priority=1 LIMIT 1"
                    ).fetchone()
                    if high_job:
                        # 暂停当前任务，利用断点续扫恢复
                        con.execute("UPDATE scan_jobs SET status='paused', finished_at=? WHERE id=?",
                                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id))
                        con.commit()
                        logbus.pub("扫描", f"任务 #{job_id} 暂停(高优先级任务 #{high_job['id']} 需要执行, 已扫 {total_done})", lv="warn")
                        con.close()
                        return
                dir_cid = stack.pop()
                # 断点续扫: 已 done 的目录直接从本地库取子目录, 不再请求 API
                st = con.execute("SELECT status FROM scan_state WHERE cid=?", (dir_cid,)).fetchone()
                if st and st["status"] == "done" and not job["rescan"]:
                    for k in con.execute(
                            "SELECT cid,is_dir FROM tree_nodes WHERE pid=? AND cid<>pid", (dir_cid,)):
                        if k["is_dir"]:
                            stack.append(k["cid"])
                    total_done += 1
                    skipped += 1
                    if skipped == 1 or skipped % 200 == 0:
                        logbus.pub("扫描", f"#{job_id} 断点续扫: 已跳过 {skipped} 个已扫目录…")
                    con.execute("UPDATE scan_jobs SET done_count=?, total=? WHERE id=?",
                                (total_done, total_done + len(stack), job_id))
                    con.commit()
                    continue
                items = api115.list_children_paged(dir_cid, cookie)
                for it in items:
                    con.execute(
                        "INSERT OR REPLACE INTO tree_nodes(cid,pid,root,name,is_dir,size) VALUES(?,?,?,?,?,?)",
                        (it["cid"], dir_cid, target_cid, it["name"], it["is_dir"], it["size"]))
                    if it["is_dir"]:
                        stack.append(it["cid"])
                con.execute(
                    "INSERT OR REPLACE INTO scan_state(cid,status,scanned_at,node_count) VALUES(?,?,?,?)",
                    (dir_cid, "done", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(items)))
                total_done += 1
                con.execute("UPDATE scan_jobs SET done_count=?, total=? WHERE id=?",
                            (total_done, total_done + len(stack), job_id))
                con.commit()
                rname = con.execute("SELECT name FROM tree_nodes WHERE cid=?", (dir_cid,)).fetchone()
                logbus.pub("扫描", f"#{job_id} [{total_done}/{total_done + len(stack)}] "
                           f"{rname['name'] if rname else dir_cid} ({len(items)} 项)")
                time.sleep(random.uniform(*INTERVAL))

            con.execute(
                "UPDATE scan_jobs SET status='done', finished_at=?, total=done_count WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), job_id))
            con.commit()
            logbus.pub("扫描", f"任务 #{job_id} 完成: 共 {total_done} 个目录"
                       f"{f'(含断点跳过 {skipped})' if skipped else ''}", lv="ok")
        except Exception as e:
            traceback.print_exc()
            self._auto_retry(con, job_id, str(e))
        finally:
            con.close()

    def _auto_retry(self, con, job_id: int, err_msg: str):
        """任务失败后自动探测重试: 等 RETRY_WAIT 秒探测一次网络+Cookie,
        通过则回到队列续扫(已扫目录自动跳过); 最多 MAX_AUTO_RETRY 轮, 仍不行才标 error"""
        def now():
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            row = con.execute("SELECT retry_count FROM scan_jobs WHERE id=?", (job_id,)).fetchone()
            retries = (row["retry_count"] or 0) if row else 0
        except Exception:
            retries = 0

        while retries < MAX_AUTO_RETRY:
            retries += 1
            con.execute(
                "UPDATE scan_jobs SET status='retry_wait', err=?, retry_count=?, finished_at=? WHERE id=?",
                (err_msg[:300], retries, now(), job_id))
            con.commit()
            logbus.pub("扫描", f"任务 #{job_id} 失败: {err_msg[:100]}"
                       f" -> {RETRY_WAIT}s 后自动探测 (第 {retries}/{MAX_AUTO_RETRY} 轮)", lv="error")
            # 可中断等待(用户请求停止时立即结束等待)
            for _ in range(RETRY_WAIT):
                if self.stop_event.is_set():
                    con.execute("UPDATE scan_jobs SET status='stopped', finished_at=? WHERE id=?",
                                (now(), job_id))
                    con.commit()
                    logbus.pub("扫描", f"任务 #{job_id} 在重试等待中被手动停止", lv="warn")
                    return
                time.sleep(1)
            # 探测: DNS/网络/Cookie 一次过
            try:
                cookie = api115.load_cookie()
                pr = api115.probe(cookie)
            except Exception as e:
                pr = {"ok": False, "error": str(e)[:120]}
            if pr["ok"]:
                con.execute("UPDATE scan_jobs SET status='queued', finished_at=NULL WHERE id=?",
                            (job_id,))
                con.commit()
                logbus.pub("扫描", f"探测通过, 任务 #{job_id} 自动续扫", lv="ok")
                return
            logbus.pub("扫描", f"探测未通过: {pr['error']}", lv="warn")

        con.execute("UPDATE scan_jobs SET status='error', finished_at=? WHERE id=?",
                    (now(), job_id))
        con.commit()
        logbus.pub("扫描", f"任务 #{job_id} 自动重试 {MAX_AUTO_RETRY} 轮仍未恢复, "
                   f"标记失败(可在任务列表点继续)", lv="error")


# 全局单例
_manager = None

def get_manager() -> ScanManager:
    global _manager
    if _manager is None:
        _manager = ScanManager()
    return _manager
