# src/plugins/subscribe.py
"""
订阅中心：服务器状态 / 新曲 / 天气。

命令：
    订阅 状态
    订阅 新曲
    订阅 天气 杭州
    订阅列表
    取消订阅 状态 / 新曲 / 天气 / 全部

推送规则：
- 状态：只在「正常 -> 降级/宕机等异常」时推送，避免重复刷屏；
- 新曲：定时对比水鱼 music_data，发现新 id 时推送曲名、难度，本地有官谱时附带 DXTag 标签；
- 天气：按小时检查今明两天预报，只在降雨/高温/雾霾大风等异常时推送。

实现上用 asyncio 后台循环，不额外依赖 APScheduler。订阅配置保存在
data/subscriptions.json；重启后订阅关系保留。
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot import get_bot, get_driver, on_command
from nonebot.adapters.onebot.v11 import Event, Message
from nonebot.log import logger
from nonebot.params import CommandArg

from src.libraries import maidata_store
from src.libraries.config_env import load_project_env
from src.plugins.recommend import DIFF_LABELS, fetch_music_data
from src.plugins.status import API_URL as STATUS_API, LEVEL_STYLE, VERDICT_STYLE
from src.plugins.weather import AMAP_API_KEY, CITY_CODE_MAP, DEFAULT_CITY

load_project_env()

ROOT = Path(__file__).resolve().parents[2]
SUB_PATH = ROOT / "data" / "subscriptions.json"

STATUS_INTERVAL = 60           # 状态检查间隔
NEW_SONG_INTERVAL = 30 * 60    # 新曲检查间隔
WEATHER_INTERVAL = 60 * 60     # 天气检查间隔
MAX_NEW_SONGS_PER_PUSH = 10

NORMAL_KEYS = {"normal", "ok", "healthy", "recovering"}
ALERT_KEYS = {
    "degraded", "outage", "down", "error", "unstable",
    "warning", "partial", "maintenance",
}
TAG_AXES = ["键盘", "星星", "技巧", "体力", "爆发"]

_SUBS: Optional[Dict[str, Any]] = None
_SUBS_LOCK = asyncio.Lock()
_TASK: Optional[asyncio.Task] = None


# ========== 订阅数据 ==========

def _empty_subs() -> Dict[str, Any]:
    return {"groups": {}, "known_song_ids": [], "updated_at": ""}


def _normalize_subs(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _empty_subs()
    data.setdefault("groups", {})
    data.setdefault("known_song_ids", [])
    if not isinstance(data["groups"], dict):
        data["groups"] = {}
    if not isinstance(data["known_song_ids"], list):
        data["known_song_ids"] = []
    return data


def _read_subs_sync() -> Dict[str, Any]:
    if not SUB_PATH.is_file():
        return _empty_subs()
    try:
        return _normalize_subs(json.loads(SUB_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _empty_subs()


def _write_subs_sync(data: Dict[str, Any]) -> None:
    SUB_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = SUB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SUB_PATH)


async def _load_subs() -> Dict[str, Any]:
    global _SUBS
    if _SUBS is None:
        _SUBS = await asyncio.to_thread(_read_subs_sync)
    return _SUBS


async def _save_subs(data: Dict[str, Any]) -> None:
    global _SUBS
    _SUBS = data
    try:
        await asyncio.to_thread(_write_subs_sync, data)
    except OSError as exc:
        logger.warning(f"[subscribe] 写入订阅配置失败：{exc}")


def _group_id(event: Event) -> Optional[str]:
    group_id = getattr(event, "group_id", None)
    return str(group_id) if group_id else None


def _enabled(group: Dict[str, Any], key: str) -> bool:
    value = group.get(key)
    return isinstance(value, dict) and bool(value.get("enabled"))


def _has_subscribers(data: Dict[str, Any], key: str) -> bool:
    return any(_enabled(group, key) for group in (data.get("groups") or {}).values())


# ========== 发送 ==========

async def _send_group(group_id: str, text: str) -> bool:
    try:
        bot = get_bot()
        await bot.send_group_msg(group_id=int(group_id), message=Message(text))
        return True
    except Exception as exc:  # noqa: BLE001 - 单个群发送失败不应该中断整轮检查
        logger.warning(f"[subscribe] 推送到群 {group_id} 失败：{exc}")
        return False


# ========== 状态订阅 ==========

def _status_key(data: Dict[str, Any]) -> str:
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict:
        return verdict
    for key in ("incident_level", "status"):
        value = str(data.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def _status_label(key: str) -> str:
    style = VERDICT_STYLE.get(key) or LEVEL_STYLE.get(key)
    return style[0] if style else (key or "未知")


async def _fetch_status() -> Optional[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(STATUS_API)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[subscribe] 获取服务器状态失败：{exc}")
        return None


def _format_status_alert(data: Dict[str, Any], current: str) -> str:
    label = _status_label(current)
    summary = str(data.get("summary") or data.get("status_text") or "").strip()
    lines = [f"⚠️ 舞萌DX 服务器状态异常：正常 → {label}"]
    if summary:
        lines.append(summary)
    bad_services = []
    for service in data.get("services") or []:
        if not isinstance(service, dict):
            continue
        state = str(service.get("state") or "").lower()
        if state and state not in ("ok", "normal"):
            bad_services.append(str(service.get("name") or service.get("key") or "未知"))
    if bad_services:
        lines.append("异常服务：" + "、".join(bad_services[:6]))
    updated = str(data.get("timestamp") or "").strip()
    if updated:
        lines.append(f"数据时间：{updated}")
    lines.append("发送「status」查看详情")
    return "\n".join(lines)


async def _check_status() -> None:
    data = await _load_subs()
    groups = data.get("groups") or {}
    if not groups or not _has_subscribers(data, "status"):
        return

    status = await _fetch_status()
    if not status:
        return
    current = _status_key(status)
    if not current:
        return

    changed = False
    for group_id, group in groups.items():
        if not isinstance(group, dict):
            continue
        sub = group.get("status")
        if not isinstance(sub, dict) or not sub.get("enabled"):
            continue

        previous = str(sub.get("last_verdict") or "").strip().lower()
        if not previous:
            # 第一次检查只记录基线，不推送，避免启动就误报
            sub["last_verdict"] = current
            changed = True
            continue

        if previous in NORMAL_KEYS and current in ALERT_KEYS:
            if await _send_group(group_id, _format_status_alert(status, current)):
                sub["last_verdict"] = current
                changed = True
            continue

        if previous != current:
            sub["last_verdict"] = current
            changed = True

    if changed:
        await _save_subs(data)


# ========== 新曲订阅 ==========

async def _format_new_songs(items: List[Dict[str, Any]], total_new: int) -> str:
    lines = [f"🆕 水鱼曲库更新：新增 {total_new} 首"]
    if total_new > len(items):
        lines.append(f"（只列出前 {len(items)} 首）")

    tag_map: Dict[int, Dict[str, float]] = {}
    try:
        await asyncio.to_thread(maidata_store.build_index)
        jobs = []
        positions = []
        for index, item in enumerate(items):
            try:
                song_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            levels = item.get("level") or []
            if not levels:
                continue
            diff_index = len(levels) - 1
            chart = maidata_store.find_chart(
                song_id,
                str(item.get("type") or ""),
                str(item.get("title") or ""),
                diff_index + 2,
            )
            if chart is not None:
                jobs.append((chart, diff_index + 2))
                positions.append(index)
        if jobs:
            scores = await asyncio.to_thread(maidata_store.score_slots, jobs)
            for position, score in zip(positions, scores):
                if score:
                    tag_map[position] = score
    except Exception as exc:  # noqa: BLE001 - 标签只是附加信息
        logger.warning(f"[subscribe] 计算新曲标签失败：{exc}")

    for index, item in enumerate(items):
        title = str(item.get("title") or "未知曲目")
        song_id = item.get("id")
        music_type = str(item.get("type") or "")
        levels = item.get("level") or []
        ds_list = item.get("ds") or []

        diffs = []
        for diff_index, level in enumerate(levels):
            if diff_index >= len(ds_list):
                break
            label = DIFF_LABELS[diff_index] if diff_index < len(DIFF_LABELS) else "?"
            try:
                ds = float(ds_list[diff_index])
            except (TypeError, ValueError):
                ds = 0.0
            diffs.append(f"{label}{level}({ds:g})")

        line = f"{index + 1}. id{song_id} {title[:40]}"
        if music_type:
            line += f" [{music_type}]"
        lines.append(line)
        if diffs:
            lines.append("   难度：" + " / ".join(diffs))
        score = tag_map.get(index)
        if score:
            tag_text = "｜".join(f"{axis} {float(score.get(axis, 0)):.1f}" for axis in TAG_AXES)
            lines.append("   标签：" + tag_text)

    lines.append("发送「id<编号>」查看曲目详情")
    return "\n".join(lines)


async def _check_new_songs() -> None:
    data = await _load_subs()
    if not _has_subscribers(data, "new_songs"):
        return

    music_data = await fetch_music_data()
    if not music_data:
        return

    current_ids = {str(key) for key in music_data}
    known_ids = {str(value) for value in (data.get("known_song_ids") or [])}

    if not known_ids:
        data["known_song_ids"] = sorted(current_ids)
        await _save_subs(data)
        return

    new_ids = current_ids - known_ids
    if not new_ids:
        return

    ordered = sorted(new_ids, key=lambda value: int(value) if value.isdigit() else 0)
    items = [music_data[song_id] for song_id in ordered[:MAX_NEW_SONGS_PER_PUSH] if song_id in music_data]
    if not items:
        data["known_song_ids"] = sorted(current_ids)
        await _save_subs(data)
        return

    text = await _format_new_songs(items, len(ordered))
    for group_id, group in (data.get("groups") or {}).items():
        if isinstance(group, dict) and _enabled(group, "new_songs"):
            await _send_group(group_id, text)

    data["known_song_ids"] = sorted(current_ids)
    await _save_subs(data)


# ========== 天气订阅 ==========

def _canonical_city(city: Any, city_code: Any) -> str:
    """按 adcode 反查标准城市名，顺手修复历史数据里的乱码城市名"""
    code = str(city_code or "")
    for name, mapped in CITY_CODE_MAP.items():
        if mapped == code:
            return name
    return str(city or "").strip() or DEFAULT_CITY


def _resolve_city(name: str) -> Tuple[Optional[str], Optional[str]]:
    raw = (name or "").strip() or DEFAULT_CITY
    if raw in CITY_CODE_MAP:
        return raw, CITY_CODE_MAP[raw]
    for city, code in CITY_CODE_MAP.items():
        if raw in city or city in raw:
            return city, code
    return None, None


async def _fetch_forecast(city_code: str) -> List[Dict[str, Any]]:
    params = {
        "key": AMAP_API_KEY,
        "city": city_code,
        "extensions": "all",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
    if str(payload.get("status")) != "1":
        raise ValueError(str(payload.get("info") or "高德天气接口返回异常"))
    forecasts = payload.get("forecasts") or []
    if not forecasts:
        return []
    return list((forecasts[0] or {}).get("casts") or [])


def _format_weather_alert(city: str, casts: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    alert_lines: List[str] = []
    key_parts: List[str] = []
    advices: List[str] = []

    for index, cast in enumerate(casts[:2]):
        if not isinstance(cast, dict):
            continue
        date = str(cast.get("date") or "")
        day_weather = str(cast.get("dayweather") or "")
        night_weather = str(cast.get("nightweather") or "")
        weather_text = f"{day_weather}{night_weather}"
        day_temp = str(cast.get("daytemp") or "")
        night_temp = str(cast.get("nighttemp") or "")

        hazards: List[str] = []
        if any(word in weather_text for word in ("雨", "雪", "雷", "冰雹")):
            hazards.append("降水")
            if not any("带伞" in item for item in advices):
                advices.append("☔ 有降水，记得带伞")
        try:
            high = max(int(day_temp), int(night_temp))
        except (TypeError, ValueError):
            high = None
        if high is not None and high >= 35:
            hazards.append("高温")
            if not any("防暑" in item for item in advices):
                advices.append("🥵 高温，注意防暑补水")
        if any(word in weather_text for word in ("大风", "台风", "沙尘", "雾", "霾", "浮尘", "扬沙")):
            hazards.append("异常天气")
            if not any("出行安全" in item for item in advices):
                advices.append("⚠️ 天气异常，注意出行安全")

        if not hazards:
            continue

        label = "今天" if index == 0 else "明天"
        short_date = date[-5:] if len(date) >= 5 else date
        try:
            low = min(int(day_temp), int(night_temp))
            high = max(int(day_temp), int(night_temp))
            temp_text = f"{low}~{high}°C"
        except (TypeError, ValueError):
            temp_text = f"{day_temp}~{night_temp}°C"
        alert_lines.append(
            f"{label} {short_date}：{day_weather}/{night_weather}，{temp_text}"
        )
        key_parts.append(f"{date}:{','.join(hazards)}")

    if not alert_lines:
        return None, ""

    text = f"🌦️ 天气提醒｜{city}\n" + "\n".join(alert_lines)
    if advices:
        text += "\n" + "\n".join(advices)
    return text, "|".join(key_parts)


async def _check_weather() -> None:
    data = await _load_subs()
    groups = data.get("groups") or {}
    if not groups or not _has_subscribers(data, "weather"):
        return

    forecast_cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    changed = False

    for group_id, group in groups.items():
        if not isinstance(group, dict):
            continue
        sub = group.get("weather")
        if not isinstance(sub, dict) or not sub.get("enabled"):
            continue
        city_code = str(sub.get("city_code") or "")
        city = _canonical_city(sub.get("city"), city_code)
        if city != str(sub.get("city") or ""):
            sub["city"] = city
            changed = True
        if not city_code:
            city, city_code = _resolve_city(city)
            if not city_code:
                continue
            sub["city"] = city
            sub["city_code"] = city_code
            changed = True

        if city_code not in forecast_cache:
            try:
                forecast_cache[city_code] = await _fetch_forecast(city_code)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[subscribe] 获取 {city} 天气失败：{exc}")
                forecast_cache[city_code] = None

        casts = forecast_cache.get(city_code)
        if not casts:
            continue

        text, alert_key = _format_weather_alert(city, casts)
        if not text:
            if sub.get("last_alert"):
                sub["last_alert"] = ""
                changed = True
            continue

        if alert_key == sub.get("last_alert"):
            continue

        if await _send_group(group_id, text):
            sub["last_alert"] = alert_key
            changed = True

    if changed:
        await _save_subs(data)


# ========== 后台循环 ==========

async def _subscribe_loop() -> None:
    await asyncio.sleep(15)
    last_new_song = 0.0
    last_weather = 0.0

    while True:
        try:
            await _check_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[subscribe] 状态检查异常：{exc}")

        now = time.monotonic()
        if now - last_new_song >= NEW_SONG_INTERVAL:
            last_new_song = now
            try:
                await _check_new_songs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[subscribe] 新曲检查异常：{exc}")

        if now - last_weather >= WEATHER_INTERVAL:
            last_weather = now
            try:
                await _check_weather()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[subscribe] 天气检查异常：{exc}")

        await asyncio.sleep(STATUS_INTERVAL)


@get_driver().on_startup
async def _start_subscribe_loop() -> None:
    global _TASK
    if _TASK is None or _TASK.done():
        _TASK = asyncio.create_task(_subscribe_loop())
        logger.info("[subscribe] 订阅检查循环已启动")


@get_driver().on_shutdown
async def _stop_subscribe_loop() -> None:
    global _TASK
    if _TASK is not None:
        _TASK.cancel()
        _TASK = None


# ========== 命令 ==========

HELP_TEXT = """【订阅中心】
订阅 状态              服务器从正常变为降级/宕机时推送
订阅 新曲              水鱼曲库出现新曲时推送曲名、难度、本地标签
订阅 天气 杭州         降雨/高温/雾霾大风等异常天气提醒（不写城市默认杭州）
订阅列表               查看本群订阅
取消订阅 状态/新曲/天气/全部

说明：
· 订阅按群保存，重启后仍然有效；
· 状态只在「正常 -> 异常」时推送，不会重复刷屏；
· 天气每小时检查一次，同一类预警当天只推一次。"""


subscribe = on_command("订阅", priority=6)
subscribe_list = on_command("订阅列表", aliases={"订阅查看", "我的订阅"}, priority=6)
subscribe_help = on_command("订阅帮助", aliases={"订阅说明"}, priority=6)
unsubscribe = on_command("取消订阅", aliases={"退订"}, priority=6)


def _format_group_subscriptions(group: Dict[str, Any]) -> str:
    if not group:
        return "📭 本群还没有订阅，发送「订阅 状态」「订阅 新曲」或「订阅 天气 杭州」添加。"

    lines = ["📬 本群订阅"]
    status = group.get("status") or {}
    if status.get("enabled"):
        last = str(status.get("last_verdict") or "")
        extra = f"（最近：{_status_label(last)}）" if last else ""
        lines.append(f"· 服务器状态：已订阅{extra}")
    else:
        lines.append("· 服务器状态：未订阅")

    new_songs = group.get("new_songs") or {}
    lines.append(f"· 新曲：{'已订阅' if new_songs.get('enabled') else '未订阅'}")

    weather = group.get("weather") or {}
    if weather.get("enabled"):
        lines.append(f"· 天气：已订阅（{_canonical_city(weather.get('city'), weather.get('city_code'))}）")
    else:
        lines.append("· 天气：未订阅")

    lines.append("")
    lines.append("发送「订阅 状态 / 新曲 / 天气 城市」添加，发送「取消订阅 ...」移除。")
    return "\n".join(lines)


@subscribe_help.handle()
async def _handle_help():
    await subscribe_help.finish(HELP_TEXT)


@subscribe.handle()
async def _handle_subscribe(event: Event, args: Message = CommandArg()):
    group_id = _group_id(event)
    if group_id is None:
        await subscribe.finish("订阅只能在群里设置哦。")

    text = args.extract_plain_text().strip()
    if not text or text in ("帮助", "help", "说明"):
        await subscribe.finish(HELP_TEXT)

    parts = text.split()
    kind = parts[0].casefold()

    if kind in ("列表", "查看"):
        data = await _load_subs()
        await subscribe.finish(_format_group_subscriptions((data.get("groups") or {}).get(group_id) or {}))

    async with _SUBS_LOCK:
        data = await _load_subs()
        groups = data.setdefault("groups", {})
        group = groups.setdefault(group_id, {})

        if kind in ("状态", "服务器", "status"):
            current = group.get("status") if isinstance(group.get("status"), dict) else {}
            current["enabled"] = True
            group["status"] = current
            message = "✅ 已订阅服务器状态：正常变为降级/宕机时会推送。"

        elif kind in ("新曲", "新歌", "曲库", "歌曲"):
            group["new_songs"] = {"enabled": True}
            message = "✅ 已订阅新曲：水鱼曲库出现新曲时会推送。"

        elif kind in ("天气", "weather"):
            city_input = " ".join(parts[1:]).strip() or DEFAULT_CITY
            city, city_code = _resolve_city(city_input)
            if not city_code:
                await subscribe.finish(
                    f"❌ 没有找到城市「{city_input}」的编码，可以换一个城市名试试。"
                )
            group["weather"] = {
                "enabled": True,
                "city": city,
                "city_code": city_code,
                "last_alert": "",
            }
            message = f"✅ 已订阅天气：{city}（降雨/高温/雾霾大风等异常时提醒）。"

        else:
            await subscribe.finish(HELP_TEXT)

        await _save_subs(data)

    await subscribe.finish(message)


@subscribe_list.handle()
async def _handle_list(event: Event):
    group_id = _group_id(event)
    if group_id is None:
        await subscribe_list.finish("订阅列表只能在群里看哦。")
    data = await _load_subs()
    await subscribe_list.finish(
        _format_group_subscriptions((data.get("groups") or {}).get(group_id) or {})
    )


@unsubscribe.handle()
async def _handle_unsubscribe(event: Event, args: Message = CommandArg()):
    group_id = _group_id(event)
    if group_id is None:
        await unsubscribe.finish("取消订阅只能在群里操作哦。")

    text = args.extract_plain_text().strip()
    if not text:
        await unsubscribe.finish("用法：取消订阅 状态 / 新曲 / 天气 / 全部")

    kind = text.split()[0].casefold()
    async with _SUBS_LOCK:
        data = await _load_subs()
        groups = data.get("groups") or {}
        group = groups.get(group_id)
        if not isinstance(group, dict) or not group:
            await unsubscribe.finish("本群还没有订阅。")

        if kind in ("全部", "all"):
            groups.pop(group_id, None)
            message = "✅ 已取消本群全部订阅。"
        elif kind in ("状态", "服务器", "status"):
            if not _enabled(group, "status"):
                await unsubscribe.finish("本群没有订阅服务器状态。")
            group.pop("status", None)
            message = "✅ 已取消服务器状态订阅。"
        elif kind in ("新曲", "新歌", "曲库", "歌曲"):
            if not _enabled(group, "new_songs"):
                await unsubscribe.finish("本群没有订阅新曲。")
            group.pop("new_songs", None)
            message = "✅ 已取消新曲订阅。"
        elif kind in ("天气", "weather"):
            if not _enabled(group, "weather"):
                await unsubscribe.finish("本群没有订阅天气。")
            group.pop("weather", None)
            message = "✅ 已取消天气订阅。"
        else:
            await unsubscribe.finish("用法：取消订阅 状态 / 新曲 / 天气 / 全部")

        await _save_subs(data)

    await unsubscribe.finish(message)
