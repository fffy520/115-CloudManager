#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试总入口: 逐个文件跑(每个文件有自己的临时库/环境, 互不干扰)。
用法: ./venv/bin/python tests/run.py
全部离线(不碰 115 接口)、全部用临时库(不碰真实 115_tree.db)。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
FILES = [
    "test_schema.py",        # 建表/迁移/种子排序
    "test_parse.py",         # 转存链接与 Excel 解析
    "test_dedup.py",         # 查重统计与精确重复判定
    "test_ai.py",            # AI 解析策略 + subtitle 入库
    "test_stop.py",          # 停止/暂停语义
    "test_transfer_sync.py", # 转存快照差集对账
    "test_restore.py",       # worker 停工/复工 + 备份恢复
]

def main():
    failed = []
    for f in FILES:
        print(f"\n========== {f} ==========")
        r = subprocess.run([PY, os.path.join(HERE, f)])
        if r.returncode != 0:
            failed.append(f)
    print("\n" + "=" * 40)
    if failed:
        print(f"❌ {len(failed)} 个文件有用例失败: {', '.join(failed)}")
        sys.exit(1)
    print(f"✅ 全部通过({len(FILES)} 个测试文件)")

if __name__ == "__main__":
    main()
