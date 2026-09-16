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