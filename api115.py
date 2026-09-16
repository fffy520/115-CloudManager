# -*- coding: utf-8 -*-
"""
115 网盘 API 统一客户端 (curl_cffi 版)
- 所有请求走 Chrome TLS 指纹, 绕过阿里云 WAF 对 Python 指纹的拦截
- 统一 WAF 检测 + 冷却重试
- 覆盖: 列目录 / 转存(snap+receive) / 删除(回收站) / 下载直链 / 建目录 / 移动
"""
import os
import time

from curl_cffi import requests as creq

import config

TIMEOUT = 25
WAF_COOLDOWN = 600  # WAF 触发后的冷却秒数


def load_cookie() -> str:
    try:
        with open(config.COOKIE_FILE, encoding="utf-8") as f:
            c = f.read().strip()
    except FileNotFoundError:
        # from None: 不把 FileNotFoundError 一起打进堆栈(否则控制台刷两遍 traceback)
        raise RuntimeError("Cookie 未配置: 请在 Cookie 管理中粘贴您的 115 网盘 Cookie") from None
    if not c or ("UID" not in c and "uid" not in c):
        raise RuntimeError("cookie115.txt 无效(缺少 UID), 请重新从浏览器复制")
    return c


def _headers(cookie: str) -> dict:
    return {
        "Cookie": cookie,
        "Referer": "https://115.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://115.com",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    }


class WAFBlocked(Exception):
    """被 WAF 拦截(冷却后仍失败)"""


def _is_waf(resp) -> bool:
    if resp is None:
        return False
    try:
        ct = resp.headers.get("content-type", "")
        if "json" in ct:
            return False
        txt = (resp.text or "")[:2000].lower()
        return "<html" in txt or "aliyun" in txt or "captcha" in txt or "安全验证" in txt
    except Exception:
        return False


def get_json(url: str, cookie: str, retry: int = 2, waf_retry: bool = True) -> dict:
    """GET 并解析 JSON; WAF 触发时冷却一次重试"""
    resp = None
    for attempt in range(retry + 1):
        try:
            resp = creq.get(url, headers=_headers(cookie), impersonate="chrome124", timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as e:
            if resp is not None and _is_waf(resp):
                if waf_retry:
                    time.sleep(WAF_COOLDOWN)
                    return get_json(url, cookie, retry, waf_retry=False)
                raise WAFBlocked("WAF 拦截(冷却后仍失败)")
            if attempt < retry:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def post_json(url: str, data: dict, cookie: str, retry: int = 2, waf_retry: bool = True) -> dict:
    resp = None
    for attempt in range(retry + 1):
        try:
            resp = creq.post(url, headers=_headers(cookie), data=data,
                             impersonate="chrome124", timeout=TIMEOUT)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception:
                    raise RuntimeError("响应不是 JSON")
            raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as e:
            if resp is not None and _is_waf(resp):
                if waf_retry:
                    time.sleep(WAF_COOLDOWN)
                    return post_json(url, data, cookie, retry, waf_retry=False)
                raise WAFBlocked("WAF 拦截(冷却后仍失败)")
            if attempt < retry:
                time.sleep(5 * (attempt + 1))
                continue
            raise


# ---------------- 业务封装 ----------------

def list_children(cid: str, cookie: str, offset: int = 0, limit: int = 1000,
                  sort_by: str = "file_time", asc: bool = False) -> dict:
    """列目录, 返回 {items:[{cid,name,is_dir,size,pick_code}], count, has_more}
    注意: 文件项 cid=父目录, fid=自己; 文件夹项 cid=自己
    sort_by: file_name(文件名), file_time(时间), file_size(大小)
    asc: True升序, False降序"""
    url = (f"https://webapi.115.com/files?aid=1&cid={cid}&o={sort_by}&asc={1 if asc else 0}"
           f"&offset={offset}&limit={limit}&show_dir=1")
    d = get_json(url, cookie)
    if not d.get("state") and "data" not in d:
        raise RuntimeError(f"列目录失败: {d.get('error')} (err={d.get('errno')})")
    data = d.get("data", {})
    lst = data.get("list", []) if isinstance(data, dict) else (data or [])
    items = []
    for it in lst:
        is_dir = 1 if it.get("fc", 1) == 0 else 0
        own = (it.get("cid") if is_dir else it.get("fid")) or it.get("fid")
        if not own:
            continue
        items.append({
            "cid": str(own),
            "pid": str(cid),
            "name": it.get("n") or it.get("name") or "?",
            "is_dir": is_dir,
            "size": it.get("s", 0) or 0,
            "pick_code": it.get("pc", "") or "",
        })
    return {
        "items": items,
        "count": data.get("count", len(items)) if isinstance(data, dict) else len(items),
        "offset": offset,
        "limit": limit,
    }


def list_children_paged(cid: str, cookie: str, sort_by: str = "file_time", asc: bool = False) -> list:
    """分页拉取 cid 下全部子项
    注意: 115 的 count 字段返回的是本页条数而非总数, 不能用它判断是否拉完;
    用「页不满」+「首页 cid 重复(超范围 offset 会回退返回最后一页)」双条件终止,
    并按 cid 去重防止重叠页产生重复"""
    out, offset = [], 0
    seen_first, seen_cids = set(), set()
    while True:
        r = list_children(cid, cookie, offset=offset, limit=1000, sort_by=sort_by, asc=asc)
        items = r["items"]
        if not items:
            break
        if items[0]["cid"] in seen_first:
            break  # offset 超出范围时 115 会回退返回最后一页
        seen_first.add(items[0]["cid"])
        for it in items:
            if it["cid"] not in seen_cids:
                seen_cids.add(it["cid"])
                out.append(it)
        if len(items) < 1000:
            break
        offset += 1000
    return out


def check_cookie(cookie: str) -> dict:
    """校验 cookie 有效性, 顺带返回用户信息"""
    d = get_json("https://webapi.115.com/files?aid=1&cid=0&offset=0&limit=1&show_dir=1", cookie)
    ok = "data" in d
    return {"ok": ok, "error": None if ok else d.get("error", str(d)[:100])}


def probe(cookie: str) -> dict:
    """轻量连通性探测(用于扫描任务失败后的自动重试判断):
    一次请求同时验证 DNS/网络/Cookie; 不触发 WAF 冷却重试, 快速失败"""
    try:
        d = get_json("https://webapi.115.com/files?aid=1&cid=0&offset=0&limit=1&show_dir=1",
                     cookie, retry=0, waf_retry=False)
        ok = "data" in d
        return {"ok": ok, "error": None if ok else str(d.get("error", ""))[:100]}
    except WAFBlocked:
        return {"ok": False, "error": "WAF拦截"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def mkdir(name: str, pid: str, cookie: str) -> str:
    """创建目录, 返回新目录 cid; 已存在则返回已有 cid"""
    res = post_json("https://webapi.115.com/files/add", {"pid": pid, "cname": name}, cookie)
    if res.get("state"):
        d = res.get("data", {})
        cid = d.get("file_id") or d.get("fid") or res.get("file_id")
        if cid:
            return str(cid)
    # 可能已存在 -> 找一遍
    for it in list_children_paged(pid, cookie):
        if it["is_dir"] and it["name"] == name:
            return it["cid"]
    raise RuntimeError(f"创建目录失败: {res.get('error')}")


def delete_to_recycle(fids: list, pid: str, cookie: str) -> dict:
    """删除文件/目录到 115 回收站(30天可恢复)
    fids: [cid or fid], pid: 它们的父目录 cid
    删除后验证: 等待 2 秒后检查目录是否真正消失，防止 115 API 返回成功但实际未删除"""
    data = {"pid": pid}
    for i, fid in enumerate(fids):
        data[f"fid[{i}]"] = fid
    res = post_json("https://webapi.115.com/rb/delete", data, cookie)
    if not res.get("state"):
        return {"ok": False, "error": res.get("error", "删除失败")}

    # 验证删除是否生效：等待后检查文件是否仍然存在
    time.sleep(2)
    for fid in fids:
        try:
            children = list_children_paged(pid, cookie)
            still_exists = any(it["cid"] == fid for it in children)
            if still_exists:
                # 115 API 返回成功但文件仍存在，重试一次
                res2 = post_json("https://webapi.115.com/rb/delete", data, cookie)
                if res2.get("state"):
                    time.sleep(2)
                    children2 = list_children_paged(pid, cookie)
                    still_exists = any(it["cid"] == fid for it in children2)
                    if still_exists:
                        return {"ok": False, "error": "115 服务器未执行删除（API 返回成功但文件仍存在）"}
        except Exception:
            pass  # 验证失败时不阻塞，信任 115 的返回值

    return {"ok": True, "count": len(fids)}


def move_files(fids: list, pid: str, to_cid: str, cookie: str) -> dict:
    """移动文件/目录到目标目录(已实测验证)
    fids: [cid or fid], to_cid: 目标目录 cid
    115 接口参数: pid=目标目录cid, fid[i]=要移动的项 (pid 是目标! 不是源父目录)
    单次上限约 1000 个, 超出自动分批
    返回: {ok: bool, success: [fid,...], failed: [{fid, error},...], count: int}"""
    success, failed = [], []
    for i in range(0, len(fids), 500):
        batch = fids[i:i + 500]
        data = {"pid": to_cid}
        for j, fid in enumerate(batch):
            data[f"fid[{j}]"] = fid
        res = post_json("https://webapi.115.com/files/move", data, cookie)
        if res.get("state"):
            success.extend(batch)
        else:
            err = res.get("error", "移动失败")
            failed.extend([{"fid": fid, "error": err} for fid in batch])
    return {
        "ok": len(failed) == 0,
        "success": success,
        "failed": failed,
        "count": len(success),
    }


def get_download_url(pick_code: str, cookie: str) -> dict:
    """取下载直链
    注意: 115 对超大文件会拒绝并返回 msg="文件大小超出限制，请使用115电脑端下载"(msg_code 50028),
    该提示在 msg 字段而非 error，必须两个都读"""
    d = get_json(f"https://webapi.115.com/files/download?pickcode={pick_code}", cookie)
    if d.get("state") is False:
        raise RuntimeError(d.get("error") or d.get("msg") or "获取下载链接失败")
    url = (d.get("file_url")
           or (d.get("data") or {}).get("url")
           or d.get("url"))
    return {"url": url, "name": d.get("file_name"), "size": d.get("file_size")}


def share_snap(share_code: str, receive_code: str, cookie: str) -> dict:
    """列出分享根目录内容(同时验证分享有效性)"""
    from urllib.parse import quote
    url = (f"https://webapi.115.com/share/snap?share_code={share_code}"
           f"&receive_code={quote(receive_code)}&cid=0&offset=0&limit=1150&get_count=1")
    last = None
    for attempt in range(3):
        d = get_json(url, cookie)
        last = d
        if d.get("state") and (d.get("data") or {}).get("list"):
            return d
        time.sleep(5 + attempt * 5)
    return last or {"state": False, "error": "snap 请求失败"}


def share_receive(share_code: str, receive_code: str, fids: list, target_cid: str, cookie: str) -> dict:
    """转存文件到 target_cid"""
    res = post_json("https://webapi.115.com/share/receive", {
        "share_code": share_code,
        "receive_code": receive_code,
        "file_id": ",".join(fids),
        "cid": target_cid,
    }, cookie)
    return res


def classify_transfer_error(msg: str) -> str:
    """错误信息 -> 状态分类"""
    m = str(msg)
    if any(k in m for k in ("已接收", "无需重复", "已经转存", "存在同名")):
        return "repeat"
    if any(k in m for k in ("取消", "不存在", "违规", "审核", "失效", "过期", "访问码错误", "链接错误")):
        return "expired"
    return "failed"
