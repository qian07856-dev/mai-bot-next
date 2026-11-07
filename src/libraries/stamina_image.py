# src/libraries/stamina_image.py
"""
底力分析图：把 b50 内所有谱面的标签统计成「最大 / 平均 / 最小」三组，
在同一张五轴雷达图里对比，右侧给出逐轴数值。
"""

import asyncio
import io
import math
import time
from typing import Dict, List, Sequence, Tuple

from PIL import Image, ImageDraw

from src.libraries.maimai_best50_new import FONT_NUM, FONT_TITLE, DrawText

WIDTH = 1400
HEIGHT = 1000
PAD = 40

BG_TOP = (30, 26, 48)
BG_BOTTOM = (18, 19, 30)
TEXT_MAIN = (246, 247, 255, 255)
TEXT_DIM = (176, 180, 202, 255)
GRID = (255, 255, 255, 46)
ROW_EVEN = (38, 40, 58, 255)
ROW_ODD = (45, 47, 68, 255)

AXES = ["键盘", "星星", "技巧", "体力", "爆发"]
SERIES: Sequence[Tuple[str, Tuple[int, int, int, int]]] = (
    ("最大", (255, 130, 130, 255)),
    ("平均", (150, 118, 255, 255)),
    ("最小", (96, 170, 255, 255)),
)

RADAR_CENTER = (360, 570)
RADAR_RADIUS = 215
PANEL_X = 700
PANEL_RIGHT = WIDTH - PAD
PANEL_TOP = 300
ROW_H = 74


def _width(path, size: int, text) -> int:
    import PIL.ImageFont as ImageFont

    return int(ImageFont.truetype(str(path), size).getlength(str(text)))


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


def _render(
    player: str,
    rating: int,
    stats: Dict[str, Dict[str, float]],
    sample: int,
    total: int,
) -> Image.Image:
    canvas = _gradient(WIDTH, HEIGHT).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    sy = DrawText(draw, FONT_TITLE)
    num = DrawText(draw, FONT_NUM)

    accent = SERIES[1][1]
    draw.rounded_rectangle((0, 0, WIDTH, 6), radius=0, fill=accent)

    sy.draw(PAD, 40, 42, "底力分析", TEXT_MAIN)
    subtitle = f"{player} · Rating {rating}" if player else f"Rating {rating}"
    sy.draw(PAD, 100, 22, subtitle, TEXT_DIM)

    badge = f"样本 {sample} / {total}"
    sy.draw(WIDTH - PAD - _width(FONT_TITLE, 26, badge), 54, 26, badge, accent)

    # 雷达网格
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for step in range(1, 6):
        ratio = step / 5
        ld.polygon([_axis_point(i, ratio) for i in range(len(AXES))], outline=GRID, width=2)
    for i in range(len(AXES)):
        ld.line((RADAR_CENTER, _axis_point(i, 1.0)), fill=GRID, width=2)
    for step in range(1, 6):
        point = _axis_point(0, step / 5)
        num.draw(point[0] + 10, point[1] - 8, 16, str(step * 2), TEXT_DIM)
    canvas.alpha_composite(layer)

    # 三组折线
    for key, color in SERIES:
        values = [max(0.0, min(10.0, float(stats[axis][key]))) for axis in AXES]
        points = [_axis_point(i, values[i] / 10.0) for i in range(len(AXES))]
        shape = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shape)
        sd.polygon(points, fill=color[:3] + (28,), outline=color)
        sd.line(points + [points[0]], fill=color, width=4, joint="curve")
        for p in points:
            sd.ellipse((p[0] - 6, p[1] - 6, p[0] + 6, p[1] + 6), fill=color)
        canvas.alpha_composite(shape)

    # 轴名（取平均值）
    for i, axis in enumerate(AXES):
        angle = -math.pi / 2 + i * 2 * math.pi / len(AXES)
        x = RADAR_CENTER[0] + math.cos(angle) * (RADAR_RADIUS + 52)
        y = RADAR_CENTER[1] + math.sin(angle) * (RADAR_RADIUS + 52)
        label = f"{axis} {stats[axis]['平均']:.1f}"
        sy.draw(x - _width(FONT_TITLE, 23, label) / 2, y - 12, 23, label, TEXT_MAIN)

    # 图例
    legend_x = PAD
    for key, color in SERIES:
        draw.ellipse((legend_x, 860, legend_x + 16, 876), fill=color)
        sy.draw(legend_x + 24, 856, 20, key, TEXT_DIM)
        legend_x += 120

    # 右侧表格
    sy.draw(PANEL_X, PANEL_TOP - 56, 22, "逐轴统计", TEXT_DIM)
    headers = ["最大", "平均", "最小"]
    column_x = [PANEL_X + 300, PANEL_X + 450, PANEL_X + 600]
    for label, x in zip(headers, column_x):
        sy.draw(x, PANEL_TOP - 56, 18, label, TEXT_DIM)

    rows = [(axis, stats[axis]) for axis in AXES]
    rows.append(
        (
            "综合",
            {
                key: sum(stats[axis][key] for axis in AXES) / len(AXES)
                for key in ("最大", "平均", "最小")
            },
        )
    )

    for index, (label, values) in enumerate(rows):
        top = PANEL_TOP + index * ROW_H
        draw.rounded_rectangle(
            (PANEL_X - 16, top, PANEL_RIGHT, top + ROW_H - 10),
            radius=12,
            fill=ROW_EVEN if index % 2 == 0 else ROW_ODD,
        )
        is_total = label == "综合"
        sy.draw(PANEL_X, top + 16, 24, label, accent if is_total else TEXT_MAIN)
        for (key, color), x in zip(SERIES, column_x):
            value = f"{values[key]:.1f}"
            num.draw(x + 20 - _width(FONT_NUM, 26, value) / 2, top + 14, 26, value, color)

    draw.line((PAD, HEIGHT - 74, WIDTH - PAD, HEIGHT - 74), fill=(90, 92, 110, 255), width=1)
    sy.draw(
        PAD,
        HEIGHT - 54,
        19,
        "数据来源：水鱼查分器 b50 / DXTag 谱面标签",
        TEXT_DIM,
    )
    stamp = time.strftime("%Y-%m-%d %H:%M")
    sy.draw(
        WIDTH - PAD - _width(FONT_TITLE, 19, stamp),
        HEIGHT - 54,
        19,
        stamp,
        TEXT_DIM,
    )
    return canvas


async def render_stamina(
    player: str,
    rating: int,
    stats: Dict[str, Dict[str, float]],
    sample: int,
    total: int,
) -> Image.Image:
    return await asyncio.to_thread(_render, player, rating, stats, sample, total)


def image_to_bytes(image: Image.Image, fmt: str = "jpg", quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    if str(fmt).lower() in ("jpg", "jpeg"):
        image.convert("RGB").save(buffer, format="JPEG", quality=quality, progressive=True)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
