# -*- coding: utf-8 -*-
"""
配置示例文件
展示如何自定义项目配置
"""

import os

# ==================== 服务配置 ====================

# 服务端口
PORT = 8765

# 服务地址
HOST = "0.0.0.0"

# ==================== 数据库配置 ====================

# 数据库文件路径
TREE_DB = "115_tree.db"

# 库存数据库路径（可选）
INV_DB = "115_inventory.db"

# ==================== 凭据配置 ====================

# Cookie 文件路径
COOKIE_FILE = "cookie115.txt"

# AI 配置文件路径
AI_CONFIG = "ai_config.json"

# ==================== 备份配置 ====================

# 备份目录
BACKUP_DIR = "backups"

# 每日备份数量
BACKUP_KEEP_DAILY = 7

# 每周备份数量
BACKUP_KEEP_WEEKLY = 4

# ==================== 扫描配置 ====================

# 扫描最小间隔（秒）
SCAN_INTERVAL_MIN = 3.0

# 扫描最大间隔（秒）
SCAN_INTERVAL_MAX = 5.0

# 扫描重试等待时间（秒）
SCAN_RETRY_WAIT = 300

# 扫描最大重试次数
SCAN_RETRY_MAX = 3

# ==================== AI 配置 ====================

# AI 批处理大小
AI_BATCH_SIZE = 10

# AI 调用最小延迟（秒）
AI_CALL_DELAY_MIN = 1.0

# AI 调用最大延迟（秒）
AI_CALL_DELAY_MAX = 2.0

# ==================== 自定义配置示例 ====================

# 自定义配置示例
CUSTOM_CONFIG = {
    # 自定义扫描规则
    "scan_rules": {
        "exclude_dirs": [".git", "node_modules", "__pycache__"],
        "exclude_files": [".DS_Store", "Thumbs.db"],
        "max_depth": 10,
    },
    
    # 自定义标签规则
    "tag_rules": {
        "4K": r"(4K|2160P|UHD)",
        "1080P": r"(1080P|FHD)",
        "720P": r"(720P|HD)",
        "原盘": r"(原盘|BluRay|BDMV)",
        "Remux": r"(Remux|REMUX)",
        "Web-DL": r"(Web-DL|WEBDL)",
        "HDTV": r"(HDTV|HDTVRip)",
    },
    
    # 自定义 AI 提示词
    "ai_prompts": {
        "classify": "请分析以下目录名称，判断其资源类型和载体格式。",
        "format": "请以 JSON 格式返回，包含 type 和 format 字段。",
    },
    
    # 自定义通知设置
    "notifications": {
        "enabled": False,
        "email": "",
        "webhook": "",
    },
    
    # 自定义日志设置
    "logging": {
        "level": "INFO",
        "file": "logs/app.log",
        "max_size": 10 * 1024 * 1024,  # 10MB
        "backup_count": 5,
    },
}

# ==================== 环境变量覆盖 ====================
# 以下配置可以通过环境变量覆盖
# 例如: export PORT=8080

def get_config():
    """获取配置，优先使用环境变量"""
    return {
        "port": int(os.environ.get("PORT", PORT)),
        "host": os.environ.get("HOST", HOST),
        "tree_db": os.environ.get("TREE_DB", TREE_DB),
        "inv_db": os.environ.get("INV_DB", INV_DB),
        "cookie_file": os.environ.get("COOKIE_FILE", COOKIE_FILE),
        "ai_config": os.environ.get("AI_CONFIG", AI_CONFIG),
        "backup_dir": os.environ.get("BACKUP_DIR", BACKUP_DIR),
        "backup_keep_daily": int(os.environ.get("BACKUP_KEEP_DAILY", BACKUP_KEEP_DAILY)),
        "backup_keep_weekly": int(os.environ.get("BACKUP_KEEP_WEEKLY", BACKUP_KEEP_WEEKLY)),
        "scan_interval_min": float(os.environ.get("SCAN_INTERVAL_MIN", SCAN_INTERVAL_MIN)),
        "scan_interval_max": float(os.environ.get("SCAN_INTERVAL_MAX", SCAN_INTERVAL_MAX)),
        "scan_retry_wait": int(os.environ.get("SCAN_RETRY_WAIT", SCAN_RETRY_WAIT)),
        "scan_retry_max": int(os.environ.get("SCAN_RETRY_MAX", SCAN_RETRY_MAX)),
        "ai_batch_size": int(os.environ.get("AI_BATCH_SIZE", AI_BATCH_SIZE)),
        "ai_call_delay_min": float(os.environ.get("AI_CALL_DELAY_MIN", AI_CALL_DELAY_MIN)),
        "ai_call_delay_max": float(os.environ.get("AI_CALL_DELAY_MAX", AI_CALL_DELAY_MAX)),
    }

if __name__ == "__main__":
    # 测试配置
    config = get_config()
    print("当前配置:")
    for key, value in config.items():
        print(f"  {key}: {value}")
