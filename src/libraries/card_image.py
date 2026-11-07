# src/libraries/card_image.py
"""
通用信息卡图片渲染：把「标题 + 分组 + 行」画成一张深色卡片。

给 public.py（命令帮助）、status.py（服务器状态）等共用，和推荐图一样复用
B50 那套字体（中文 ResourceHanRounded / 数字 Torus），不新增素材，也不使用 emoji
（字体里没有彩色 emoji，图片里用色块圆点代替）。

用法：
    image = await render_card(
        title="可用命令",
        subtitle="发送以下命令即可使用",
        badge="帮助",
        sections=[{"heading": "查歌 / 查谱",
                   "rows": [{"label": "查歌<标题片段>", "value": "查询符合条件的乐曲"},
                            {"label": "id<编号>", "value": "查询乐曲信息", "color": (120, 226, 150, 255)}]}],
        footer="数据来源：mai-bot",
    )
"""

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from src.libraries.maimai_best50_new import FONT_NUM, FONT_TITLE, DrawText

WIDTH = 1400
PAD = 40
HEADER_H = 150
ROW_H = 50
ROW_GAP = 6
FOOTER_H = 60

BG = (24, 26, 40, 255)
ROW_EVEN = (255, 255, 255, 16)
ROW_ODD = (255, 255, 255, 26)
TEXT_MAIN = (246, 247, 255, 255)
TEXT_DIM = (176, 180, 202, 255)
ACCENT_DEFAULT = (150, 118, 255, 255)

_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def _gradient(width: int, height: int, top: Tuple[int, int, int], bottom: Tuple[int, int, int]) -> Image.Image:
    """竖向渐变（用于卡片头部）"""
    grad = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / max(height - 1, 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)))
    return grad.resize((width, height)).convert("RGBA")


def _text_width(font_path, size: int, text: Any) -> int:
    from PIL import ImageFont

    return ImageFont.truetype(str(font_path), size).getbbox(str(text))[2]


def _section_height(section: Dict[str, Any]) -> int:
    height = 40 if section.get("heading") else 0
    height += len(section.get("rows") or []) * (ROW_H + ROW_GAP)
    if section.get("note"):
        height += 34
    return height + 10


def _split_sections(sections: List[Dict[str, Any]], columns: int) -> List[List[Dict[str, Any]]]:
    if columns <= 1:
        return [sections]
    buckets: List[List[Dict[str, Any]]] = [[] for _ in range(columns)]
    heights = [0 for _ in range(columns)]
    for section in sections:
        index = min(range(columns), key=lambda i: heights[i])
        buckets[index].append(section)
        heights[index] += _section_height(section)
    return buckets


def _fit_text(text: Any, font_path, size: int, max_width: int) -> str:
    """按像素宽度截断，避免双列布局里长命令压到右侧数值"""
    value = str(text or "")
    if max_width <= 0:
        return ""
    if _text_width(font_path, size, value) <= max_width:
        return value
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if _text_width(font_path, size, value[:mid] + "…") <= max_width:
            low = mid
        else:
            high = mid - 1
    return value[:low] + "…" if low else ""


def _render(
    title: str,
    subtitle: str,
    badge: str,
    badge_color: Tuple[int, int, int, int],
    sections: List[Dict[str, Any]],
    footer: str,
    accent: Tuple[int, int, int, int],
    columns: int = 1,
    width: int = WIDTH,
) -> Image.Image:
    columns = max(1, min(4, int(columns)))
    width = max(800, int(width))
    gap = 40 if columns > 1 else 0
    column_width = (width - PAD * 2 - gap * (columns - 1)) // columns
    column_sections = _split_sections(sections, columns)

    body = 0
    for items in column_sections:
        body = max(body, sum(_section_height(section) for section in items))
    height = HEADER_H + body + PAD + FOOTER_H

    canvas = Image.new("RGBA", (width, height), BG)
    canvas.alpha_composite(
        _gradient(
            width,
            HEADER_H,
            (accent[0] // 3, accent[1] // 3, accent[2] // 3),
            (24, 26, 40),
        ),
        (0, 0),
    )

    draw = ImageDraw.Draw(canvas)
    sy = DrawText(draw, FONT_TITLE)
    tb = DrawText(draw, FONT_NUM)

    sy.draw(PAD, 34, 46, title, TEXT_MAIN)
    if subtitle:
        sy.draw(PAD, 100, 22, subtitle, TEXT_DIM)
    if badge:
        sy.draw(width - PAD - _text_width(FONT_TITLE, 30, badge), 62, 30, badge, badge_color)

    # 半透明底必须画在独立图层再合成
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    row_index = 0
    for column_index, items in enumerate(column_sections):
        x = PAD + column_index * (column_width + gap)
        y = HEADER_H + 14
        for section in items:
            heading = str(section.get("heading") or "")
            rows = section.get("rows") or []
            if heading:
                od.rounded_rectangle((x, y + 6, x + 6, y + 26), radius=3, fill=accent)
                y += 40
            for row in rows:
                color = row.get("color")
                od.rounded_rectangle(
                    (x, y, x + column_width, y + ROW_H),
                    radius=12,
                    fill=ROW_EVEN if row_index % 2 == 0 else ROW_ODD,
                )
                if color:
                    od.rounded_rectangle((x, y, x + 6, y + ROW_H), radius=3, fill=color)
                y += ROW_H + ROW_GAP
                row_index += 1
            if section.get("note"):
                y += 34
            y += 10

    od.line((PAD, height - FOOTER_H, width - PAD, height - FOOTER_H), fill=(255, 255, 255, 60), width=1)
    canvas.alpha_composite(overlay)

    # 文字（画在合成之后）
    row_index = 0
    for column_index, items in enumerate(column_sections):
        x = PAD + column_index * (column_width + gap)
        y = HEADER_H + 14
        for section in items:
            heading = str(section.get("heading") or "")
            rows = section.get("rows") or []
            if heading:
                sy.draw(x + 18, y + 4, 24, heading, accent)
                y += 40
            for row in rows:
                label = str(row.get("label") or "")
                value = str(row.get("value") or "")
                note = str(row.get("note") or "")

                value_width = _text_width(FONT_TITLE, 22, value) if value else 0
                note_width = _text_width(FONT_TITLE, 16, note) if note else 0
                reserved = value_width + (14 + note_width if note else 0) + 24
                label = _fit_text(label, FONT_TITLE, 22, column_width - 48 - reserved)
                sy.draw(x + 24, y + 12, 22, label, TEXT_MAIN)

                if value:
                    vx = x + column_width - 18 - value_width
                    if _has_cjk(value):
                        sy.draw(vx, y + 12, 22, value, row.get("value_color") or TEXT_DIM)
                    else:
                        tb.draw(vx, y + 13, 22, value, row.get("value_color") or TEXT_DIM)
                if note:
                    nx = x + column_width - 18 - value_width - 14 - note_width
                    sy.draw(nx, y + 17, 16, note, TEXT_DIM)
                y += ROW_H + ROW_GAP
                row_index += 1
            if section.get("note"):
                sy.draw(x + 24, y + 4, 18, str(section["note"]), TEXT_DIM)
                y += 34
            y += 10

    if footer:
        sy.draw(PAD, height - FOOTER_H + 20, 20, footer, TEXT_DIM)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    sy.draw(width - PAD - _text_width(FONT_TITLE, 20, stamp), height - FOOTER_H + 20, 20, stamp, TEXT_DIM)

    return canvas


async def render_card(
    title: str,
    subtitle: str = "",
    badge: str = "",
    badge_color: Optional[Tuple[int, int, int, int]] = None,
    sections: Optional[List[Dict[str, Any]]] = None,
    footer: str = "",
    accent: Optional[Tuple[int, int, int, int]] = None,
    columns: int = 1,
    width: int = WIDTH,
) -> Image.Image:
    """渲染信息卡；columns=2 + width=2300 可生成宽版双列布局"""
    return await asyncio.to_thread(
        _render,
        title,
        subtitle,
        badge,
        badge_color or TEXT_MAIN,
        sections or [],
        footer,
        accent or ACCENT_DEFAULT,
        columns,
        width,
    )


def image_to_bytes(image: Image.Image, fmt: str = "jpg", quality: int = 88) -> bytes:
    """按格式转字节（jpg 体积约为 png 的 1/3）"""
    import io

    buffer = io.BytesIO()
    if str(fmt).lower() in ("jpg", "jpeg"):
        image.convert("RGB").save(buffer, format="JPEG", quality=quality, progressive=True)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
