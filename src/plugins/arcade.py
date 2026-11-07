# src/plugins/arcade.py
"""
机厅人数上报 / 查询插件（对接 nearcade 街机地图）

命令（机厅别名为 arcade.json 里配置的 1~3 个字母）：
    <别名><人数>        上报当前人数，例如 m1 / mt5
    <别名>j / <别名>几   查询当前人数 + 最近 3 小时的上报记录
  人数必须是 0 ~ 50 的整数（0 表示没人了），其它输入会提示"真是笨蛋呢，人数怎么可能是这个数啦！"

配置文件 arcade.json（放在项目根目录，和 bot.py 同级，也可以放 src/plugins/ 下）：
    [
      {"group_id": 995960263, "name": "魔力空间（上城天街店）", "alias": "m"},
      {"group_id": 995960263, "name": "酷漫地带（成都龙湖上城店）", "alias": "k"}
    ]
  · group_id 群号（字符串或数字都行），alias 别名 1~3 个字母（不区分大小写），
    name 必须和 nearcade 里的机厅名一致（可以在 https://nearcade.cn 搜一下确认）
  · group_id 留空 / 写成 0 表示"所有群通用"

可选配置（.env）：
    NEARCADE_TOKEN=nk_xxx     上报必须；在 https://nearcade.cn/settings/api-tokens 生成
    NEARCADE_BASE=https://nearcade.cn   接口域名，默认 nearcade.cn
    ARCADE_CONFIG=arcade.json 配置文件路径

数据来源：nearcade API（GET /api/shops、GET/POST /api/shops/{id}/attendance）。
上报需要 Bearer 令牌；只查询不需要令牌。
"""

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, Event
from nonebot.log import logger
from src.libraries.config_env import load_project_env

# 让 .env 里的 NEARCADE_TOKEN 等配置生效（nonebot 不会把 .env 写进环境变量）
load_project_env()

BASE_URL = (os.getenv("NEARCADE_BASE") or "https://nearcade.cn").rstrip("/")
API = BASE_URL + "/api"
TOKEN = (os.getenv("NEARCADE_TOKEN") or "").strip()
HEADERS = {
    "User-Agent": "mai-bot-arcade/1.0",
    "Accept": "application/json",
}

CONFIG_PATH = os.getenv("ARCADE_CONFIG") or "arcade.json"
STATE_PATH = Path(os.getenv("ARCADE_STATE") or "data/arcade_state.json")
SHOP_CACHE_TTL = 12 * 3600  # 机厅 id / 机台 id 缓存 12 小时
LOCAL_REPORT_KEEP = 200     # 本地上报记录最多保留条数
REPORT_MAX_HOURS = 3        # 查询时展示最近 3 小时的上报
MAX_PLAYERS = int(os.getenv("ARCADE_MAX_PLAYERS") or 50)  # 单次上报允许的最大人数
TZ = timezone(timedelta(hours=8))

# 消息格式：<别名><人数> 或 <别名>j / <别名>几
MESSAGE_PATTERN = re.compile(r"^\s*([A-Za-z]{1,3})\s*([+-]?\d+(?:\.\d+)?|[jJ]|几|多少|人数|人)\s*$")
QUERY_WORDS = {"j", "J", "几", "多少", "人数", "人"}
INVALID_COUNT_TIP = "真是笨蛋呢，人数怎么可能是这个数啦！"

PRIORITY = 7  # 早于 chat.py 的兜底消息处理


@dataclass
class ArcadeEntry:
    group_id: str
    name: str
    alias: str


_config_cache: Dict[str, Any] = {"mtime": 0.0, "path": None, "entries": []}
_state_cache: Optional[Dict[str, Any]] = None


def _normalize(text: str) -> str:
    """全角转半角、去掉空格和常见括号/符号，用于机厅名比对"""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    for ch in " （）()[]【】<>《》-—_·.,，。:：/\\":
        text = text.replace(ch, "")
    return text


def _find_config_file() -> Optional[Path]:
    candidates = [Path(CONFIG_PATH)]
    if not Path(CONFIG_PATH).is_absolute():
        candidates.append(Path(__file__).parent / CONFIG_PATH)
    for path in candidates:
        if path.exists():
            return path
    return None


def _load_config() -> List[ArcadeEntry]:
    """读 arcade.json（按 mtime 缓存，改完不用重启）"""
    path = _find_config_file()
    if path is None:
        if _config_cache["path"] is not None:
            logger.warning(f"[arcade] 配置文件不见了：{_config_cache['path']}")
        return []

    mtime = path.stat().st_mtime
    if _config_cache["path"] == str(path) and _config_cache["mtime"] == mtime:
        return _config_cache["entries"]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[arcade] 读取 {path} 失败：{e}")
        return _config_cache["entries"]

    if isinstance(raw, dict):
        items = raw.get("arcades") or raw.get("机厅") or raw.get("list") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    entries: List[ArcadeEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        group = item.get("group_id", item.get("group", item.get("群号", "")))
        name = str(item.get("name", item.get("full_name", item.get("全名", ""))) or "").strip()
        alias = str(item.get("alias", item.get("别名", "")) or "").strip()
        if not (name and alias):
            continue
        entries.append(ArcadeEntry(group_id=str(group or "").strip(), name=name, alias=alias[:3]))

    _config_cache.update({"path": str(path), "mtime": mtime, "entries": entries})
    logger.info(f"[arcade] 已加载 {len(entries)} 条机厅配置（{path}）")
    return entries


def _load_state() -> Dict[str, Any]:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    if STATE_PATH.exists():
        try:
            _state_cache = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[arcade] 读取 {STATE_PATH} 失败：{e}")
            _state_cache = {}
    else:
        _state_cache = {}
    _state_cache.setdefault("shops", {})
    _state_cache.setdefault("reports", [])
    return _state_cache


def _save_state() -> None:
    if _state_cache is None:
        return
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(_state_cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"[arcade] 写入 {STATE_PATH} 失败：{e}")


def _color_block(count: int) -> str:
    """人数色块：1-4 绿 / 5-6 黄 / 7-8 橙 / 9+ 红"""
    if count <= 4:
        return "🟩"
    if count <= 6:
        return "🟨"
    if count <= 8:
        return "🟧"
    return "🟥"


def _parse_count(arg: str) -> Optional[int]:
    """
    校验上报人数：必须是不大于 MAX_PLAYERS 的非负整数（允许写 +1，不接受小数/负数/超限）。
    合法返回 int，非法返回 None。
    """
    if not re.fullmatch(r"\+?\d+", str(arg or "").strip()):
        return None
    value = int(str(arg).lstrip("+"))
    if value < 0 or value > MAX_PLAYERS:
        return None
    return value


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> Tuple[bool, Any]:
    """统一请求：返回 (是否成功, 数据或错误信息)"""
    try:
        resp = await client.request(method, API + path, **kwargs)
    except httpx.TimeoutException:
        return False, "请求超时，请稍后再试"
    except httpx.HTTPError as e:
        return False, f"网络请求失败：{e}"

    try:
        data = resp.json()
    except ValueError:
        data = None

    if resp.status_code == 200 and (not isinstance(data, dict) or data.get("success", True)):
        return True, data if data is not None else {}
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or data.get("error") or "")
    if resp.status_code == 401:
        hint = "令牌无效或已过期，请在 .env 里检查 NEARCADE_TOKEN"
        message = f"{message}｜{hint}" if message and message != hint else hint
    elif resp.status_code == 403:
        message = message or "没有权限上报该机厅"
    elif resp.status_code == 404:
        message = message or "机厅不存在（检查 arcade.json 里的全名）"
    return False, message or f"HTTP {resp.status_code}"


async def _resolve_shop(client: httpx.AsyncClient, name: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """把配置里的机厅全名解析成 nearcade 的 shop id + maimai 机台 id"""
    state = _load_state()
    cached = state["shops"].get(name)
    if cached and time.time() - float(cached.get("ts") or 0) < SHOP_CACHE_TTL:
        return cached, ""

    ok, data = await _request(client, "GET", "/shops", params={"q": name, "limit": 10}, headers=HEADERS)
    if not ok:
        return (cached, "") if cached else (None, str(data))

    shops = (data or {}).get("shops") or []
    if not shops:
        return None, f"nearcade 搜不到机厅「{name}」"

    target = _normalize(name)
    shop = next((s for s in shops if _normalize(s.get("name")) == target), None)
    if shop is None:
        shop = next((s for s in shops if target and (target in _normalize(s.get("name")) or _normalize(s.get("name")) in target)), None)
    if shop is None:
        shop = shops[0]

    games = shop.get("games") or []
    maimai = next(
        (g for g in games if g.get("titleId") == 1
         or "maimai" in str(g.get("name") or "").lower()
         or "舞萌" in str(g.get("name") or "")),
        None,
    )
    info = {
        "id": shop.get("id"),
        "name": shop.get("name"),
        "game_id": (maimai or {}).get("gameId"),
        "game_name": (maimai or {}).get("name"),
        "ts": time.time(),
    }
    state["shops"][name] = info
    _save_state()
    logger.info(f"[arcade] 解析机厅「{name}」-> {info['name']}({info['id']}) maimai 机台 {info['game_name']}({info['game_id']})")
    return info, ""


def _record_local_report(shop: Dict[str, Any], count: int, event: Event) -> None:
    """记下本地上报，查询时用来显示"上报者 QQ 昵称"（nearcade 只存自己的用户名）"""
    sender = getattr(event, "sender", None)
    nickname = ""
    if sender is not None:
        nickname = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "")
    state = _load_state()
    state["reports"].append({
        "shop_id": shop.get("id"),
        "game_id": shop.get("game_id"),
        "count": count,
        "qq": str(getattr(event, "user_id", "")),
        "nickname": nickname or str(getattr(event, "user_id", "")),
        "ts": time.time(),
    })
    state["reports"] = state["reports"][-LOCAL_REPORT_KEEP:]
    _save_state()


def _local_nickname(shop_id: Any, reported_at: Optional[datetime]) -> Optional[str]:
    """按时间就近匹配本地记录，取 QQ 昵称"""
    if reported_at is None:
        return None
    target = reported_at.timestamp()
    for item in reversed(_load_state()["reports"]):
        if item.get("shop_id") != shop_id:
            continue
        if abs(float(item.get("ts") or 0) - target) <= 180:
            return str(item.get("nickname") or "")
    return None


def _format_report_time(dt: Optional[datetime], fallback: str = "") -> str:
    if dt is None:
        return fallback or "未知"
    return dt.strftime("%m-%d %H:%M")


def _age_label(age_seconds: float) -> str:
    """距今时间 -> 时间段文案"""
    if age_seconds <= 3600:
        return "1小时内"
    if age_seconds <= 7200:
        return "1-2小时"
    if age_seconds <= 10800:
        return "2-3小时"
    return "3小时以上"


def _latest_report_lines(shop: Dict[str, Any], reported: List[Dict[str, Any]]) -> List[str]:
    """
    只展示最新的一条上报：
        第一行 = 这条上报距今的时间段（1小时内 / 1-2小时 / 2-3小时 / 3小时以上）
        第二行 = 来源：<昵称>｜时间：<真实上报时间>
    """
    now = datetime.now(TZ)
    newest: Optional[Tuple[datetime, Dict[str, Any]]] = None
    for item in reported:
        if shop.get("game_id") and item.get("gameId") != shop.get("game_id"):
            continue
        when = _parse_time(item.get("reportedAt"))
        if when is None:
            continue
        if newest is None or when > newest[0]:
            newest = (when, item)

    if newest is None:
        return []

    when, item = newest
    reporter = item.get("reporter") or {}
    source = (
        _local_nickname(shop.get("id"), when)
        or str(reporter.get("displayName") or reporter.get("name") or item.get("reportedBy") or "未知")
    )
    return [
        _age_label((now - when).total_seconds()),
        f"来源：{source}｜时间：{_format_report_time(when)}",
    ]


def _match_entry(event: Event, text: str) -> Optional[Tuple[ArcadeEntry, str]]:
    """按群号 + 别名匹配配置，返回 (条目, 参数)"""
    match = MESSAGE_PATTERN.match(text)
    if not match:
        return None
    alias, arg = match.group(1), match.group(2)
    group_id = str(getattr(event, "group_id", "") or "")
    entries = _load_config()
    for entry in sorted(entries, key=lambda e: -len(e.alias)):  # 长别名优先，避免 abc 与 ab 撞车
        if entry.alias.lower() != alias.lower():
            continue
        if entry.group_id and group_id and entry.group_id != group_id:
            continue
        if entry.group_id and not group_id:  # 配了群号的只在群里生效
            continue
        return entry, arg
    return None


arcade = on_message(priority=PRIORITY, block=False)


@arcade.handle()
async def handle_arcade(bot: Bot, event: Event):
    """监听 <别名><人数> / <别名>j 这样的消息"""
    text = event.get_plaintext().strip()
    if not text or len(text) > 12:
        return

    matched = _match_entry(event, text)
    if matched is None:
        return
    entry, arg = matched

    # 先校验人数（在联网之前），非法就直接怼回去
    count = None
    if arg not in QUERY_WORDS:
        count = _parse_count(arg)
        if count is None:
            await arcade.finish(INVALID_COUNT_TIP)
            return

    async with httpx.AsyncClient(timeout=15.0) as client:
        shop, error = await _resolve_shop(client, entry.name)
        if shop is None:
            await arcade.finish(f"❌ {error}")
            return

        if arg in QUERY_WORDS:
            await _query_and_reply(client, shop)
            return

        await _report_and_reply(client, shop, int(count), event)


async def _report_and_reply(client: httpx.AsyncClient, shop: Dict[str, Any], count: int, event: Event) -> None:
    if not TOKEN:
        await arcade.finish(
            "❌ 未配置上报令牌：在 .env 里加一行 NEARCADE_TOKEN=nk_xxx（"
            f"https://nearcade.cn/settings/api-tokens 生成）后才能上报"
        )
        return
    if not shop.get("game_id"):
        await arcade.finish(f"❌ 机厅「{shop.get('name')}」在 nearcade 里没有 maimai 机台，无法上报")
        return

    sender = getattr(event, "sender", None)
    nickname = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "") if sender else ""
    payload = {
        "games": [{"id": shop["game_id"], "currentAttendances": count}],
        "comment": f"上报自 QQ {getattr(event, 'user_id', '')}{('（' + nickname + '）') if nickname else ''}",
    }
    ok, data = await _request(
        client,
        "POST",
        f"/shops/{shop['id']}/attendance",
        headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"},
        json=payload,
    )
    if not ok:
        logger.warning(f"[arcade] 上报失败：{data}")
        await arcade.finish(f"❌ 上报失败：{data}")
        return

    _record_local_report(shop, count, event)
    logger.info(f"[arcade] 已上报 {shop.get('name')} {count} 人（QQ {getattr(event, 'user_id', '')}）")
    await arcade.finish(f"✅ 更新成功，已经上传nearcade\n{shop.get('name')}\n当前：{count} 人")


async def _query_and_reply(client: httpx.AsyncClient, shop: Dict[str, Any]) -> None:
    ok, data = await _request(
        client,
        "GET",
        f"/shops/{shop['id']}/attendance",
        params={"reported": "true"},
        headers=HEADERS,
    )
    if not ok:
        await arcade.finish(f"❌ 查询失败：{data}")
        return

    games = (data or {}).get("games") or []
    mine = next((g for g in games if g.get("gameId") == shop.get("game_id")), None)
    count = int((mine or {}).get("total") or 0) if mine else int((data or {}).get("total") or 0)

    lines = [str(shop.get("name")), f"当前：{count}人 {_color_block(count)}"]
    latest = _latest_report_lines(shop, (data or {}).get("reported") or [])
    if latest:
        lines.extend(latest)
    else:
        lines.append("暂无上报记录")

    await arcade.finish("\n".join(lines))
