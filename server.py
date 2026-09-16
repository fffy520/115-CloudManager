# -*- coding: utf-8 -*-
"""
115 网盘管理器 — 本地 Web 服务
启动: python server.py  ->  http://127.0.0.1:8765
功能: 目录树浏览 / 扫描管理(自定义+定时) / 转存(粘贴+上传解析) / 查重 / 文件管理
"""
import json
import os
import random
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import ai_worker
import api115
import config
import db
import dedup
import logbus
import scanner
import tag_rules
import tags
import transfer_worker

BASE_DIR = config.PROJECT_DIR
TREE_DB = config.TREE_DB
BACKUP_DIR = config.BACKUP_DIR
COOKIE_FILE = config.COOKIE_FILE

app = FastAPI(title="115 管理器")
scan_mgr = scanner.get_manager()  # 全局单例: transfer_worker 也通过 get_manager() 取同一实例, 避免多实例各自跑扫描线程
transfer_mgr = transfer_worker.TransferManager()
ai_mgr = ai_worker.AiManager()

# ---------- 定时扫描调度(每分钟检查 schedules 表) ----------
def _schedule_tick():
    con = scanner.get_conn()
    now = datetime.now()
    for s in con.execute("SELECT * FROM schedules WHERE enabled=1").fetchall():
        last = s["last_run"] or ""
        today_key = f"{now:%Y-%m-%d}"
        if (s["hour"] == now.hour and s["minute"] == now.minute
                and not last.startswith(today_key)):
            jid = scan_mgr.submit(s["target_cid"], s["target_name"], rescan=True)
            con.execute("UPDATE schedules SET last_run=? WHERE id=?",
                        (now.strftime("%Y-%m-%d %H:%M:%S"), s["id"]))
            con.commit()
            logbus.pub("扫描", f"定时任务触发「{s['target_name']}」→ 任务 #{jid}", lv="ok")
    con.close()


# ---------- 数据库备份 ----------
# 策略:
#   - 启动时立刻备份一次 (基线)
#   - 每天凌晨 03:00 自动备份
#   - 保留最近 7 个每日备份 + 4 个周备份(每周日的备份升级为周备份)
#   - 备份文件含 db 主文件 + cookie (cookie 单独存,因为不属于 db)
#   - 用 SQLite backup API 解决 WAL 模式直接 cp 会有 -shm/-wal 残留的问题

def _backup_now(reason: str = "manual") -> dict:
    """执行一次备份,返回 {name, size, path, ts, kind}"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = int(time.time())
    kind = "weekly" if datetime.now().weekday() == 6 else "daily"  # 周日=周备份
    name = f"115_tree-{datetime.now():%Y%m%d-%H%M%S}-{kind}-{reason}.db"
    target = os.path.join(BACKUP_DIR, name)

    # SQLite 自带 backup API,一致快照
    src = sqlite3.connect(TREE_DB, timeout=30)
    try:
        dst = sqlite3.connect(target, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size = os.path.getsize(target)
    logbus.pub("系统", f"数据库备份({reason}): {name} ({size//1024} KB)", lv="ok")

    # 顺便备份一份 cookie(每次 db 备份都同步一份)
    if os.path.exists(COOKIE_FILE):
        shutil.copy2(COOKIE_FILE, os.path.join(BACKUP_DIR, "cookie115.latest.txt"))

    # 清理旧备份
    _cleanup_old_backups()
    return {"name": name, "size": size, "path": target,
            "ts": ts, "kind": kind, "reason": reason}


def _cleanup_old_backups():
    """保留最近 N 个每日 + M 个周备份(数量由环境变量控制)"""
    if not os.path.isdir(BACKUP_DIR):
        return
    items = []
    for fn in os.listdir(BACKUP_DIR):
        if not fn.endswith(".db") or not fn.startswith("115_tree-"):
            continue
        full = os.path.join(BACKUP_DIR, fn)
        items.append((os.path.getmtime(full), full))
    items.sort(reverse=True)
    keep = []
    weekly = []
    daily = []
    for _, p in items:
        fn = os.path.basename(p)
        if "weekly" in fn:
            weekly.append(p)
            if len(weekly) > config.BACKUP_KEEP_WEEKLY:
                try: os.remove(p); print(f"[备份清理] 删除周备份 {fn}", flush=True)
                except: pass
        else:
            daily.append(p)
            if len(daily) > config.BACKUP_KEEP_DAILY:
                try: os.remove(p); print(f"[备份清理] 删除日备份 {fn}", flush=True)
                except: pass


def _backup_tick():
    """每小时检查一次,凌晨 3 点触发每日备份"""
    now = datetime.now()
    state_file = os.path.join(BACKUP_DIR, "_last_daily.txt")
    today = now.strftime("%Y-%m-%d")
    if now.hour == 3 and now.minute < 5:
        try:
            last = open(state_file).read().strip() if os.path.exists(state_file) else ""
            if last != today:
                _backup_now("scheduled-0300")
                open(state_file, "w").write(today)
        except Exception as e:
            print(f"[备份] 定时备份失败: {e}", flush=True)


def _list_backups() -> list:
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not fn.endswith(".db") or not fn.startswith("115_tree-"):
            continue
        full = os.path.join(BACKUP_DIR, fn)
        st = os.stat(full)
        kind = "weekly" if "weekly" in fn else "daily"
        reason = "scheduled" if "scheduled" in fn else ("startup" if "startup" in fn else "manual")
        out.append({"name": fn, "size": st.st_size, "ts": int(st.st_mtime),
                    "kind": kind, "reason": reason})
    return out


def _restore_backup(name: str) -> dict:
    """从备份恢复。会先备份当前 DB 再覆盖。"""
    # 校验文件名白名单(防路径穿越)
    import re
    if not re.match(r'^115_tree-[\w.-]+\.db$', name):
        raise HTTPException(400, "非法文件名")
    src = os.path.join(BACKUP_DIR, name)
    # 确保文件路径在 BACKUP_DIR 内
    if os.path.realpath(src) != os.path.realpath(os.path.join(BACKUP_DIR, os.path.basename(src))):
        raise HTTPException(400, "非法文件路径")
    if not os.path.isfile(src):
        raise HTTPException(404, "备份不存在")
    # 先备份当前（保护性恢复）
    safety = _backup_now(f"pre-restore-{name[:30]}")
    # 停止三个 worker(避免并发写入)
    logbus.pub("系统", "恢复前停止后台任务...", lv="warn")
    scan_mgr = scanner.get_manager()
    transfer_mgr = transfer_worker.TransferManager()
    ai_mgr = ai_worker.AiManager()
    # 断开所有 SQLite 连接（WAL checkpoint）
    con = _db()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    # 覆盖
    shutil.copy2(src, TREE_DB)
    # 清理 WAL/SHM 残留
    for ext in ("-wal", "-shm"):
        p = TREE_DB + ext
        if os.path.exists(p):
            try: os.remove(p)
            except: pass
    logbus.pub("系统", f"数据库恢复完成: {name} (恢复前状态已备份到 {safety['name']})", lv="warn")
    return {"restored_from": name, "safety_backup": safety["name"]}


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler()
    sched.add_job(_schedule_tick, "interval", seconds=30, id="tick")
    sched.add_job(_backup_tick, "interval", minutes=5, id="backup_tick")
    # 每日 03:05 保存总量快照（趋势数据）
    sched.add_job(_stats_snapshot_job, "cron", hour=3, minute=5, id="stats_snapshot")
    # 每 30 分钟主动体检 Cookie（不依赖浏览器开着）
    sched.add_job(_cookie_tick, "interval", minutes=30, id="cookie_tick")
    sched.start()
    threading.Thread(target=lambda: None, daemon=True).start()


def _stats_snapshot_job():
    try:
        r = _stats_snapshot()
        print(f"[快照] {r['date']}: {r['dirs']} 目录 / {r['files']} 文件 / "
              f"{r['total_size']/1024**4:.2f} TB", flush=True)
    except Exception as e:
        print(f"[快照] 失败: {e}", flush=True)


def _cookie_tick():
    try:
        cookie = api115.load_cookie()
        chk = api115.check_cookie(cookie)
        _cookie_heartbeat(chk["ok"], chk.get("error") or "")
        if not chk["ok"]:
            logbus.pub("系统", f"Cookie 定时体检失败: {chk.get('error')}", lv="error")
    except Exception as e:
        _cookie_heartbeat(False, str(e))
        logbus.pub("系统", f"Cookie 定时体检异常: {e}", lv="error")


_start_scheduler()

# 启动时立即备份一次(基线)
try:
    _backup_now("startup")
except Exception as e:
    print(f"[备份] 启动备份失败: {e}", flush=True)


# ---------- 通用 ----------
def _db():
    """兼容旧接口, 委托给 db.get_conn()"""
    return db.get_conn()


# ---------- 启动时建表(若不存在) ----------
def _ensure_tables():
    """表已在 db.py 中创建, 此函数只做种子数据初始化"""
    con = _db()
    # 标签系统表 + 种子词表
    tags.ensure_tables()
    tags.ensure_seed_tags()
    # 清理 30 天前的搜索历史
    cutoff = int(time.time()) - 30 * 86400
    con.execute("DELETE FROM search_history WHERE ts < ?", (cutoff,))
    con.commit()
    con.close()


_ensure_tables()


# ---------- KV / Cookie 健康记录 ----------
def _kv_get(con, key: str):
    row = con.execute("SELECT value FROM cookie_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _kv_set(con, key: str, value: str):
    con.execute("INSERT OR REPLACE INTO cookie_meta(key,value) VALUES(?,?)", (key, value))


def _cookie_heartbeat(ok: bool, err: str = "") -> dict:
    """记录每次 Cookie 体检结果，返回 ok_since（本次连续正常起点）"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con = _db()
    try:
        if ok:
            if not _kv_get(con, "first_ok_at"):
                _kv_set(con, "first_ok_at", now)
            _kv_set(con, "last_ok_at", now)
        else:
            _kv_set(con, "last_fail_at", now)
            _kv_set(con, "last_fail_msg", (err or "")[:200])
        first_ok = _kv_get(con, "first_ok_at") or now
        last_fail = _kv_get(con, "last_fail_at") or ""
        ok_since = now if (last_fail and last_fail > first_ok) else first_ok
        return {"ok_since": ok_since, "last_fail_at": last_fail or None}
    finally:
        con.commit()
        con.close()


def _stats_snapshot() -> dict:
    """把当日总量写入 stats_history（INSERT OR REPLACE 幂等）"""
    con = _db()
    try:
        row = con.execute("""
            SELECT
              (SELECT count(*) FROM tree_nodes WHERE is_dir=1) AS dirs,
              (SELECT count(*) FROM tree_nodes WHERE is_dir=0) AS files,
              (SELECT coalesce(sum(size),0) FROM tree_nodes WHERE is_dir=0) AS total_size,
              (SELECT count(*) FROM scan_state WHERE status='done') AS scanned
        """).fetchone()
        today = datetime.now().strftime("%Y-%m-%d")
        con.execute(
            "INSERT OR REPLACE INTO stats_history(date,dirs,files,total_size,scanned) VALUES(?,?,?,?,?)",
            (today, row["dirs"], row["files"], row["total_size"], row["scanned"]))
        con.commit()
        return {"date": today, "dirs": row["dirs"], "files": row["files"],
                "total_size": row["total_size"], "scanned": row["scanned"]}
    finally:
        con.close()


# 启动时补当日快照（幂等，让趋势图从第一天就有数据）
try:
    _stats_snapshot()
except Exception as e:
    print(f"[快照] 启动快照失败: {e}", flush=True)


@app.exception_handler(Exception)
async def on_err(request, exc):
    return JSONResponse(status_code=500, content={"error": str(exc)[:300]})


# ---------- 实时日志(前端日志窗口增量拉取) ----------
@app.get("/api/logs")
def logs(after: int = 0, src: str = "", limit: int = 500):
    """返回内存日志总线中 seq > after 的新条目; src 可选按来源过滤"""
    items, latest = logbus.since(after, src)
    if limit and len(items) > limit:
        items = items[-limit:]
    return {"latest": latest, "entries": items}


# ---------- 健康检查 ----------
@app.get("/api/health")
def health():
    try:
        cookie = api115.load_cookie()
        chk = api115.check_cookie(cookie)
        meta = _cookie_heartbeat(chk["ok"], chk.get("error") or "")
        return {"cookie_ok": chk["ok"], "cookie_error": chk["error"], **meta}
    except Exception as e:
        try:
            meta = _cookie_heartbeat(False, str(e))
        except Exception:
            meta = {"ok_since": None, "last_fail_at": None}
        return {"cookie_ok": False, "cookie_error": str(e)[:200], **meta}


# ---------- Cookie 管理 ----------
class CookieReq(BaseModel):
    cookie: str = ""


@app.get("/api/cookie")
def cookie_get():
    """获取当前 Cookie 状态(不含明文内容, 防止局域网泄露)"""
    try:
        cookie = api115.load_cookie()
        chk = api115.check_cookie(cookie)
        return {
            "cookie_ok": chk["ok"], "cookie_error": chk.get("error", ""),
            "cookie_length": len(cookie),
            "has_uid": "UID" in cookie or "uid" in cookie,
            "has_session": "USERSESSIONID" in cookie or "usersessionid" in cookie,
        }
    except Exception as e:
        return {
            "cookie_ok": False, "cookie_error": str(e)[:200],
            "cookie_length": 0,
            "has_uid": False, "has_session": False,
        }


@app.post("/api/cookie")
def cookie_update(req: CookieReq):
    """更新 Cookie: 格式校验 → 写入文件 → 立即测试 → 记录心跳"""
    c = (req.cookie or "").strip()
    if not c:
        raise HTTPException(400, "Cookie 不能为空")
    if "UID" not in c and "uid" not in c:
        raise HTTPException(400, "Cookie 格式无效，缺少 UID 字段")
    if "USERSESSIONID" not in c and "usersessionid" not in c:
        raise HTTPException(400, "Cookie 格式无效，缺少 USERSESSIONID 字段")
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(c)
        chk = api115.check_cookie(c)
        _cookie_heartbeat(chk["ok"], chk.get("error") or "")
        logbus.pub("系统", f"Cookie 已更新(长度 {len(c)}, {'正常' if chk['ok'] else '异常'})",
                   lv="ok" if chk["ok"] else "warn")
        return {"success": True, "cookie_ok": chk["ok"], "cookie_error": chk.get("error", "")}
    except HTTPException:
        raise
    except Exception as e:
        logbus.pub("系统", f"Cookie 更新失败: {e}", lv="error")
        raise HTTPException(500, f"保存失败: {e}")


@app.post("/api/cookie/test")
def cookie_test(req: CookieReq):
    """测试 Cookie 有效性(不写入文件)"""
    c = (req.cookie or "").strip()
    if not c:
        raise HTTPException(400, "Cookie 不能为空")
    try:
        chk = api115.check_cookie(c)
        return {"cookie_ok": chk["ok"], "cookie_error": chk.get("error", ""),
                "message": "Cookie 有效" if chk["ok"] else "Cookie 无效: " + (chk.get("error") or "")}
    except Exception as e:
        return {"cookie_ok": False, "cookie_error": str(e)[:200],
                "message": f"测试失败: {e}"}


# ---------- 概览仪表盘 ----------
_dashboard_cache = {"ts": 0.0, "data": None}
_GB = 1024 ** 3


def _build_dashboard() -> dict:
    con = _db()
    con.row_factory = sqlite3.Row
    try:
        stats = dedup.get_stats()

        # Top 10 最大文件（含完整路径，沿 pid 链反查）
        name_cache: dict = {}
        pid_cache: dict = {}

        def get_name(cid):
            if cid not in name_cache:
                r = con.execute("SELECT name FROM tree_nodes WHERE cid=?", (cid,)).fetchone()
                name_cache[cid] = r["name"] if r else "?"
            return name_cache[cid]

        def get_pid(cid):
            if cid not in pid_cache:
                r = con.execute("SELECT pid FROM tree_nodes WHERE cid=?", (cid,)).fetchone()
                pid_cache[cid] = r["pid"] if r else None
            return pid_cache[cid]

        top_files = []
        for r in con.execute(
                "SELECT cid,pid,root,name,size FROM tree_nodes WHERE is_dir=0 ORDER BY size DESC LIMIT 10"):
            parts, cur, depth = [r["name"]], r["pid"], 0
            while cur and cur != r["cid"] and depth < 12:
                depth += 1
                if cur == r["root"]:
                    parts.append(get_name(cur))
                    break
                n = get_name(cur)
                if n == "?":
                    break
                parts.append(n)
                nxt = get_pid(cur)
                if not nxt or nxt == cur:
                    break
                cur = nxt
            top_files.append({"cid": r["cid"], "pid": r["pid"], "name": r["name"],
                              "size": r["size"] or 0, "path": " / ".join(reversed(parts))})

        # Top 10 一级目录（按 root 聚合其子树文件大小）
        top_roots = [dict(r) for r in con.execute("""
            SELECT t.root AS cid, coalesce(t.name, agg.root) AS name, agg.sz, agg.fc
            FROM (SELECT root, sum(size) AS sz, count(*) AS fc
                  FROM tree_nodes WHERE is_dir=0 GROUP BY root) agg
            LEFT JOIN tree_nodes t ON t.cid = agg.root
            ORDER BY agg.sz DESC LIMIT 10""")]

        # 文件大小分布桶
        b = con.execute("""
            SELECT
              sum(CASE WHEN size <        :g        THEN 1 ELSE 0 END) AS lt1gb,
              sum(CASE WHEN size >= :g  AND size <  :g10  THEN 1 ELSE 0 END) AS gb1_10,
              sum(CASE WHEN size >= :g10 AND size < :g100 THEN 1 ELSE 0 END) AS gb10_100,
              sum(CASE WHEN size >= :g100 THEN 1 ELSE 0 END) AS gb100p
            FROM tree_nodes WHERE is_dir=0""",
            {"g": _GB, "g10": 10 * _GB, "g100": 100 * _GB}).fetchone()
        buckets = {k: (b[k] or 0) for k in ("lt1gb", "gb1_10", "gb10_100", "gb100p")}

        # 近 30 天总量趋势（每日快照）
        trend = [dict(r) for r in con.execute(
            "SELECT date,dirs,files,total_size,scanned FROM stats_history ORDER BY date DESC LIMIT 30")][::-1]

        # 近 7 天每日扫描目录数 + 最近任务
        scan_daily = [dict(r) for r in con.execute("""
            SELECT substr(scanned_at,1,10) AS d, count(*) AS n FROM scan_state
            WHERE status='done' AND scanned_at >= date('now','-6 days')
            GROUP BY d ORDER BY d""")]
        recent_jobs = [dict(r) for r in con.execute(
            "SELECT id,target_name,status,total,done_count,started_at,finished_at"
            " FROM scan_jobs ORDER BY id DESC LIMIT 5")]
    finally:
        con.close()

    # 转存：管理器任务表 + 脚本时代 transfer_state.json
    con = _db()
    try:
        t = con.execute("""SELECT count(*) AS n, coalesce(sum(processed),0) AS processed,
            coalesce(sum(n_success),0) AS s, coalesce(sum(n_repeat),0) AS r,
            coalesce(sum(n_expired),0) AS e, coalesce(sum(n_failed),0) AS f
            FROM transfer_tasks""").fetchone()
        transfer_db = {"tasks": t["n"], "processed": t["processed"], "success": t["s"],
                       "repeat": t["r"], "expired": t["e"], "failed": t["f"]}
    finally:
        con.close()
    transfer_script = {"total": 0, "success": 0, "repeat": 0, "expired": 0, "failed": 0}
    try:
        state_path = os.path.join(BASE_DIR, "transfer_state.json")
        if os.path.exists(state_path):
            from collections import Counter
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            c = Counter(v.get("status") for v in state.values())
            transfer_script = {"total": len(state), "success": c.get("success", 0),
                               "repeat": c.get("repeat", 0), "expired": c.get("expired", 0),
                               "failed": c.get("failed", 0)}
    except Exception:
        pass

    # 查重概况
    dup_groups = dedup.get_duplicates()
    exact_groups, reclaim, extra = 0, 0, 0
    for g in dup_groups["groups"]:
        if g["exact"]:
            exact_groups += 1
        for cluster in g["exact"]:
            if len(cluster) < 2:
                continue
            for m in sorted(cluster, key=lambda x: -(x["sz"] or 0))[1:]:
                reclaim += m["sz"] or 0
                extra += 1

    return {
        "stats": stats,
        "top_files": top_files,
        "top_roots": top_roots,
        "buckets": buckets,
        "trend": trend,
        "scan_daily": scan_daily,
        "recent_jobs": recent_jobs,
        "transfer_db": transfer_db,
        "transfer_script": transfer_script,
        "dup": {"groups": len(dup_groups["groups"]), "exact_groups": exact_groups,
                "reclaim": reclaim, "extra_copies": extra},
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/dashboard")
def dashboard():
    now = time.time()
    if _dashboard_cache["data"] is not None and now - _dashboard_cache["ts"] < 60:
        return _dashboard_cache["data"]
    data = _build_dashboard()
    _dashboard_cache["data"] = data
    _dashboard_cache["ts"] = now
    return data


# ---------- 网盘根目录(实时) ----------
@app.get("/api/roots")
def roots():
    cookie = api115.load_cookie()
    items = api115.list_children_paged("0", cookie)
    con = _db()
    for it in items:
        if it["is_dir"]:
            # 一级子目录的扫描完成比例 -> none/partial/done
            r = con.execute("""
                SELECT
                  (SELECT count(*) FROM tree_nodes WHERE pid=? AND is_dir=1 AND cid<>pid) AS kids,
                  (SELECT count(*) FROM scan_state s JOIN tree_nodes t
                    ON s.cid=t.cid WHERE t.pid=? AND t.is_dir=1 AND t.cid<>t.pid
                    AND s.status='done') AS done_kids,
                  (SELECT count(*) FROM tree_nodes WHERE root=?) AS nodes
                """, (it["cid"], it["cid"], it["cid"])).fetchone()
            kids, done_kids = r["kids"], r["done_kids"]
            if kids > 0 and done_kids >= kids:
                it["scan_state"] = "done"
            elif done_kids > 0:
                it["scan_state"] = "partial"
            else:
                it["scan_state"] = "none"
            it["node_count"] = max(r["nodes"], kids)
    con.close()
    # 一级目录的标签(行内角标用)
    tmap = tags.tags_for_cids([i["cid"] for i in items if i["is_dir"]])
    for i in items:
        if i["is_dir"]:
            i["tags"] = tmap.get(i["cid"], [])
    return {"items": [i for i in items if i["is_dir"]]}


# ---------- 树浏览(本地库, 懒加载) ----------
@app.get("/api/tree/{cid}")
def tree(cid: str, sort: str = "time_desc"):
    con = _db()
    # 排序映射: time_desc/time_asc/name_asc/name_desc
    sort_map = {
        "time_desc": "is_dir DESC, cid DESC",
        "time_asc": "is_dir DESC, cid ASC",
        "name_asc": "is_dir DESC, name ASC",
        "name_desc": "is_dir DESC, name DESC",
    }
    order = sort_map.get(sort, "is_dir DESC, cid DESC")
    rows = con.execute(
        f"SELECT cid,pid,name,is_dir,size FROM tree_nodes WHERE pid=? AND cid<>pid"
        f" ORDER BY {order}", (cid,)).fetchall()
    self_row = con.execute("SELECT cid,name FROM tree_nodes WHERE cid=?", (cid,)).fetchone()
    # 子目录是否还有孩子(用于前端显示展开箭头)
    kids_map = {}
    for r in rows:
        if r["is_dir"]:
            c = con.execute("SELECT count(*) c FROM tree_nodes WHERE pid=?", (r["cid"],)).fetchone()["c"]
            kids_map[r["cid"]] = c
    con.close()
    # 子节点标签角标(按 500 分块)
    tmap = tags.tags_for_cids([r["cid"] for r in rows]) if rows else {}
    return {
        "self": {"cid": cid, "name": self_row["name"] if self_row else cid},
        "children": [{
            "cid": r["cid"], "pid": r["pid"], "name": r["name"], "is_dir": r["is_dir"],
            "size": r["size"], "child_count": kids_map.get(r["cid"], 0),
            "tags": tmap.get(r["cid"], []),
        } for r in rows],
    }


@app.get("/api/node/{cid}")
def node_info(cid: str):
    """单个节点的本地库信息（目录选择器用来判断"是否已入库/已扫描"）"""
    con = _db()
    try:
        row = con.execute(
            "SELECT cid,pid,root,name,is_dir FROM tree_nodes WHERE cid=?", (cid,)).fetchone()
        st = con.execute(
            "SELECT status,scanned_at FROM scan_state WHERE cid=?", (cid,)).fetchone()
        kids = con.execute(
            "SELECT count(*) c FROM tree_nodes WHERE pid=? AND cid<>pid", (cid,)).fetchone()["c"]
    finally:
        con.close()
    if not row:
        return {"exists": False, "cid": cid, "name": None, "is_dir": None,
                "scan_state": None, "scanned_at": None, "child_count": 0}
    return {"exists": True, "cid": row["cid"], "name": row["name"],
            "is_dir": bool(row["is_dir"]), "pid": row["pid"], "root": row["root"],
            "scan_state": st["status"] if st else None,
            "scanned_at": st["scanned_at"] if st else None,
            "child_count": kids}


# ---------- 实时浏览(未扫描目录直接看云端) ----------
@app.get("/api/live/{cid}")
def live(cid: str, sort: str = "time_desc"):
    cookie = api115.load_cookie()
    # 排序映射
    sort_map = {
        "time_desc": ("file_time", False),
        "time_asc": ("file_time", True),
        "name_asc": ("file_name", True),
        "name_desc": ("file_name", False),
    }
    sort_by, asc = sort_map.get(sort, ("file_time", False))
    # 获取全部子项并排序
    items = api115.list_children_paged(cid, cookie, sort_by=sort_by, asc=asc)
    return {"children": items}


# ---------- 搜索 ----------
@app.get("/api/search")
def search(q: str, type: str = "all", root: str = "",
           min_size: int = 0, max_size: int = 0, limit: int = 300):
    """搜索已扫描的目录/文件，返回完整路径
    type: all|file|dir
    root: 限定 root cid（用 /api/roots 数据填充）
    min_size/max_size: 字节；0=不限（仅文件有效）"""
    q = (q or "").strip()
    if not q:
        return {"rows": []}
    con = _db()
    con.row_factory = sqlite3.Row
    where = ["name LIKE ?"]
    args = [f"%{q}%"]
    if type == "file":
        where.append("is_dir = 0")
    elif type == "dir":
        where.append("is_dir = 1")
    if root:
        where.append("root = ?")
        args.append(root)
    if min_size and min_size > 0:
        where.append("size >= ?")
        args.append(int(min_size))
    if max_size and max_size > 0:
        where.append("size <= ?")
        args.append(int(max_size))
    rows = con.execute(
        f"SELECT cid,pid,root,name,is_dir,size FROM tree_nodes WHERE {' AND '.join(where)}"
        f" ORDER BY is_dir DESC, size DESC, name LIMIT ?", (*args, limit)).fetchall()
    # 拼完整路径：走 pid 链直到 root
    name_cache: dict = {}
    pid_cache: dict = {}

    def get_name(cid):
        if cid in name_cache:
            return name_cache[cid]
        r = con.execute("SELECT name FROM tree_nodes WHERE cid=?",
                        (cid,)).fetchone()
        name_cache[cid] = r["name"] if r else "?"
        return name_cache[cid]

    def get_pid(cid):
        if cid in pid_cache:
            return pid_cache[cid]
        r = con.execute("SELECT pid FROM tree_nodes WHERE cid=?",
                        (cid,)).fetchone()
        pid_cache[cid] = r["pid"] if r else None
        return pid_cache[cid]

    out = []
    for r in rows:
        cid = r["cid"]
        path_parts = [r["name"]]
        cur = r["pid"]
        depth = 0
        while cur and cur != cid and depth < 12:
            depth += 1
            if cur == r["root"]:
                path_parts.append(get_name(cur))
                break
            n = get_name(cur)
            if n == "?":
                break
            path_parts.append(n)
            nxt = get_pid(cur)
            if not nxt or nxt == cur:
                break
            cur = nxt
        path = " / ".join(reversed(path_parts))
        out.append({
            "cid": cid, "pid": r["pid"], "root": r["root"],
            "name": r["name"], "is_dir": bool(r["is_dir"]),
            "size": r["size"] or 0, "path": path,
        })
    con.close()
    return {"rows": out, "total": len(out)}


# ---------- 搜索历史(服务端共享) ----------
@app.get("/api/search/history")
def search_history_list():
    con = _db()
    rows = con.execute(
        "SELECT id, q, scope, n, ts FROM search_history ORDER BY ts DESC LIMIT 100"
    ).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


class SearchHistoryItem(BaseModel):
    q: str
    scope: str = ""
    n: int = 0


@app.post("/api/search/history")
def search_history_add(it: SearchHistoryItem):
    q = (it.q or "").strip()
    if not q:
        return {"ok": False}
    con = _db()
    now = int(time.time())
    scope = it.scope or ""
    # 同 q+scope 去重:更新 ts + 累加 n
    ex = con.execute(
        "SELECT id, n FROM search_history WHERE q=? AND scope=?",
        (q, scope)).fetchone()
    if ex:
        con.execute(
            "UPDATE search_history SET ts=?, n=? WHERE id=?",
            (now, ex["n"] + max(1, it.n), ex["id"]))
    else:
        con.execute(
            "INSERT INTO search_history (q, scope, n, ts) VALUES (?,?,?,?)",
            (q, scope, max(1, it.n), now))
    # 顺手清 30 天前
    cutoff = now - 30 * 86400
    con.execute("DELETE FROM search_history WHERE ts < ?", (cutoff,))
    con.commit()
    con.close()
    return {"ok": True}


@app.delete("/api/search/history")
def search_history_clear():
    con = _db()
    con.execute("DELETE FROM search_history")
    con.commit()
    con.close()
    return {"ok": True}


# ---------- 统计 / 查重 ----------
@app.get("/api/stats")
def stats():
    return dedup.get_stats()


@app.get("/api/dups")
def dups(scope: Optional[str] = None):
    return dedup.get_duplicates(scope_root=scope)


# ---------- 扫描任务 ----------
class ScanReq(BaseModel):
    cid: str
    name: str
    rescan: bool = False


@app.post("/api/scan/start")
def scan_start(req: ScanReq):
    jid = scan_mgr.submit(req.cid, req.name, req.rescan)
    logbus.pub("扫描", f"收到扫描请求:「{req.name}」→ 任务 #{jid}"
               f"{'(强制重扫)' if req.rescan else ''}", lv="ok")
    return {"job_id": jid}


@app.get("/api/scan/jobs")
def scan_jobs(limit: int = 50):
    con = scanner.get_conn()
    rows = con.execute(
        "SELECT * FROM scan_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"jobs": [dict(r) for r in rows]}


@app.post("/api/scan/stop/{jid}")
def scan_stop(jid: int):
    ok = scan_mgr.stop(jid)
    if ok:
        logbus.pub("扫描", f"任务 #{jid} 收到停止请求", lv="warn")
    return {"stopped": ok}


@app.post("/api/scan/resume/{jid}")
def scan_resume(jid: int):
    """继续(重试)失败/已停止/已暂停的任务: 重新入队, 已扫目录自动跳过"""
    con = scanner.get_conn()
    try:
        row = con.execute("SELECT status FROM scan_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] not in ("error", "stopped", "paused"):
            raise HTTPException(400, f"当前状态 [{row['status']}] 无需继续")
        con.execute(
            "UPDATE scan_jobs SET status='queued', err=NULL, finished_at=NULL, retry_count=0 WHERE id=?",
            (jid,))
        con.commit()
    finally:
        con.close()
    logbus.pub("扫描", f"任务 #{jid} 手动继续(重新入队, 从断点续扫)", lv="ok")
    return {"resumed": jid}


@app.post("/api/scan/pause/{jid}")
def scan_pause(jid: int):
    """暂停正在运行的任务: 设置状态为 paused, 利用断点续扫恢复"""
    # 先通知扫描线程停止（在更新数据库之前，否则stop方法会因状态已变而返回False）
    scan_mgr.stop(jid)
    con = scanner.get_conn()
    try:
        row = con.execute("SELECT status FROM scan_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] not in ("running", "queued", "retry_wait"):
            raise HTTPException(400, f"当前状态 [{row['status']}] 无法暂停")
        con.execute(
            "UPDATE scan_jobs SET status='paused', finished_at=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), jid))
        con.commit()
    finally:
        con.close()
    logbus.pub("扫描", f"任务 #{jid} 已暂停(可随时继续)", lv="ok")
    return {"paused": jid}


@app.delete("/api/scan/jobs/{jid}")
def scan_delete(jid: int):
    """删除任务记录(仅限已结束状态; 运行中的任务需先停止/暂停)"""
    con = scanner.get_conn()
    try:
        row = con.execute("SELECT status, target_name FROM scan_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row["status"] in ("running", "queued", "retry_wait"):
            raise HTTPException(400, f"任务正在 [{row['status']}] 状态，请先停止或暂停再删除")
        con.execute("DELETE FROM scan_jobs WHERE id=?", (jid,))
        con.commit()
    finally:
        con.close()
    logbus.pub("扫描", f"任务 #{jid}「{row['target_name']}」记录已删除", lv="warn")
    return {"deleted": jid}


# ---------- AI 分类建议 ----------
@app.get("/api/ai/config")
def ai_config_get():
    cfg = ai_worker.load_config()
    key = cfg.get("key", "")
    masked = ""
    if key:
        masked = (key[:6] + "****" + key[-4:]) if len(key) >= 12 else "已设置"
    tmdb_key = cfg.get("tmdb_api_key", "")
    tmdb_masked = ""
    if tmdb_key:
        tmdb_masked = (tmdb_key[:6] + "****" + tmdb_key[-4:]) if len(tmdb_key) >= 12 else "已设置"
    return {"base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "key_masked": masked, "key_set": bool(key),
            "tmdb_key_masked": tmdb_masked, "tmdb_key_set": bool(tmdb_key),
            "batch_size": cfg.get("batch_size", 10),
            "taxonomy": ai_worker.get_taxonomy(), "prompt": ai_worker.get_prompt(),
            "ready": ai_worker.config_ready()}


class AiConfigReq(BaseModel):
    base_url: str = ""
    model: str = ""
    key: str = ""   # 留空 = 不修改已保存的 Key
    prompt: str = ""  # 系统提示词(留空=不修改)
    batch_size: int = 0  # 每块条数(0=不修改)
    tmdb_api_key: str = ""  # TMDB API Key(留空=不修改)


@app.post("/api/ai/config")
def ai_config_post(req: AiConfigReq):
    cfg = ai_worker.load_config()
    if req.base_url.strip():
        cfg["base_url"] = req.base_url.strip()
    if req.model.strip():
        cfg["model"] = req.model.strip()
    if req.key.strip():
        cfg["key"] = req.key.strip()
    if req.batch_size > 0:
        cfg["batch_size"] = req.batch_size
    if req.tmdb_api_key.strip():
        cfg["tmdb_api_key"] = req.tmdb_api_key.strip()
    ai_worker.save_config(cfg)
    if req.prompt.strip():
        ai_worker.set_prompt(req.prompt)
    return {"ok": True, "ready": ai_worker.config_ready()}


@app.post("/api/ai/config/test")
def ai_config_test():
    return ai_worker.test_connection()


class AiAnalyzeReq(BaseModel):
    scope_cid: str
    scope_name: str = ""
    limit: int = 0   # 试跑条数, 0=全量


@app.post("/api/ai/analyze")
def ai_analyze(req: AiAnalyzeReq):
    if not req.scope_cid:
        raise HTTPException(400, "缺少范围目录")
    if not ai_worker.config_ready():
        raise HTTPException(400, "请先在设置卡填写并保存 AI 配置")
    bid = ai_mgr.create_batch(req.scope_cid, req.scope_name, req.limit)
    logbus.pub("AI", f"分析批次 #{bid} 提交: 范围「{req.scope_name or req.scope_cid}」"
               f"({'试跑 ' + str(req.limit) + ' 条' if req.limit else '全量'})", lv="ok")
    return {"batch_id": bid}


@app.get("/api/ai/batches")
def ai_batches(limit: int = 20):
    con = ai_worker.get_conn()
    try:
        rows = con.execute("SELECT * FROM ai_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return {"batches": [dict(r) for r in rows]}
    finally:
        con.close()


@app.delete("/api/ai/batches/{bid}")
def ai_delete_batch(bid: int):
    con = ai_worker.get_conn()
    try:
        row = con.execute("SELECT status FROM ai_batches WHERE id=?", (bid,)).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        if row["status"] == "running":
            raise HTTPException(400, "不能删除运行中的批次，请先停止")
        # 删除该批次的建议（仅 pending 状态，已通过的保留）
        cur = con.execute("DELETE FROM ai_suggestions WHERE batch_id=? AND status='pending'", (bid,))
        deleted_sug = cur.rowcount
        # 删除批次记录
        con.execute("DELETE FROM ai_batches WHERE id=?", (bid,))
        con.commit()
        logbus.pub("AI", f"已删除批次 #{bid}（释放 {deleted_sug} 条待审建议）", lv="ok")
        return {"deleted": True, "suggestions_removed": deleted_sug}
    finally:
        con.close()


@app.post("/api/ai/stop/{bid}")
def ai_stop(bid: int):
    ok = ai_mgr.stop(bid)
    if ok:
        logbus.pub("AI", f"批次 #{bid} 收到停止请求", lv="warn")
    return {"stopped": ok}


@app.get("/api/ai/suggestions")
def ai_suggestions(status: str = "pending", category: str = "", min_conf: float = 0,
                   batch_id: int = 0, limit: int = 500):
    con = _db()
    try:
        where, args = [], []
        if batch_id:
            where.append("batch_id=?")
            args.append(batch_id)
        if status and status != "all":
            where.append("status=?")
            args.append(status)
        if category:
            where.append("category=?")
            args.append(category)
        if min_conf and min_conf > 0:
            where.append("confidence>=?")
            args.append(min_conf)
        q = "SELECT * FROM ai_suggestions"
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY confidence DESC, name LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in con.execute(q, args)]
        by_status = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, count(*) n FROM ai_suggestions GROUP BY status")}
        by_category = [dict(r) for r in con.execute(
            "SELECT category, count(*) n, sum(status='pending') pending"
            " FROM ai_suggestions GROUP BY category ORDER BY n DESC")]
        return {"rows": rows, "by_status": by_status, "by_category": by_category,
                "taxonomy": ai_worker.get_taxonomy()}
    finally:
        con.close()


class AiStatusReq(BaseModel):
    cids: list
    status: str          # approved / rejected / pending
    category: str = ""   # 可选: 同时改判分类


@app.post("/api/ai/suggestions/status")
def ai_suggestions_status(req: AiStatusReq):
    if req.status not in ("approved", "rejected", "pending", "moved"):
        raise HTTPException(400, "非法状态")
    if not req.cids:
        raise HTTPException(400, "cids 为空")
    con = _db()
    try:
        # 预取现有建议的类型/分辨率/字幕/国家/画质/音频(标签桥接用; 改判时以 req.category 为准)
        sug_map = {}
        for i in range(0, len(req.cids), 500):
            chunk = [str(c) for c in req.cids[i:i + 500]]
            qm = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT cid,category,"
                    f"COALESCE(resolution,attribute,format,'') AS res,"
                    f"COALESCE(subtitle,'') AS sub,"
                    f"COALESCE(country,'') AS country,COALESCE(quality,'') AS quality,"
                    f"COALESCE(audio,'') AS audio FROM ai_suggestions WHERE cid IN ({qm})", chunk):
                sug_map[r["cid"]] = (r["category"], r["res"] or "", r["sub"] or "",
                                     r["country"] or "", r["quality"] or "", r["audio"] or "")
        n = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for i in range(0, len(req.cids), 500):
            chunk = [str(c) for c in req.cids[i:i + 500]]
            qm = ",".join("?" * len(chunk))
            sets, args = "status=?, updated_at=?", [req.status, now]
            if req.category:
                sets += ", category=?"
                args.append(req.category)
            args += chunk
            cur = con.execute(f"UPDATE ai_suggestions SET {sets} WHERE cid IN ({qm})", args)
            n += cur.rowcount
        con.commit()
    finally:
        con.close()
    # 标签桥接: 通过=落 标签; 驳回/移回待审=摘掉 AI 标签; moved 不动(标签随 cid 自动跟随)
    try:
        if req.status == "approved":
            tagged = 0
            for cid in req.cids:
                cat, res, sub, country, quality, audio = sug_map.get(str(cid), ("", "", "", "", "", ""))
                tagged += tags.assign_ai_suggestion(str(cid), req.category or cat,
                                                    res, sub, country, quality, audio)
            if tagged:
                logbus.pub("标签", f"AI 审核通过自动落标 {tagged} 条", lv="ok")
        elif req.status in ("rejected", "pending"):
            removed = tags.revoke_ai_suggestion([str(c) for c in req.cids])
            if removed:
                logbus.pub("标签", f"AI 建议撤销, 摘掉标签关联 {removed} 条")
    except Exception as e:
        logbus.pub("标签", f"AI 建议标签桥接失败(状态本身已更新): {e}", lv="warn")
    return {"updated": n}


class AiDeleteReq(BaseModel):
    status: str = ""   # 空=全部, rejected/moved/approved/pending
    batch_id: int = 0  # 可选: 只清指定批次

@app.post("/api/ai/suggestions/delete")
def ai_suggestions_delete(req: AiDeleteReq):
    """清除 AI 建议(按状态/批次), 释放 cid 允许重新分析"""
    con = _db()
    try:
        conditions, args = [], []
        if req.status:
            conditions.append("status=?")
            args.append(req.status)
        if req.batch_id:
            conditions.append("batch_id=?")
            args.append(req.batch_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        # 先摘掉相关标签关联
        cids = [r["cid"] for r in con.execute(f"SELECT cid FROM ai_suggestions{where}", args).fetchall()]
        if cids:
            for i in range(0, len(cids), 500):
                chunk = cids[i:i+500]
                tags.revoke_ai_suggestion(chunk)
        cur = con.execute(f"DELETE FROM ai_suggestions{where}", args)
        con.commit()
        logbus.pub("AI", f"清除建议: 删除 {cur.rowcount} 条"
                   + (f" (状态={req.status})" if req.status else "")
                   + (f" (批次=#{req.batch_id})" if req.batch_id else ""), lv="ok")
        return {"deleted": cur.rowcount}
    finally:
        con.close()


class AiPlanReq(BaseModel):
    cids: list
    parent_cid: str
    parent_name: str = ""
    disc_separate: bool = True   # 原盘载体内容单独归入 蓝光原盘/{类型} 两级子目录


@app.post("/api/ai/plan")
def ai_plan(req: AiPlanReq):
    """按已通过(approved)的建议生成移动计划:
    每个分类在父目录下 mkdir(幂等), 返回 cid->to_cid 映射; 不执行移动
    cids 为空 = 取全部已通过的建议;
    disc_separate=True 时原盘载体内容归入 蓝光原盘/{类型} 两级子目录"""
    cookie = api115.load_cookie()
    con = _db()
    items, skipped_moved = [], 0
    try:
        base_q = ("SELECT s.cid, s.name, s.category, s.status,"
                  " COALESCE(s.attribute,s.format,'') AS attr, s.country, t.pid"
                  " FROM ai_suggestions s LEFT JOIN tree_nodes t ON t.cid=s.cid")
        if req.cids:
            items_q = []
            for i in range(0, len(req.cids), 500):
                chunk = [str(c) for c in req.cids[i:i + 500]]
                qm = ",".join("?" * len(chunk))
                items_q.extend(dict(r) for r in con.execute(base_q + f" WHERE s.cid IN ({qm})", chunk))
        else:
            items_q = [dict(r) for r in con.execute(base_q + " WHERE s.status='approved'")]
        for r in items_q:
            if r["status"] == "moved":
                skipped_moved += 1
                continue
            if r["status"] != "approved":
                continue
            items.append({"cid": r["cid"], "name": r["name"],
                          "pid": r["pid"] or "", "category": r["category"],
                          "format": r["attr"] or "", "country": r["country"] or ""})
    finally:
        con.close()
    if not items:
        raise HTTPException(400, "没有已通过(approved)的建议可生成计划")
    groups = ai_worker.build_plan_groups(items, req.disc_separate)
    mkdirs, plan = [], []
    cache = {}
    con2 = _db()
    for parts, lst in groups:
        cur, acc = req.parent_cid, []
        for part in parts:
            acc.append(part)
            key = (cur, part)
            if key not in cache:
                cache[key] = api115.mkdir(part, cur, cookie)
                # 同步新建的目录到本地数据库
                new_cid = cache[key]
                if new_cid:
                    # 查询根目录
                    root_row = con2.execute("SELECT root FROM tree_nodes WHERE cid=?", (cur,)).fetchone()
                    root = (root_row[0] if root_row else None) or cur
                    # 写入本地数据库（幂等）
                    con2.execute(
                        "INSERT OR IGNORE INTO tree_nodes(cid,pid,root,name,is_dir,size) VALUES(?,?,?,?,?,?)",
                        (new_cid, cur, root, part, 1, 0))
            cur = cache[key]
        path = "/".join(acc)
        mkdirs.append({"path": path, "cid": cur})
        for it in lst:
            plan.append({"cid": it["cid"], "pid": it["pid"],
                         "name": it["name"], "to_cid": cur, "to_name": path})
    con2.commit()
    con2.close()
    logbus.pub("AI", f"移动计划: {len(plan)} 项 / {len(mkdirs)} 个分类目录 "
              f"(父目录 {req.parent_name or req.parent_cid}, 原盘单独归档={req.disc_separate})")
    return {"plan": plan, "mkdirs": mkdirs, "skipped_moved": skipped_moved}


# ---------- 移动历史 ----------
class MoveHistoryReq(BaseModel):
    batch_id: int = 0
    items: list = []   # [{cid, name, from_path, to_path, status, err}]

@app.post("/api/ai/move-history")
def move_history_record(req: MoveHistoryReq):
    """记录移动操作明细"""
    con = _db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for it in req.items:
            con.execute(
                "INSERT INTO ai_move_history(batch_id,cid,name,from_path,to_path,status,err,moved_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (req.batch_id, it.get("cid", ""), it.get("name", ""),
                 it.get("from_path", ""), it.get("to_path", ""),
                 it.get("status", "ok"), it.get("err", ""), now))
        con.commit()
        return {"ok": True, "recorded": len(req.items)}
    finally:
        con.close()

@app.get("/api/ai/move-history")
def move_history_list(batch_id: int = 0, limit: int = 200):
    """查询移动历史"""
    con = _db()
    try:
        if batch_id:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM ai_move_history WHERE batch_id=? ORDER BY id DESC LIMIT ?",
                (batch_id, limit)).fetchall()]
        else:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM ai_move_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        return {"rows": rows}
    finally:
        con.close()


@app.delete("/api/ai/move-history")
def move_history_clear():
    """清除所有移动历史"""
    con = _db()
    try:
        count = con.execute("SELECT count(*) FROM ai_move_history").fetchone()[0]
        con.execute("DELETE FROM ai_move_history")
        con.commit()
        return {"deleted": count}
    finally:
        con.close()


# ---------- 标签系统 ----------
@app.get("/api/tags")
def tags_list():
    return {"items": tags.list_tags(), "categories": tags.CATEGORIES}


class TagReq(BaseModel):
    name: str = ""
    category: str = "自定义"
    color: str = ""


class TagApplyReq(BaseModel):
    cids: list
    add: list = []      # 要打上的标签 id
    remove: list = []   # 要摘掉的标签 id
    source: str = "manual"


class TagRulesReq(BaseModel):
    dry_run: bool = False
    clean: bool = False


@app.post("/api/tags/apply")
def tags_apply(req: TagApplyReq):
    """批量打标/摘标; 返回受影响 cids 的最新标签映射供前端原地刷新"""
    if not req.cids:
        raise HTTPException(400, "cids 为空")
    added = removed = 0
    for tid in req.add:
        added += tags.assign(int(tid), req.cids, req.source)
    for tid in req.remove:
        removed += tags.unassign(int(tid), req.cids)
    if added or removed:
        logbus.pub("标签", f"打标 +{added} / 摘标 -{removed} (共 {len(req.cids)} 个节点)", lv="ok")
    return {"added": added, "removed": removed, "tags": tags.tags_for_cids(req.cids)}


@app.post("/api/tags/cleanup")
def tags_cleanup():
    n = tags.cleanup_orphans()
    logbus.pub("标签", f"清理失效标签关联 {n} 条")
    return {"removed": n}


@app.post("/api/tags/clear-all")
def tags_clear_all():
    con = tags.get_conn()
    try:
        cur = con.execute("DELETE FROM node_tags")
        n = cur.rowcount
        con.commit()
    finally:
        con.close()
    logbus.pub("标签", f"一键清零全部标签关联 {n} 条", lv="warn")
    return {"removed": n}


@app.post("/api/tags/rules/run")
def tags_rules_run(req: TagRulesReq):
    return tag_rules.run_rules(clean=req.clean, dry_run=req.dry_run)


class TagRuleSaveReq(BaseModel):
    tag_name: str = ""
    dir_pattern: str = ""
    file_pattern: str = ""
    on_files: bool = True
    enabled: bool = True
    note: str = ""


class TagRuleDenyReq(BaseModel):
    deny_ext: str = ""


@app.get("/api/tags/rules")
def tags_rules_list():
    return tag_rules.list_rules()


@app.put("/api/tags/rules/deny-ext")
def tags_rules_deny(req: TagRuleDenyReq):
    tag_rules.set_deny_ext(req.deny_ext)
    logbus.pub("标签", f"规则文件级排除扩展名更新: {req.deny_ext.strip() or '(清空)'}")
    return {"ok": True}


@app.post("/api/tags/rules")
def tags_rules_add(req: TagRuleSaveReq):
    try:
        rid = tag_rules.add_rule(req.tag_name, req.dir_pattern, req.file_pattern,
                                 req.on_files, req.enabled, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logbus.pub("标签", f"新增规则「{req.tag_name.strip()}」")
    return {"id": rid}


@app.put("/api/tags/rules/{rule_id}")
def tags_rules_update(rule_id: int, req: TagRuleSaveReq):
    try:
        tag_rules.update_rule(rule_id, req.tag_name, req.dir_pattern, req.file_pattern,
                              req.on_files, req.enabled, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logbus.pub("标签", f"修改规则 #{rule_id}「{req.tag_name.strip()}」")
    return {"ok": True}


@app.delete("/api/tags/rules/{rule_id}")
def tags_rules_delete(rule_id: int):
    try:
        name = tag_rules.delete_rule(rule_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    logbus.pub("标签", f"删除规则「{name}」，已按它打上的关联待下次「先清理」重打时移除", lv="warn")
    return {"ok": True}


@app.post("/api/tags")
def tags_create(req: TagReq):
    tid = tags.create_tag(req.name, req.category, req.color)
    logbus.pub("标签", f"新建标签「{req.name.strip()}」({req.category})")
    return {"id": tid}


@app.get("/api/tags/{tag_id}/nodes")
def tags_nodes(tag_id: int, limit: int = 500):
    rows, total = tags.nodes_for_tag(tag_id, limit)
    return {"rows": rows, "total": total}


@app.post("/api/tags/{tag_id}")
def tags_update(tag_id: int, req: TagReq):
    tags.update_tag(tag_id, req.name or None, req.category, req.color)
    return {"ok": True}


@app.delete("/api/tags/{tag_id}")
def tags_delete(tag_id: int):
    name = tags.delete_tag(tag_id)
    logbus.pub("标签", f"删除标签「{name}」及全部关联", lv="warn")
    return {"ok": True}


@app.get("/api/nodes/tags")
def nodes_tags(cids: str = ""):
    ids = [c.strip() for c in (cids or "").split(",") if c.strip()]
    return {"map": tags.tags_for_cids(ids)}


# ---------- 定时扫描 ----------
class SchedReq(BaseModel):
    cid: str
    name: str
    hour: int
    minute: int


@app.post("/api/schedules")
def sched_add(req: SchedReq):
    if not (0 <= req.hour <= 23 and 0 <= req.minute <= 59):
        raise HTTPException(400, "时间不合法")
    con = scanner.get_conn()
    cur = con.execute(
        "INSERT INTO schedules(target_cid,target_name,hour,minute,enabled,created_at)"
        " VALUES(?,?,?,?,1,?)",
        (req.cid, req.name, req.hour, req.minute,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    sid = cur.lastrowid
    con.close()
    return {"id": sid}


@app.get("/api/schedules")
def sched_list():
    con = scanner.get_conn()
    rows = con.execute("SELECT * FROM schedules ORDER BY id").fetchall()
    con.close()
    return {"schedules": [dict(r) for r in rows]}


@app.delete("/api/schedules/{sid}")
def sched_del(sid: int):
    con = scanner.get_conn()
    con.execute("DELETE FROM schedules WHERE id=?", (sid,))
    con.commit()
    con.close()
    return {"ok": True}


# ---------- 转存: 解析(预览) ----------
class ParseTextReq(BaseModel):
    text: str


@app.post("/api/transfer/parse/text")
def parse_text(req: ParseTextReq):
    items = transfer_worker.parse_text(req.text)
    return {"items": items}


@app.post("/api/transfer/parse/file")
async def parse_file(file: UploadFile = File(...)):
    content = await file.read()
    items = transfer_worker.parse_upload(file.filename, content)
    return {"filename": file.filename, "items": items}


# ---------- 转存: 目标目录 ----------
class MkdirReq(BaseModel):
    name: str
    pid: str = "0"


@app.post("/api/fs/mkdir")
def mkdir(req: MkdirReq):
    cookie = api115.load_cookie()
    cid = api115.mkdir(req.name, req.pid, cookie)
    # 同步写入本地树（root 取父目录的 root，若无则用 cid 自身）
    con = _db()
    parent = con.execute("SELECT root FROM tree_nodes WHERE cid=?", (req.pid,)).fetchone()
    root = parent["root"] if parent else cid
    con.execute(
        "INSERT OR REPLACE INTO tree_nodes(cid,pid,root,name,is_dir,size) VALUES(?,?,?,?,?,?)",
        (cid, req.pid, root, req.name, 1, 0))
    con.commit()
    # 验证写入
    check = con.execute("SELECT cid FROM tree_nodes WHERE cid=? AND pid=?", (cid, req.pid)).fetchone()
    con.close()
    logbus.pub("文件", f"新建目录「{req.name}」(cid={cid}, pid={req.pid}, root={root}, "
               f"写入{'成功' if check else '失败'})", lv="ok" if check else "error")
    return {"cid": cid}


# ---------- 转存: 任务 ----------
class TaskReq(BaseModel):
    name: str = ""
    items: list  # [{share_code, receive_code, title}]
    target_cid: str
    target_name: str


@app.post("/api/transfer/task")
def create_task(req: TaskReq):
    if not req.items:
        raise HTTPException(400, "链接列表为空")
    tid = transfer_mgr.create_task(req.name, req.items, req.target_cid, req.target_name)
    logbus.pub("转存", f"转存任务 #{tid} 创建:「{req.name or '未命名'}」共 {len(req.items)} 条"
               f" → 「{req.target_name}」", lv="ok")
    return {"task_id": tid}


@app.get("/api/transfer/tasks")
def task_list(limit: int = 50):
    con = transfer_worker.get_conn()
    rows = con.execute(
        "SELECT * FROM transfer_tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return {"tasks": [dict(r) for r in rows]}


@app.get("/api/transfer/tasks/{tid}")
def task_detail(tid: int, status: Optional[str] = None):
    con = transfer_worker.get_conn()
    t = con.execute("SELECT * FROM transfer_tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        con.close()
        raise HTTPException(404, "任务不存在")
    q = "SELECT * FROM transfer_items WHERE task_id=?"
    args = [tid]
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY id LIMIT 500"
    items = con.execute(q, args).fetchall()
    con.close()
    return {"task": dict(t), "items": [dict(r) for r in items]}


@app.post("/api/transfer/stop/{tid}")
def task_stop(tid: int):
    ok = transfer_mgr.stop(tid)
    if ok:
        logbus.pub("转存", f"任务 #{tid} 收到停止请求", lv="warn")
    return {"stopped": ok}


# ---------- 文件管理 ----------
class DeleteReq(BaseModel):
    cid: str          # 要删的文件/目录 id
    pid: str          # 所在父目录 id
    confirm: bool = False


@app.post("/api/fs/delete")
def fs_delete(req: DeleteReq):
    if not req.confirm:
        raise HTTPException(400, "需要前端二次确认(confirm=true)")
    cookie = api115.load_cookie()
    res = api115.delete_to_recycle([req.cid], req.pid, cookie)
    if not res["ok"]:
        logbus.pub("文件", f"删除 {req.cid} 失败: {res.get('error', '')}", lv="error")
        raise HTTPException(500, res.get("error", "删除失败"))
    # 本地库同步移除该子树
    con = _db()
    _remove_subtree(con, req.cid)
    con.commit()
    con.close()
    logbus.pub("文件", f"已删除 1 项并移入回收站 (cid={req.cid})", lv="warn")
    return {"ok": True, "note": "已移入 115 回收站(30天内可恢复)"}


class DeleteBatchReq(BaseModel):
    items: list       # [{cid, pid, name?}]
    confirm: bool = False


@app.post("/api/fs/delete/batch")
def fs_delete_batch(req: DeleteBatchReq):
    """批量删除(进回收站): 按父目录分组串行调用 115 接口, 组间限速, 本地库同步"""
    if not req.confirm:
        raise HTTPException(400, "需要前端二次确认(confirm=true)")
    if not req.items:
        raise HTTPException(400, "删除列表为空")
    cookie = api115.load_cookie()
    by_pid = {}
    for it in req.items:
        by_pid.setdefault(str(it["pid"]), []).append(str(it["cid"]))
    results, con = [], _db()
    try:
        for pid, fids in by_pid.items():
            res = api115.delete_to_recycle(fids, pid, cookie)
            for fid in fids:
                if res["ok"]:
                    _remove_subtree(con, fid)
                    results.append({"cid": fid, "ok": True})
                else:
                    results.append({"cid": fid, "ok": False, "error": res.get("error", "删除失败")})
            con.commit()
            if len(by_pid) > 1:
                time.sleep(random.uniform(2, 4))
    finally:
        con.close()
    ok_n = sum(1 for r in results if r["ok"])
    logbus.pub("文件", f"批量删除: 成功 {ok_n} / 失败 {len(results) - ok_n}(移入回收站)",
               lv="ok" if ok_n == len(results) else "warn")
    return {"ok": ok_n == len(results), "success": ok_n, "fail": len(results) - ok_n,
            "results": results, "note": "已移入 115 回收站(30天内可恢复)"}


def _remove_subtree(con, cid: str):
    """删除 cid 及其全部后代(含文件), 并清理关联的 scan_state 和 node_tags"""
    # 收集 cid 及其所有后代(包括文件和目录)
    subs = _collect_descendants(con, cid)
    # 按 500 分块防超 SQLite 变量上限
    for i in range(0, len(subs), 500):
        chunk = subs[i:i + 500]
        qm = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM tree_nodes WHERE cid IN ({qm})", chunk)
        con.execute(f"DELETE FROM scan_state WHERE cid IN ({qm})", chunk)
        con.execute(f"DELETE FROM node_tags WHERE cid IN ({qm})", chunk)


def _collect_descendants(con, cid: str) -> list:
    """BFS 收集 cid 及其全部后代 cid(不含锚点行); IN 分块防超 SQLite 999 变量上限"""
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


class MoveReq(BaseModel):
    items: list       # [{cid, pid, name?}]
    to_cid: str       # 目标目录
    to_name: str = ""
    confirm: bool = False


@app.post("/api/fs/move")
def fs_move(req: MoveReq):
    """批量移动: 按父目录分组调 115 接口, 云端成功后本地树同步更新(A方案, 不丢数据)"""
    if not req.confirm:
        raise HTTPException(400, "需要前端二次确认(confirm=true)")
    if not req.items:
        raise HTTPException(400, "移动列表为空")
    cookie = api115.load_cookie()
    con = _db()
    try:
        # 防呆: 目标目录不能是自己或自己的后代
        moving = [str(it["cid"]) for it in req.items]
        for cid in moving:
            if cid == req.to_cid or req.to_cid in _collect_descendants(con, cid):
                raise HTTPException(400, "不能把目录移动到它自己或它的子目录里")
        by_pid = {}
        for it in req.items:
            by_pid.setdefault(str(it["pid"]), []).append(str(it["cid"]))
        results = []
        for pid, fids in by_pid.items():
            res = api115.move_files(fids, pid, req.to_cid, cookie)
            # 记录逐 fid 结果(只看当前分组, 不跨组累积)
            for fid in res.get("success", []):
                results.append({"cid": fid, "ok": True})
            for item in res.get("failed", []):
                results.append({"cid": item["fid"], "ok": False, "error": item["error"]})
            # 本地树同步(只处理当前分组确认成功的 fid)
            for fid in res.get("success", []):
                _move_subtree_local(con, fid, req.to_cid)
            con.commit()
            if len(by_pid) > 1:
                time.sleep(random.uniform(2, 4))
    finally:
        con.close()
    ok_n = sum(1 for r in results if r["ok"])
    logbus.pub("文件", f"批量移动 {len(req.items)} 项 → 「{req.to_name or req.to_cid}」: "
               f"成功 {ok_n} / 失败 {len(results) - ok_n}",
               lv="ok" if ok_n == len(results) else "warn")
    return {"ok": ok_n == len(results), "success": ok_n, "fail": len(results) - ok_n,
            "results": results}


def _move_subtree_local(con, fid: str, to_cid: str):
    """本地树同步: A方案自动更新, 移动后数据不丢
    - 若 fid 本身是扫描根(root==cid): 子树 root 全指向它自己, 内部自洽, 只改 pid
    - 否则: BFS 收集后代, 整棵子树 root 改挂到目标目录的 root(目标未入库则挂 fid 自身)"""
    row = con.execute("SELECT cid,root FROM tree_nodes WHERE cid=?", (fid,)).fetchone()
    if not row:
        return  # 未入库(未扫描), 云端已移动即可
    if row["root"] == fid:
        con.execute("UPDATE tree_nodes SET pid=? WHERE cid=?", (to_cid, fid))
        return
    trow = con.execute("SELECT root FROM tree_nodes WHERE cid=?", (to_cid,)).fetchone()
    new_root = trow["root"] if trow else fid
    subs = _collect_descendants(con, fid)
    for i in range(0, len(subs), 500):
        chunk = subs[i:i + 500]
        qm = ",".join("?" * len(chunk))
        con.execute(f"UPDATE tree_nodes SET root=? WHERE cid IN ({qm})", [new_root] + chunk)
    con.execute("UPDATE tree_nodes SET pid=? WHERE cid=?", (to_cid, fid))


@app.get("/api/fs/download/{cid}")
def fs_download(cid: str):
    """实时取下载直链: 需先在云端列出父目录找到 pick_code"""
    cookie = api115.load_cookie()
    con = _db()
    row = con.execute("SELECT pid,name FROM tree_nodes WHERE cid=?", (cid,)).fetchone()
    con.close()
    pid = row["pid"] if row else None
    if not pid:
        raise HTTPException(404, "本地库中找不到该文件")
    for it in api115.list_children_paged(pid, cookie):
        if it["cid"] == cid and it["pick_code"]:
            return api115.get_download_url(it["pick_code"], cookie)
    raise HTTPException(404, "云端未找到该文件的下载信息")


# ---------- 备份管理 ----------
@app.get("/api/backups")
def api_backups_list():
    return {"items": _list_backups(), "dir": BACKUP_DIR}


@app.post("/api/backup")
def api_backup_now():
    """手动触发一次备份"""
    try:
        r = _backup_now("manual")
        return {"ok": True, **r}
    except Exception as e:
        raise HTTPException(500, f"备份失败: {e}")


@app.delete("/api/backups/{name}")
def api_backup_delete(name: str):
    full = os.path.join(BACKUP_DIR, name)
    if not os.path.isfile(full) or not name.startswith("115_tree-") or not name.endswith(".db"):
        raise HTTPException(400, "非法文件名")
    try:
        os.remove(full)
        return {"ok": True, "deleted": name}
    except Exception as e:
        raise HTTPException(500, f"删除失败: {e}")


class BackupRestoreReq(BaseModel):
    confirm: bool = False


@app.post("/api/backups/restore/{name}")
def api_backup_restore(name: str, req: BackupRestoreReq):
    """从备份恢复(会先自动备份当前 DB)"""
    if not req.confirm:
        raise HTTPException(400, "需要 confirm=true")
    try:
        return _restore_backup(name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"恢复失败: {e}")


# ---------- 静态前端 ----------
app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    # 局域网可访问：绑定 0.0.0.0（同一 Wi-Fi/网段内的设备均可打开）
    logbus.pub("系统", f"115 管理器启动: http://127.0.0.1:{config.PORT} (局域网设备请用本机 IP 访问)", lv="ok")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
