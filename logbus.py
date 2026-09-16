# -*- coding: utf-8 -*-
"""
内存日志总线: 扫描/转存/AI/文件操作等后台线程把运行事件发布到这里,
前端通过 GET /api/logs?after=seq 增量拉取, 实现网页端实时滚动日志窗口。
- 环形缓冲(默认 2000 条), 超出自动淘汰最旧; seq 单调递增作为游标
- pub() 默认同时回显到服务端控制台(格式 [来源] 消息), 与原 print 习惯一致;
  原代码已有 print 的位置改用 pub 替代, 避免控制台出现重复行
"""
import sys
import threading
from collections import deque
from datetime import datetime

_LOCK = threading.Lock()
_BUF = deque(maxlen=2000)
_SEQ = 0


def pub(src: str, msg: str, lv: str = "info", echo: bool = True):
    """发布一条日志。lv: info/warn/error/ok; echo=True 时同步打印到控制台"""
    global _SEQ
    t = datetime.now().strftime("%H:%M:%S")
    with _LOCK:
        _SEQ += 1
        _BUF.append({"seq": _SEQ, "t": t, "src": src, "lv": lv, "msg": msg})
    if echo:
        print(f"[{src}] {msg}", flush=True, file=sys.stdout)


def since(seq: int = 0, src: str = ""):
    """返回 seq 之后的新条目(可按来源过滤)及当前最新 seq"""
    with _LOCK:
        items = [dict(e) for e in _BUF if e["seq"] > seq and (not src or e["src"] == src)]
        return items, _SEQ


def latest_seq() -> int:
    with _LOCK:
        return _SEQ
