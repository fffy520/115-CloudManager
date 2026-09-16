# -*- coding: utf-8 -*-
"""
TMDB API 客户端: 根据 tmdbid 查询电影/剧集信息
- 用于补充AI分析的上下文（国家、类型、年份等）
- API文档: https://developer.themoviedb.org/reference/introduction/getting-started
"""
import json
import re
import time
from urllib.parse import quote

from curl_cffi import requests as creq

import config

# 缓存: tmdb_id -> {title, country, year, genre, overview}
_cache = {}
_cache_ttl = 86400  # 24小时缓存

def _load_api_key() -> str:
    """从 ai_config.json 读取 TMDB API key"""
    try:
        with open(config.AI_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("tmdb_api_key", "")
    except Exception:
        return ""

def _get_headers() -> dict:
    api_key = _load_api_key()
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

# 国家代码 -> 中文名映射
COUNTRY_MAP = {
    "US": "美国", "GB": "英国", "UK": "英国", "CN": "华语", "HK": "港台", "TW": "港台",
    "JP": "日本", "KR": "韩国", "IN": "印度", "FR": "法国", "DE": "德国",
    "ES": "西班牙", "TH": "泰国", "IT": "意大利", "AU": "澳大利亚", "CA": "加拿大",
    "BR": "巴西", "RU": "俄罗斯", "MX": "墨西哥", "SE": "瑞典", "DK": "丹麦",
    "NO": "挪威", "FI": "芬兰", "NL": "荷兰", "BE": "比利时", "CH": "瑞士",
    "AT": "奥地利", "IE": "爱尔兰", "NZ": "新西兰", "SG": "新加坡",
}

# 类型ID -> 中文名映射
GENRE_MAP = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪",
    99: "纪录片", 18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史",
    27: "恐怖", 10402: "音乐", 9648: "悬疑", 10749: "爱情", 878: "科幻",
    10770: "电视电影", 53: "惊悚", 10752: "战争", 37: "西部",
}

def _normalize_country(origin_country: list, spoken_languages: list) -> str:
    """根据国家代码列表，返回最合适的中文国家名"""
    if not origin_country:
        # 尝试从语言判断
        for lang in spoken_languages or []:
            code = lang.get("iso_639_1", "")
            if code == "zh":
                return "华语"
            if code == "ja":
                return "日本"
            if code == "ko":
                return "韩国"
        return ""
    # 优先匹配中文相关
    for c in origin_country:
        if c in ("CN", "HK", "TW"):
            return COUNTRY_MAP.get(c, "")
    # 匹配第一个
    first = origin_country[0] if origin_country else ""
    return COUNTRY_MAP.get(first, first)

def _normalize_genres(genres: list) -> str:
    """类型列表 -> 中文类型"""
    if not genres:
        return ""
    names = [GENRE_MAP.get(g.get("id", 0), g.get("name", "")) for g in genres]
    return "/".join(names)

def query_movie(tmdb_id: int, media_type: str = "movie") -> dict:
    """查询电影/剧集信息
    返回: {title, original_title, country, year, genres, overview, poster_path}
    """
    # 检查缓存
    cache_key = f"{media_type}_{tmdb_id}"
    if cache_key in _cache:
        cached = _cache[cache_key]
        if time.time() - cached.get("_ts", 0) < _cache_ttl:
            return cached

    api_key = _load_api_key()
    if not api_key:
        return {}

    # 查询详情
    url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?language=zh-CN"
    try:
        r = creq.get(url, headers=_get_headers(), timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
    except Exception:
        return {}

    # 提取信息
    result = {
        "title": data.get("title") or data.get("name", ""),
        "original_title": data.get("original_title") or data.get("original_name", ""),
        "country": _normalize_country(data.get("origin_country", []), data.get("spoken_languages", [])),
        "year": (data.get("release_date") or data.get("first_air_date") or "")[:4],
        "genres": _normalize_genres(data.get("genres", [])),
        "overview": (data.get("overview") or "")[:200],
        "poster_path": data.get("poster_path", ""),
        "_ts": time.time(),
    }
    _cache[cache_key] = result
    return result

def extract_tmdb_id(name: str) -> tuple:
    """从文件名中提取 tmdb_id 和 media_type
    返回: (tmdb_id, media_type) 或 (None, None)
    支持格式:
    - {tmdb-12345} 或 {tmdbid-12345} 或 {tmdb=12345} 或 [tmdbid-12345]
    """
    patterns = [
        r"\{tmdb[-=](\d+)\}",
        r"\{tmdbid[-=](\d+)\}",
        r"\[tmdb[-=](\d+)\]",
        r"\[tmdbid[-=](\d+)\]",
        r"tmdb[:\s]*(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, name, re.I)
        if m:
            tmdb_id = int(m.group(1))
            # 默认movie，如果名字包含S01E01等剧集标记则为tv
            if re.search(r"S\d{1,2}E\d{1,3}", name, re.I):
                return tmdb_id, "tv"
            return tmdb_id, "movie"
    return None, None

def get_movie_info(name: str) -> dict:
    """根据文件名自动查询电影信息
    返回: {country, genres, year, title, overview} 或空dict
    """
    tmdb_id, media_type = extract_tmdb_id(name)
    if not tmdb_id:
        return {}
    return query_movie(tmdb_id, media_type)

# 测试入口
if __name__ == "__main__":
    test_names = [
        "奥德赛 (2026) {tmdb-1368337}",
        "复仇者联盟4终局之战 {tmdb-299534}",
        "咒怨 (2002) {tmdb-11838}",
    ]
    for name in test_names:
        info = get_movie_info(name)
        print(f"\n{name}:")
        print(f"  国家: {info.get('country', '未知')}")
        print(f"  类型: {info.get('genres', '未知')}")
        print(f"  年份: {info.get('year', '未知')}")
        print(f"  标题: {info.get('title', '未知')}")
