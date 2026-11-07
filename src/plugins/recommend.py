# src/plugins/recommend.py
"""
推分推荐插件（数据来源：水鱼查分器 https://www.diving-fish.com）

实现思路：
1. POST /query/player 用 QQ（或用户名）取玩家的 rating 与简略成绩
   （接口返回的是"新版 rating 最高的 15 张 + 旧版 rating 最高的 25/35 张"，即 b50 数据）
2. 按 rating 推算应该去推的定数区间：
       averageDs = round((rating / (22.4 * 50)) * 10) / 10   # 把 rating 平摊到 50 首曲子
       min = max(1,  round(averageDs * 10) / 10)             # 下限：平均定数
       max = min(15, round((averageDs + 0.5) * 10) / 10)     # 上限：平均定数 + 0.5
3. 用 /chart_stats 的"拟合定数 fit_diff"与"平均达成率 avg"在区间内筛选排序：
       · 定数差值：fit_diff - 官方 ds 越小，说明相对官标越简单，越容易推
       · 平均达成率：avg 越高，说明对大多数玩家越简单
       · 综合：两者名次取平均（默认）
4. 默认筛掉你已经鸟加（达成率 >= 100.5%）的谱面，可切换门槛或关闭筛选
5. 吃分推荐（默认排序）：rating = (旧曲 Best 35 + 新曲 Best 15) / 50，
   所以只有"鸟加后的 ra 超过所在曲池末位 ra"的谱才真能让 rating 涨；
   插件用 b50 末位 ra 过滤 + 按能涨多少 ra 排序，直接告诉你打哪首最划算

命令用法（推荐 / 推分推荐）：
    推荐                      用自己 QQ 的 rating 自动推荐
    推荐 15000                按目标 rating 推荐（想上彩框就把 rating 定高一点）
    推荐 15000 鸟             目标 rating + 门槛（鸟加 / 鸟 / ss+ / ss / 无）
    推荐 14.2 14.5            自定义定数区间（单个数字则只看该定数，保留旧用法）
    推荐 14.2 14.5 达成率      指定排序方式（综合 / 拟合 / 达成率）
    推荐 吃分 / 推荐 达成率     切换排序方式（吃分=鸟加收益，默认）
    推荐 第2页                翻页看第 16~30 条（只认「第x页」这一种写法，不会和数字参数冲突）
    推荐 15000 鸟 第3页        页码可以和其它参数叠加
    推荐 用户名               查指定用户名的玩家
    推荐 @某人                 查被 @ 的玩家

不写「第x页」时默认是第 1 页（和显式写「第1页」完全一样）。

可选配置（项目根目录 .env）：
    RECOMMEND_FILTER=100.5    默认门槛，改 0 表示不筛（100.5=鸟加，100=鸟，99.5=SS+）
    RECOMMEND_TOP_N=15        默认显示条数（1~30）
    RECOMMEND_SORT=吃分        默认排序方式（吃分 / 综合 / 拟合 / 达成率）
    RECOMMEND_MIN_CNT=50      谱面样本数低于该值不参与推荐（避免数据太少）
"""

import base64
import re
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from src.libraries.config_env import load_project_env
from src.libraries.player_source import fetch_player_any

# 让 .env 里的 RECOMMEND_* 配置生效（nonebot 不会把 .env 写进环境变量）
load_project_env()

# ========== 接口与常量 ==========

BASE_URL = "https://www.diving-fish.com/api/maimaidxprober"
MUSIC_DATA_API = f"{BASE_URL}/music_data"
CHART_STATS_API = f"{BASE_URL}/chart_stats"


HEADERS = {
    "User-Agent": "mai-bot-recommend/1.0",
    "Referer": "https://www.diving-fish.com/maimaidx/prober/",
}

DIFF_LABELS = ["绿", "黄", "红", "紫", "白"]
RATING_DIVISOR = 22.4 * 50  # rating 平摊到 50 首推分曲目

# 评分的两首歌池：旧曲 Best 35 + 新曲 Best 15
POOL_CAPACITY = {"old": 35, "new": 15}
POOL_LABELS = {"old": "旧曲池", "new": "新曲池"}
SSSP_ACHIEVEMENT = 100.5  # 鸟加，单谱 ra 的上限达成率

# 官方单谱 DX Rating 的达成率系数表（从高到低匹配）
RA_FACTOR_TABLE = [
    (100.5, 22.4),
    (100.0, 21.6),
    (99.5, 21.1),
    (99.0, 20.8),
    (98.0, 20.3),
    (97.0, 20.0),
    (94.0, 16.8),
    (90.0, 15.2),
    (80.0, 13.6),
    (75.0, 12.0),
    (70.0, 11.2),
    (60.0, 9.6),
    (50.0, 8.0),
]


def compute_ra(ds: float, achievement: float) -> int:
    """
    官方单谱 DX Rating：ra = floor(ds * min(100.5, 达成率) / 100 * 系数)
    100.5%（鸟加）时系数为 22.4，等价于 floor(ds * 22.512)。
    """
    base = 7.0
    for threshold, factor in RA_FACTOR_TABLE:
        if achievement >= threshold:
            base = factor
            break
    return int(ds * (min(SSSP_ACHIEVEMENT, achievement) / 100) * base)


def ra_for_sssp(ds: float) -> int:
    """该谱打到鸟加（100.5%）能拿到的单谱 ra"""
    return compute_ra(ds, SSSP_ACHIEVEMENT)

# 门槛关键词 -> 达成率阈值
FILTER_KEYWORDS = {
    "鸟加": 100.5,
    "鸟": 100.0,
    "ss+": 99.5,
    "ss": 99.0,
    "无": 0.0,
    "全部": 0.0,
    "不筛": 0.0,
    "关闭筛选": 0.0,
}

# 排序方式关键词
SORT_KEYWORDS = {
    "吃分": "吃分",
    "推分": "吃分",
    "涨分": "吃分",
    "收益": "吃分",
    "吃分推荐": "吃分",
    "综合": "综合",
    "拟合": "拟合",
    "定数差值": "拟合",
    "拟合定数": "拟合",
    "达成率": "达成率",
    "平均达成率": "达成率",
}


def _env_num(name: str, default, cast=float):
    """读取数字型环境变量，没写/留空/写错都回退默认值"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning(f"[recommend] 环境变量 {name}={raw!r} 不是数字，改用默认值 {default}")
        return default


RECOMMEND_FILTER = _env_num("RECOMMEND_FILTER", 100.5)
RECOMMEND_TOP_N = max(1, min(30, int(_env_num("RECOMMEND_TOP_N", 15))))
RECOMMEND_SORT = (os.getenv("RECOMMEND_SORT") or "吃分").strip() or "吃分"
MIN_SAMPLE_CNT = _env_num("RECOMMEND_MIN_CNT", 50)

# 推分推荐图：默认出图（失败自动回退文字）
RECOMMEND_IMAGE = os.getenv("RECOMMEND_IMAGE", "true").lower() in ("1", "true", "yes", "on")
RECOMMEND_IMAGE_FORMAT = (os.getenv("RECOMMEND_IMAGE_FORMAT") or "jpg").strip().lower()
RECOMMEND_IMAGE_QUALITY = max(50, min(95, int(_env_num("RECOMMEND_IMAGE_QUALITY", 88))))

# ========== 缓存 ==========

_music_data_cache: Optional[Dict[str, Any]] = None
_music_data_etag: Optional[str] = None
_chart_stats_cache: Optional[Dict[str, Any]] = None
_chart_stats_time: float = 0.0
CHART_STATS_TTL = 300.0  # 拟合定数/平均达成率缓存 5 分钟


async def fetch_music_data() -> Optional[Dict[str, Any]]:
    """获取歌曲数据（带 etag 缓存），返回 {song_id: music} 形式的字典"""
    global _music_data_cache, _music_data_etag

    headers = dict(HEADERS)
    if _music_data_etag:
        headers["If-None-Match"] = _music_data_etag

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(MUSIC_DATA_API, headers=headers)

            if response.status_code == 304 and _music_data_cache:
                logger.info("[recommend] 歌曲数据未变化，使用缓存")
                return _music_data_cache

            if response.status_code == 200:
                data = response.json()
                _music_data_cache = {str(item["id"]): item for item in data}
                etag = response.headers.get("etag")
                if etag:
                    _music_data_etag = etag
                logger.info(f"[recommend] 歌曲数据更新完成，共 {len(_music_data_cache)} 首")
                return _music_data_cache

            logger.warning(f"[recommend] 获取歌曲数据失败：HTTP {response.status_code}")
            return _music_data_cache
    except Exception as e:
        logger.error(f"[recommend] 请求歌曲数据异常：{e}")
        return _music_data_cache


async def fetch_chart_stats() -> Optional[Dict[str, Any]]:
    """获取谱面统计（拟合定数 fit_diff / 平均达成率 avg），带 5 分钟缓存"""
    global _chart_stats_cache, _chart_stats_time

    if _chart_stats_cache and (time.time() - _chart_stats_time) < CHART_STATS_TTL:
        return _chart_stats_cache

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(CHART_STATS_API, headers=HEADERS)
            if response.status_code == 200:
                _chart_stats_cache = response.json()
                _chart_stats_time = time.time()
                logger.info(f"[recommend] 谱面统计更新完成，共 {len(_chart_stats_cache.get('charts') or {})} 首")
                return _chart_stats_cache

            logger.warning(f"[recommend] 获取谱面统计失败：HTTP {response.status_code}")
            return _chart_stats_cache
    except Exception as e:
        logger.error(f"[recommend] 请求谱面统计异常：{e}")
        return _chart_stats_cache


async def fetch_player(qq: Optional[str] = None, username: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    查询玩家 B50：水鱼优先，失败自动回退落雪。

    返回 (数据, 错误信息)，成功时错误信息为空字符串。
    数据额外带 _source 字段（diving-fish / lxns）。
    """
    return await fetch_player_any(qq=qq, username=username)


# ========== 推荐逻辑 ==========


def calc_ds_range(rating: float) -> Tuple[float, float]:
    """
    按参考公式推算推荐定数区间。

    averageDs 是把 rating 平摊到 50 首曲子后的等效定数，
    下限取平均定数（要鸟加这种曲子 rating 才会涨），上限 +0.5。
    """
    average_ds = round((rating / RATING_DIVISOR) * 10) / 10
    ds_min = max(1.0, round(average_ds * 10) / 10)
    ds_max = min(15.0, round((average_ds + 0.5) * 10) / 10)
    # rating 极小时原始公式会算出下限 > 上限，这里兜一下，保证区间有效
    ds_max = max(ds_min, ds_max)
    return ds_min, ds_max


def parse_achievements(player: Dict[str, Any]) -> Dict[Tuple[int, int], float]:
    """把玩家简略成绩整理成 {(song_id, 难度下标): 达成率}"""
    result: Dict[Tuple[int, int], float] = {}
    charts = player.get("charts")

    items: List[Dict[str, Any]] = []
    if isinstance(charts, dict):
        for key in ("sd", "dx"):
            items.extend(charts.get(key) or [])
    elif isinstance(charts, list):
        items.extend(charts)

    for item in items:
        if not isinstance(item, dict):
            continue
        # 宴会场谱面难度下标和普通谱面会撞车，直接跳过
        if "?" in str(item.get("level", "")) or item.get("level_label") == "Utage":
            continue
        try:
            song_id = int(item.get("song_id"))
            diff_index = int(item.get("level_index"))
            achievements = float(item.get("achievements"))
        except (TypeError, ValueError):
            continue
        result[(song_id, diff_index)] = achievements

    return result


def analyze_player_pools(player: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """
    分析玩家 b50 的两个曲池，算出"末位 ra"（floor）。

    rating = (旧曲池 Best 35 + 新曲池 Best 15) / 50，
    所以一首谱只有在"鸟加后的 ra 超过所在池子的末位 ra"时，才能让 rating 真的涨。
    池子没满（成绩条数 < 容量）时末位视为 0，随便一首都能拉高总和。
    """
    charts = player.get("charts") if isinstance(player, dict) else None
    buckets = {"old": [], "new": []}
    if isinstance(charts, dict):
        buckets["old"] = [c for c in (charts.get("sd") or []) if isinstance(c, dict)]
        buckets["new"] = [c for c in (charts.get("dx") or []) if isinstance(c, dict)]

    pools: Dict[str, Dict[str, int]] = {}
    for pool, capacity in POOL_CAPACITY.items():
        ras = [int(c["ra"]) for c in buckets[pool] if isinstance(c.get("ra"), (int, float))]
        full = len(ras) >= capacity
        pools[pool] = {
            "floor": min(ras) if (full and ras) else 0,
            "count": len(ras),
            "capacity": capacity,
        }
    return pools


def apply_gain(
    candidates: List[Dict[str, Any]],
    pools: Dict[str, Dict[str, int]],
) -> List[Dict[str, Any]]:
    """
    给每张候选谱算"鸟加收益"，并滤掉鸟加也顶不掉末位（涨不了分）的谱。

    gain = 该谱鸟加后的 ra - 所在池子的末位 ra，也就是 rating 总和能涨多少。
    """
    kept: List[Dict[str, Any]] = []
    for item in candidates:
        pool = "new" if item.get("is_new") else "old"
        floor = int((pools.get(pool) or {}).get("floor", 0))
        target_ra = ra_for_sssp(item["ds"])
        gain = target_ra - floor
        item.update({"pool": pool, "floor": floor, "target_ra": target_ra, "gain": gain})
        if gain > 0:
            kept.append(item)
    return kept


def build_candidates(
    chart_stats: Dict[str, Any],
    music_data: Dict[str, Any],
    ds_min: float,
    ds_max: float,
) -> List[Dict[str, Any]]:
    """把落在定数区间内、数据完整的谱面整理出来"""
    candidates: List[Dict[str, Any]] = []
    charts = (chart_stats or {}).get("charts") or {}

    for song_id, chart_list in charts.items():
        music = music_data.get(str(song_id))
        if not music or not isinstance(chart_list, list):
            continue

        levels = music.get("level") or []
        ds_list = music.get("ds") or []

        for diff_index, stat in enumerate(chart_list):
            if not isinstance(stat, dict):
                continue

            fit_diff = stat.get("fit_diff")
            avg = stat.get("avg")
            cnt = stat.get("cnt")
            if fit_diff is None or avg is None:
                continue
            if isinstance(cnt, (int, float)) and cnt < MIN_SAMPLE_CNT:
                continue
            if diff_index >= len(levels) or diff_index >= len(ds_list):
                continue

            level = str(levels[diff_index])
            if "?" in level:  # 宴会场
                continue

            try:
                ds = float(ds_list[diff_index])
                fit_diff = float(fit_diff)
                avg = float(avg)
            except (TypeError, ValueError):
                continue

            if not (ds_min <= ds <= ds_max):
                continue

            candidates.append({
                "song_id": int(song_id),
                "diff_index": diff_index,
                "title": music.get("title", ""),
                "type": music.get("type", ""),
                "is_new": bool((music.get("basic_info") or {}).get("is_new")),
                "ds": ds,
                "level": level,
                "fit_diff": fit_diff,
                "avg": avg,
                "cnt": cnt,
                "gap": round(fit_diff - ds, 3),
            })

    return candidates


def rank_candidates(
    candidates: List[Dict[str, Any]],
    achievements: Dict[Tuple[int, int], float],
    threshold: float,
    sort_mode: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    先按门槛筛掉玩家已达成的谱面，再按指定方式排序。

    返回 (排序后的列表, 被筛掉的条数)
    """
    for item in candidates:
        item["mine"] = achievements.get((item["song_id"], item["diff_index"]))

    if threshold > 0:
        kept = [c for c in candidates if (c["mine"] is None or c["mine"] < threshold)]
        skipped = len(candidates) - len(kept)
    else:
        kept = list(candidates)
        skipped = 0

    if sort_mode == "吃分":
        # 先看鸟加收益（能不能顶掉 b50 末位、能涨多少 ra），再看谱面本身好不好推
        kept.sort(key=lambda c: (-c.get("gain", 0), c["gap"], -c["avg"]))
    elif sort_mode == "拟合":
        kept.sort(key=lambda c: (c["gap"], -c["avg"]))
    elif sort_mode == "达成率":
        kept.sort(key=lambda c: (-c["avg"], c["gap"]))
    else:
        # 综合：两种排序的名次取平均，越小越靠前
        by_gap = sorted(kept, key=lambda c: c["gap"])
        by_avg = sorted(kept, key=lambda c: -c["avg"])
        rank_gap = {id(c): i for i, c in enumerate(by_gap)}
        rank_avg = {id(c): i for i, c in enumerate(by_avg)}
        kept.sort(key=lambda c: (rank_gap[id(c)] + rank_avg[id(c)], c["gap"]))
        sort_mode = "综合"

    return kept, skipped


def format_result(
    player: Optional[Dict[str, Any]],
    player_rating: Optional[float],
    rating_used: Optional[float],
    ds_min: float,
    ds_max: float,
    rows: List[Dict[str, Any]],
    page_rows: List[Dict[str, Any]],
    page: int,
    total_pages: int,
    next_hint: Optional[str],
    pools: Optional[Dict[str, Dict[str, int]]],
    fallback_note: str,
    threshold: float,
    sort_mode: str,
    skipped: int,
    total: int,
    top_n: int,
) -> str:
    """拼装推荐结果文本"""
    lines: List[str] = []

    nickname = (player.get("nickname") or player.get("username")) if player else None
    head = f"🎯 {nickname} 的推分推荐" if nickname else "🎯 推分推荐"

    if player:
        if player_rating and rating_used and player_rating != rating_used:
            head += f"（当前 Rating {player_rating:g}｜按目标 {rating_used:g} 计算）"
        elif player_rating:
            head += f"（当前 Rating {player_rating:g}）"
        else:
            head += f"（按 Rating {rating_used:g} 计算）"
    elif rating_used:
        head += f"（按 Rating {rating_used:g} 计算）"
    else:
        head += "（手动指定定数区间）"
    lines.append(head)

    lines.append(f"📏 推荐定数区间：{ds_min:g} ~ {ds_max:g}｜共 {total} 张谱面")
    if pools:
        old, new = pools["old"], pools["new"]
        lines.append(
            f"📈 b50 末位 ra：旧曲 {old['floor']}（{old['count']}/{old['capacity']}）｜"
            f"新曲 {new['floor']}（{new['count']}/{new['capacity']}）｜只列鸟加后能顶掉末位的谱"
        )
    if threshold > 0:
        lines.append(f"🧹 已筛掉达成率 >= {threshold:g}% 的 {skipped} 张（门槛可用「鸟加/鸟/ss+/无」调整）")
    if fallback_note:
        lines.append(f"ℹ️ {fallback_note}")
    lines.append("")

    if not page_rows:
        lines.append("该区间内没有符合条件的新曲目，试试把目标 Rating 定高一点，或者把门槛放宽（推荐 15000 无）")
        return "\n".join(lines)

    base_index = (page - 1) * top_n
    for offset, item in enumerate(page_rows, 1):
        index = base_index + offset
        diff_label = DIFF_LABELS[item["diff_index"]] if item["diff_index"] < len(DIFF_LABELS) else "?"
        line = (
            f"{index}. {item['title'][:26]} "
            f"id{item['song_id']} {diff_label}{item['level']}({item['ds']:g}) "
            f"拟合{item['fit_diff']:.2f}({item['gap']:+.2f}) 均{item['avg']:.1f}%"
        )
        if item.get("mine") is not None:
            line += f" 你{item['mine']:.2f}%"
        if item.get("gain") is not None:
            line += f" 鸟加{item['target_ra']}ra(+{item['gain']})"
        lines.append(line)

    lines.append(f"…… 第 {page}/{total_pages} 页（每页 {top_n} 条，共 {len(rows)} 条）")

    sort_desc = {
        "吃分": "鸟加收益（能顶掉 b50 末位多少 ra）",
        "综合": "综合（拟合定数差值 + 平均达成率）",
        "拟合": "定数差值（fit_diff - ds）",
        "达成率": "平均达成率",
    }
    lines.append("")
    lines.append(f"📊 排序：{sort_desc.get(sort_mode, sort_mode)}｜输入「推荐帮助」看用法")
    if next_hint:
        lines.append(f"➡️ 下一页：{next_hint}")
    return "\n".join(lines)


# ========== 参数解析 ==========


def parse_options(text: str, event: Event) -> Dict[str, Any]:
    """
    解析命令参数，尽量宽松：
      数字 >= 1000 视为目标 rating；<= 15.5 的数字视为定数（两个则是区间）
      关键词 → 门槛 / 排序方式；其它非数字 → 用户名
    """
    options: Dict[str, Any] = {
        "rating": None,
        "ds_range": None,
        "username": None,
        "qq": None,
        "filter": RECOMMEND_FILTER,
        "sort": RECOMMEND_SORT if RECOMMEND_SORT in SORT_KEYWORDS.values() else None,
        "top_n": RECOMMEND_TOP_N,
        "page": 1,           # 不指定页码时默认第 1 页
        "page_token": None,
        "wrong_page_form": False,
    }

    # @某人 优先
    try:
        for segment in event.get_message():
            if segment.type == "at" and str(segment.data.get("qq")) not in ("all", ""):
                options["qq"] = str(segment.data["qq"])
                break
    except Exception:
        pass

    ds_values: List[float] = []
    plain = (text or "").replace("@", " ").strip()

    for token in plain.split():
        keyword = token.strip().lower()
        if not keyword:
            continue

        # 页码只认「第x页」这一种写法，避免和定数 / Rating 的数字混在一起产生歧义
        page_match = re.fullmatch(r"第(\d{1,3})页", keyword)
        if page_match:
            options["page"] = max(1, int(page_match.group(1)))
            options["page_token"] = token
            continue
        # 写成了「2页」「p2」这种其它形式：给出提示而不是当成用户名
        if re.fullmatch(r"\d{1,3}页", keyword) or re.fullmatch(r"p\d{1,3}", keyword):
            options["wrong_page_form"] = True
            continue

        if keyword in FILTER_KEYWORDS:
            options["filter"] = FILTER_KEYWORDS[keyword]
            continue
        if keyword in SORT_KEYWORDS:
            options["sort"] = SORT_KEYWORDS[keyword]
            continue
        if keyword.startswith("前") and keyword[1:].isdigit():
            options["top_n"] = max(1, min(30, int(keyword[1:])))
            continue

        try:
            value = float(keyword)
        except ValueError:
            if options["username"] is None:
                options["username"] = token.strip()
            continue

        if value >= 1000 and options["rating"] is None:
            options["rating"] = value
        elif value <= 15.5:
            ds_values.append(value)
        elif options["rating"] is None:
            # 1000 以下、15.5 以上：也按目标 rating 处理（例如 800 这种低 rating）
            options["rating"] = value

    if ds_values:
        ds_values.sort()
        if len(ds_values) == 1:
            ds = max(1.0, min(15.0, ds_values[0]))
            options["ds_range"] = (ds, ds)
        else:
            ds_min = max(1.0, min(15.0, ds_values[0]))
            ds_max = max(1.0, min(15.0, ds_values[-1]))
            options["ds_range"] = (ds_min, ds_max)
        options["rating"] = None  # 定了区间就不再按 rating 推区间

    return options


# ========== 命令处理器 ==========

# 保持较低优先级，早于 chat.py 的兜底消息处理；聊天插件现在不会再吞命令。
recommend = on_command("推荐", aliases={"推分推荐"}, priority=6)


@recommend.handle()
async def handle_recommend(bot: Bot, event: Event, args: Message = CommandArg()):
    """推荐推分曲目：不填参数就用当前 QQ 的 rating 自动算"""
    text = args.extract_plain_text()
    options = parse_options(text, event)

    if options["wrong_page_form"]:
        await recommend.finish("❌ 页码要写成「第x页」，例如：推荐 第2页（带上其它参数也行，如「推荐 15000 鸟 第2页」）")
        return

    qq = options["qq"] or (None if options["username"] else str(event.get_user_id()))
    need_player = options["ds_range"] is None or options["filter"] > 0

    player: Optional[Dict[str, Any]] = None
    query_error = ""
    if need_player:
        await recommend.send("🔍 正在查询查分器数据…")
        player, query_error = await fetch_player(qq=qq, username=options["username"])

    # 取用哪个 rating：显式目标 > 玩家当前 rating
    rating_used = options["rating"]
    if rating_used is None and player and isinstance(player.get("rating"), (int, float)):
        rating_used = float(player["rating"])

    if options["ds_range"] is None:
        if rating_used is None:
            message = f"❌ 拿不到 Rating，没法自动推算推荐区间\n原因：{query_error or '未知错误'}"
            message += "\n可以手动指定，例如：「推荐 14.2 14.5」或「推荐 15000」"
            await recommend.finish(message)
            return
        ds_min, ds_max = calc_ds_range(float(rating_used))
    else:
        ds_min, ds_max = options["ds_range"]
        if rating_used is None and player:
            rating_used = float(player.get("rating") or 0)

    music_data = await fetch_music_data()
    chart_stats = await fetch_chart_stats()
    if not music_data or not chart_stats:
        await recommend.finish("❌ 获取查分器歌曲/谱面数据失败，请稍后重试")
        return

    candidates = build_candidates(chart_stats, music_data, ds_min, ds_max)
    if not candidates:
        await recommend.finish(f"❌ 定数 {ds_min:g} ~ {ds_max:g} 区间内没有可用的谱面数据")
        return

    # 吃分推荐：只保留"鸟加后能顶掉 b50 末位"的谱，并按鸟加收益排序
    pools = analyze_player_pools(player) if player else None
    graded = apply_gain(candidates, pools) if pools else []
    use_gain = bool(graded)
    fallback_note = ""
    if pools and not graded:
        fallback_note = "该区间内没有鸟加后能顶掉 b50 末位的谱，下面改按最易推的程度排序"
    sort_mode = options["sort"] or "吃分"
    if sort_mode == "吃分" and not use_gain:
        # 没有 b50 数据（比如手动指定区间 + 不筛），吃分无从计算，退回综合
        sort_mode = "综合"

    achievements = parse_achievements(player) if player else {}
    rows, skipped = rank_candidates(graded if use_gain else candidates, achievements, options["filter"], sort_mode)

    # 不写「第x页」就是第 1 页，和显式写「第1页」完全一致
    page = options["page"]
    top_n = options["top_n"]
    total_pages = max(1, (len(rows) + top_n - 1) // top_n)

    if page > total_pages:
        await recommend.finish(
            f"❌ 没有第 {page} 页：当前条件下共 {total_pages} 页（每页 {top_n} 条，共 {len(rows)} 条）"
        )
        return

    page_rows = rows[(page - 1) * top_n: page * top_n]

    # 下一页提示：把原来的「第x页」换成下一页；@ 别人的情况不给提示，避免点错人
    next_hint: Optional[str] = None
    if page < total_pages and not options["qq"]:
        hint_tokens = text.split()
        if options["page_token"]:
            try:
                hint_tokens.remove(options["page_token"])
            except ValueError:
                pass
        hint_tokens.append(f"第{page + 1}页")
        next_hint = " ".join(["推荐", *hint_tokens])

    # 图片输出的元信息（和文字版用同一批数据，保证图文一致）
    sort_desc_map = {
        "吃分": "吃分（鸟加收益）",
        "综合": "综合（拟合差 + 达成率）",
        "拟合": "定数差值",
        "达成率": "平均达成率",
    }
    meta = {
        "player_name": (player.get("nickname") or player.get("username")) if player else "未绑定查分器",
        "player_rating": float(player["rating"]) if player and isinstance(player.get("rating"), (int, float)) else None,
        "target_rating": float(rating_used) if rating_used is not None else None,
        "ds_min": ds_min,
        "ds_max": ds_max,
        "total_rows": len(rows),
        "sort_mode": sort_mode,
        "sort_desc": sort_desc_map.get(sort_mode, sort_mode),
        "threshold_text": "不筛" if options["filter"] <= 0 else f"筛除 ≥{options['filter']:g}%",
        "skipped": skipped,
        "pools_text": (
            f"b50 末位：旧曲 {pools['old']['floor']}｜新曲 {pools['new']['floor']}"
            if (pools and use_gain) else ""
        ),
        "next_hint": next_hint,
        "page": page,
        "page_size": top_n,
        "total_pages": total_pages,
        "fallback_note": fallback_note,
    }

    if RECOMMEND_IMAGE and page_rows:
        payload: Optional[bytes] = None
        try:
            # 延迟导入：出图依赖新版 B50 的素材与字体
            from src.libraries.recommend_image import image_to_bytes, render_recommend

            image = await render_recommend(page_rows, meta)
            payload = image_to_bytes(image, RECOMMEND_IMAGE_FORMAT, RECOMMEND_IMAGE_QUALITY)
        except Exception as e:
            logger.exception(f"[recommend] 出图失败，回退文字输出：{e}")
        # finish 要放在 try 外面：它靠抛 FinishedException 结束处理器，不能被 except 吞掉
        if payload:
            logger.info(f"[recommend] 已生成推荐图：{len(payload) / 1024:.0f} KB（{page}/{total_pages} 页）")
            await recommend.finish(Message([
                MessageSegment("image", {"file": "base64://" + base64.b64encode(payload).decode()})
            ]))
            return

    message = format_result(
        player=player,
        player_rating=float(player["rating"]) if player and isinstance(player.get("rating"), (int, float)) else None,
        rating_used=float(rating_used) if rating_used is not None else None,
        ds_min=ds_min,
        ds_max=ds_max,
        rows=rows,
        page_rows=page_rows,
        page=page,
        total_pages=total_pages,
        next_hint=next_hint,
        pools=pools if use_gain else None,
        fallback_note=fallback_note,
        threshold=options["filter"],
        sort_mode=sort_mode,
        skipped=skipped,
        total=len(candidates),
        top_n=top_n,
    )
    if query_error and options["ds_range"] is None:
        message += f"\n（注：{query_error}）"

    await recommend.finish(message)


# ========== 帮助命令 ==========

recommend_help = on_command("推荐帮助", aliases={"推分帮助"}, priority=6)


@recommend_help.handle()
async def handle_recommend_help(bot: Bot, event: Event):
    """显示推分推荐帮助"""
    help_text = f"""
📖 推分推荐帮助（数据来源：水鱼查分器，失败自动尝试落雪）

命令格式：
  推荐                      用自己 QQ 的 Rating 自动算推荐区间
  推荐 <目标Rating>          按目标 Rating 算（比如现在 14500 想冲彩框，就写 15000）
  推荐 <定数> <定数上限>      手动指定定数区间
  推荐 @某人                 查被 @ 的人
  推荐 <用户名>              查指定查分器用户名

可叠加的筛选 / 排序参数：
  鸟加 / 鸟 / ss+ / ss / 无   门槛（默认鸟加：筛掉你已 >=100.5% 的曲子；「无」= 不筛）
  吃分 / 综合 / 拟合 / 达成率  排序方式（默认吃分=鸟加收益）
  第x页                     翻页（只认这一种写法，每页 {RECOMMEND_TOP_N} 条；不写默认第 1 页）
  前20                      显示条数（默认 {RECOMMEND_TOP_N} 条，最多 30）

🌰 示例：
  推荐                       # 用自己 Rating 自动推荐
  推荐 15000                 # 目标 Rating 15000
  推荐 15000 鸟              # 目标 15000，只保留还没打鸟的
  推荐 第2页                 # 看第 16~30 条
  推荐 15000 鸟 第3页         # 目标 15000 + 门槛 + 翻页
  推荐 14.2 14.5 无           # 定数 14.2~14.5，不筛已打的曲子
  推荐 前20 达成率            # 自己 Rating，按平均达成率排序，显示 20 条

📏 推荐区间怎么来的：
  平均定数 = round((Rating / (22.4*50)) * 10) / 10
  区间 = [平均定数, 平均定数 + 0.5]（上限 15）

📄 页码说明：
  · 不写「第x页」默认就是第 1 页，和写「推荐 第1页」完全一样
  · 翻页写法：推荐 第2页 = 第 16~30 条，也可以叠加其它参数，如「推荐 15000 鸟 第2页」
  · 页码用的是「第x页」这种写法，所以「推荐 14」依旧表示定数 14，不会混淆

📊 排序依据：
  · 吃分（默认）：rating = (旧曲 Best 35 + 新曲 Best 15) / 50，
    只有鸟加后 ra 超过所在曲池末位的谱才涨分；按"能涨多少 ra"从高到低排，
    每行末尾的「鸟加310ra(+9)」= 该谱鸟加后的单谱 ra，以及能超过末位多少
  · 定数差值：拟合定数 - 官方定数，越小说明相对官标越容易推
  · 平均达成率：该谱面全体玩家的平均达成率，越高说明对多数人越简单
  · 综合：两种排序的名次取平均

💡 说明：
  · 用的是 /query/player 的简略成绩（新版前 15 + 旧版前 25/35），
    所以"已鸟加筛除"只覆盖这部分成绩，b50 之外的成绩判断不到
  · 已排除宴会场谱面与样本数过少（< {MIN_SAMPLE_CNT:g}）的谱面
  · 越级推分不建议，推荐区间内以鸟加为目标推分才最稳
    """
    await recommend_help.finish(help_text.strip())
