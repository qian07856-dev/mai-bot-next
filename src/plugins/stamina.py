# src/plugins/stamina.py
"""
底力分析：按当前 QQ 的 b50，对 50 张谱面逐张算标签（DXTag 五轴），
统计键盘 / 星星 / 技巧 / 体力 / 爆发的最大值、平均值、最小值，
三组数据画在同一张雷达图里。

命令：底力分析（别名：底力）
数据来源：水鱼查分器 b50 + 本地 maidata 谱库（MAIDATA_DIR）
"""

import asyncio
import base64
import os
from typing import Any, Dict, List

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule

from src.libraries import maidata_store
from src.libraries.config_env import load_project_env
from src.libraries.tool import hide_local_paths
from src.plugins.recommend import fetch_player

load_project_env()

AXES = ["键盘", "星星", "技巧", "体力", "爆发"]
IMAGE_FORMAT = (os.getenv("STAMINA_IMAGE_FORMAT") or "jpg").strip().lower()

def _stamina_trigger(event: Event) -> bool:
    """只接受「底力」「底力分析」这两个词，后面可以有空格参数，避免句子误触发"""
    text = event.get_plaintext().strip()
    for start in (".", "/", "．"):
        if text.startswith(start):
            text = text[len(start):].lstrip()
            break
    if text.startswith("底力分析"):
        rest = text[len("底力分析"):]
    elif text.startswith("底力"):
        rest = text[len("底力"):]
    else:
        return False
    return not rest or rest[0].isspace()


stamina = on_command("底力分析", aliases={"底力"}, priority=6, rule=Rule(_stamina_trigger))


def aggregate(scores: List[Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    stats: Dict[str, Dict[str, float]] = {}
    for axis in AXES:
        values = [float(item.get(axis, 0.0)) for item in scores]
        stats[axis] = {
            "最大": max(values),
            "平均": sum(values) / len(values),
            "最小": min(values),
        }
    return stats


def format_text(player: str, rating: int, stats, sample: int, total: int) -> str:
    lines = [f"【底力分析】{player} · Rating {rating}｜样本 {sample}/{total}"]
    for axis in AXES:
        row = stats[axis]
        lines.append(
            f"{axis}  最大 {row['最大']:.1f} / 平均 {row['平均']:.1f} / 最小 {row['最小']:.1f}"
        )
    return "\n".join(lines)


async def _send_image(matcher, image) -> None:
    from src.libraries.stamina_image import image_to_bytes

    payload = image_to_bytes(image, IMAGE_FORMAT, 90)
    await matcher.finish(
        Message(
            [
                MessageSegment(
                    "image",
                    {"file": "base64://" + base64.b64encode(payload).decode()},
                )
            ]
        )
    )


@stamina.handle()
async def handle_stamina(bot: Bot, event: Event, args: Message = CommandArg()):
    target_qq = None
    for segment in event.get_message():
        if segment.type == "at":
            value = str(segment.data.get("qq") or "")
            if value and value != "all":
                target_qq = value
                break

    username = args.extract_plain_text().strip()
    if username in ("帮助", "help", "说明"):
        await stamina.finish(
            "底力分析：\n"
            "· 底力 / 底力分析 —— 分析自己的 B50\n"
            "· 底力分析 @某人 —— 分析被 @ 的群友\n"
            "· 底力分析 用户名 —— 按水鱼/落雪用户名查询"
        )

    if target_qq:
        query_desc = f"QQ {target_qq}"
    elif username:
        query_desc = f"用户名 {username}"
    else:
        target_qq = str(event.get_user_id())
        query_desc = "自己"

    await stamina.send(f"🔍 正在获取 {query_desc} 的 b50 并计算标签，请稍候…")

    if target_qq:
        payload, error = await fetch_player(qq=target_qq)
    else:
        payload, error = await fetch_player(username=username)
    if error:
        await stamina.finish(f"❌ 查询失败：{error}")

    charts: Dict[str, Any] = (payload or {}).get("charts") or {}
    items = list(charts.get("sd") or []) + list(charts.get("dx") or [])
    if not items:
        await stamina.finish("b50 是空的，先去打几首歌吧。")

    await asyncio.to_thread(maidata_store.build_index)
    jobs = []
    for item in items:
        try:
            song_id = int(item.get("song_id"))
            level_index = int(item.get("level_index") or 0)
        except (TypeError, ValueError):
            continue
        slot = level_index + 2  # 水鱼 0=Basic → maidata 2=BASIC
        chart = maidata_store.find_chart(
            song_id, str(item.get("type") or ""), str(item.get("title") or ""), slot
        )
        if chart is not None:
            jobs.append((chart, slot))

    if not jobs:
        await stamina.finish(
            "b50 里的谱面都还没收录 maidata，先补充谱库后再试。\n"
            f"（谱库目录：{hide_local_paths(str(maidata_store.chart_root()))}）"
        )

    computed = await asyncio.to_thread(maidata_store.score_slots, jobs)
    scores = [score for score in computed if score]
    if not scores:
        await stamina.finish("标签计算失败，请检查 maidata 是否完整。")

    stats = aggregate(scores)
    player = str(payload.get("nickname") or payload.get("username") or qq)
    rating = int(payload.get("rating") or 0)
    try:
        from src.libraries.stamina_image import render_stamina

        image = await render_stamina(player, rating, stats, len(scores), len(items))
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[stamina] 底力分析图生成失败，回退文字：{exc}")
        await stamina.finish(format_text(player, rating, stats, len(scores), len(items)))
    await _send_image(stamina, image)
