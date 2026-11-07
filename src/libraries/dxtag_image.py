# src/libraries/dxtag_image.py
"""
标签谱面图：左侧五轴雷达 + 右侧分数条。

数据来自 DXTag（键盘/星星/技巧/体力/爆发，0.0~10.0），
字体沿用项目既有素材（ResourceHanRoundedCN-Bold / Torus SemiBold）。
"""

import asyncio
import io
import math
import time
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.libraries.maimai_best50_new import FONT_NUM, FONT_TITLE, DrawText

WIDTH = 1400
HEIGHT = 1020
PAD = 40

BG_TOP = (30, 26, 48)
BG_BOTTOM = (18, 19, 30)
TEXT_MAIN = (246, 247, 255, 255)
TEXT_DIM = (176, 180, 202, 255)
GRID = (255, 255, 255, 46)
GRID_STRONG = (255, 255, 255, 80)
CARD = (255, 255, 255, 22)

AXES = ["键盘", "星星", "技巧", "体力", "爆发"]
AXIS_COLORS: Dict[str, Tuple[int, int, int, int]] = {
    "键盘": (150, 118, 255, 255),
    "星星": (255, 208, 96, 255),
    "技巧": (120, 226, 150, 255),
    "体力": (96, 170, 255, 255),
    "爆发": (255, 130, 130, 255),
}

# 雷达几何
RADAR_CENTER = (360, 600)
RADAR_RADIUS = 208

# 右侧评分区
PANEL_X = 700
PANEL_RIGHT = WIDTH - PAD
PANEL_TOP = 300
ROW_H = 104
BAR_TOP_OFFSET = 46
BAR_H = 20


def _font(path, size: int):
    return ImageFont.truetype(str(path), size)


def _text_width(path, size: int, text: Any) -> int:
    return int(_font(path, size).getlength(str(text)))


def _gradient(width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (1, height))
    for y in range(height):
        k = y / max(1, height - 1)
        canvas.putpixel(
            (0, y),
            tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * k) for i in range(3)),
        )
    return canvas.resize((width, height))


def _axis_point(index: int, ratio: float, radius: float = RADAR_RADIUS) -> Tuple[float, float]:
    angle = -math.pi / 2 + index * 2 * math.pi / len(AXES)
    return (
        RADAR_CENTER[0] + math.cos(angle) * radius * ratio,
        RADAR_CENTER[1] + math.sin(angle) * radius * ratio,
    )


def _draw_radar(canvas: Image.Image) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    # 同心五边形网格
    for step in range(1, 6):
        ratio = step / 5
        points = [_axis_point(i, ratio) for i in range(len(AXES))]
        draw.polygon(points, outline=GRID_STRONG if step == 5 else GRID, width=2)

    # 轴线
    for i in range(len(AXES)):
        draw.line((RADAR_CENTER, _axis_point(i, 1.0)), fill=GRID, width=2)

    canvas.alpha_composite(layer)


def _draw_scores(canvas: Image.Image, values: List[float]) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    points = [
        _axis_point(i, max(0.0, min(10.0, values[i])) / 10.0)
        for i in range(len(AXES))
    ]
    accent = (150, 118, 255, 255)
    draw.polygon(points, fill=(150, 118, 255, 70), outline=accent)
    draw.line(points + [points[0]], fill=accent, width=4, joint="curve")

    for i, point in enumerate(points):
        color = AXIS_COLORS[AXES[i]]
        draw.ellipse(
            (point[0] - 8, point[1] - 8, point[0] + 8, point[1] + 8),
            fill=color,
            outline=(255, 255, 255, 220),
            width=2,
        )
    canvas.alpha_composite(layer)


def _render(
    title: str,
    difficulty: str,
    level: str,
    scores: Dict[str, float],
    footer: str,
) -> Image.Image:
    canvas = _gradient(WIDTH, HEIGHT).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    sy = DrawText(draw, FONT_TITLE)
    num = DrawText(draw, FONT_NUM)

    values = [float(scores.get(axis, 0.0) or 0.0) for axis in AXES]
    average = sum(values) / len(values) if values else 0.0
    accent = AXIS_COLORS[AXES[values.index(max(values))]] if values else (150, 118, 255, 255)

    # 顶部色带
    draw.rounded_rectangle((0, 0, WIDTH, 6), radius=0, fill=accent)

    sy.draw(PAD, 46, 44, title, TEXT_MAIN)
    meta = " · ".join(x for x in (difficulty, level) if x)
    if meta:
        sy.draw(PAD, 116, 24, meta, TEXT_DIM)

    badge = f"综合 {average:.1f}"
    sy.draw(
        WIDTH - PAD - _text_width(FONT_TITLE, 32, badge),
        62,
        32,
        badge,
        accent,
    )
    sy.draw(
        WIDTH - PAD - _text_width(FONT_TITLE, 18, "五轴平均"),
        112,
        18,
        "五轴平均",
        TEXT_DIM,
    )

    # 雷达
    _draw_radar(canvas)
    _draw_scores(canvas, values)

    # 刻度 2/4/6/8/10
    for step in range(1, 6):
        ratio = step / 5
        point = _axis_point(0, ratio)
        num.draw(point[0] + 10, point[1] - 8, 16, str(step * 2), TEXT_DIM)

    # 轴名 + 分数
    for i, axis in enumerate(AXES):
        angle = -math.pi / 2 + i * 2 * math.pi / len(AXES)
        x, y = _axis_point(i, 1.0, RADAR_RADIUS + 52)
        label = f"{axis} {values[i]:.1f}"
        width = _text_width(FONT_TITLE, 24, label)
        dx = x - width / 2
        dy = y - 16
        sy.draw(dx, dy, 24, label, AXIS_COLORS[axis])

    # 右侧分项
    sy.draw(PANEL_X, PANEL_TOP - 54, 22, "标签评分", TEXT_DIM)
    for i, axis in enumerate(AXES):
        top = PANEL_TOP + i * ROW_H
        color = AXIS_COLORS[axis]
        sy.draw(PANEL_X, top, 26, axis, TEXT_MAIN)

        score_text = f"{values[i]:.1f}"
        num.draw(
            PANEL_RIGHT - _text_width(FONT_NUM, 30, score_text),
            top - 4,
            30,
            score_text,
            color,
        )

        bar_left = PANEL_X
        bar_right = PANEL_RIGHT
        bar_top = top + BAR_TOP_OFFSET
        draw.rounded_rectangle(
            (bar_left, bar_top, bar_right, bar_top + BAR_H),
            radius=BAR_H // 2,
            fill=(255, 255, 255, 26),
        )
        filled = bar_left + (bar_right - bar_left) * max(0.0, min(10.0, values[i])) / 10.0
        if filled > bar_left:
            draw.rounded_rectangle(
                (bar_left, bar_top, filled, bar_top + BAR_H),
                radius=BAR_H // 2,
                fill=color,
            )

    # 底部解读
    strongest = AXES[values.index(max(values))]
    weakest = AXES[values.index(min(values))]
    summary = f"相对强项：{strongest}｜相对弱项：{weakest}"
    sy.draw(PAD, 850, 24, summary, TEXT_MAIN)
    sy.draw(
        PAD,
        892,
        18,
        "标签由谱面音符结构计算，0~10 为固定标尺分数；同一首曲不同难度差异可能很大",
        TEXT_DIM,
    )

    draw.line((PAD, HEIGHT - 72, WIDTH - PAD, HEIGHT - 72), fill=(255, 255, 255, 60), width=1)
    if footer:
        sy.draw(PAD, HEIGHT - 52, 20, footer, TEXT_DIM)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    sy.draw(
        WIDTH - PAD - _text_width(FONT_TITLE, 20, stamp),
        HEIGHT - 52,
        20,
        stamp,
        TEXT_DIM,
    )
    return canvas


async def render_dxtag(
    title: str,
    difficulty: str = "",
    level: str = "",
    scores: Dict[str, float] | None = None,
    footer: str = "",
) -> Image.Image:
    """生成标签图（PIL 放线程里跑，避免阻塞事件循环）"""
    return await asyncio.to_thread(
        _render, title, difficulty, level, scores or {}, footer
    )


def image_to_bytes(image: Image.Image, fmt: str = "jpg", quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    if str(fmt).lower() in ("jpg", "jpeg"):
        image.convert("RGB").save(
            buffer, format="JPEG", quality=quality, progressive=True
        )
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
