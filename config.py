# -*- coding: utf-8 -*-
"""
统一配置: 从环境变量 / .env 文件读取, 提供全局默认值
- 所有模块从这里 import 配置, 不再各自硬编码路径
- .env 文件放在项目根目录(与115manager同级), 格式 KEY=VALUE
- 优先级: 环境变量 > .env 文件 > 代码默认值
"""
import os

# ---------- 自动加载 .env ----------
def _load_dotenv():
    """从项目根目录加载 .env 文件(无第三方依赖)"""
    # 向上找: 115manager/config.py -> 115manager/ -> 项目根/
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:  # 环境变量优先, 不覆盖
                os.environ[key] = value

_load_dotenv()

# ---------- 项目路径 ----------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------- 数据库 ----------
TREE_DB = os.environ.get("TREE_DB", os.path.join(PROJECT_DIR, "115_tree.db"))
INV_DB = os.environ.get("INV_DB", os.path.join(PROJECT_DIR, "115_inventory.db"))

# ---------- 凭据 / 配置文件 ----------
COOKIE_FILE = os.environ.get("COOKIE_FILE", os.path.join(PROJECT_DIR, "cookie115.txt"))
AI_CONFIG = os.environ.get("AI_CONFIG", os.path.join(PROJECT_DIR, "ai_config.json"))

# ---------- 服务 ----------
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "0.0.0.0")

# ---------- 备份 ----------
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(PROJECT_DIR, "backups"))
BACKUP_KEEP_DAILY = int(os.environ.get("BACKUP_KEEP_DAILY", "7"))
BACKUP_KEEP_WEEKLY = int(os.environ.get("BACKUP_KEEP_WEEKLY", "4"))

# ---------- 扫描 ----------
SCAN_INTERVAL_MIN = float(os.environ.get("SCAN_INTERVAL_MIN", "3.0"))
SCAN_INTERVAL_MAX = float(os.environ.get("SCAN_INTERVAL_MAX", "5.0"))
SCAN_RETRY_WAIT = int(os.environ.get("SCAN_RETRY_WAIT", "300"))
SCAN_RETRY_MAX = int(os.environ.get("SCAN_RETRY_MAX", "3"))

# ---------- AI ----------
AI_BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "10"))
AI_CALL_DELAY_MIN = float(os.environ.get("AI_CALL_DELAY_MIN", "1.0"))
AI_CALL_DELAY_MAX = float(os.environ.get("AI_CALL_DELAY_MAX", "2.0"))
