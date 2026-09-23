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
import db
import logbus
import scanner

BASE_DIR = config.PROJECT_DIR
TREE_DB = config.TREE_DB
DELAY = 4.0  # 每条之间的基础间隔(秒)

SLUG_RE = re.compile(r"(?:115(?:cdn)?|anxia)\.com/s/([a-z0-9]+)", re.I)
PW_RE = re.compile(r"password=([^&#\s]+)", re.I)
# 单独成行的访问码, 形如 "访问码：ab12" / "访问码:ab12"
PW_LINE_RE = re.compile(r"访问码[：:]\s*(\S+)")
# xlsx 单元格里的访问码候选: 3-8 位短串且必须"同时含字母和数字"。
# 纯数字(片名 "2012")、纯字母(片名 "Se7en" 之外的词)不再被误当访问码吃掉。
PWCELL_RE = re.compile(r"(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]{3,8}")


def get_conn() -> sqlite3.Connection:
    """兼容旧接口, 委托给 db.get_conn()"""
    return db.get_conn()


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
        # 原生115分享: 链接在第一行，标题和访问码在后续行。
        # 顺序关键: 必须先认访问码行、再消费标题行 —— "链接+访问码"(无标题)是
        # 最常见格式, 旧代码先消费标题会把访问码当标题吃掉, 该格式 100% 转存失败。
        if not pw and i + 1 < len(lines):
            pm2 = PW_LINE_RE.match(lines[i + 1].strip())
            if pm2:
                pw = pm2.group(1).strip()
                i += 1
        # 标题: 下一行不是链接/访问码也不是空行 → 当作标题
        if not title and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if next_line and not SLUG_RE.search(next_line) and not PW_LINE_RE.match(next_line):
                title = next_line.strip(" \t,-|:：")
                i += 1
        # 三行格式(链接+标题+访问码): 消费完标题再往后认一次访问码
        if not pw and i + 1 < len(lines):
            pm2 = PW_LINE_RE.match(lines[i + 1].strip())
            if pm2:
                pw = pm2.group(1).strip()
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
                    # 访问码兜底: 同行其他单元格里"字母+数字混合"的短串才当访问码,
                    # 纯数字片名(如 "2012")不再被误吃成访问码
                    for i, c in enumerate(cells):
                        if i != link_idx and PWCELL_RE.fullmatch(c):
                            pw = c
                            pw_idx = i
                            break
                title = " ".join(c for i, c in enumerate(cells)
                                 if i != link_idx and i != pw_idx
                                 and not PWCELL_RE.fullmatch(c))
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

# 全局单例(镜像 scanner.get_manager): 谁都别再自己 new TransferManager ——
# 每 new 一次就多一条永不退出的工作线程(旧版恢复接口就是这么泄漏的)
_manager = None


def get_manager() -> "TransferManager":
    global _manager
    if _manager is None:
        _manager = TransferManager()
    return _manager


class TransferManager:
    def __init__(self):
        # 服务重启时, 上次卡在 running 的任务重置为 queued(断点续传)
        con = get_conn()
        con.execute("UPDATE transfer_tasks SET status='queued' WHERE status='running'")
        con.commit()
        con.close()
        self.worker = None
        self._shutdown = threading.Event()                   # 停工信号(备份恢复/退出前用)
        self._stop_events: dict[int, threading.Event] = {}  # 按 task_id 记录停止事件
        # 单进程直跑, 见 scanner.ScanManager.__init__ 说明: 必须无条件启动转存线程
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
        """停止排队/运行中的任务。排队中的直接写库拦下(旧版只认 running, 假成功)"""
        con = get_conn()
        try:
            row = con.execute("SELECT status FROM transfer_tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] not in ("queued", "running"):
                return False
            con.execute(
                "UPDATE transfer_tasks SET status='stopped', finished_at=?"
                " WHERE id=? AND status IN ('queued','running')",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
            con.commit()
            ev = self._stop_events.get(task_id)   # 用 get: 与工作线程的清理 pop 有竞态
            if ev:
                ev.set()
            return True
        finally:
            con.close()

    def shutdown(self, timeout: float = 30) -> bool:
        """停稳工作线程(备份恢复/退出前调用): 让当前任务断点保存后退出循环"""
        self._shutdown.set()
        for ev in list(self._stop_events.values()):
            ev.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout)
        return self.worker is None or not self.worker.is_alive()

    def resume(self):
        """复工: 残留的 running 退回排队(断点续传), 重启工作线程"""
        self._shutdown.clear()
        con = get_conn()
        con.execute("UPDATE transfer_tasks SET status='queued' WHERE status='running'")
        con.commit()
        con.close()
        self._start_worker()

    def _start_worker(self):
        if self.worker is not None and self.worker.is_alive():
            return   # 防重复启动: 两条循环会把同一任务领两遍
        self.worker = threading.Thread(target=self._loop, daemon=True, name="transfer-worker")
        self.worker.start()

    def _loop(self):
        while not self._shutdown.is_set():
            try:
                con = get_conn()
                task = con.execute(
                    "SELECT * FROM transfer_tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                con.close()
                if not task:
                    time.sleep(3)
                    continue
                # 为当前任务创建 stop_event
                self._stop_events[task["id"]] = threading.Event()
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
                if self._stop_events.get(task_id, threading.Event()).is_set():
                    con.execute("UPDATE transfer_tasks SET status='stopped', finished_at=? WHERE id=?",
                                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), task_id))
                    con.commit()
                    logbus.pub("转存", f"任务 #{task_id} 已停止(已处理 {idx-1}/{len(rows)})", lv="warn")
                    self._stop_events.pop(task_id, None)
                    con.close()
                    return
                # ① 快照(转存前): 记下目标目录现状, 用于转存后差集对账。
                #    铁律: 本地树只登记"对账确认的新项"(网盘真编号/真名字/真大小),
                #    绝不写占位记录 —— 旧版"分享侧 fid 占位 + 按名字校准"会产生
                #    云端查无此项的幽灵节点(占位记录没被校准命中就永远残留)。
                before_ids = None
                try:
                    before_ids = {it["cid"] for it in api115.list_children_paged(target_cid, cookie)}
                except Exception as e:
                    logbus.pub("转存", f"#{task_id} 快照失败(转存照常, 本地树由扫描补齐): {str(e)[:80]}",
                               lv="warn")
                status, msg = self._transfer_one(r["share_code"], r["receive_code"], target_cid, cookie)
                stats[status] += 1
                _lv = {"success": "ok", "repeat": "warn", "expired": "warn"}.get(status, "error")
                _st = {"success": "成功", "repeat": "已存在", "expired": "已失效"}.get(status, "失败")
                logbus.pub("转存", f"#{task_id} [{idx}/{len(rows)}] {r['title'] or r['share_code']}"
                           f": {_st} — {msg[:80]}", lv=_lv)
                # ② 对账(转存后): 新出现的项 = 这次转存的成果, 直接用真编号登记,
                #    并给新文件夹排高优先级扫描补全内部结构
                if status == "success":
                    if before_ids is None:
                        logbus.pub("转存", f"#{task_id} 未拍到快照, 跳过本地登记(可对该目录发起扫描补齐)",
                                   lv="warn")
                    else:
                        try:
                            after = api115.list_children_paged(target_cid, cookie)
                            new_items = [it for it in after if it["cid"] not in before_ids]
                            self._sync_new_items(con, new_items, target_cid)
                            logbus.pub("转存", f"#{task_id} 对账完成: 新登记 {len(new_items)} 项(全部真编号)",
                                       lv="ok")
                            for it in new_items:
                                if it["is_dir"]:
                                    job_id = scanner.get_manager().submit(it["cid"], it["name"], priority=1)
                                    logbus.pub("转存", f"#{task_id} 自动创建高优先级扫描任务 #{job_id}: {it['name']}", lv="ok")
                        except Exception as sync_err:
                            # 宁缺毋假: 对账失败就不登记, 交给扫描全量补齐
                            logbus.pub("转存", f"#{task_id} 对账失败, 本地树交由扫描补齐: {sync_err}", lv="warn")
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
            # 清理该任务的 stop_event
            self._stop_events.pop(task_id, None)
            con.close()

    def _transfer_one(self, share_code: str, receive_code: str, target_cid: str, cookie: str):
        """单条转存, 返回 (status, message)。本地树登记不在这里做, 由调用方差集对账"""
        try:
            snap = api115.share_snap(share_code, receive_code, cookie)
            if not snap.get("state"):
                msg = snap.get("error", "分享无效或访问码错误")
                return api115.classify_transfer_error(msg), msg
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
                return "failed", "分享内容为空"
            total = 0
            for i in range(0, len(fids), 900):
                part = fids[i:i + 900]
                res = api115.share_receive(share_code, receive_code, part, target_cid, cookie)
                if not res.get("state"):
                    msg = res.get("error", "转存失败")
                    return api115.classify_transfer_error(msg), msg
                total += int((res.get("data") or {}).get("recv_file_count", 0) or 0)
            return "success", f"{len(fids)} 个对象 / {total} 个文件"
        except Exception as e:
            return "failed", f"异常: {e}"

    def _sync_new_items(self, con, new_items, target_cid):
        """把对账确认的新项登记进本地树(真编号/真名字/真大小, 均来自网盘列目录结果)"""
        if not new_items:
            return 0
        row = con.execute(
            "SELECT root FROM tree_nodes WHERE cid=?", (target_cid,)).fetchone()
        root = (row["root"] if row else None) or target_cid
        for it in new_items:
            con.execute(
                "INSERT OR REPLACE INTO tree_nodes(cid,pid,root,name,is_dir,size)"
                " VALUES(?,?,?,?,?,?)",
                (str(it["cid"]), target_cid, root, it["name"], it["is_dir"], it["size"] or 0))
        con.commit()
        return len(new_items)
