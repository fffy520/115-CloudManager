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
import db
import logbus

INTERVAL = (config.SCAN_INTERVAL_MIN, config.SCAN_INTERVAL_MAX)
RETRY_WAIT = config.SCAN_RETRY_WAIT
MAX_AUTO_RETRY = config.SCAN_RETRY_MAX


def get_conn() -> sqlite3.Connection:
    """兼容旧接口, 委托给 db.get_conn()"""
    return db.get_conn()


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
        self._stop_events: dict[int, threading.Event] = {}  # 按 job_id 记录停止事件
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
            if job_id in self._stop_events:
                self._stop_events[job_id].set()
            return True
        return False

    # ---------- 内部 ----------
    def _start_worker(self):
        self.worker = threading.Thread(target=self._loop, daemon=True, name="scan-worker")
        self.worker.start()

    def _loop(self):
        # 启动时先认领「上一个进程留下的孤儿任务」。
        # 进程被重启/杀掉后，status 会永久停在 running(或 retry_wait)：
        # _loop 只挑 queued，界面又只在 error/stopped/paused 时给「继续」按钮，
        # 结果就是任务看起来永远在跑、实际早就死了。这里统一退回 queued 让它续扫
        # （已扫过的目录靠 scan_state 断点跳过，不会重复请求 115）。
        try:
            con = get_conn()
            orphans = con.execute(
                "SELECT id FROM scan_jobs WHERE status IN ('running','retry_wait')").fetchall()
            if orphans:
                con.execute("UPDATE scan_jobs SET status='queued', current_path='',"
                            " current_cid='' WHERE status IN ('running','retry_wait')")
                con.commit()
                logbus.pub("扫描", f"检测到 {len(orphans)} 个上次未完成的任务, 已重新排队续扫",
                           lv="warn")
            con.close()
        except Exception:
            traceback.print_exc()

        while True:
            try:
                con = get_conn()
                # 优先取高优先级任务，同优先级按ID顺序
                job = con.execute(
                    "SELECT * FROM scan_jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1").fetchone()
                if not job:
                    # 无排队任务时，检查是否有被抢占暂停的任务需要恢复
                    paused = con.execute(
                        "SELECT id FROM scan_jobs WHERE status='paused' AND rescan=0 ORDER BY id LIMIT 1"
                    ).fetchone()
                    if paused:
                        con.execute("UPDATE scan_jobs SET status='queued' WHERE id=?", (paused["id"],))
                        con.commit()
                        logbus.pub("扫描", f"自动恢复被抢占的任务 #{paused['id']}", lv="ok")
                    con.close()
                    time.sleep(3)
                    continue
                con.close()
                # 为当前任务创建 stop_event
                self._stop_events[job["id"]] = threading.Event()
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
                if self._stop_events.get(job_id, threading.Event()).is_set():
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
                    # 断点跳过阶段同样要刷新「当前目录」，否则界面会长时间停在旧位置上
                    cur_path = db.full_path(con, dir_cid, keep=3) or dir_cid
                    con.execute("UPDATE scan_jobs SET done_count=?, total=?,"
                                " current_cid=?, current_path=? WHERE id=?",
                                (total_done, total_done + len(stack),
                                 dir_cid, cur_path, job_id))
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
                # 记录「当前正在扫的目录」的完整路径，界面据此显示真实位置。
                # keep=3: 只留末 3 段(… / 音乐合集 / VA - CPO / 专辑)，避免深层路径撑爆日志行。
                cur_path = db.full_path(con, dir_cid, keep=3) or dir_cid
                con.execute("UPDATE scan_jobs SET done_count=?, total=?,"
                            " current_cid=?, current_path=? WHERE id=?",
                            (total_done, total_done + len(stack),
                             dir_cid, cur_path, job_id))
                con.commit()
                logbus.pub("扫描", f"#{job_id} [{total_done}/{total_done + len(stack)}] "
                           f"{cur_path} ({len(items)} 项)")
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
            # 清理该任务的 stop_event
            self._stop_events.pop(job_id, None)
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
                if self._stop_events.get(job_id, threading.Event()).is_set():
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
