# -*- coding: utf-8 -*-
"""
AI 分类建议引擎: 规则预分类 + OpenAI 兼容 API 批量分析 + 后台任务
- 建议落 ai_suggestions(cid 主键); 分析时跳过已有建议的 cid => 断点续跑
- 配置: 项目根 ai_config.json {base_url, model, key} (与 cookie115.txt 同信任级别)
- 分类体系: 固定类别列表(存 cookie_meta kv, 可改), AI 只能从中选
- 任务模式与 transfer_worker 一致: 全局串行线程 + 任务表 + 重启自动续跑
"""
import json
import os
import random
import re
import sqlite3
import threading
import time
import traceback
from datetime import datetime

import config
import db
import logbus
import scanner

def get_batch_size():
    """动态读取批次大小, 从 ai_config.json 读取, 无需重启"""
    try:
        with open(config.AI_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return max(1, int(cfg.get("batch_size", 10)))
    except Exception:
        return 10

CALL_DELAY = (config.AI_CALL_DELAY_MIN, config.AI_CALL_DELAY_MAX)
CALL_RETRY = 2
CALL_TIMEOUT = 120

DEFAULT_TAXONOMY = ["电影", "剧集", "动漫", "纪录片", "综艺",
                    "音乐", "体育", "其他·待定"]
FALLBACK_CATEGORY = "其他·待定"

# 默认系统提示词(用户可在 AI 配置页编辑, 存 cookie_meta)
DEFAULT_PROMPT = """你是网盘影视库整理助手。输入一批网盘条目，每个条目附带上下文:
- name: 条目名
- type: 条目类型("目录"或文件类型: "视频"/"音频"/"字幕"/"图片"/"文件")
- parent: 父目录名
- grandparent: 祖父目录名
- children: (仅目录) 内部子项列表(前20个)
- tmdb_country: TMDB查询到的国家(如有)
- tmdb_genres: TMDB查询到的类型(如有)
- tmdb_year: TMDB查询到的年份(如有)
- tmdb_title: TMDB查询到的标题(如有)

**重要**: 如果条目包含 tmdb_country 字段且非空，**必须优先使用 tmdb_country 作为国家判断结果**，不要自行猜测。

综合分析名字、层级路径、内部结构来判断以下维度(尽量多判断, 不确定的留空):
【类型】(必填, 只能用这些, 一字不差): {{tax_line}}
【分辨率】(编码格式): {{res_line}}
【字幕】(语言/音轨): {{sub_line}}
【国家】(如能判断): {{country_line}}
【画质】(如能判断): {{quality_line}}
【音频】(如能判断): {{audio_line}}

判定要点:
- type 为"视频"的文件通常是影视内容; type 为"音频"的文件通常是音乐
- 父/祖父目录名包含强分类信号(如"美剧"→剧集, "日本"→动漫概率高, "港台"→华语电影)
- 文件名中的 S01E01 标记→剧集或动漫(日本动画/动画IP特征选动漫, 否则剧集)
- iso/bdmv→分辨率选"原盘"; Remux/Web-DL/HDTV/BluRay 按文件名判断
- 字幕判断: 中字/国语/粤语/双语/多音轨 按文件名中的线索
- 国家/地区判断(必须用这些值): {{country_line}}
  · 国语/中字/华语/Mandarin/Cantonese/简繁/双语/DIY国语 → 华语
  · 日语/日文/Japanese/JP/日本 → 日本
  · 韩语/韩文/Korean/KR/KOR/韩国 → 韩国
  · 粤语/港/台/HK/TW/Hong Kong/Taiwan → 港台
  · 印地语/Hindi/Bollywood/印度/IND → 印度
  · 法语/French/FR/法国 → 法国
  · 德语/German/DE/德国 → 德国
  · 西语/Spanish/ES/西班牙/Español → 西班牙
  · 泰语/Thai/TH/泰国 → 泰国
  · 美剧/好莱坞/Hollywood/US/USA/American → 美国
  · 英剧/英式/British/UK/GB → 英国
  · 英语/English/EUR/欧美 → 欧美(无法细分到具体国家时兜底)
- 画质判断: 2160p/4K→4K, 1080p→1080P, 720p→720P; HDR/DV/HLG 按文件名
- 音频判断: DTS-HD/DTS-HD MA/TrueHD/Atmos/DTS/DTS-X/AC3/AAC/FLAC/LPCM
- 仅凭名字无法判断类型时给"其他·待定"并给低置信度
- 不得编造 cid; 每个输入 cid 都必须恰好返回一条

输出严格 JSON: {{"items":[{{"cid":"...","category":"类型","resolution":"分辨率","subtitle":"字幕","country":"国家","quality":"画质","audio":"音频","confidence":0.85,"reason":"不超过20字","suggested_name":""}}]}}"""
DISC_DIR = "蓝光原盘"          # 原盘内容单独归档时的目录名(类型作其子目录)
VALID_FORMATS = ["4K", "1080P", "720P", "480P", "Remux", "Web-DL", "HDTV", "BluRay", "原盘", "Encode"]

# 类型 → 国家/地区子目录映射 (网盘管理系统目录结构)
TYPE_COUNTRY_MAP = {
    "电影":   {"华语": "华语片", "美国": "美国片", "英国": "英国片", "欧美": "欧美片",
               "日本": "日本片", "韩国": "韩国片", "港台": "港台片", "印度": "印度片",
               "法国": "法国片", "德国": "德国片", "西班牙": "西班牙片", "泰国": "泰国片",
               "_default": "其他"},
    "剧集":   {"华语": "国产剧", "美国": "美剧", "英国": "英剧", "欧美": "欧美剧",
               "日本": "日剧", "韩国": "韩剧", "港台": "港台剧", "印度": "印度剧",
               "法国": "法剧", "德国": "德剧", "西班牙": "西语剧", "泰国": "泰剧",
               "_default": "其他"},
    "动漫":   {"日本": "日漫", "华语": "国漫", "美国": "美国动漫", "英国": "英国动漫",
               "欧美": "欧美动漫", "_default": "其他"},
    "音乐":   {"华语": "华语", "美国": "美国", "英国": "英国", "欧美": "欧美",
               "日本": "日本", "韩国": "韩国", "港台": "港台", "印度": "印度",
               "法国": "法国", "德国": "德国", "西班牙": "西班牙", "泰国": "泰国",
               "_default": "其他"},
    "纪录片": {"华语": "华语", "美国": "美国", "英国": "英国", "欧美": "欧美",
               "日本": "日本", "韩国": "韩国", "港台": "港台", "印度": "印度",
               "法国": "法国", "德国": "德国", "西班牙": "西班牙", "泰国": "泰国",
               "_default": "其他"},
    "综艺":   {"华语": "国内", "美国": "国外", "英国": "国外", "欧美": "国外",
               "日本": "国外", "韩国": "国外", "港台": "港台", "印度": "国外",
               "法国": "国外", "德国": "国外", "西班牙": "国外", "泰国": "国外",
               "_default": "其他"},
}
TYPE_DIR_MAP = {
    "电影": "电影", "剧集": "电视剧", "动漫": "动漫", "音乐": "音乐",
    "纪录片": "纪录片", "综艺": "综艺",
    "体育": "体育", "其他·待定": "其他",
}

# 国家/地区识别正则 (规则优先于AI)
COUNTRY_RULES = [
    ("华语", re.compile(r"国语|中字|华语|Chinese|Mandarin|Cantonese|简繁|双语|DIY国语|DIY配音", re.I)),
    ("日本", re.compile(r"日语|日文|Japanese|\bJP\b|BDRip.*JAP|日本", re.I)),
    ("韩国", re.compile(r"韩语|韩文|Korean|\bKR\b|\bKOR\b|韩国", re.I)),
    ("港台", re.compile(r"粤语|港|台|港台|HK|TW|Hong Kong|Taiwan", re.I)),
    ("印度", re.compile(r"印地语|Hindi|Bollywood|印度|\bIND\b", re.I)),
    ("法国", re.compile(r"法语|French|\bFR\b|法国|法兰西", re.I)),
    ("德国", re.compile(r"德语|German|\bDE\b|德国|Deutschland", re.I)),
    ("西班牙", re.compile(r"西语|Spanish|\bES\b|西班牙|Español", re.I)),
    ("泰国", re.compile(r"泰语|Thai|\bTH\b|泰国", re.I)),
    ("美国", re.compile(r"美剧|美语|好莱坞|Hollywood|\bUS\b|\bUSA\b|American", re.I)),
    ("英国", re.compile(r"英剧|英式|British|\bUK\b|\bGB\b", re.I)),
    ("欧美", re.compile(r"英语|英文|English|\bEUR\b|欧美", re.I)),
]

_init_lock = threading.Lock()
_init_done = False

SERIES_RE = re.compile(r"\bS\d{1,2}(?:\s?E\d{1,3})?\b", re.I)
CONCERT_RE = re.compile(r"演唱会|音乐会|concert|unplugged|音乐节|live\s*at|演唱会(?:现场)?", re.I)
DISC_RE = re.compile(r"(?i)\.iso\b|bdmv|video_ts|certificate|原盘")
EP_RE = re.compile(r"\bE\d{1,3}\b", re.I)
# 中文剧集标记: 第1集、第01集、EP01、Vol.01 等
CH_EP_RE = re.compile(r"第\s*\d+\s*集|EP?\s*\d+|Vol\.?\s*\d+|DISC\s*\d+", re.I)

VIDEO_EXT = {'.mkv', '.mp4', '.avi', '.rmvb', '.ts', '.m2ts', '.wmv', '.flv', '.mov', '.mpg', '.mpeg', '.iso'}
AUDIO_EXT = {'.flac', '.mp3', '.wav', '.ape', '.dsf', '.dff', '.aac', '.ogg', '.wma', '.m4a'}
SUBTITLE_EXT = {'.srt', '.ass', '.ssa', '.sub', '.idx', '.sup'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}


def detect_file_type(name: str) -> str:
    """根据文件扩展名返回类型描述，帮助 AI 判断。"""
    low = name.lower()
    dot = low.rfind(".")
    ext = low[dot:] if dot > 0 else ""
    if ext in VIDEO_EXT:
        return "视频"
    if ext in AUDIO_EXT:
        return "音频"
    if ext in SUBTITLE_EXT:
        return "字幕"
    if ext in IMAGE_EXT:
        return "图片"
    return "文件"


# ---------------- 配置 ----------------
def load_config() -> dict:
    try:
        with open(config.AI_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    result = {k: str(cfg.get(k, "") or "").strip() for k in ("base_url", "model", "key")}
    result["batch_size"] = int(cfg.get("batch_size", 10))
    result["tmdb_api_key"] = str(cfg.get("tmdb_api_key", "") or "").strip()
    return result


def save_config(cfg: dict):
    data = {k: str(cfg.get(k, "") or "").strip() for k in ("base_url", "model", "key")}
    data["batch_size"] = int(cfg.get("batch_size", 10))
    data["tmdb_api_key"] = str(cfg.get("tmdb_api_key", "") or "").strip()
    with open(config.AI_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def config_ready() -> bool:
    c = load_config()
    return bool(c["base_url"] and c["model"] and c["key"])


def get_conn() -> sqlite3.Connection:
    """兼容旧接口, 委托给 db.get_conn()"""
    return db.get_conn()


def _kv_get(con, key):
    row = con.execute("SELECT value FROM cookie_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_taxonomy() -> list:
    con = get_conn()
    try:
        raw = _kv_get(con, "ai_taxonomy")
        if raw:
            try:
                lst = json.loads(raw)
                if isinstance(lst, list) and lst:
                    return [str(x) for x in lst]
            except Exception:
                pass
        return list(DEFAULT_TAXONOMY)
    finally:
        con.close()


def set_taxonomy(tax: list):
    con = get_conn()
    try:
        con.execute("INSERT OR REPLACE INTO cookie_meta(key,value) VALUES('ai_taxonomy',?)",
                    (json.dumps(tax, ensure_ascii=False),))
        con.commit()
    finally:
        con.close()


def get_prompt() -> str:
    """获取用户自定义系统提示词, 没有则返回默认"""
    con = get_conn()
    try:
        raw = _kv_get(con, "ai_prompt")
        return raw if raw else DEFAULT_PROMPT
    finally:
        con.close()


def set_prompt(text: str):
    """保存用户自定义系统提示词"""
    con = get_conn()
    try:
        con.execute("INSERT OR REPLACE INTO cookie_meta(key,value) VALUES('ai_prompt',?)",
                    (text.strip(),))
        con.commit()
    finally:
        con.close()


def build_ai_taxonomy(con) -> dict:
    """从标签库动态读取 AI 分类体系。
    返回 {types:[], resolutions:[], subtitles:[], countries:[], qualities:[], audios:[]}
    标签库为空时 fallback 到硬编码默认值。"""
    types = [r["name"] for r in con.execute(
        "SELECT name FROM tags WHERE category='类型' ORDER BY id")]
    resolutions = [r["name"] for r in con.execute(
        "SELECT name FROM tags WHERE category='分辨率' ORDER BY id")]
    subtitles = [r["name"] for r in con.execute(
        "SELECT name FROM tags WHERE category='字幕' ORDER BY id")]
    countries = [r["name"] for r in con.execute(
        "SELECT name FROM tags WHERE category='国家' ORDER BY id")]
    qualities = [r["name"] for r in con.execute(
        "SELECT name FROM tags WHERE category='画质' ORDER BY id")]
    audios = [r["name"] for r in con.execute(
        "SELECT name FROM tags WHERE category='音频' ORDER BY id")]
    if not types:
        types = list(DEFAULT_TAXONOMY)
    if not resolutions:
        resolutions = list(VALID_FORMATS)
    return {"types": types, "resolutions": resolutions, "subtitles": subtitles,
            "countries": countries, "qualities": qualities, "audios": audios}


def _is_classifiable_dir(con, dir_cid: str) -> tuple:
    """判断目录是否为"单体文件夹"（可分类）。
    返回 (is_classifiable, children_list):
      - (True, [子项名列表]) : 单体文件夹（1个文件 / 蓝光结构 / 单视频+辅助文件 / 剧集文件夹）
      - (False, []) : 集合文件夹（多个视频/子目录），跳过不分类"""
    kids = con.execute(
        "SELECT name, is_dir FROM tree_nodes WHERE pid=? AND cid<>? ORDER BY is_dir DESC, name",
        (dir_cid, dir_cid)).fetchall()
    file_kids = [k for k in kids if not k["is_dir"]]
    dir_kids = [k for k in kids if k["is_dir"]]

    # 蓝光结构: 包含 BDMV 或 VIDEO_TS 子目录
    bd_names = {k["name"].upper() for k in dir_kids}
    if "BDMV" in bd_names or "VIDEO_TS" in bd_names:
        child_list = ["📁" + k["name"] if k["is_dir"] else "📄" + k["name"] for k in kids[:20]]
        return True, child_list

    # 只有1个文件，没有子目录 → 单体文件夹
    if len(file_kids) == 1 and len(dir_kids) == 0:
        return True, ["📄" + file_kids[0]["name"]]

    # 单视频 + 辅助文件：只有1个视频文件，其余都是字幕/图片/文本 → 单体文件夹
    video_kids = [k for k in file_kids if _is_video_file(k["name"])]
    if len(video_kids) == 1 and len(dir_kids) == 0:
        other_kids = [k for k in file_kids if not _is_video_file(k["name"])]
        if all(_is_aux_file(k["name"]) for k in other_kids):
            child_list = ["📄" + k["name"] for k in kids[:20]]
            return True, child_list

    # 剧集文件夹: 多个视频文件，80%+ 命中剧集标记，无子目录（或只有字幕/信息子目录）
    video_kids = [k for k in file_kids if _is_video_file(k["name"])]
    if len(video_kids) >= 3:
        ep_count = sum(1 for k in video_kids if _has_episode_marker(k["name"]))
        if ep_count >= len(video_kids) * 0.8:
            child_list = ["📁" + k["name"] if k["is_dir"] else "📄" + k["name"] for k in kids[:20]]
            return True, child_list

    # 正片+花絮/特典文件夹: 多个视频文件，其中一个是正片，其余都是花絮/特典
    if len(video_kids) >= 2:
        bonus_keywords = ["花絮", "特典", "幕后", "记者会", "发布会", "上映会", "首映",
                         "舞台", "挨拶", "試写会", "披露", "制作", "making", "behind",
                         "interview", "press", "premiere", "bonus", "extra"]
        bonus_count = sum(1 for k in video_kids
                         if any(kw in k["name"].lower() for kw in bonus_keywords))
        # 如果花絮文件占多数（>=50%），认为是正片+花絮文件夹
        if bonus_count >= len(video_kids) * 0.5:
            child_list = ["📄" + k["name"] for k in kids[:20]]
            return True, child_list

    # 剧集季文件夹: 只有1个Season子文件夹（如 Season 1、S01、第1季）
    if len(dir_kids) == 1 and len(file_kids) == 0:
        season_name = dir_kids[0]["name"]
        season_patterns = re.compile(r"^(season|s|第)\s*\d+", re.I)
        if season_patterns.search(season_name):
            child_list = ["📁" + season_name]
            return True, child_list

    # 其他情况（多个文件、有子目录、空目录）→ 集合，跳过
    return False, []


def _is_video_file(name: str) -> bool:
    """判断文件是否为视频文件"""
    low = name.lower()
    dot = low.rfind(".")
    ext = low[dot:] if dot > 0 else ""
    return ext in VIDEO_EXT


def _is_aux_file(name: str) -> bool:
    """判断文件是否为辅助文件（字幕/图片/文本/nfo 等），不影响内容分类"""
    low = name.lower()
    dot = low.rfind(".")
    ext = low[dot:] if dot > 0 else ""
    # 字幕、图片、文本、nfo、txt、md 等
    AUX_EXT = SUBTITLE_EXT | IMAGE_EXT | {'.txt', '.md', '.nfo', '.log', '.json', '.xml', '.ini', '.cfg'}
    return ext in AUX_EXT


def _has_episode_marker(name: str) -> bool:
    """判断文件名是否包含剧集标记（S01E01、E01、第X集等）"""
    return bool(SERIES_RE.search(name) or EP_RE.search(name) or CH_EP_RE.search(name))


def collect_items_with_context(con, scope_cid: str, _depth: int = 0, _max_depth: int = 3) -> list:
    """收集 scope_cid 下可分类的条目（智能展开集合文件夹）。
    分析:
      - 单体文件夹（1个文件 / 蓝光结构 / 单视频+辅助文件）→ 作为条目
      - 剧集文件夹（多个视频带剧集标记）→ 作为条目
      - 集合文件夹（多个视频/子目录）→ 跳过，递归收集内部子项
      - 散落的文件 → 直接纳入
    返回 [{cid, name, is_dir, size, parent_name, grandparent_name, children:[str]}]"""
    # 防止递归过深
    if _depth >= _max_depth:
        return []

    # 获取 scope_cid 的名字和父目录名（用于上下文）
    scope_row = con.execute("SELECT name, pid FROM tree_nodes WHERE cid=?", (scope_cid,)).fetchone()
    scope_name = scope_row["name"] if scope_row else ""
    parent_name = ""
    grandparent_name = ""
    if scope_row and scope_row["pid"]:
        p_row = con.execute("SELECT name, pid FROM tree_nodes WHERE cid=?", (scope_row["pid"],)).fetchone()
        if p_row:
            parent_name = p_row["name"]
            if p_row["pid"]:
                gp_row = con.execute("SELECT name FROM tree_nodes WHERE cid=?", (p_row["pid"],)).fetchone()
                if gp_row:
                    grandparent_name = gp_row["name"]

    # 获取 scope_cid 的直接子项
    direct_children = con.execute(
        "SELECT cid, pid, name, is_dir, size FROM tree_nodes WHERE pid=? AND cid<>? ORDER BY is_dir DESC, name",
        (scope_cid, scope_cid)).fetchall()

    result = []
    for r in direct_children:
        if not r["is_dir"]:
            # 文件 → 直接纳入
            result.append({
                "cid": r["cid"],
                "name": r["name"],
                "is_dir": False,
                "size": r["size"] or 0,
                "parent_name": scope_name,
                "grandparent_name": parent_name,
                "children": [],
            })
        else:
            # 目录 → 判断是单体还是集合
            is_classifiable, children = _is_classifiable_dir(con, r["cid"])
            if is_classifiable:
                # 单体文件夹（1个文件 或 蓝光结构）→ 作为条目分析
                result.append({
                    "cid": r["cid"],
                    "name": r["name"],
                    "is_dir": True,
                    "size": 0,
                    "parent_name": scope_name,
                    "grandparent_name": parent_name,
                    "children": children,
                })
            else:
                # 集合文件夹 → 跳过，递归收集内部子项
                nested = collect_items_with_context(con, r["cid"], _depth=_depth + 1, _max_depth=_max_depth)
                result.extend(nested)
    return result


def _get_dir_children(con, dir_cid: str) -> list:
    """获取目录的子项列表（前20个），用于AI判断上下文"""
    kids = con.execute(
        "SELECT name, is_dir FROM tree_nodes WHERE pid=? AND cid<>? ORDER BY is_dir DESC, name LIMIT 20",
        (dir_cid, dir_cid)).fetchall()
    return ["📁" + k["name"] if k["is_dir"] else "📄" + k["name"] for k in kids]


def build_system_prompt(taxonomy: dict) -> str:
    """动态生成 SYSTEM_PROMPT: 读取用户自定义提示词, 替换分类体系占位符。"""
    tax_line = "、".join(taxonomy["types"])
    res_line = "、".join(taxonomy["resolutions"]) if taxonomy["resolutions"] else "无"
    sub_line = "、".join(taxonomy["subtitles"]) if taxonomy["subtitles"] else "无"
    country_line = "、".join(taxonomy["countries"]) if taxonomy["countries"] else "无"
    quality_line = "、".join(taxonomy["qualities"]) if taxonomy["qualities"] else "无"
    audio_line = "、".join(taxonomy["audios"]) if taxonomy["audios"] else "无"
    prompt = get_prompt()
    return prompt.replace("{{tax_line}}", tax_line) \
                 .replace("{{res_line}}", res_line) \
                 .replace("{{sub_line}}", sub_line) \
                 .replace("{{country_line}}", country_line) \
                 .replace("{{quality_line}}", quality_line) \
                 .replace("{{audio_line}}", audio_line)


# ---------------- 规则预分类 ----------------
def rule_hint(name: str, meta: dict):
    """返回 (final_category, hint, fmt, country) 四个值:
    - fmt: 载体形态('原盘'或''), 与类型互相独立——演唱会/电影/纪录片都可能是原盘
    - final: 仅当内容类型可从名字可信推断时才定案(演唱会);
      ISO/BDMV 只是载体, 不据此定案「蓝光原盘」, 类型仍交 AI 按内容判断
    - hint: 提供给 AI 的线索
    - country: 规则识别的国家/地区"""
    low = name or ""
    fmt = "原盘" if DISC_RE.search(low) else ""
    country = ""
    hints = []
    # 国家/地区规则识别
    for c_name, c_re in COUNTRY_RULES:
        if c_re.search(low):
            country = c_name
            hints.append(f"国家/地区={c_name}(规则识别)")
            break
    if fmt:
        hints.append("载体为原盘(BDMV/ISO), 类型仍按内容判断")
    if CONCERT_RE.search(low):
        return "音乐", ";".join(hints), fmt, country
    if SERIES_RE.search(low) or bool(meta.get("episode_marker")):
        hints.append("疑似剧集或动漫(看名字语义区分)")
    if meta.get("year"):
        hints.append("年份" + str(meta["year"]))
    if meta.get("tmdb_id"):
        hints.append("tmdb=" + str(meta["tmdb_id"]))
    if meta.get("quality_tags"):
        hints.append(str(meta["quality_tags"]))
    return "", ";".join(hints), fmt, country


def calc_target_path(category: str, country: str) -> str:
    """根据类型和国家/地区计算目标目录路径 (网盘管理系统/类型/国家)"""
    type_dir = TYPE_DIR_MAP.get(category, "其他")
    country_map = TYPE_COUNTRY_MAP.get(category, {})
    country_dir = country_map.get(country, country_map.get("_default", "其他"))
    return f"网盘管理系统/{type_dir}/{country_dir}"


def rule_country_from_parent(parent_name: str, grandparent_name: str) -> str:
    """从父/祖父目录名推断国家/地区"""
    combined = f"{parent_name} {grandparent_name}"
    for c_name, c_re in COUNTRY_RULES:
        if c_re.search(combined):
            return c_name
    return ""


# ---------------- AI 客户端 (OpenAI 兼容) ----------------
def chat_json(messages: list, max_tokens: int = 6000):
    """调用 /chat/completions, 返回 (content, prompt_tokens, completion_tokens)"""
    cfg = load_config()
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    from curl_cffi import requests as creq
    body = {"model": cfg["model"], "messages": messages,
            "temperature": 0.2, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json",
               "Accept": "application/json, text/plain, */*",
               "Referer": cfg["base_url"].rstrip("/") + "/",
               "Origin": cfg["base_url"].rstrip("/")}
    last = None
    attempt = 0
    while attempt <= CALL_RETRY:
        try:
            r = creq.post(url, headers=headers, json=body, timeout=CALL_TIMEOUT)
            if r.status_code == 200:
                d = r.json()
                content = d["choices"][0]["message"]["content"] or ""
                usage = d.get("usage", {}) or {}
                return content, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            txt = (r.text or "")[:200]
            if "response_format" in txt and body.get("response_format"):
                body.pop("response_format")   # 供应商不支持 json_object -> 去掉重发(不计次)
                continue
            last = RuntimeError(f"HTTP {r.status_code}: {txt}")
        except Exception as e:
            last = e
        attempt += 1
        if attempt <= CALL_RETRY:
            # 随机退避: 3~8秒, 避免固定间隔被识别
            time.sleep(random.uniform(3, 8))
    raise last


def test_connection() -> dict:
    if not config_ready():
        return {"ok": False, "error": "配置未填写完整(base_url/model/key)"}
    t0 = time.time()
    try:
        content, _, _ = chat_json(
            [{"role": "user", "content": "只回复两个字: 正常"}], max_tokens=16)
        return {"ok": True, "reply": content.strip()[:40],
                "ms": int((time.time() - t0) * 1000), "model": load_config()["model"]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def parse_ai_items(content: str, expect_cids: list, taxonomy: dict) -> dict:
    """解析 AI 返回 JSON -> {cid: {category, resolution, subtitle, country, quality, audio, confidence, reason, suggested_name}}
    解析失败/缺漏/类别非法的条目一律兜底, 绝不丢 cid"""
    types = taxonomy["types"]
    resolutions = taxonomy.get("resolutions", [])
    subtitles = taxonomy.get("subtitles", [])
    countries = taxonomy.get("countries", [])
    qualities = taxonomy.get("qualities", [])
    audios = taxonomy.get("audios", [])
    out = {}
    for cid in expect_cids:
        out[cid] = {"category": FALLBACK_CATEGORY, "resolution": "", "subtitle": "",
                    "country": "", "quality": "", "audio": "", "confidence": 0.0,
                    "reason": "AI未返回或解析失败", "suggested_name": ""}
    data = None
    try:
        m = re.search(r"\{[\s\S]*\}", content)
        if m:
            data = json.loads(m.group(0))
    except Exception:
        data = None
    items = None
    if isinstance(data, dict):
        items = data.get("items")
        if items is None and all(isinstance(v, dict) for v in data.values()):
            items = list(data.values())   # 宽容: 直接给 {cid:{...}} 也认
    elif isinstance(data, list):
        items = data
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            cid = str(it.get("cid", "")).strip()
            if cid not in out:
                continue
            cat = str(it.get("category", "")).strip()
            # 兼容: AI 可能返回 "resolution" 或旧的 "attribute"/"format"
            res = str(it.get("resolution", "") or it.get("attribute", "") or it.get("format", "") or "").strip()
            if cat not in types:
                if cat == DISC_DIR:
                    cat = FALLBACK_CATEGORY
                    if not res:
                        res = "原盘"
                else:
                    cat = FALLBACK_CATEGORY
            if resolutions and res not in resolutions:
                res = ""
            # 字幕
            sub = str(it.get("subtitle", "") or "").strip()
            if subtitles and sub not in subtitles:
                sub = ""
            # 国家/画质/音频：可选字段，不在列表中则留空
            country = str(it.get("country", "") or "").strip()
            if countries and country not in countries:
                country = ""
            quality = str(it.get("quality", "") or "").strip()
            if qualities and quality not in qualities:
                quality = ""
            audio = str(it.get("audio", "") or "").strip()
            if audios and audio not in audios:
                audio = ""
            try:
                conf = max(0.0, min(1.0, float(it.get("confidence", 0))))
            except Exception:
                conf = 0.0
            reason_text = str(it.get("reason", "")).strip()[:60]
            # 校验: reason 里提到的类型与 category 不一致时，以 reason 为准
            if cat in types and reason_text:
                for t in types:
                    if t != cat and t in reason_text:
                        cat = t
                        break
            out[cid] = {"category": cat, "resolution": res, "subtitle": sub,
                        "country": country, "quality": quality, "audio": audio,
                        "confidence": conf,
                        "reason": reason_text,
                        "suggested_name": str(it.get("suggested_name", "") or "").strip()[:120]}
    return out


def build_plan_groups(items: list, disc_separate: bool = True):
    """移动计划分组: items=[{cid,name,pid,category,format,country}]
    返回 [(path_parts, items)]; 
    按 类型/国家 分组, 原盘归入 蓝光原盘/类型/国家 三级结构"""
    groups = {}
    for it in items:
        cat = it["category"] or FALLBACK_CATEGORY
        country = it.get("country", "")
        country_dir = TYPE_COUNTRY_MAP.get(cat, {}).get(country, 
                      TYPE_COUNTRY_MAP.get(cat, {}).get("_default", "其他"))
        if disc_separate and it.get("format") == "原盘":
            parts = (DISC_DIR, TYPE_DIR_MAP.get(cat, "其他"), country_dir)
        else:
            parts = (TYPE_DIR_MAP.get(cat, "其他"), country_dir)
        groups.setdefault(parts, []).append(it)
    return sorted(groups.items(), key=lambda kv: kv[0])


# ---------------- 后台任务 ----------------


class AiManager:
    """全局单例: 串行分析线程 + 断点续跑"""

    def __init__(self):
        con = get_conn()
        con.execute("UPDATE ai_batches SET status='queued' WHERE status='running'")
        con.commit()
        con.close()
        self.worker = None
        self._stop_events: dict[int, threading.Event] = {}  # 按 batch_id 记录停止事件
        # 单进程直跑, 见 scanner.ScanManager.__init__ 说明: 必须无条件启动分析线程
        self._start_worker()

    def create_batch(self, scope_cid: str, scope_name: str, limit: int = 0) -> int:
        con = get_conn()
        try:
            cur = con.execute(
                "INSERT INTO ai_batches(scope_cid,scope_name,status,limit_n,created_at)"
                " VALUES(?,?, 'queued', ?, ?)",
                (scope_cid, scope_name or "", int(limit or 0),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def stop(self, batch_id: int) -> bool:
        con = get_conn()
        try:
            row = con.execute("SELECT status FROM ai_batches WHERE id=?", (batch_id,)).fetchone()
            if row and row["status"] in ("queued", "running"):
                if batch_id in self._stop_events:
                    self._stop_events[batch_id].set()
                return True
            return False
        finally:
            con.close()

    def _start_worker(self):
        self.worker = threading.Thread(target=self._loop, daemon=True, name="ai-worker")
        self.worker.start()

    def _loop(self):
        while True:
            try:
                con = get_conn()
                job = con.execute(
                    "SELECT * FROM ai_batches WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                con.close()
                if not job:
                    time.sleep(3)
                    continue
                # 为当前批次创建 stop_event
                self._stop_events[job["id"]] = threading.Event()
                self._run_batch(job["id"])
            except Exception:
                traceback.print_exc()
                time.sleep(10)

    def _estimate(self, dirs, meta_map):
        n_rule = 0
        for cid, name in dirs:
            if rule_hint(name, meta_map.get(cid, {}))[0]:
                n_rule += 1
        need_ai = len(dirs) - n_rule
        calls = (need_ai + get_batch_size() - 1) // get_batch_size()
        return n_rule, need_ai, calls

    def _run_batch(self, batch_id: int):
        con = get_conn()
        try:
            b = con.execute("SELECT * FROM ai_batches WHERE id=?", (batch_id,)).fetchone()
            if not b:
                return
            scope_cid, limit = b["scope_cid"], int(b["limit_n"] or 0)
            con.execute("UPDATE ai_batches SET status='running', started_at=? WHERE id=?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
            con.commit()
            if not config_ready():
                con.execute("UPDATE ai_batches SET status='error', err='AI 配置未填写完整', finished_at=? WHERE id=?",
                            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
                con.commit()
                logbus.pub("AI", f"批次 #{batch_id} 配置未填写完整, 无法分析", lv="warn")
                return

            batch_start = time.time()
            cfg = load_config()
            logbus.pub("AI", f"批次 #{batch_id} 使用模型: {cfg['model']}", lv="info")

            # Phase 0: 从标签库读取分类体系 + 收集条目上下文
            taxonomy = build_ai_taxonomy(con)
            all_items = collect_items_with_context(con, scope_cid)
            if limit > 0:
                all_items = all_items[:limit]

            # 断点续跑: 跳过已有建议的 cid（被驳回的可重新分析）
            existing = {r["cid"] for r in con.execute(
                "SELECT cid FROM ai_suggestions WHERE status NOT IN ('rejected')")}
            items = [it for it in all_items if it["cid"] not in existing]

            con.execute("UPDATE ai_batches SET total=? WHERE id=?", (len(items), batch_id))
            con.commit()
            skipped = len(all_items) - len(items)

            # 没有待处理条目时，标记为 warning 并提示原因
            if not items:
                reason = "该范围下没有可分析的条目"
                if not all_items:
                    reason = "未扫描到内容，请先对该范围进行扫描"
                elif skipped:
                    reason = f"所有 {skipped} 个条目已有分析建议"
                con.execute("UPDATE ai_batches SET status='done', err=?, finished_at=? WHERE id=?",
                            (reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
                con.commit()
                logbus.pub("AI", f"批次 #{batch_id} {reason}", lv="warn")
                return

            logbus.pub("AI", f"批次 #{batch_id} 范围 {b['scope_name'] or scope_cid}: "
                       f"共 {len(all_items)} 个条目(目录+文件), 待处理 {len(items)} 个"
                       + (f" (跳过 {skipped} 个已有建议)" if skipped else ""), lv="ok")

            def upsert_suggestion(it, item, source):
                # 计算目标路径
                target_path = calc_target_path(item["category"], item.get("country", ""))
                con.execute(
                    "INSERT OR REPLACE INTO ai_suggestions"
                    "(cid,name,pid,hint,category,resolution,attribute,format,country,quality,audio,"
                    "suggested_name,confidence,reason,source,status,batch_id,target_path,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it["cid"], it["name"], scope_cid, "",
                     item["category"], item.get("resolution", ""), "", "",
                     item.get("country", ""), item.get("quality", ""), item.get("audio", ""),
                     item.get("suggested_name", ""), item["confidence"], item.get("reason", ""),
                     source, "pending", batch_id, target_path,
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

            # Phase 1: AI 批量分析（主路径，带丰富上下文）
            system_prompt = build_system_prompt(taxonomy)
            n_blocks = (len(items) + get_batch_size() - 1) // get_batch_size()
            n_failed_blocks = 0

            for i in range(0, len(items), get_batch_size()):
                if self._stop_events.get(batch_id, threading.Event()).is_set():
                    con.execute("UPDATE ai_batches SET status='stopped', finished_at=? WHERE id=?",
                                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
                    con.commit()
                    logbus.pub("AI", f"批次 #{batch_id} 已停止(已处理 {i}/{len(items)})", lv="warn")
                    self._stop_events.pop(batch_id, None)
                    return

                chunk = items[i:i + get_batch_size()]
                block_start = time.time()
                # 构造带上下文的输入（包含tmdb信息）
                import tmdb as tmdb_mod
                input_lines = []
                for it in chunk:
                    entry = {"cid": it["cid"], "name": it["name"],
                             "type": "目录" if it["is_dir"] else detect_file_type(it["name"]),
                             "parent": it["parent_name"],
                             "grandparent": it["grandparent_name"]}
                    if it["is_dir"] and it["children"]:
                        entry["children"] = it["children"]
                    # 查询tmdb信息补充上下文
                    try:
                        movie_info = tmdb_mod.get_movie_info(it["name"])
                        if movie_info:
                            entry["tmdb_country"] = movie_info.get("country", "")
                            entry["tmdb_genres"] = movie_info.get("genres", "")
                            entry["tmdb_year"] = movie_info.get("year", "")
                            entry["tmdb_title"] = movie_info.get("title", "")
                    except Exception:
                        pass  # tmdb查询失败不影响分析
                    input_lines.append(json.dumps(entry, ensure_ascii=False))

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": "\n".join(input_lines)},
                ]
                try:
                    content, tin, tout = chat_json(messages)
                except Exception as e:
                    n_failed_blocks += 1
                    logbus.pub("AI", f"批次 #{batch_id} 第 {i//get_batch_size()+1}/{n_blocks} 块调用失败, "
                               f"{len(chunk)} 条留待重试: {str(e)[:120]}", lv="error")
                    continue

                # 检查 AI 是否返回了错误信息而不是 JSON
                # 去掉模型的思维链标签(如 <think>...</think>)
                content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
                # 去掉 markdown 代码块包裹 (```json ... ``` 或 ``` ... ```)
                content = re.sub(r"^```(?:json)?\s*\n?", "", content.strip())
                content = re.sub(r"\n?```\s*$", "", content.strip())
                if not content or not content.strip().startswith("{"):
                    n_failed_blocks += 1
                    logbus.pub("AI", f"批次 #{batch_id} 第 {i//get_batch_size()+1}/{n_blocks} 块 AI 未返回 JSON: "
                               f"{content[:200]}", lv="error")
                    continue

                parsed = parse_ai_items(content, [it["cid"] for it in chunk], taxonomy)
                for it in chunk:
                    item = parsed.get(it["cid"]) or {
                        "category": FALLBACK_CATEGORY, "attribute": "", "country": "",
                        "quality": "", "audio": "", "confidence": 0.0,
                        "reason": "AI未返回", "suggested_name": ""}

                    # Phase 2: 规则并行校验
                    final, hint, fmt, rule_country = rule_hint(it["name"], {})
                    # 规则识别的国家补充到 item (如果 AI 没给)
                    if rule_country and not item.get("country"):
                        item["country"] = rule_country
                    # 从父目录名推断国家 (如果仍然没有)
                    if not item.get("country"):
                        parent_country = rule_country_from_parent(it.get("parent_name", ""), it.get("grandparent_name", ""))
                        if parent_country:
                            item["country"] = parent_country
                    src = "ai"
                    if final:
                        if item["category"] == FALLBACK_CATEGORY:
                            # AI 未判断出 → 规则兜底
                            item["category"] = final
                            item["confidence"] = 0.9
                            item["reason"] = "规则兜底(演唱会特征)"
                            src = "rule"
                            upsert_suggestion(it, item, src)
                        elif item["category"] == final:
                            # AI 与规则一致 → 提升置信度
                            item["confidence"] = min(1.0, item["confidence"] + 0.1)
                            upsert_suggestion(it, item, src)
                        else:
                            # AI 与规则冲突 → 降低置信度，保留 AI 结果供人工复核
                            item["confidence"] = max(0.1, item["confidence"] - 0.3)
                            item["reason"] = (item["reason"] or "") + "[规则警告]"
                            upsert_suggestion(it, item, src)
                    else:
                        upsert_suggestion(it, item, src)

                    # 如果规则识别出属性且 AI 没给，补充
                    if fmt and not item.get("attribute"):
                        con.execute("UPDATE ai_suggestions SET attribute=?, format=? WHERE cid=?",
                                    (fmt, fmt, it["cid"]))

                    # === 逐条详情日志 ===
                    conf = item["confidence"]
                    conf_mark = "●" if conf >= 0.7 else "◐" if conf >= 0.4 else "○"
                    name_short = it["name"][:30]
                    tags = item["category"]
                    if item.get("country"):
                        tags += "/" + item["country"]
                    if item.get("resolution"):
                        tags += "/" + item["resolution"]
                    source_mark = "[规则]" if src == "rule" else ""
                    warn_mark = " ⚠" + (item.get("reason") or "") if "[规则警告]" in (item.get("reason") or "") else ""
                    logbus.pub("AI", f"  {conf_mark} {name_short} → {tags} "
                               f"(置信度 {conf:.0%}) {source_mark}{warn_mark}",
                               lv="info" if conf >= 0.5 else "warn")

                con.execute(
                    "UPDATE ai_batches SET processed=processed+?, n_calls=n_calls+1,"
                    " tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE id=?",
                    (len(chunk), tin, tout, batch_id))
                con.commit()
                block_elapsed = time.time() - block_start
                logbus.pub("AI", f"#{batch_id} AI 块 {i//get_batch_size()+1}/{n_blocks} 完成 "
                           f"(+{len(chunk)} 条, tokens {tin}+{tout}, 耗时 {block_elapsed:.1f}s)")
                time.sleep(random.uniform(*CALL_DELAY))

            if n_failed_blocks and n_failed_blocks == n_blocks:
                con.execute("UPDATE ai_batches SET status='error', err=?, finished_at=? WHERE id=?",
                            (f"全部 {n_blocks} 块 AI 调用失败, 请检查 base_url/model/key 后重新分析",
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
                con.commit()
                logbus.pub("AI", f"批次 #{batch_id} 全部 AI 调用失败, 标记 error (未产生任何建议)", lv="error")
                return
            con.execute("UPDATE ai_batches SET status='done', finished_at=? WHERE id=?",
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
            con.commit()
            row = con.execute("SELECT processed,n_calls FROM ai_batches WHERE id=?", (batch_id,)).fetchone()
            batch_elapsed = time.time() - batch_start
            logbus.pub("AI", f"批次 #{batch_id} 完成: {row['processed']} 条, "
                       f"{row['n_calls']} 次调用, 耗时 {batch_elapsed:.0f}s"
                       + (f" (失败块 {n_failed_blocks} 个)" if n_failed_blocks else ""), lv="ok")
            # 分类分布统计
            dist = con.execute(
                "SELECT category, count(*) as n FROM ai_suggestions "
                "WHERE batch_id=? GROUP BY category ORDER BY n DESC", (batch_id,)).fetchall()
            if dist:
                logbus.pub("AI", f"  分类分布: " + " / ".join(
                    f"{r['category']} {r['n']}" for r in dist), lv="ok")
            # 平均置信度 + 低置信度提醒
            avg_row = con.execute(
                "SELECT avg(confidence), sum(case when confidence<0.5 then 1 else 0 end) "
                "FROM ai_suggestions WHERE batch_id=?", (batch_id,)).fetchone()
            avg_conf = avg_row[0] or 0
            low_conf = avg_row[1] or 0
            if low_conf:
                logbus.pub("AI", f"  平均置信度: {avg_conf:.0%}  ⚠ {low_conf} 条低置信度需复核", lv="warn")
            else:
                logbus.pub("AI", f"  平均置信度: {avg_conf:.0%}", lv="ok")
        except Exception as e:
            con.execute("UPDATE ai_batches SET status='error', err=?, finished_at=? WHERE id=?",
                        (str(e)[:300], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), batch_id))
            con.commit()
            logbus.pub("AI", f"批次 #{batch_id} 异常终止: {str(e)[:120]}", lv="error")
            traceback.print_exc()
        finally:
            # 清理该批次的 stop_event
            self._stop_events.pop(batch_id, None)
            con.close()
