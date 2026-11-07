# src/libraries/player_source.py
"""
玩家 B50 数据源：水鱼查分器优先，失败后回退落雪查分器。

对外只暴露 fetch_player_any(qq=..., username=...)，返回的数据结构与水鱼
/query/player 兼容，现有 B50 绘图、推荐、底力分析、上分模拟可以共用。

落雪 API 文档：
    https://maimai.lxns.net/docs/api/maimai
    GET /api/v0/maimai/player/qq/{qq}
    GET /api/v0/maimai/player/{friend_code}/bests

落雪开发者 API 需要配置开发者 Token：
    LXNS_DEVELOPER_TOKEN=...
（也兼容 LXNS_API_KEY）
"""

import asyncio
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot.log import logger

from src.libraries import accounts
from src.libraries.config_env import load_project_env
from src.libraries.maimaidx_music import total_list
from src.libraries.oauth_accounts import fetch_divingfish_oauth, fetch_lxns_oauth

load_project_env()

DIVINGFISH_API = "https://www.diving-fish.com/api/maimaidxprober/query/player"
DIVINGFISH_HEADERS = {
    "User-Agent": "mai-bot/1.0",
    "Referer": "https://www.diving-fish.com/maimaidx/prober/",
}
LXNS_BASE = (os.getenv("LXNS_BASE") or "https://maimai.lxns.net/api/v0").rstrip("/")
LXNS_TOKEN = (
    os.getenv("LXNS_DEVELOPER_TOKEN")
    or os.getenv("LXNS_API_KEY")
    or ""
).strip()
TIMEOUT = 20.0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _compute_ra(ds: float, achievement: float) -> int:
    """落雪偶尔没有 dx_rating 时的本地兜底公式"""
    table = (
        (100.5, 22.4), (100.0, 21.6), (99.5, 21.1), (99.0, 20.8),
        (98.0, 20.3), (97.0, 20.0), (94.0, 16.8), (90.0, 15.2),
        (80.0, 13.6), (75.0, 12.0), (70.0, 11.2), (60.0, 9.6),
        (50.0, 8.0),
    )
    factor = 7.0
    for threshold, value in table:
        if achievement >= threshold:
            factor = value
            break
    return math.floor(ds * (min(100.5, achievement) / 100) * factor)


def _song_meta(song_id: int, level_index: int) -> Dict[str, Any]:
    """从现有水鱼曲库补曲名、定数、等级；找不到就返回空字段"""
    music = total_list.by_id(str(song_id)) or total_list.by_id(str(song_id + 10000))
    if music is None:
        return {"title": "", "ds": 0.0, "level": ""}
    levels = list(music.get("level") or [])
    ds_list = list(music.get("ds") or [])
    return {
        "title": str(music.get("title") or ""),
        "ds": _float(ds_list[level_index]) if level_index < len(ds_list) else 0.0,
        "level": str(levels[level_index]) if level_index < len(levels) else "",
    }


def _normalize_lxns_score(score: Dict[str, Any], chart_type: str) -> Optional[Dict[str, Any]]:
    """把落雪 Score 转成水鱼 /query/player 的 score 结构"""
    if not isinstance(score, dict):
        return None
    song_id = _int(score.get("id"), -1)
    level_index = _int(score.get("level_index"), -1)
    if song_id < 0 or level_index < 0:
        return None
    # 落雪的谱面 id 可能带 10000（标准/ DX 同 id），宴会场 > 100000
    base_id = song_id % 10000 if song_id > 10000 else song_id
    meta = _song_meta(base_id, level_index)
    achievement = _float(score.get("achievements"), 0.0)
    ds = _float(meta["ds"], 0.0)
    ra = _int(score.get("dx_rating"), 0)
    if ra <= 0 and ds > 0:
        ra = _compute_ra(ds, achievement)
    return {
        "song_id": str(base_id),
        "title": meta["title"],
        "type": "DX" if chart_type == "dx" else "SD",
        "level_index": level_index,
        "level": meta["level"],
        "ds": ds,
        "achievements": achievement,
        "ra": ra,
        "fc": str(score.get("fc") or ""),
        "fs": str(score.get("fs") or ""),
        "rate": str(score.get("rate") or ""),
        "dxScore": _int(score.get("dx_score"), 0),
    }


async def fetch_divingfish(qq: Optional[str] = None, username: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """水鱼查分器 B50"""
    if not qq and not username:
        return None, "没有可查询的账号"

    payload: Dict[str, Any] = {"b50": "1"}
    if username:
        payload["username"] = username
    else:
        payload["qq"] = str(qq)

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(DIVINGFISH_API, json=payload, headers=DIVINGFISH_HEADERS)
    except httpx.TimeoutException:
        return None, "水鱼查询超时"
    except Exception as exc:  # noqa: BLE001
        return None, f"水鱼请求失败：{exc}"

    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            return None, "水鱼返回了非 JSON 数据"
        if not isinstance(data, dict):
            return None, "水鱼返回数据格式异常"
        data.setdefault("nickname", data.get("username") or "")
        data["_source"] = "diving-fish"
        return data, ""

    detail = ""
    try:
        detail = str((response.json() or {}).get("message") or "")
    except Exception:  # noqa: BLE001
        detail = response.text[:100]

    if response.status_code == 400:
        target = f"用户名 {username}" if username else f"QQ {qq}"
        return None, f"水鱼没有找到 {target}（{detail or 'no such user'}）"
    if response.status_code == 403:
        return None, "水鱼账号设置了隐私或未同意用户协议"
    return None, f"水鱼查询失败：HTTP {response.status_code} {detail}".strip()


def _lxns_headers() -> Dict[str, str]:
    return {"Authorization": LXNS_TOKEN, "Accept": "application/json"}


def _lxns_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    message = str(payload.get("message") or "").strip()
    if response.status_code == 401:
        return "落雪开发者 Token 无效或已过期"
    if response.status_code == 403:
        return "落雪玩家未授权第三方读取成绩"
    if response.status_code == 404:
        return "落雪没有找到该玩家"
    if response.status_code == 429:
        return "落雪请求过于频繁，请稍后重试"
    return f"落雪查询失败：HTTP {response.status_code} {message}".strip()


async def _lxns_get(client: httpx.AsyncClient, path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """兼容落雪统一响应结构 {success, code, message, data}"""
    last_error = ""
    for attempt in range(2):
        try:
            response = await client.get(LXNS_BASE + path, headers=_lxns_headers())
        except httpx.TimeoutException:
            last_error = "落雪查询超时"
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = f"落雪请求失败：{exc}"
            continue

        if response.status_code == 429 and attempt == 0:
            await asyncio.sleep(1.0)
            continue
        if response.status_code != 200:
            return None, _lxns_error(response)
        try:
            payload = response.json()
        except ValueError:
            return None, "落雪返回了非 JSON 数据"
        if not isinstance(payload, dict) or not payload.get("success", False):
            message = str((payload or {}).get("message") or "落雪返回失败")
            return None, message
        data = payload.get("data")
        return data if isinstance(data, dict) else None, ""

    return None, last_error or "落雪查询失败"


async def fetch_lxns(qq: Optional[str] = None, username: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """落雪查分器 B50；QQ 或好友码（username 为纯数字时按好友码处理）"""
    if not LXNS_TOKEN:
        return None, "未配置 LXNS_DEVELOPER_TOKEN，无法使用落雪查询"

    friend_code: Optional[str] = None
    player_name = ""
    player_rating = 0

    try:
        client = httpx.AsyncClient(timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 本机代理配置异常等
        return None, f"落雪请求初始化失败：{exc}"

    async with client:
        if qq:
            info, error = await _lxns_get(client, f"/maimai/player/qq/{qq}")
            if info is None:
                return None, error
            friend_code = str(info.get("friend_code") or "")
            player_name = str(info.get("name") or "")
            player_rating = _int(info.get("rating"), 0)
        elif username:
            if username.isdigit():
                friend_code = username
            else:
                return None, "落雪只支持 QQ 号或好友码查询，不支持用户名"
        else:
            return None, "没有可查询的账号"

        if not friend_code:
            return None, "落雪没有拿到好友码"

        bests, error = await _lxns_get(client, f"/maimai/player/{friend_code}/bests")
        if bests is None:
            return None, error

    standard_raw = bests.get("standard") or []
    dx_raw = bests.get("dx") or []
    standard = [item for raw in standard_raw if (item := _normalize_lxns_score(raw, "standard"))]
    dx = [item for raw in dx_raw if (item := _normalize_lxns_score(raw, "dx"))]
    if not standard and not dx:
        return None, "落雪没有返回 B50 成绩"

    total_ra = sum(_int(item.get("ra"), 0) for item in standard + dx)
    data = {
        "nickname": player_name or str(username or qq or ""),
        "username": player_name or str(username or qq or ""),
        "rating": player_rating or total_ra,
        "charts": {"sd": standard, "dx": dx},
        "_source": "lxns",
        "_friend_code": friend_code,
    }
    return data, ""


async def fetch_player_any(
    qq: Optional[str] = None,
    username: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    按用户的数据源偏好查询 B50：

    水鱼：公开 /query/player → 水鱼 OAuth
    落雪：落雪 OAuth → 落雪开发者 Token / 好友码

    source=auto 时水鱼优先；指定数据源时先查指定源，失败仍会回退另一个源。
    返回的数据结构与水鱼 /query/player 的 b50 结果兼容，额外带 _source 字段。
    """
    errors: List[str] = []

    if not qq:
        data, error = await fetch_divingfish(username=username)
        if data is not None:
            return data, ""
        errors.append(error or "水鱼查询失败")
        data, error = await fetch_lxns(username=username)
        if data is not None:
            return data, ""
        errors.append(error or "落雪查询失败")
        return None, "；".join(errors)

    account = await accounts.get_user(qq)
    source = str(account.get("source") or "auto")
    has_df = isinstance(account.get("divingfish"), dict)
    has_lx = isinstance(account.get("lxns"), dict)

    async def try_divingfish() -> Optional[Dict[str, Any]]:
        data, error = await fetch_divingfish(qq=qq)
        if data is not None:
            return data
        errors.append(error or "水鱼查询失败")
        if has_df:
            data, error = await fetch_divingfish_oauth(qq)
            if data is not None:
                return data
            errors.append(error or "水鱼 OAuth 查询失败")
        return None

    async def try_lxns() -> Optional[Dict[str, Any]]:
        # 落雪开发者 /bests 已经按“旧版本 B35 / 当前版本 B15”分好组，
        # 是最可靠的来源；失败后再退回 OAuth 全成绩自行分池。
        data, error = await fetch_lxns(qq=qq)
        if data is not None:
            return data
        errors.append(error or "落雪开发者查询失败")
        if has_lx:
            data, error = await fetch_lxns_oauth(qq)
            if data is not None:
                return data
            errors.append(error or "落雪 OAuth 查询失败")
        return None

    order = [try_divingfish, try_lxns]
    if source == "lxns":
        order = [try_lxns, try_divingfish]

    for fetch in order:
        data = await fetch()
        if data is not None:
            logger.info(f"[player] 数据源={source}，实际使用={data.get('_source')}")
            return data, ""

    return None, "；".join(errors)
