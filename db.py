# -*- coding: utf-8 -*-
"""
统一数据库层: 连接工厂 + 一次性建表/迁移 + 事务助手
消除 scanner/transfer_worker/ai_worker/tags/server 各自重复的 get_conn() + DDL
"""
import sqlite3
import threading
from contextlib import contextmanager

import config

# 一次性初始化锁
_init_lock = threading.Lock()
_initialized = False


def _init_tables(con: sqlite3.Connection):
    """一次性建表 + 旧库迁移"""
    con.executescript("""
        -- scanner 模块表
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
            status TEXT DEFAULT 'queued',
            rescan INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0, done_count INTEGER DEFAULT 0,
            current_cid TEXT DEFAULT '', current_path TEXT DEFAULT '',
            err TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_cid TEXT NOT NULL, target_name TEXT NOT NULL,
            hour INTEGER NOT NULL, minute INTEGER NOT NULL,
            enabled INTEGER DEFAULT 1, last_run TEXT, created_at TEXT);

        -- transfer_worker 模块表
        CREATE TABLE IF NOT EXISTS transfer_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, target_cid TEXT NOT NULL, target_name TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
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

        -- ai_worker 模块表
        CREATE TABLE IF NOT EXISTS ai_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_cid TEXT NOT NULL, scope_name TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            limit_n INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0, processed INTEGER DEFAULT 0,
            n_rule INTEGER DEFAULT 0, n_calls INTEGER DEFAULT 0,
            tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
            err TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS ai_suggestions (
            cid TEXT PRIMARY KEY,
            name TEXT DEFAULT '', pid TEXT DEFAULT '',
            hint TEXT DEFAULT '',
            category TEXT DEFAULT '', suggested_name TEXT DEFAULT '',
            confidence REAL DEFAULT 0, reason TEXT DEFAULT '',
            source TEXT DEFAULT 'ai',
            status TEXT DEFAULT 'pending',
            batch_id INTEGER,
            updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_ai_sug_status ON ai_suggestions(status, category);
        CREATE TABLE IF NOT EXISTS ai_move_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER,
            cid TEXT, name TEXT DEFAULT '',
            from_path TEXT DEFAULT '', to_path TEXT DEFAULT '',
            status TEXT DEFAULT 'ok',
            err TEXT DEFAULT '',
            moved_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_move_history_batch ON ai_move_history(batch_id);

        -- tags 模块表
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT '自定义',
            color TEXT DEFAULT '',
            deleted INTEGER DEFAULT 0,
            created_at TEXT);
        CREATE TABLE IF NOT EXISTS node_tags (
            cid TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            source TEXT DEFAULT 'manual',
            assigned_at TEXT,
            PRIMARY KEY (cid, tag_id));
        CREATE INDEX IF NOT EXISTS idx_node_tags_tag ON node_tags(tag_id);
        CREATE TABLE IF NOT EXISTS tag_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_name TEXT NOT NULL,
            dir_pattern TEXT NOT NULL,
            file_pattern TEXT DEFAULT '',
            on_files INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1,
            note TEXT DEFAULT '',
            created_at TEXT);
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT);

        -- server 模块表
        CREATE TABLE IF NOT EXISTS search_history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            q       TEXT    NOT NULL,
            scope   TEXT    DEFAULT '',
            n       INTEGER DEFAULT 0,
            ts      INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_search_ts   ON search_history(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_search_qkey ON search_history(q, scope);
        CREATE TABLE IF NOT EXISTS stats_history (
            date       TEXT PRIMARY KEY,
            dirs       INTEGER DEFAULT 0,
            files      INTEGER DEFAULT 0,
            total_size INTEGER DEFAULT 0,
            scanned    INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cookie_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    # 旧库迁移: scan_jobs 补列
    cols = [r[1] for r in con.execute("PRAGMA table_info(scan_jobs)")]
    if "retry_count" not in cols:
        con.execute("ALTER TABLE scan_jobs ADD COLUMN retry_count INTEGER DEFAULT 0")
    if "priority" not in cols:
        con.execute("ALTER TABLE scan_jobs ADD COLUMN priority INTEGER DEFAULT 0")
    # 当前正在扫描的目录(供扫描管理页展示真实位置)
    if "current_cid" not in cols:
        con.execute("ALTER TABLE scan_jobs ADD COLUMN current_cid TEXT DEFAULT ''")
    if "current_path" not in cols:
        con.execute("ALTER TABLE scan_jobs ADD COLUMN current_path TEXT DEFAULT ''")
    # 旧库迁移: ai_suggestions 补列
    cols = [r[1] for r in con.execute("PRAGMA table_info(ai_suggestions)")]
    for col, typ in [
        ("format", "TEXT DEFAULT ''"),
        ("attribute", "TEXT DEFAULT ''"),
        ("resolution", "TEXT DEFAULT ''"),
        ("subtitle", "TEXT DEFAULT ''"),
        ("country", "TEXT DEFAULT ''"),
        ("quality", "TEXT DEFAULT ''"),
        ("audio", "TEXT DEFAULT ''"),
        ("target_path", "TEXT DEFAULT ''"),
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE ai_suggestions ADD COLUMN {col} {typ}")
    # 旧库迁移: tags 补 sort_order 列(标签拖拽排序)
    cols = [r[1] for r in con.execute("PRAGMA table_info(tags)")]
    if "sort_order" not in cols:
        con.execute("ALTER TABLE tags ADD COLUMN sort_order INTEGER DEFAULT 0")
    con.commit()


def get_conn() -> sqlite3.Connection:
    """获取数据库连接(线程安全, 自动初始化)"""
    global _initialized
    con = sqlite3.connect(config.TREE_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    if not _initialized:
        with _init_lock:
            if not _initialized:  # 双检锁
                _init_tables(con)
                _initialized = True
    return con


@contextmanager
def transaction():
    """事务上下文管理器, 自动 commit/rollback"""
    con = get_conn()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def full_path(con, cid, cache=None, sep=" / ", max_depth=60, keep=None) -> str:
    """沿 pid 链向上重建节点的完整路径。

    背景: tree_nodes 只存了 (cid, pid, name)，而界面多处直接拿叶子 name 去拼扫描根，
    导致「我的音乐/专辑名」这种只有两级的假路径 —— 中间层被吞掉。
    这里统一按 pid 链还原真实路径，例如:
        我的音乐 / 音乐合集 / VA - CPO Collection 665CD / [999 530-2] J.C Bach

    根节点判定: pid == cid (扫描目标自身)，作为路径起点。

    cache: 可选的 {cid: Row} 复用字典。批量调用(如仪表盘 30 条)时传同一个 dict，
           把 N+1 次查询压到「每个节点只查一次」。
    keep:  只保留最后 N 段，超出部分用 "…" 前缀，用于日志行/窄列等空间受限处。
    """
    parts, seen, node = [], set(), str(cid)
    while node and node not in seen:
        seen.add(node)
        row = cache.get(node) if isinstance(cache, dict) else None
        if row is None:
            row = con.execute(
                "SELECT name, pid FROM tree_nodes WHERE cid=?", (node,)).fetchone()
            if row is None:
                break
            if isinstance(cache, dict):
                cache[node] = row
        parts.append(row["name"])
        pid = row["pid"]
        if pid == node or len(parts) >= max_depth:   # 到根 / 防御性深度上限
            break
        node = pid
    parts.reverse()
    if not parts:
        return ""
    if keep and len(parts) > keep:
        return "…" + sep + sep.join(parts[-keep:])
    return sep.join(parts)


def kv_get(key: str) -> str | None:
    """读取 KV 配置"""
    con = get_conn()
    try:
        row = con.execute("SELECT value FROM cookie_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        con.close()


def kv_set(key: str, value: str):
    """写入 KV 配置"""
    con = get_conn()
    try:
        con.execute("INSERT OR REPLACE INTO cookie_meta(key,value) VALUES(?,?)", (key, value))
        con.commit()
    finally:
        con.close()