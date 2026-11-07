# src/plugins/upgrade.py
"""
上分模拟器 / 目标拆解。

命令：
    上分 15000                 按当前 B50 算还差多少 ra，并给出最划算的替换顺序
    上分 15000 鸟加            指定目标成绩门槛（鸟加 / 鸟 / ss+ / ss）
    上分 曲名 白 100.5         单曲打到指定达成率后 Rating 变多少

数据来源：水鱼查分器 b50 + chart_stats。目标模式复用 recommend.py 的
定数区间、ra 计算和候选谱筛选逻辑，不另外请求一套数据。
"""

import asyncio
import base64
import os
from typing import Any, Dict, List, Optional, Tuple

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.libraries import song_alias
from src.libraries.config_env import load_project_env
from src.libraries.maimaidx_music import total_list
from src.plugins.recommend import (
    DIFF_LABELS,
    FILTER_KEYWORDS,
    POOL_CAPACITY,
    build_candidates,
    calc_ds_range,
    compute_ra,
    fetch_chart_stats,
    fetch_music_data,
    fetch_player,
)

upgrade = on_command("上分", aliases={"上分模拟", "目标拆解"}, priority=6)

load_project_env()

DIFF_INDEX = {label: index for index, label in enumerate(DIFF_LABELS)}
MAX_STEPS = 20

UPGRADE_IMAGE = (os.getenv("UPGRADE_IMAGE", "true") or "true").strip().lower() in (
    "1", "true", "yes", "on",
)
UPGRADE_IMAGE_FORMAT = (os.getenv("UPGRADE_IMAGE_FORMAT") or "jpg").strip().lower()

GREEN = (120, 226, 150, 255)
RED = (255, 130, 130, 255)
GRAY = (176, 180, 202, 255)


async def _send_result(
    title: str,
    subtitle: str,
    badge: str,
    sections: List[Dict[str, Any]],
    footer: str,
    fallback_text: str,
    accent: Tuple[int, int, int, int] = GREEN,
) -> None:
    """优先出图，失败或关闭时回退文字（finish 必须放在 try 外）"""
    payload = None
    if UPGRADE_IMAGE:
        try:
            from src.libraries.card_image import image_to_bytes, render_card

            image = await render_card(
                title=title,
                subtitle=subtitle,
                badge=badge,
                badge_color=accent,
                sections=sections,
                footer=footer,
                accent=accent,
            )
            payload = image_to_bytes(image, UPGRADE_IMAGE_FORMAT, 90)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[upgrade] 出图失败，回退文字：{exc}")

    if payload:
        logger.info(f"[upgrade] 已生成上分模拟图：{len(payload) / 1024:.0f} KB")
        await upgrade.finish(Message([
            MessageSegment("image", {"file": "base64://" + base64.b64encode(payload).decode()})
        ]))
        return

    await upgrade.finish(fallback_text)

HELP_TEXT = """【上分模拟器】
上分 15000                 按当前 B50 算还差多少 ra、打哪几首最划算
上分 15000 鸟加            指定目标成绩门槛（鸟加/鸟/ss+/ss）
上分 曲名 白 100.5         单曲打到指定达成率后 Rating 变多少
上分 曲名 白               不写达成率时默认按 100.5% 算

说明：
· 目标模式按「替换 B50 末位」估算，只有新谱的 ra 超过所在池末位才会涨分；
· 单曲模式会区分「已在 B50 内」和「需要替换末位/补位」两种情况；
· 当前 QQ 需要先在水鱼或落雪查分器绑定；水鱼查不到时会自动尝试落雪。"""


# ========== B50 基础计算 ==========

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


def _b50_snapshot(player: Dict[str, Any]) -> Tuple[Dict[str, List[int]], Dict[Tuple[int, int], Dict[str, Any]], int]:
    """
    提取 b50 的两个池子：
        old = charts.sd（旧曲 Best 35）
        new = charts.dx（新曲 Best 15）
    返回 (池内 ra 列表, {(song_id, diff_index): 信息}, ra 总和)
    """
    charts = player.get("charts") if isinstance(player, dict) else None
    pools: Dict[str, List[int]] = {"old": [], "new": []}
    by_key: Dict[Tuple[int, int], Dict[str, Any]] = {}
    total = 0

    if not isinstance(charts, dict):
        return pools, by_key, total

    for pool_name, chart_key in (("old", "sd"), ("new", "dx")):
        for item in charts.get(chart_key) or []:
            if not isinstance(item, dict):
                continue
            song_id = _int(item.get("song_id"), -1)
            diff_index = _int(item.get("level_index"), -1)
            if song_id < 0 or diff_index < 0:
                continue
            ra = _int(item.get("ra"), 0)
            pools[pool_name].append(ra)
            total += ra
            by_key[(song_id, diff_index)] = {
                "ra": ra,
                "pool": pool_name,
                "achievements": _float(item.get("achievements"), 0.0),
            }

    return pools, by_key, total


def _candidate_key(candidate: Dict[str, Any]) -> Tuple[int, int]:
    return _int(candidate.get("song_id"), -1), _int(candidate.get("diff_index"), -1)


def _candidate_pool(candidate: Dict[str, Any], by_key: Dict[Tuple[int, int], Dict[str, Any]]) -> str:
    """已在 B50 里的谱以 b50 池子为准，否则按 is_new 判断"""
    info = by_key.get(_candidate_key(candidate))
    if info:
        return str(info["pool"])
    return "new" if candidate.get("is_new") else "old"


def _candidate_target_ra(candidate: Dict[str, Any], threshold: float) -> int:
    return compute_ra(_float(candidate.get("ds"), 0.0), threshold)


def _gain_for(
    candidate: Dict[str, Any],
    pools: Dict[str, List[int]],
    by_key: Dict[Tuple[int, int], Dict[str, Any]],
    threshold: float,
) -> int:
    key = _candidate_key(candidate)
    target_ra = _candidate_target_ra(candidate, threshold)

    info = by_key.get(key)
    if info:
        return max(0, target_ra - _int(info.get("ra"), 0))

    pool = _candidate_pool(candidate, by_key)
    ras = pools[pool]
    capacity = POOL_CAPACITY[pool]
    if len(ras) < capacity:
        return max(0, target_ra)
    floor = min(ras) if ras else 0
    return max(0, target_ra - floor)


def _apply_candidate(
    candidate: Dict[str, Any],
    pools: Dict[str, List[int]],
    by_key: Dict[Tuple[int, int], Dict[str, Any]],
    threshold: float,
) -> int:
    """把候选谱按“鸟加/目标达成率”写进模拟池，返回本次实际增加的 ra"""
    key = _candidate_key(candidate)
    target_ra = _candidate_target_ra(candidate, threshold)
    gain = _gain_for(candidate, pools, by_key, threshold)
    if gain <= 0:
        return 0

    info = by_key.get(key)
    if info:
        pool = str(info["pool"])
        old_ra = _int(info.get("ra"), 0)
        try:
            pools[pool].remove(old_ra)
        except ValueError:
            pass
        pools[pool].append(target_ra)
        info["ra"] = target_ra
        return target_ra - old_ra

    pool = _candidate_pool(candidate, by_key)
    ras = pools[pool]
    capacity = POOL_CAPACITY[pool]
    if len(ras) >= capacity:
        floor = min(ras)
        ras.remove(floor)
        ras.append(target_ra)
        by_key[key] = {"ra": target_ra, "pool": pool, "achievements": threshold}
        return target_ra - floor

    ras.append(target_ra)
    by_key[key] = {"ra": target_ra, "pool": pool, "achievements": threshold}
    return target_ra


# ========== 参数解析 ==========

def _looks_like_achievement(token: str) -> bool:
    try:
        value = float(token)
    except ValueError:
        return False
    return 50.0 <= value <= 101.0


def _parse_single_args(text: str) -> Tuple[str, Optional[str], float]:
    """'曲名 白 100.5' -> (曲名, 白, 100.5)"""
    tokens = text.split()
    achievement = 100.5
    if tokens and _looks_like_achievement(tokens[-1]):
        achievement = float(tokens.pop())

    diff_color = None
    if tokens and tokens[-1] in DIFF_INDEX:
        diff_color = tokens.pop()

    return " ".join(tokens).strip(), diff_color, achievement


def _find_difficulty(music, diff_color: Optional[str]) -> Optional[int]:
    levels = list(music.get("level") or [])
    if diff_color:
        index = DIFF_INDEX[diff_color]
        return index if index < len(levels) else None
    return len(levels) - 1 if levels else None


async def _resolve_music(query: str):
    """按 id / 曲名 / 别名找一首歌，优先精确匹配，再退回部分匹配"""
    query = query.strip()
    if not query:
        return None

    if query.isdigit():
        music = total_list.by_id(query)
        if music is not None:
            return music

    folded = query.casefold()
    exact = [m for m in total_list if str(m.get("title") or "").casefold() == folded]
    if exact:
        return exact[0]

    try:
        ids = await song_alias.find_song_ids(query)
    except Exception:  # noqa: BLE001
        ids = []
    for base in ids:
        music = total_list.by_id(str(base + 10000)) or total_list.by_id(str(base))
        if music is not None:
            return music

    partial = [m for m in total_list if folded in str(m.get("title") or "").casefold()]
    return partial[0] if partial else None


# ========== 目标模式 ==========

async def _run_target(event: Event, target: float, threshold: float):
    player, error = await fetch_player(qq=str(event.get_user_id()))
    if not player:
        await upgrade.finish(f"❌ 拿不到你的 B50：{error or '未知错误'}\n请先在水鱼查分器绑定 QQ 号。")

    pools, by_key, total_ra = _b50_snapshot(player)
    if not by_key:
        await upgrade.finish("当前 B50 是空的，先去打几首歌吧。")

    # 舞萌的 Rating 就是 B50 的 ra 总和（不是平均值）
    current_rating = total_ra
    gap = target - total_ra
    if gap <= 0:
        await upgrade.finish(
            f"✅ 你已经达到目标了：当前 Rating {current_rating:.1f}（{total_ra} ra）≥ 目标 {target:g}"
        )

    ds_min, ds_max = calc_ds_range(target)
    music_data, chart_stats = await asyncio.gather(fetch_music_data(), fetch_chart_stats())
    if not music_data or not chart_stats:
        await upgrade.finish("❌ 获取水鱼曲库/谱面统计失败，请稍后重试。")

    candidates = build_candidates(chart_stats, music_data, ds_min, ds_max)
    if not candidates:
        await upgrade.finish(f"❌ 定数 {ds_min:g}~{ds_max:g} 区间没有可用的候选谱。")

    used = set()
    rows: List[Tuple[Dict[str, Any], int, int, float]] = []
    cumulative = 0

    for _ in range(MAX_STEPS):
        best: Optional[Dict[str, Any]] = None
        best_gain = 0
        for candidate in candidates:
            key = _candidate_key(candidate)
            if key in used:
                continue
            gain = _gain_for(candidate, pools, by_key, threshold)
            if gain > best_gain:
                best = candidate
                best_gain = gain

        if best is None:
            break

        gain = _apply_candidate(best, pools, by_key, threshold)
        used.add(_candidate_key(best))
        cumulative += gain
        rows.append((best, gain, cumulative, total_ra + cumulative))
        if cumulative >= gap:
            break

    if threshold >= 100.5:
        label = "鸟加"
    elif threshold >= 100.0:
        label = "鸟"
    elif threshold >= 99.5:
        label = "ss+"
    elif threshold >= 99.0:
        label = "ss"
    else:
        label = f"{threshold:g}%"
    lines = [
        f"🎯 上分目标：{target:g}｜当前 Rating {current_rating:.1f}（{total_ra} ra）",
        f"还差 {gap:.0f} ra｜按 {label} 计算",
        f"推荐定数区间：{ds_min:g} ~ {ds_max:g}｜候选 {len(candidates)} 张谱",
        "",
    ]

    overview_rows = [
        {"label": "当前 Rating", "value": f"{current_rating:.1f}"},
        {"label": "目标 Rating", "value": f"{target:g}"},
        {"label": "还差", "value": f"{gap:.0f} ra", "value_color": RED},
        {"label": "推荐定数", "value": f"{ds_min:g} ~ {ds_max:g}"},
        {"label": "计算门槛", "value": label},
        {"label": "候选谱", "value": f"{len(candidates)} 张"},
    ]
    sections: List[Dict[str, Any]] = [{"heading": "总览", "rows": overview_rows}]

    if rows:
        rec_rows = []
        for index, (candidate, gain, cumulative_ra, rating) in enumerate(rows, 1):
            diff_index = _int(candidate.get("diff_index"), 0)
            diff_label = DIFF_LABELS[diff_index] if diff_index < len(DIFF_LABELS) else "?"
            target_ra = _candidate_target_ra(candidate, threshold)
            title = str(candidate.get("title") or "")[:24]
            rec_rows.append({
                "label": f"{index}. {title}  {diff_label}{candidate.get('level')}({_float(candidate.get('ds')):g})",
                "value": f"+{gain} ra → {rating:.1f}",
                "note": f"{label}{target_ra}ra",
                "value_color": GREEN,
            })
            lines.append(
                f"{index}. {title} "
                f"id{candidate.get('song_id')} {diff_label}{candidate.get('level')}({_float(candidate.get('ds')):g}) "
                f"{label}{target_ra}ra(+{gain}) → 累计+{cumulative_ra}ra / Rating {rating:.1f}"
            )
        sections.append({
            "heading": "推荐替换顺序",
            "rows": rec_rows,
            "note": (
                f"累计 +{cumulative} ra（Rating {total_ra + cumulative:.1f}）"
                if cumulative >= gap
                else f"前 {len(rows)} 首累计 +{cumulative} ra，还差 {gap - cumulative:.0f} ra"
            ),
        })
    else:
        sections.append({
            "heading": "推荐替换顺序",
            "rows": [{
                "label": "当前区间没有能顶掉 B50 末位的谱",
                "value": "",
                "color": RED,
            }],
            "note": "可以试试「推荐」放宽定数区间，或提高目标达成率门槛",
        })
        lines.append("当前区间没有能顶掉 B50 末位的谱，可以试试「推荐」放宽定数区间。")

    if cumulative >= gap:
        lines.append("")
        lines.append(f"✅ 按这个顺序打，预计可以达到 {target:g}。")
    else:
        lines.append("")
        lines.append(
            f"仅列前 {len(rows)} 首：累计 +{cumulative} ra（+{cumulative} Rating），"
            f"距离目标还差 {gap - cumulative:.0f} ra。"
        )

    await _send_result(
        title="上分目标拆解",
        subtitle=f"当前 {current_rating:.1f} → 目标 {target:g}｜还差 {gap:.0f} ra",
        badge="上分",
        sections=sections,
        footer="数据来源：水鱼查分器 / chart_stats｜发送「推荐帮助」查看推分推荐",
        fallback_text="\n".join(lines),
        accent=GREEN,
    )


# ========== 单曲模式 ==========

async def _run_single(event: Event, query: str, diff_color: Optional[str], achievement: float):
    if not (0 <= achievement <= 101):
        await upgrade.finish("达成率要在 0~101 之间，例如 100.5。")

    music = await _resolve_music(query)
    if music is None:
        await upgrade.finish(f"🔍 没找到「{query}」，可以试试完整曲名、别名或歌曲 id。")

    diff_index = _find_difficulty(music, diff_color)
    if diff_index is None:
        available = " / ".join(
            f"{DIFF_LABELS[i]}{level}" for i, level in enumerate(music.get("level") or [])
            if i < len(DIFF_LABELS)
        )
        await upgrade.finish(f"⚠️ 这首歌没有该难度，可用难度：{available or '无'}")

    levels = list(music.get("level") or [])
    ds_list = list(music.get("ds") or [])
    if diff_index >= len(ds_list):
        await upgrade.finish("这首歌的定数数据不完整，暂时算不了。")
    ds = _float(ds_list[diff_index])
    new_ra = compute_ra(ds, achievement)

    player, error = await fetch_player(qq=str(event.get_user_id()))
    if not player:
        await upgrade.finish(f"❌ 拿不到你的 B50：{error or '未知错误'}\n请先在水鱼查分器绑定 QQ 号。")

    pools, by_key, total_ra = _b50_snapshot(player)
    current_rating = total_ra
    key = (_int(music.get("id"), -1), diff_index)
    info = by_key.get(key)
    old_ra: Optional[int] = _int(info.get("ra"), 0) if info else None
    pool_name = str(info["pool"]) if info else ("new" if (music.get("basic_info") or {}).get("is_new") else "old")
    capacity = POOL_CAPACITY[pool_name]
    pool_ras = pools[pool_name]

    if info:
        new_total = total_ra - _int(old_ra, 0) + new_ra
        note = "已在当前 B50 中，按替换自身旧成绩计算"
    elif len(pool_ras) < capacity:
        new_total = total_ra + new_ra
        note = f"不在 B50，{ '新曲池' if pool_name == 'new' else '旧曲池' }未满，按补位计算"
    else:
        floor = min(pool_ras) if pool_ras else 0
        if new_ra > floor:
            new_total = total_ra - floor + new_ra
            note = f"不在 B50，按替换{ '新曲池' if pool_name == 'new' else '旧曲池' }末位 ra {floor} 计算"
        else:
            new_total = total_ra
            note = f"不在 B50，且 {new_ra} ra 不超过末位 {floor}，不会改变 Rating"

    delta_ra = new_total - total_ra
    new_rating = new_total
    old_label = f"{old_ra} ra" if old_ra is not None else "不在 B50"
    diff_label = DIFF_LABELS[diff_index] if diff_index < len(DIFF_LABELS) else "?"
    level = levels[diff_index] if diff_index < len(levels) else "?"

    lines = [
        "🎯 单曲上分模拟",
        f"曲目：{music.get('title')}",
        f"难度：{diff_label}{level}（定数 {ds:g}）",
        f"目标达成率：{achievement:g}%｜单谱 ra：{old_label} → {new_ra} ra",
        f"B50 池：{'新曲池' if pool_name == 'new' else '旧曲池'}（{len(pool_ras)}/{capacity}）",
        note,
        f"Rating：{current_rating:.1f} → {new_rating:.1f}（{delta_ra:+.1f}）",
    ]

    if delta_ra > 0:
        accent, delta_color = GREEN, GREEN
    elif delta_ra < 0:
        accent, delta_color = RED, RED
    else:
        accent, delta_color = GRAY, GRAY

    sections = [
        {
            "heading": "单曲信息",
            "rows": [
                {"label": "曲目", "value": str(music.get("title") or "")},
                {"label": "难度", "value": f"{diff_label}{level}（定数 {ds:g}）"},
                {"label": "目标达成率", "value": f"{achievement:g}%"},
                {"label": "单谱 ra", "value": f"{old_label} → {new_ra} ra"},
                {"label": "B50 池", "value": f"{'新曲池' if pool_name == 'new' else '旧曲池'}（{len(pool_ras)}/{capacity}）"},
            ],
        },
        {
            "heading": "Rating 变化",
            "rows": [
                {"label": "当前 Rating", "value": f"{current_rating:.1f}"},
                {"label": "模拟后 Rating", "value": f"{new_rating:.1f}", "value_color": accent},
                {"label": "变化", "value": f"{delta_ra:+.1f}", "value_color": delta_color},
            ],
            "note": note,
        },
    ]
    await _send_result(
        title="单曲上分模拟",
        subtitle=f"{music.get('title')}｜{diff_label}{level}｜{achievement:g}%",
        badge="单曲",
        sections=sections,
        footer="数据来源：水鱼查分器 b50｜单谱 ra 按官方系数估算",
        fallback_text="\n".join(lines),
        accent=accent,
    )


# ========== 命令入口 ==========

@upgrade.handle()
async def _handle_upgrade(event: Event, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if not text or text in ("帮助", "help", "说明"):
        await upgrade.finish(HELP_TEXT)

    # 目标模式：第一个参数是 Rating（>= 1000）
    first = text.split()[0]
    try:
        target = float(first)
    except ValueError:
        target = None

    if target is not None and target >= 1000:
        tokens = text.split()[1:]
        threshold = 100.5
        if tokens:
            keyword = tokens[0].casefold()
            if keyword in FILTER_KEYWORDS:
                threshold = FILTER_KEYWORDS[keyword]
            else:
                try:
                    threshold = float(tokens[0])
                except ValueError:
                    threshold = 0.0
            if threshold <= 0:
                await upgrade.finish(f"看不懂门槛「{tokens[0]}」，可用：鸟加 / 鸟 / ss+ / ss")
        await _run_target(event, target, threshold)
        return

    query, diff_color, achievement = _parse_single_args(text)
    if not query:
        await upgrade.finish(HELP_TEXT)
    await _run_single(event, query, diff_color, achievement)
