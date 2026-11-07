# src/libraries/recommend_image.py
"""
推分推荐图合成：把 recommend.py 算好的 rows 画成一张图（每次输出一页）

复用新版 B50 的绘图基建（字体、素材路径、DrawText、rating 区块、封面查找），
不新增素材依赖：
    · 字体      src/static/font/{ResourceHanRoundedCN-Bold.ttf, Torus SemiBold.otf}
    · 顶部底图  src/static/mai/pic/circle/b50.png（裁上部做背景）
    · 封面      src/static/mai/cover/{5位id}.png（缺图自动补下，仍缺则用占位）
    · rating    build_rating_block()（档位 01~12 + >=14000 星级）

版面（1400 宽，高度随条数变化）：
    0    ~ 250   顶部信息区：玩家人名 / 当前→目标 rating / rating 区块 / 区间统计
    250  ~ 306   表头
    306  ~ +96n  数据行：序号、封面、曲名、难度·定数、拟合(差)、平均达成率(带条)、你的达成率、鸟加收益(带条)
    末尾 64      页脚：数据来源 + 生成时间 + 页码 / 下一页提示
"""

import asyncio
import io
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageDraw, ImageFont

from src.libraries.maimai_best50_new import (
    COVER_DIR,
    DIFF_TEXT_COLOR,
    FONT_NUM,
    FONT_TITLE,
    PIC_DIR,
    THEME_DIR,
    DrawText,
    _open,
    build_rating_block,
    change_column_width,
    coloum_width,
    song_cover,
)

WIDTH = 1400
HEADER_H = 250
TABLE_H = 56
ROW_H = 96
FOOTER_H = 64

DIFF_LABELS = ["绿", "黄", "红", "紫", "白"]
# 每档难度的强调色（左竖条 / 难度文字）
DIFF_ACCENT = [
    (69, 193, 36, 255),
    (255, 186, 1, 255),
    (255, 90, 102, 255),
    (188, 118, 255, 255),
    (228, 214, 255, 255),
]

# 曲名列可用像素宽度（超出就按像素截断，避免撞到"拟合"列）
TITLE_MAX_WIDTH = 244

BG_DARK = (26, 28, 44, 255)
CARD_EVEN = (255, 255, 255, 18)
CARD_ODD = (255, 255, 255, 28)
TEXT_MAIN = (246, 247, 255, 255)
TEXT_DIM = (178, 182, 205, 255)
GOOD = (120, 226, 150, 255)
WARN = (255, 208, 96, 255)
BAD = (255, 130, 130, 255)
NEUTRAL = (190, 192, 210, 255)


def _text_width(font_path, size: int, text: Any) -> int:
    return ImageFont.truetype(str(font_path), size).getbbox(str(text))[2]


async def _ensure_covers(song_ids: List[int]) -> None:
    """补齐本页缺失的封面（落雪优先，水鱼兜底）"""
    from src.libraries.cover_source import ensure_covers

    await ensure_covers(song_ids)


def _cover_thumb(song_id: int, size: int = 64) -> Image.Image:
    """圆角封面缩略图"""
    cover = song_cover(song_id).convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=8, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(cover, (0, 0), mask)
    return out


def _gain_color(gain: int) -> Tuple[int, int, int, int]:
    if gain >= 15:
        return GOOD
    if gain >= 5:
        return WARN
    return NEUTRAL


def _rate_color(gap: Optional[float]) -> Tuple[int, int, int, int]:
    if gap is None:
        return NEUTRAL
    if gap <= -0.3:
        return GOOD
    if gap <= 0:
        return WARN
    return BAD


def _avg_bar(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, avg: float) -> None:
    """平均达成率进度条：95% ~ 100.5% 映射到 0 ~ width"""
    ratio = (avg - 95.0) / 5.5
    ratio = max(0.0, min(1.0, ratio))
    draw.rounded_rectangle((x, y, x + width, y + 10), radius=5, fill=(255, 255, 255, 34))
    filled = int(width * ratio)
    if filled > 2:
        color = GOOD if ratio >= 0.82 else (WARN if ratio >= 0.6 else BAD)
        draw.rounded_rectangle((x, y, x + filled, y + 10), radius=5, fill=color)


def _render(page_rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> Image.Image:
    height = HEADER_H + TABLE_H + ROW_H * max(len(page_rows), 1) + FOOTER_H
    canvas = Image.new("RGBA", (WIDTH, height), BG_DARK)

    # 顶部：裁 b50 底图的上部做背景，压一层暗色保证文字可读
    try:
        art = _open(THEME_DIR / "b50.png")
        canvas.alpha_composite(art.crop((0, 0, WIDTH, HEADER_H)).convert("RGBA"), (0, 0))
        canvas.alpha_composite(Image.new("RGBA", (WIDTH, HEADER_H), (12, 12, 34, 140)), (0, 0))
    except Exception:
        pass

    # 半透明图形必须画在独立图层再 alpha_composite —— ImageDraw 直接画会覆盖像素而不是混合
    overlay = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    max_gain = max((int(r.get("gain") or 0) for r in page_rows), default=0)
    for offset, item in enumerate(page_rows):
        top = HEADER_H + TABLE_H + offset * ROW_H
        y0, y1 = top + 6, top + ROW_H - 6
        od.rounded_rectangle((24, y0, WIDTH - 24, y1), radius=14,
                             fill=CARD_EVEN if offset % 2 == 0 else CARD_ODD)
        level_index = min(max(int(item.get("diff_index") or 0), 0), 4)
        od.rounded_rectangle((24, y0, 32, y1), radius=4, fill=DIFF_ACCENT[level_index])

        avg = float(item.get("avg") or 0)
        ratio = max(0.0, min(1.0, (avg - 95.0) / 5.5))
        od.rounded_rectangle((700, y0 + 52, 860, y0 + 62), radius=5, fill=(255, 255, 255, 45))
        if ratio > 0:
            od.rounded_rectangle((700, y0 + 52, 700 + int(160 * ratio), y0 + 62), radius=5,
                                 fill=GOOD if ratio >= 0.82 else (WARN if ratio >= 0.6 else BAD))

        gain = item.get("gain")
        if gain is not None:
            gain = int(gain)
            gain_ratio = max(0.0, min(1.0, (gain / max_gain) if max_gain else 0.0))
            od.rounded_rectangle((1080, y0 + 64, 1300, y0 + 74), radius=5, fill=(255, 255, 255, 45))
            if gain_ratio > 0:
                od.rounded_rectangle((1080, y0 + 64, 1080 + int(220 * gain_ratio), y0 + 74), radius=5,
                                     fill=_gain_color(gain))

    od.line((24, HEADER_H + TABLE_H - 1, WIDTH - 24, HEADER_H + TABLE_H - 1), fill=(255, 255, 255, 60), width=1)
    foot_y = height - FOOTER_H
    od.line((24, foot_y, WIDTH - 24, foot_y), fill=(255, 255, 255, 60), width=1)
    canvas.alpha_composite(overlay)

    draw = ImageDraw.Draw(canvas)
    # 中文一律用 FONT_TITLE（思源/ResourceHanRounded 有 CJK 字形），数字用 FONT_NUM
    sy = DrawText(draw, FONT_TITLE)
    tb = DrawText(draw, FONT_NUM)

    # ---------- 顶部信息区 ----------
    sy.draw(48, 30, 38, str(meta.get("player_name") or "未知玩家"), TEXT_MAIN)

    player_rating = meta.get("player_rating")
    target_rating = meta.get("target_rating")
    if player_rating and target_rating and int(player_rating) != int(target_rating):
        sy.draw(48, 90, 24, f"当前 {int(player_rating)} → 目标 {int(target_rating)}", TEXT_DIM)
    elif player_rating:
        sy.draw(48, 90, 24, f"当前 Rating {int(player_rating)}", TEXT_DIM)
    elif target_rating:
        sy.draw(48, 90, 24, f"按目标 Rating {int(target_rating)} 计算", TEXT_DIM)

    if player_rating:
        try:
            block = build_rating_block(int(player_rating))
            scale = 46 / block.size[1]
            canvas.alpha_composite(block.resize((int(block.size[0] * scale), 46)), (48, 130))
        except Exception:
            pass

    ds_min, ds_max = meta.get("ds_min"), meta.get("ds_max")
    sy.draw(48, 198, 22, f"推荐区间 {ds_min:g} ~ {ds_max:g}｜共 {meta.get('total_rows', 0)} 条", TEXT_DIM)

    right = 1352
    lines = [
        (50, f"排序：{meta.get('sort_desc', meta.get('sort_mode', '综合'))}", TEXT_MAIN),
        (88, f"阈值：{meta.get('threshold_text', '不筛')}｜已筛 {meta.get('skipped', 0)} 条", TEXT_DIM),
    ]
    if meta.get("pools_text"):
        lines.append((126, meta["pools_text"], TEXT_DIM))
    if meta.get("next_hint"):
        lines.append((164, f"下一页：{meta['next_hint']}", TEXT_DIM))
    for y, text, color in lines:
        sy.draw(right - _text_width(FONT_TITLE, 22, text), y, 22, text, color)

    # ---------- 表头 ----------
    head_y = HEADER_H + 22
    for x, label in ((56, "#"), (176, "曲名 / 难度"), (430, "拟合定数(差值)"),
                     (700, "平均达成率"), (900, "你的达成率"), (1080, "鸟加收益")):
        sy.draw(x, head_y, 20, label, TEXT_DIM)

    # ---------- 数据行 ----------
    base_index = (int(meta.get("page") or 1) - 1) * int(meta.get("page_size") or len(page_rows) or 15)
    for offset, item in enumerate(page_rows):
        top = HEADER_H + TABLE_H + offset * ROW_H
        y0 = top + 6
        level_index = min(max(int(item.get("diff_index") or 0), 0), 4)
        accent = DIFF_ACCENT[level_index]

        tb.draw(60, y0 + 48, 26, base_index + offset + 1, TEXT_DIM, anchor="mm")
        try:
            canvas.alpha_composite(_cover_thumb(int(item["song_id"])), (96, y0 + 13))
        except Exception:
            pass

        title = str(item.get("title") or "")
        if coloum_width(title) > 30:
            title = change_column_width(title, 29)
        # 再按像素宽度收一刀（中文/日文宽度差异比字符数大得多）
        while title and _text_width(FONT_TITLE, 22, title + "...") > TITLE_MAX_WIDTH:
            title = title[:-1]
        if title != str(item.get("title") or ""):
            title += "..."
        sy.draw(176, y0 + 12, 22, title, TEXT_MAIN)
        sy.draw(176, y0 + 50, 18, f"{DIFF_LABELS[level_index]}{item.get('level')}", accent)
        tb.draw(300, y0 + 48, 20, item.get("ds"), TEXT_DIM)
        tb.draw(350, y0 + 50, 14, f"id{item.get('song_id')}", TEXT_DIM)

        tb.draw(430, y0 + 12, 20, f"{float(item.get('fit_diff') or 0):.2f}", TEXT_MAIN)
        tb.draw(430, y0 + 48, 20, f"({float(item.get('gap') or 0):+.2f})", _rate_color(item.get("gap")))

        avg = float(item.get("avg") or 0)
        tb.draw(700, y0 + 12, 20, f"{avg:.1f}%", TEXT_MAIN)
        mine = item.get("mine")
        if mine is None:
            sy.draw(900, y0 + 12, 20, "未打", TEXT_DIM)
        else:
            tb.draw(900, y0 + 12, 20, f"{float(mine):.2f}%", GOOD if float(mine) >= 100 else TEXT_MAIN)

        gain = item.get("gain")
        if gain is None:
            sy.draw(1080, y0 + 14, 26, "—", TEXT_DIM)
        else:
            gain = int(gain)
            tb.draw(1080, y0 + 14, 32, f"+{gain}", _gain_color(gain))
            tb.draw(1080 + _text_width(FONT_NUM, 32, f"+{gain}") + 6, y0 + 28, 18, "ra", TEXT_DIM)

    # ---------- 页脚 ----------
    left = f"数据来源：水鱼查分器｜{time.strftime('%Y-%m-%d %H:%M')}"
    if meta.get("fallback_note"):
        left = f"※ {meta['fallback_note']}｜" + left
    sy.draw(24, foot_y + 20, 20, left, TEXT_DIM)
    page_text = f"第 {meta.get('page', 1)}/{meta.get('total_pages', 1)} 页"
    sy.draw(WIDTH - 24 - _text_width(FONT_TITLE, 22, page_text), foot_y + 18, 22, page_text, TEXT_MAIN)

    return canvas


async def render_recommend(page_rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> Image.Image:
    """渲染一页推荐图（线程里跑 PIL，避免阻塞事件循环）"""
    await _ensure_covers([int(r["song_id"]) for r in page_rows if r.get("song_id")])
    return await asyncio.to_thread(_render, page_rows, meta)


def image_to_bytes(image: Image.Image, fmt: str = "jpg", quality: int = 88) -> bytes:
    """按格式转成字节（jpg 体积约为 png 的 1/3）"""
    buffer = io.BytesIO()
    if fmt.lower() in ("jpg", "jpeg"):
        image.convert("RGB").save(buffer, format="JPEG", quality=quality, progressive=True)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
