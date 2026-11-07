# src/libraries/song_image.py
"""
查歌 / 随机谱面用图：封面 + 曲目信息 + 难度定数 + 标签（DXTag）。

两张图共用一套深色版式与字体，封面优先用本地 src/static/mai/cover。
"""

import asyncio
import io
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.libraries.maimai_best50_new import (
    COVER_DIR,
    FONT_NUM,
    FONT_TITLE,
    PIC_DIR,
    DrawText,
)
from src.libraries.maimaidx_music import get_cover_len5_id
from src.libraries.cover_source import ensure_covers

WIDTH = 1400
PAD = 40

BG_TOP = (30, 26, 48)
BG_BOTTOM = (18, 19, 30)
TEXT_MAIN = (246, 247, 255, 255)
TEXT_DIM = (176, 180, 202, 255)
CARD = (255, 255, 255, 22)
CARD_STRONG = (255, 255, 255, 30)
GRID = (255, 255, 255, 46)

AXES = ["键盘", "星星", "技巧", "体力", "爆发"]
AXIS_COLORS: Dict[str, Tuple[int, int, int, int]] = {
    "键盘": (150, 118, 255, 255),
    "星星": (255, 208, 96, 255),
    "技巧": (120, 226, 150, 255),
    "体力": (96, 170, 255, 255),
    "爆发": (255, 130, 130, 255),
}
DIFF_LABELS = ["绿", "黄", "红", "紫", "白"]
DIFF_COLORS = [
    (69, 193, 36, 255),
    (255, 186, 1, 255),
    (255, 90, 102, 255),
    (188, 118, 255, 255),
    (228, 214, 255, 255),
]
ACCENT = (150, 118, 255, 255)


_FONT_CACHE: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_GLYPH_CACHE: Dict[Tuple[str, int, str], bool] = {}
_NOTDEF_CACHE: Dict[Tuple[str, int], bytes] = {}

# 字体里没有的装饰符号，换成意思相近、字体里有的字符
GLYPH_FALLBACK = {
    "✪": "★",
    "✕": "×",
    "✖": "×",
    "✗": "×",
    "☓": "×",
}


def _font(path, size: int) -> ImageFont.FreeTypeFont:
    key = (str(path), size)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(str(path), size)
        _FONT_CACHE[key] = font
    return font


def _glyph_mask(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
    size = font.size or 40
    image = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(image).text((2, 2), ch, font=font, fill=255)
    return image.tobytes()


def _glyph_ok(font_path, size: int, ch: str) -> bool:
    key = (str(font_path), size, ch)
    cached = _GLYPH_CACHE.get(key)
    if cached is not None:
        return cached
    font = _font(font_path, size)
    notdef_key = (str(font_path), size)
    notdef = _NOTDEF_CACHE.get(notdef_key)
    if notdef is None:
        notdef = _glyph_mask(font, "\ue000")
        _NOTDEF_CACHE[notdef_key] = notdef
    ok = _glyph_mask(font, ch) != notdef
    _GLYPH_CACHE[key] = ok
    return ok


def safe_text(text: Any, font_path, size: int) -> str:
    """把字体缺失的字符换成可显示字符，避免出现方框"""
    out = []
    for ch in str(text or ""):
        if ch.isspace() or _glyph_ok(font_path, size, ch):
            out.append(ch)
        else:
            out.append(GLYPH_FALLBACK.get(ch, ""))
    return "".join(out)


def _width(path, size: int, text: Any) -> int:
    return int(_font(path, size).getlength(str(text)))


def _truncate(text: str, path, size: int, max_width: int) -> str:
    text = safe_text(text, path, size)
    if _width(path, size, text) <= max_width:
        return text
    out = text
    while out and _width(path, size, out + "…") > max_width:
        out = out[:-1]
    return (out + "…") if out else ""


def _gradient(width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (1, height))
    for y in range(height):
        k = y / max(1, height - 1)
        canvas.putpixel(
            (0, y),
            tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * k) for i in range(3)),
        )
    return canvas.resize((width, height))


def _cover(song_id: Any, size: int) -> Image.Image:
    try:
        path = COVER_DIR / f"{get_cover_len5_id(song_id)}.png"
    except (TypeError, ValueError):
        path = PIC_DIR / "UI_Icon_509506.png"
    if not path.exists():
        path = PIC_DIR / "UI_Icon_509506.png"
    try:
        return Image.open(path).convert("RGBA").resize((size, size))
    except OSError:
        return Image.new("RGBA", (size, size), (60, 62, 80, 255))


def five_tags(scores: Optional[Dict[str, float]]) -> str:
    """把标签压成一句人话标签"""
    if not scores:
        return ""
    items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_value = items[0]
    second, second_value = items[1]
    low, low_value = items[-1]
    text = f"偏{top}" if top_value - second_value >= 0.4 else f"{top}·{second}双修"
    if low_value <= 2.5:
        text += f" · {low}负担低"
    return text


def _draw_radar(
    canvas: Image.Image,
    center: Tuple[int, int],
    radius: int,
    scores: Dict[str, float],
) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def point(index: int, ratio: float) -> Tuple[float, float]:
        angle = -math.pi / 2 + index * 2 * math.pi / len(AXES)
        return (
            center[0] + math.cos(angle) * radius * ratio,
            center[1] + math.sin(angle) * radius * ratio,
        )

    for step in range(1, 6):
        draw.polygon(
            [point(i, step / 5) for i in range(len(AXES))], outline=GRID, width=2
        )
    for i in range(len(AXES)):
        draw.line((center, point(i, 1.0)), fill=GRID, width=2)

    values = [max(0.0, min(10.0, float(scores.get(axis, 0.0)))) for axis in AXES]
    polygon = [point(i, values[i] / 10.0) for i in range(len(AXES))]
    draw.polygon(polygon, fill=(150, 118, 255, 70), outline=ACCENT)
    draw.line(polygon + [polygon[0]], fill=ACCENT, width=4, joint="curve")
    for i, p in enumerate(polygon):
        draw.ellipse(
            (p[0] - 7, p[1] - 7, p[0] + 7, p[1] + 7),
            fill=AXIS_COLORS[AXES[i]],
            outline=(255, 255, 255, 220),
            width=2,
        )
    canvas.alpha_composite(layer)


def _render_search(keyword: str, rows: Sequence[Dict[str, Any]], total: int) -> Image.Image:
    height = 150 + max(1, len(rows)) * 112 + 110
    canvas = _gradient(WIDTH, height).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    sy = DrawText(draw, FONT_TITLE)

    draw.rounded_rectangle((0, 0, WIDTH, 6), radius=0, fill=ACCENT)
    sy.draw(PAD, 40, 40, "查歌", TEXT_MAIN)
    sy.draw(PAD, 96, 22, f"关键词：{keyword}", TEXT_DIM)
    badge = f"{total} 首" if total <= len(rows) else f"{len(rows)} / {total} 首"
    sy.draw(WIDTH - PAD - _width(FONT_TITLE, 30, badge), 52, 30, badge, ACCENT)

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for index in range(len(rows)):
        top = 150 + index * 112
        od.rounded_rectangle(
            (PAD, top, WIDTH - PAD, top + 100),
            radius=14,
            fill=CARD if index % 2 == 0 else CARD_STRONG,
        )
    canvas.alpha_composite(overlay)

    for index, row in enumerate(rows):
        top = 150 + index * 112
        canvas.alpha_composite(_cover(row.get("id"), 76), (PAD + 12, top + 12))

        title = _truncate(str(row.get("title") or ""), FONT_TITLE, 26, 620)
        sy.draw(PAD + 104, top + 10, 26, title, TEXT_MAIN)
        artist = _truncate(str(row.get("artist") or ""), FONT_TITLE, 17, 620)
        sy.draw(PAD + 104, top + 48, 17, artist, TEXT_DIM)

        scores = row.get("scores")
        x = PAD + 104
        for axis in AXES:
            value = 0.0 if not scores else float(scores.get(axis, 0.0))
            text = f"{axis} {value:.1f}" if scores else f"{axis} --"
            sy.draw(x, top + 72, 15, text, AXIS_COLORS[axis] if scores else TEXT_DIM)
            x += _width(FONT_TITLE, 15, text) + 16

        right = WIDTH - PAD - 16
        type_text = str(row.get("type") or "")
        if type_text:
            tw = _width(FONT_TITLE, 16, type_text)
            draw.rounded_rectangle(
                (right - tw - 16, top + 10, right, top + 38), radius=8, fill=ACCENT
            )
            sy.draw(right - tw - 8, top + 14, 16, type_text, TEXT_MAIN)

        cx = right
        for label, level in reversed(list(row.get("diffs") or [])):
            text = f"{label} {level}"
            tw = _width(FONT_TITLE, 16, text)
            cx -= tw
            sy.draw(cx, top + 54, 16, text, TEXT_DIM)
            cx -= 22

        five = row.get("five_diff")
        if five is not None and five < len(DIFF_LABELS):
            label = f"标签取 {DIFF_LABELS[five]}"
            sy.draw(right - _width(FONT_TITLE, 14, label), top + 78, 14, label, TEXT_DIM)

    draw.line((PAD, height - 74, WIDTH - PAD, height - 74), fill=(90, 92, 110, 255), width=1)
    sy.draw(PAD, height - 54, 19, "数据来源：水鱼查分器 / DXTag 谱面标签", TEXT_DIM)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    sy.draw(WIDTH - PAD - _width(FONT_TITLE, 19, stamp), height - 54, 19, stamp, TEXT_DIM)
    return canvas


async def render_search(keyword: str, rows: Sequence[Dict[str, Any]], total: int) -> Image.Image:
    await ensure_covers(row.get("id") for row in rows)
    return await asyncio.to_thread(_render_search, keyword, list(rows), total)


def _render_song(
    music: Dict[str, Any],
    chart_type: str,
    diff_index: int,
    scores: Optional[Dict[str, float]],
    footer: str,
    aliases: Sequence[str] = (),
    notes: Sequence[int] = (),
    charter: str = "",
) -> Image.Image:
    height = 900
    canvas = _gradient(WIDTH, height).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    sy = DrawText(draw, FONT_TITLE)
    num = DrawText(draw, FONT_NUM)

    draw.rounded_rectangle((0, 0, WIDTH, 6), radius=0, fill=ACCENT)
    title = _truncate(str(music.get("title") or ""), FONT_TITLE, 42, 1120)
    sy.draw(PAD, 36, 42, title, TEXT_MAIN)
    badge = f"{chart_type} · {music.get('id')}" if chart_type else str(music.get("id"))
    sy.draw(WIDTH - PAD - _width(FONT_TITLE, 24, badge), 48, 24, badge, ACCENT)

    canvas.alpha_composite(_cover(music.get("id"), 300), (PAD, 140))

    levels = list(music.get("level") or [])
    ds_list = list(music.get("ds") or [])

    # 半透明块必须画在独立图层再合成，否则 RGBA 直接写会变成不透明色块
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(
        (PAD, 140, PAD + 300, 440), radius=16, outline=(255, 255, 255, 90), width=2
    )
    if 0 <= diff_index < len(levels):
        top = 196 + diff_index * 64
        od.rounded_rectangle(
            (PAD + 352, top - 8, PAD + 780, top + 46),
            radius=12,
            fill=(255, 255, 255, 26),
        )
    canvas.alpha_composite(overlay)

    info = [
        ("艺术家", music.get("artist") or "-"),
        ("分类", music.get("genre") or "-"),
        ("BPM", str(music.get("bpm") or "-")),
        ("版本", music.get("version") or "-"),
        ("类型", chart_type or "-"),
    ]
    y = 468
    for label, value in info:
        sy.draw(PAD, y, 17, label, TEXT_DIM)
        sy.draw(PAD + 64, y, 17, _truncate(str(value), FONT_TITLE, 17, 228), TEXT_MAIN)
        y += 30

    if aliases:
        alias_text = "别名：" + " / ".join(str(a) for a in aliases[:10])
        lines: List[str] = []
        current = ""
        for ch in safe_text(alias_text, FONT_TITLE, 17):
            if _width(FONT_TITLE, 17, current + ch) > 740:
                lines.append(current)
                current = ch
                if len(lines) == 2:
                    break
            else:
                current += ch
        if current and len(lines) < 2:
            lines.append(current + ("…" if len(alias_text) > len("".join(lines) + current) else ""))
        for index, line in enumerate(lines):
            sy.draw(PAD, 636 + index * 26, 17, line, TEXT_DIM)

    sy.draw(PAD + 360, 150, 22, "难度 / 定数", TEXT_DIM)
    for index, (level, ds) in enumerate(zip(levels, ds_list)):
        top = 196 + index * 64
        chosen = index == diff_index
        color = DIFF_COLORS[index] if index < len(DIFF_COLORS) else TEXT_DIM
        draw.rounded_rectangle((PAD + 360, top, PAD + 366, top + 34), radius=3, fill=color)
        name = DIFF_LABELS[index] if index < len(DIFF_LABELS) else str(index)
        sy.draw(PAD + 380, top + 2, 22, f"{name}  {level}", TEXT_MAIN if chosen else TEXT_DIM)
        value = f"{float(ds):.1f}"
        num.draw(PAD + 760 - _width(FONT_NUM, 24, value), top + 3, 24, value, color)

    if notes:
        labels = ["TAP", "HOLD", "SLIDE", "TOUCH", "BREAK"] if len(notes) >= 5 else [
            "TAP", "HOLD", "SLIDE", "BREAK"
        ]
        note_text = "  ".join(f"{label} {count}" for label, count in zip(labels, notes))
        sy.draw(PAD + 360, 556, 16, note_text, TEXT_DIM)
    if charter:
        sy.draw(PAD + 360, 584, 16, f"谱师：{charter}", TEXT_DIM)

    if scores:
        _draw_radar(canvas, (1120, 480), 175, scores)
        for index, axis in enumerate(AXES):
            angle = -math.pi / 2 + index * 2 * math.pi / len(AXES)
            x = 1120 + math.cos(angle) * 235
            yy = 480 + math.sin(angle) * 235
            text = f"{axis} {scores.get(axis, 0.0):.1f}"
            sy.draw(x - _width(FONT_TITLE, 20, text) / 2, yy - 10, 20, text, AXIS_COLORS[axis])
        tag = five_tags(scores)
        if tag:
            sy.draw(1120 - _width(FONT_TITLE, 22, tag) / 2, 742, 22, tag, ACCENT)
    else:
        sy.draw(900, 460, 24, "标签未收录", TEXT_DIM)
        sy.draw(900, 500, 17, "补上这首的 maidata 后即可显示", TEXT_DIM)

    draw.line((PAD, height - 74, WIDTH - PAD, height - 74), fill=(90, 92, 110, 255), width=1)
    sy.draw(PAD, height - 54, 19, footer, TEXT_DIM)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    sy.draw(WIDTH - PAD - _width(FONT_TITLE, 19, stamp), height - 54, 19, stamp, TEXT_DIM)
    return canvas


async def render_song(
    music: Dict[str, Any],
    chart_type: str = "",
    diff_index: int = 4,
    scores: Optional[Dict[str, float]] = None,
    footer: str = "数据来源：水鱼查分器 / DXTag 谱面标签",
    aliases: Sequence[str] = (),
    notes: Sequence[int] = (),
    charter: str = "",
) -> Image.Image:
    await ensure_covers([music.get("id")])
    return await asyncio.to_thread(
        _render_song,
        dict(music),
        chart_type,
        diff_index,
        scores,
        footer,
        tuple(aliases),
        tuple(notes),
        charter,
    )


def image_to_bytes(image: Image.Image, fmt: str = "jpg", quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    if str(fmt).lower() in ("jpg", "jpeg"):
        image.convert("RGB").save(buffer, format="JPEG", quality=quality, progressive=True)
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
