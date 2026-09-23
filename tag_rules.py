# -*- coding: utf-8 -*-
"""
规则引擎: 按名字正则给已入库的【目录 + 单体文件】打标签(纯本地/零成本/全库秒级)
- 规则存数据库 tag_rules 表, 网页「标签 → 规则管理」可查看/编辑/停用/新增/删除;
  首次运行自动把种子规则(SEED_RULES)迁移进去, 之后以库为准
- 目录: 资源整理的决策单位, 目录上的标签代表整个子树
- 单体文件: 一些资源本身就是单个文件(整盘 ISO、单文件 Remux/成片), 按文件名打标;
  两道防噪: ①剧集逐集文件(S01/E01/EP1)自动跳过 ②图片/海报/nfo 等非媒体后缀
  按全局排除表(app_settings: tag_rule_file_ext_deny)跳过, 目录不受影响
- 关联写入 source='rule'; 「清理后重打」= 先删全部 rule 关联再打 => 改规则后幂等重跑
- 手动(manual)关联不受 clean 影响; 同一目录同标签已有 manual 关联时 INSERT OR IGNORE 保留手动
- 内容类型(电影/剧集/...)不在这里定, 那是 AI 分类的活; 这里只做名字里明确可判的属性
- 用法:
    python tag_rules.py             # 干跑: 只报告命中数, 不写库
    python tag_rules.py --apply     # 实际写入
    python tag_rules.py --apply --clean
服务端触发: POST /api/tags/rules/run {dry_run, clean}; 规则 CRUD 见 server.py /api/tags/rules*
"""
import re
import sys
import threading
from datetime import datetime

import tags

# 种子规则: (标签名, 目录/通用正则, 文件级专用正则(空=用目录正则), 是否也处理单体文件, 说明)
# 仅 tag_rules 表为空的首启场景迁移一次, 之后网页上删了就真删
# 注意 CJK 与英文字母间没有 \b 词边界, 用 (?<![a-z]) / (?![a-z]) 自定义边界:
#   既不误伤英文单词(如 chase 里的 chs), 又允许紧贴中文(如 "阿凡达4K")
SEED_RULES = [
    # ---- 分辨率 ----
    ("4K",     r"2160p|(?<!\d)4k(?!\d)|(?<![a-z])uhd(?![a-z])|ultra.?hd",
                                                      "", True,  "名字含 2160p/4K/UHD"),
    ("1080P",  r"1080[pi]",                           "", True,  "名字含 1080p/1080i"),
    # ---- 来源/载体 ----
    ("原盘",   r"\.iso\b|bdmv|video_ts|certificate|原盘", r"\.(?:iso|nrg)$", True,
               "ISO/BDMV/原盘特征; 文件级只认 .iso/.nrg 镜像"),
    ("Remux",  r"remux",                              "", True,  "名字含 remux"),
    ("Blu-ray",r"blu.?ray|bdrip|bdremux",             "", True,  "Blu-ray/BDRip 来源"),
    ("Web-DL", r"web-?dl",                            "", True,  "WEB-DL 在线来源"),
    # ---- 视频编码 ----
    ("HEVC",   r"hevc|h\.?265|x\.?265",              "", True,  "H.265/HEVC 编码"),
    ("H.264",  r"h\.?264|x\.?264|(?<![a-z])avc(?![a-z])",
                                                      "", True,  "H.264/AVC 编码"),
    ("10bit",  r"10.?bit",                            "", True,  "10bit 色深"),
    # ---- HDR ----
    ("HDR",    r"(?<![a-z])hdr(?:10\+?)?(?![a-z])",   "", True,  "HDR/HDR10+"),
    ("杜比",   r"dolby|杜比|atmos|全景声",            "", True,  "杜比视界/全景声"),
    # ---- 音频 ----
    ("DTS",    r"(?<![a-z])dts(?:-hd(?:\.ma)?)?(?![a-z])",
                                                      "", True,  "DTS/DTS-HD/DTS-HD MA 音轨"),
    ("TrueHD", r"(?<![a-z])truehd(?![a-z])",          "", True,  "Dolby TrueHD 无损音轨"),
    ("AAC",    r"(?<![a-z])aac(?![a-z])",             "", True,  "AAC 音轨"),
    ("FLAC",   r"(?<![a-z])flac(?![a-z])",            "", True,  "FLAC 无损音轨"),
    # ---- 语言/字幕 ----
    ("国语",   r"国语|普通话|mandarin",               "", True,  "国语/普通话音轨"),
    ("中字",   r"中字|简中|繁中|简体|繁体|(?<![a-z])chs(?![a-z])|(?<![a-z])cht(?![a-z])",
                                                      "", True,  "中文字幕"),
    # ---- 其他 ----
    ("已刮削", r"tmdbid-\d+|tmdb-",                   "", True,  "带 tmdbid 已刮削标记"),
]

# 文件级打标全局排除的扩展名(空格/逗号分隔, 存 app_settings): 海报/剧照/nfo/文档等
# 这些文件的发布名往往继承资源全名(含 4K/国语/HDR 字样), 打上属性标签纯属噪音;
# 字幕(srt/ass)默认不在排除表——它们带「中字」是有意义的信息
DEFAULT_DENY_EXT = "jpg jpeg png webp bmp gif tif tiff nfo pdf xls zip rar html"
DENY_KEY = "tag_rule_file_ext_deny"

# 逐集文件标记(S01/S01E02/E01/EP1): 文件级打标时跳过, 目录级不受影响
EPISODE_RE = re.compile(
    r"(?<![a-z0-9])S\d{1,2}(?:\s?E\d{1,3})?(?![a-z0-9])|(?<![a-z0-9])EP?\d{1,3}(?![a-z0-9])", re.I)

# 结构规则: 子级出现标准碟片结构名 => 给父目录打标(仍在代码里, 属结构特征而非名字规则)
# 目录形态原盘的标准特征是内部有 BDMV/(蓝光)或 VIDEO_TS/(DVD)结构,
# 顶层目录名本身往往干干净净(如 "沙丘2 (2024)"), 名字正则抓不到, 靠结构识别
STRUCT_RULES = [
    ("原盘", re.compile(r"^(?:bdmv|video_ts|certificate)$", re.I),
     "子级含 BDMV/VIDEO_TS/CERTIFICATE 标准碟片结构"),
]

_mig_lock = threading.Lock()
_mig_done = False


# ---------------- 建表迁移 / 设置 ----------------
def _ensure(con):
    """表已由 tags.ensure_tables 建立; 这里负责种子规则迁移 + 排除表默认值(每进程一次)"""
    global _mig_done
    if _mig_done:
        return
    with _mig_lock:
        if _mig_done:
            return
        if con.execute("SELECT COUNT(*) FROM tag_rules").fetchone()[0] == 0:
            for i, (tname, dir_rx, file_rx, on_files, note) in enumerate(SEED_RULES):
                # 行级判重兜底: 与其他进程首启并发时也不会重复插
                ex = con.execute("SELECT 1 FROM tag_rules WHERE tag_name=? AND dir_pattern=?",
                                 (tname, dir_rx)).fetchone()
                if not ex:
                    con.execute(
                        "INSERT INTO tag_rules(tag_name,dir_pattern,file_pattern,on_files,enabled,note,sort)"
                        " VALUES(?,?,?,?,1,?,?)", (tname, dir_rx, file_rx, 1 if on_files else 0, note, i))
        con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)",
                    (DENY_KEY, DEFAULT_DENY_EXT))
        con.commit()
        _mig_done = True


def _deny_set(s: str) -> set:
    out = set()
    for tok in re.split(r"[\s,，;；]+", s or ""):
        tok = tok.strip().lstrip(".").lower()
        if tok:
            out.add(tok)
    return out


def _ext_of(name: str) -> str:
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].strip().lower()
    return ext if 0 < len(ext) <= 12 else ""


def _get_deny(con) -> str:
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (DENY_KEY,)).fetchone()
    return row["value"] if row and row["value"] is not None else DEFAULT_DENY_EXT


# ---------------- 规则 CRUD(网页编辑用) ----------------
def _validate(tag_name, dir_pattern, file_pattern, note):
    tag_name = (tag_name or "").strip()[:30]
    if not tag_name:
        raise ValueError("标签名不能为空")
    dir_pattern = (dir_pattern or "").strip()
    if not dir_pattern:
        raise ValueError("目录/通用正则不能为空")
    file_pattern = (file_pattern or "").strip()
    note = (note or "").strip()[:200]
    try:
        re.compile(dir_pattern)
    except re.error as e:
        raise ValueError(f"目录正则有误: {e}")
    if file_pattern:
        try:
            re.compile(file_pattern)
        except re.error as e:
            raise ValueError(f"文件级正则有误: {e}")
    return tag_name, dir_pattern, file_pattern, note


def list_rules() -> dict:
    con = tags.get_conn()
    try:
        _ensure(con)
        rules = [dict(r) for r in con.execute(
            "SELECT id,tag_name,dir_pattern,file_pattern,on_files,enabled,note,sort"
            " FROM tag_rules ORDER BY sort, id")]
        return {"rules": rules, "deny_ext": _get_deny(con)}
    finally:
        con.close()


def add_rule(tag_name, dir_pattern, file_pattern="", on_files=True, enabled=True, note="") -> int:
    tag_name, dir_pattern, file_pattern, note = _validate(tag_name, dir_pattern, file_pattern, note)
    con = tags.get_conn()
    try:
        _ensure(con)
        sort = con.execute("SELECT COALESCE(MAX(sort),0)+1 FROM tag_rules").fetchone()[0]
        cur = con.execute(
            "INSERT INTO tag_rules(tag_name,dir_pattern,file_pattern,on_files,enabled,note,sort)"
            " VALUES(?,?,?,?,?,?,?)",
            (tag_name, dir_pattern, file_pattern, 1 if on_files else 0,
             1 if enabled else 0, note, sort))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def update_rule(rule_id: int, tag_name, dir_pattern, file_pattern, on_files, enabled, note):
    tag_name, dir_pattern, file_pattern, note = _validate(tag_name, dir_pattern, file_pattern, note)
    con = tags.get_conn()
    try:
        _ensure(con)
        cur = con.execute(
            "UPDATE tag_rules SET tag_name=?,dir_pattern=?,file_pattern=?,on_files=?,enabled=?,note=?"
            " WHERE id=?",
            (tag_name, dir_pattern, file_pattern, 1 if on_files else 0,
             1 if enabled else 0, note, rule_id))
        con.commit()
        if cur.rowcount == 0:
            raise ValueError("规则不存在")
    finally:
        con.close()


def delete_rule(rule_id: int) -> str:
    con = tags.get_conn()
    try:
        _ensure(con)
        row = con.execute("SELECT tag_name FROM tag_rules WHERE id=?", (rule_id,)).fetchone()
        if not row:
            raise ValueError("规则不存在")
        con.execute("DELETE FROM tag_rules WHERE id=?", (rule_id,))
        con.commit()
        return row["tag_name"]
    finally:
        con.close()


def get_deny_ext() -> str:
    con = tags.get_conn()
    try:
        _ensure(con)
        return _get_deny(con)
    finally:
        con.close()


def set_deny_ext(value: str):
    con = tags.get_conn()
    try:
        _ensure(con)
        con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (DENY_KEY, (value or "").strip()))
        con.commit()
    finally:
        con.close()


# ---------------- 打标主流程 ----------------
def run_rules(clean: bool = False, dry_run: bool = True) -> dict:
    tags.ensure_tables()
    con = tags.get_conn()
    try:
        _ensure(con)
        # 启用中的规则(含扫描根锚点行 cid=pid 也可被打标, 节点全量载入)
        rules = [dict(r) for r in con.execute(
            "SELECT * FROM tag_rules WHERE enabled=1 ORDER BY sort, id")]
        deny_ext = _get_deny(con)
        deny = _deny_set(deny_ext)
        compiled, rule_errors = [], []
        for r in rules:
            try:
                compiled.append({**r,
                                 "dir_rx": re.compile(r["dir_pattern"], re.I),
                                 "file_rx": re.compile(r["file_pattern"], re.I) if r["file_pattern"] else None})
            except re.error as e:
                rule_errors.append(f"{r['tag_name']}: {e}")

        rows = [dict(r) for r in con.execute(
            "SELECT cid,pid,name,is_dir FROM tree_nodes")]
        # 文件级: 跳过逐集文件
        n_files_skipped = 0
        scanned = []
        for r in rows:
            if not r["is_dir"] and EPISODE_RE.search(r["name"] or ""):
                n_files_skipped += 1
                continue
            scanned.append(r)
        matched = {rule["tag_name"]: [] for rule in compiled}
        name_of = {r["cid"]: (r["name"] or "") for r in scanned}
        pid_of = {r["cid"]: r["pid"] for r in scanned}
        n_deny_skipped = 0
        for r in scanned:
            nm = r["name"] or ""
            is_dir = r["is_dir"]
            # 文件级防噪③: 图片/海报/nfo 等非媒体后缀整体跳过(名字常继承资源全名)
            if not is_dir and _ext_of(nm) in deny:
                n_deny_skipped += 1
                continue
            for rule in compiled:
                if is_dir:
                    rx = rule["dir_rx"]
                elif not rule["on_files"]:
                    continue
                else:
                    rx = rule["file_rx"] or rule["dir_rx"]   # 文件级专用正则(更严), 没配则用通用
                if rx.search(nm):
                    matched[rule["tag_name"]].append((r["cid"], is_dir))
        # 结构规则: 子项名命中标准碟片结构词 => 父目录打标(目录形态原盘)
        # 对应名字规则被整体停用时结构识别也一并停
        all_dir_cids = {r["cid"] for r in scanned if r["is_dir"]}
        enabled_names = set(matched.keys())
        struct_hits = {}
        for tname, mrx, _desc in STRUCT_RULES:
            if tname not in enabled_names:
                struct_hits[tname] = set()
                continue
            hits = set()
            for r in scanned:
                if r["pid"] and r["pid"] in all_dir_cids and mrx.search(r["name"] or ""):
                    hits.add(r["pid"])
            struct_hits[tname] = hits
            existing = set(matched.get(tname, []))
            for c in hits:
                if (c, True) not in existing:
                    matched.setdefault(tname, []).append((c, True))
                    existing.add((c, True))
        # 文件级去重: 沿 pid 祖先链向上, 任一祖先目录已命中同一标签 => 该文件不重复打
        # (标签挂在资源单位上, 避免原盘目录/BDMV/ 里的 index.bdmv 等结构文件被成批打标)
        files_pruned = {}
        for name, cids in matched.items():
            dir_set = {c for c, is_dir in cids if is_dir}
            keep, dropped = [], 0
            for c, is_dir in cids:
                if not is_dir and dir_set:
                    cur, hops, seen, has_anc = pid_of.get(c), 0, set(), False
                    while cur and cur in pid_of and cur not in seen and hops < 24:
                        seen.add(cur)
                        if cur in dir_set:
                            has_anc = True
                            break
                        cur = pid_of.get(cur)
                        hops += 1
                    if has_anc:
                        dropped += 1
                        continue
                keep.append((c, is_dir))
            matched[name] = keep
            files_pruned[name] = dropped
        report = {
            "dirs": sum(1 for r in scanned if r["is_dir"]),
            "files": sum(1 for r in scanned if not r["is_dir"]),
            "files_skipped": n_files_skipped,
            "files_deny_skipped": n_deny_skipped,
            "deny_ext": " ".join(sorted(deny)),
            "rule_errors": rule_errors,
            "matched": {name: len(cids) for name, cids in matched.items()},
            "matched_files": {name: sum(1 for _, is_dir in cids if not is_dir)
                              for name, cids in matched.items()},
            "files_pruned": files_pruned,
            "struct": {tname: len(hits) for tname, hits in struct_hits.items()},
            "sample": {name: [{"cid": c, "name": name_of.get(c, "")[:40]}
                       for c, _ in cids[:5]] for name, cids in matched.items()},
            "assigned": 0, "cleaned": 0, "dry_run": dry_run,
        }
        if dry_run:
            return report

        if clean:
            cur = con.execute("DELETE FROM node_tags WHERE source='rule'")
            report["cleaned"] = cur.rowcount
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = 0
        for rule in compiled:
            name = rule["tag_name"]
            cids = list(dict.fromkeys(c for c, _ in matched.get(name, [])))
            if not cids:
                continue
            # get_or_create 走的是另一条 SQLite 连接, 而本连接此时可能攥着未提交的写入
            # (上一轮 node_tags 批量插入 / clean 的 DELETE)。SQLite 同一时刻只允许一个
            # 写入方, 不先提交就会撞 "database is locked"(全新安装首次跑规则引擎必现)。
            con.commit()
            tid = tags.get_or_create(name, category="属性")
            before = con.total_changes
            for i in range(0, len(cids), 500):
                chunk = cids[i:i + 500]
                con.executemany(
                    "INSERT OR IGNORE INTO node_tags(cid,tag_id,source,created_at) VALUES(?,?,?,?)",
                    [(c, tid, "rule", now) for c in chunk])
            total += con.total_changes - before
        con.commit()
        report["assigned"] = total
        try:
            import logbus
            logbus.pub("标签", f"规则引擎打标完成: 命中 {sum(report['matched'].values())} 次"
                       f"(其中单体文件 {sum(report['matched_files'].values())} 次), "
                       f"新写关联 {total} 条" + (f"(先清理 {report['cleaned']} 条)" if clean else ""),
                       lv="ok")
        except Exception:
            pass
        return report
    finally:
        con.close()


def _fmt_report(r: dict, rules: list) -> str:
    pruned_total = sum(r.get("files_pruned", {}).values())
    lines = [f"已入库目录 {r['dirs']} + 单体文件 {r['files']}"
             + (f"(跳过逐集文件 {r['files_skipped']})" if r.get("files_skipped") else "")
             + (f"(跳过非媒体文件 {r['files_deny_skipped']}, 排除: {r.get('deny_ext','')})"
                if r.get("files_deny_skipped") else "")
             + (f"(父目录已带同标签而去重 {pruned_total})" if pruned_total else ""),
             "-" * 46]
    for rule in rules:
        name = rule["tag_name"]
        n = r["matched"].get(name, 0)
        nf = r.get("matched_files", {}).get(name, 0)
        ns = r.get("struct", {}).get(name, 0)
        scope = "目录+文件" if rule.get("on_files") else "仅目录"
        detail = f"{n - nf:>6} 目录"
        if ns:
            detail += f"(结构识别 {ns})"
        if rule.get("on_files"):
            detail += f" + {nf:>5} 文件"
        state = "" if rule.get("enabled") else " [停用]"
        lines.append(f"{name:<6} {detail}   {rule.get('note','') or name} [{scope}]{state}")
        for s in r.get("sample", {}).get(name, [])[:3]:
            lines.append(f"        · {s['name']}")
    for tname, _mrx, desc in STRUCT_RULES:
        ns = r.get("struct", {}).get(tname, 0)
        if ns:
            lines.append(f"{'':>6}其中 {tname} 结构识别补命中 {ns} 个目录   {desc}")
    if r.get("rule_errors"):
        lines.append(f"! 有 {len(r['rule_errors'])} 条规则正则编译失败被跳过: "
                     + "; ".join(r["rule_errors"]))
    lines.append("-" * 46)
    if r.get("dry_run"):
        lines.append("干跑结束, 未写库。加 --apply 实际写入; --clean 先清上次规则结果")
    else:
        lines.append(f"写入关联 {r.get('assigned', 0)} 条" +
                     (f"(清理旧规则关联 {r.get('cleaned', 0)} 条)" if r.get("cleaned") else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    apply_ = "--apply" in sys.argv
    rep = run_rules(clean="--clean" in sys.argv, dry_run=not apply_)
    print(_fmt_report(rep, list_rules()["rules"]))
