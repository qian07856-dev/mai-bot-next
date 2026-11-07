# src/plugins/dxtag.py
"""
标签谱面分析插件（基于 DXTag：https://github.com/kckc7887/DXTag）

DXTag 读取 maidata（Simai 谱面正文），算出键盘 / 星星 / 技巧 / 体力 / 爆发
五项 0.0~10.0 的分数。它需要逐谱的音符数据，水鱼和落雪都只给音符数量，
所以谱面文件要放在本地谱库目录里（默认 data/maidata，和查歌共用）。

命令（优先级 6，早于 chat.py 的兜底 on_message）：
    标签 曲名 [难度]     查一首谱面的标签，难度可写 白/紫/红/黄/绿/5/4/…
    标签列表             看谱库收了哪些曲子
    标签帮助             用法说明
    标签 示例            用内置示例谱面跑一遍，确认环境正常

谱面只使用管理员放入 MAIDATA_DIR（默认 data/maidata）的官谱，不再支持聊天导入。

可选配置（项目根目录 .env）：
    MAIDATA_DIR         谱库根目录，默认 data/maidata（MAIDATA_DIR 优先）
    DXTAG_CHART_DIR     兼容旧配置的谱库目录
    DXTAG_IMAGE         是否出图，默认 true（失败自动回退文字）
    DXTAG_IMAGE_FORMAT  默认 jpg，可改 png
"""

import asyncio
import base64
import difflib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

from src.libraries import dxtag_engine as engine
from src.libraries.config_env import load_project_env
from src.libraries.tool import hide_local_paths

# 让 .env 里的 DXTAG_* 配置生效（nonebot 不会把 .env 写进环境变量）
load_project_env()

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "src" / "static" / "dxtag"
SAMPLE_PATH = STATIC_DIR / "sample.maidata"
CHART_DIR = Path(
    os.getenv("MAIDATA_DIR")
    or os.getenv("DXTAG_CHART_DIR")
    or (ROOT / "data" / "maidata")
)

DXTAG_IMAGE = os.getenv("DXTAG_IMAGE", "true").lower() in ("1", "true", "yes", "on")
IMAGE_FORMAT = (os.getenv("DXTAG_IMAGE_FORMAT") or "jpg").strip().lower()

LIST_LIMIT = 20

SLOT_ALIASES: Dict[str, int] = {
    # 数字直接是 maidata / DXTag 槽位；颜色按街机配色对应
    "1": 1, "easy": 1,
    "2": 2, "绿": 2, "basic": 2,
    "3": 3, "黄": 3, "advanced": 3,
    "4": 4, "红": 4, "expert": 4,
    "5": 5, "紫": 5, "master": 5, "大师": 5,
    "6": 6, "白": 6, "remaster": 6, "re:master": 6,
    "7": 7, "宴": 7, "utage": 7,
}
SLOT_NAMES = {
    1: "EASY", 2: "BASIC", 3: "ADVANCED", 4: "EXPERT",
    5: "MASTER", 6: "Re:MASTER", 7: "UTAGE",
}
AXES = ["键盘", "星星", "技巧", "体力", "爆发"]


@dataclass
class ChartEntry:
    path: Path
    title: str
    artist: str = ""
    slots: Dict[int, str] = field(default_factory=dict)

    @property
    def slot_text(self) -> str:
        return " / ".join(SLOT_NAMES.get(slot, f"inote_{slot}") for slot in sorted(self.slots))


def _slot_name(slot: int) -> str:
    return SLOT_NAMES.get(slot, f"inote_{slot}")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[\s\u3000\-_/\\.,:;!?'\"()\[\]【】（）]+", "", text)


def parse_header(text: str) -> Tuple[str, str, Dict[int, str]]:
    """只扫 &字段=，不解析音符，用来建索引"""
    title = ""
    artist = ""
    slots: Dict[int, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line.startswith("&"):
            continue
        name, sep, value = line[1:].partition("=")
        if not sep:
            continue
        name = name.strip().lower()
        value = value.strip()
        if name == "title":
            title = value
        elif name == "artist":
            artist = value
        elif name.startswith("inote_"):
            suffix = name[len("inote_"):]
            if suffix.isdigit():
                slots[int(suffix)] = value
    # 等级单独再扫一遍，和 inote 是否为空无关
    levels: Dict[int, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line.startswith("&lv_"):
            continue
        name, sep, value = line[1:].partition("=")
        if not sep or not name[3:].isdigit():
            continue
        levels[int(name[3:])] = value.strip()
    slots = {slot: levels.get(slot, "") for slot in slots}
    return title, artist, slots


_INDEX_CACHE: Optional[Tuple[Tuple[Any, ...], List[ChartEntry]]] = None


def _chart_files() -> List[Path]:
    if not CHART_DIR.is_dir():
        return []
    return sorted(
        (p for p in CHART_DIR.rglob("*") if p.is_file() and p.suffix.lower() in (".txt", ".maidata")),
        key=lambda p: str(p).lower(),
    )


def scan_charts(force: bool = False) -> List[ChartEntry]:
    global _INDEX_CACHE
    files = _chart_files()
    signature = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if not force and _INDEX_CACHE and _INDEX_CACHE[0] == signature:
        return _INDEX_CACHE[1]

    entries: List[ChartEntry] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="utf-16")
            except UnicodeDecodeError:
                logger.warning(f"[dxtag] 跳过无法解码的谱面：{path.name}")
                continue
        except OSError as exc:
            logger.warning(f"[dxtag] 读取谱面失败 {path.name}: {exc}")
            continue
        title, artist, slots = parse_header(text)
        if not title and not slots:
            continue
        entries.append(
            ChartEntry(
                path=path,
                title=title or path.stem,
                artist=artist,
                slots=slots,
            )
        )
    _INDEX_CACHE = (signature, entries)
    return entries


def find_chart(entries: List[ChartEntry], query: str) -> Optional[ChartEntry]:
    target = _normalize(query)
    if not target:
        return None
    exact = [e for e in entries if _normalize(e.title) == target]
    if exact:
        return exact[0]
    partial = [e for e in entries if target in _normalize(e.title)]
    if partial:
        return max(partial, key=lambda e: len(e.slots))

    best: Tuple[float, Optional[ChartEntry]] = (0.0, None)
    for entry in entries:
        score = difflib.SequenceMatcher(None, target, _normalize(entry.title)).ratio()
        if score > best[0]:
            best = (score, entry)
    return best[1] if best[0] >= 0.55 else None


def parse_query(text: str) -> Tuple[str, Optional[int]]:
    """'月光 白' -> ('月光', 5)"""
    parts = [p for p in re.split(r"\s+", text.strip()) if p]
    if not parts:
        return "", None
    slot = SLOT_ALIASES.get(parts[-1].casefold())
    if slot is not None:
        return " ".join(parts[:-1]).strip(), slot
    return " ".join(parts), None


def pick_slot(entry: ChartEntry, slot: Optional[int]) -> int:
    available = sorted(entry.slots)
    if slot is not None:
        return slot
    if 5 in available:
        return 5
    return available[-1] if available else 5


def format_result_text(title: str, difficulty: str, level: str, scores: Dict[str, float]) -> str:
    values = [float(scores.get(axis, 0.0) or 0.0) for axis in AXES]
    average = sum(values) / len(values)
    head = f"【标签】{title}"
    if difficulty:
        head += f" · {difficulty}"
        if level:
            head += f" {level}"
    lines = [head]
    lines.append("｜".join(f"{axis} {values[i]:.1f}" for i, axis in enumerate(AXES[:3])))
    lines.append("｜".join(f"{axis} {values[i]:.1f}" for i, axis in enumerate(AXES) if i >= 3))
    strongest = AXES[values.index(max(values))]
    weakest = AXES[values.index(min(values))]
    lines.append(f"综合 {average:.1f}｜强项 {strongest}｜弱项 {weakest}")
    lines.append("数据来源：DXTag v" + engine.ALGORITHM_VERSION)
    return "\n".join(lines)


async def _send_card(matcher, title: str, difficulty: str, level: str, scores: Dict[str, float]) -> bool:
    """出图；失败返回 False 由调用方回退文字"""
    if not DXTAG_IMAGE:
        return False
    try:
        from src.libraries.dxtag_image import image_to_bytes, render_dxtag

        image = await render_dxtag(
            title=title,
            difficulty=difficulty,
            level=level,
            scores=scores,
            footer=f"数据来源：DXTag v{engine.ALGORITHM_VERSION}｜MIT License",
        )
        payload = image_to_bytes(image, IMAGE_FORMAT, 90)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[dxtag] 标签图生成失败，回退文字：{exc}")
        return False

    logger.info(f"[dxtag] 已生成标签图：{len(payload) / 1024:.0f} KB")
    # finish 会抛 FinishedException，这里不要再套 try
    await matcher.finish(
        Message([MessageSegment("image", {"file": "base64://" + base64.b64encode(payload).decode()})])
    )
    return True


dxtag_cmd = on_command("标签", aliases={"谱面标签", "标签分析"}, priority=6)


@dxtag_cmd.handle()
async def handle_dxtag(bot: Bot, event: Event, args: Message = CommandArg()):
    query = args.extract_plain_text().strip()

    if not query or query in ("帮助", "help", "说明"):
        await dxtag_cmd.finish(build_help_text())

    # 只认「示例」/「demo」这两个完整的词，避免把「示例谱面 红」这类曲名查询吞掉
    if query in ("示例", "DEMO", "demo", "Demo"):
        try:
            text = SAMPLE_PATH.read_text(encoding="utf-8")
            slot = 5
            result = await engine.score_chart_async(text, slot)
        except (OSError, engine.DxtagError) as exc:
            await dxtag_cmd.finish(f"❌ 示例谱面运行失败：{hide_local_paths(str(exc))}")
        title = "DXTag 示例谱面"
        difficulty = _slot_name(slot)
        level = "13"
        scores = {k: float(v) for k, v in result["scores"].items()}
        if await _send_card(dxtag_cmd, title, difficulty, level, scores):
            return
        await dxtag_cmd.finish(format_result_text(title, difficulty, level, scores))

    title_query, wanted_slot = parse_query(query)
    entries = await asyncio.to_thread(scan_charts)
    if not entries:
        await dxtag_cmd.finish(
            "📭 本地谱库还是空的。\n"
            "请管理员把官谱 maidata 放到 data/maidata（或 .env 的 MAIDATA_DIR）后再试。\n"
            "（想先确认环境是否正常，可以发「标签 示例」）"
        )

    entry = find_chart(entries, title_query)
    if entry is None:
        await dxtag_cmd.finish(
            f"🔍 谱库里没找到「{title_query}」。\n发「标签列表」看看已经收了多少首。"
        )

    slot = pick_slot(entry, wanted_slot)
    if slot not in entry.slots:
        available = entry.slot_text or "无"
        await dxtag_cmd.finish(f"⚠️「{entry.title}」没有 {_slot_name(slot)} 谱面。\n可用难度：{available}")

    try:
        text = entry.path.read_text(encoding="utf-8-sig")
        result = await engine.score_chart_async(text, slot)
    except (OSError, engine.DxtagUnavailable) as exc:
        await dxtag_cmd.finish(f"❌ 标签引擎不可用：{hide_local_paths(str(exc))}")
    except engine.DxtagError as exc:
        await dxtag_cmd.finish(f"❌ 这首谱面算不出来：{hide_local_paths(str(exc))}")

    difficulty = result.get("difficulty") or _slot_name(slot)
    level = entry.slots.get(slot, "")
    scores = {k: float(v) for k, v in result["scores"].items()}
    if await _send_card(dxtag_cmd, entry.title, difficulty, level, scores):
        return
    await dxtag_cmd.finish(format_result_text(entry.title, difficulty, level, scores))


dxtag_list = on_command("标签列表", aliases={"标签曲库", "谱面列表"}, priority=6)


@dxtag_list.handle()
async def handle_list(bot: Bot, event: Event):
    entries = await asyncio.to_thread(scan_charts)
    if not entries:
        await dxtag_list.finish(
            "📭 谱库是空的。\n请管理员把官谱 maidata 放到 data/maidata（或 .env 的 MAIDATA_DIR）后再试。"
        )

    lines = [f"📚 本地谱库：{len(entries)} 首"]
    for entry in entries[:LIST_LIMIT]:
        lines.append(f"· {entry.title}（{entry.slot_text or '无难度'}）")
    if len(entries) > LIST_LIMIT:
        lines.append(f"…… 还有 {len(entries) - LIST_LIMIT} 首")
    lines.append(f"\n谱库目录：{CHART_DIR}")
    await dxtag_list.finish(hide_local_paths("\n".join(lines)))


def build_help_text() -> str:
    return (
        "🏷 谱面标签（DXTag）\n"
        "\n"
        "标签是谱面的五轴特征：键盘 / 星星 / 技巧 / 体力 / 爆发，每项 0.0~10.0。\n"
        "分数由谱面音符结构算出，用来区分同一难度下的个人差。\n"
        "\n"
        "命令：\n"
        "· 标签 曲名 [难度] — 查标签，难度写 白/紫/红/黄/绿（Re:MASTER/MASTER/EXPERT/ADVANCED/BASIC）\n"
        "· 标签列表 — 看谱库收录情况\n"
        "· 标签 示例 — 用内置示例谱面自检\n"
        "\n"
        "谱面只使用管理员放入 data/maidata（或 .env 的 MAIDATA_DIR）的官谱，自动识别，改动后无需重启。\n"
        f"当前引擎：DXTag v{engine.ALGORITHM_VERSION}，运行时不依赖 Node.js。"
    )


dxtag_help = on_command("标签帮助", aliases={"谱面标签帮助"}, priority=6)


@dxtag_help.handle()
async def handle_help(bot: Bot, event: Event):
    await dxtag_help.finish(build_help_text())
