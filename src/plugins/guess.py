# src/plugins/guess.py
"""
舞萌DX 开字母猜歌小游戏。

玩法：
    开字母 [等级13+] [定数13.5-14.0] [dx/sd]   开始一局
    开x                                        揭开字母 x
    开歌 <曲名/别名/id>                         提交答案
    不玩了                                      结束并公布答案
    开字母榜                                    查看本群今日答对榜

设计上尽量少依赖：
- 题目直接来自现有水鱼曲库 total_list；
- 猜歌直接复用 data/aliases.json（song_alias）；
- 开字母不需要曲绘、音频、谱面视频等额外资源；
- 游戏状态只保存在内存里，重启后当前对局丢失；
- 每日答对榜落盘到 data/guess_scores.json。

移植自 apshuang/nonebot-plugin-guess-song 的开字母玩法（MIT License）：
https://github.com/apshuang/nonebot-plugin-guess-song
"""

import asyncio
import json
import os
import random
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from nonebot import on_command, on_fullmatch, on_regex
from nonebot.adapters.onebot.v11 import Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import Rule

from src.libraries import song_alias
from src.libraries.config_env import load_project_env
from src.libraries.maimaidx_music import Music, total_list

load_project_env()

ROOT = Path(__file__).resolve().parents[2]
SCORE_PATH = ROOT / "data" / "guess_scores.json"

# 一局几首、是否过滤含假名的标题、榜单保留多少天
DEFAULT_SONG_COUNT = 10
MIN_SONG_COUNT = 3
MAX_SONG_COUNT = 20
SCORE_KEEP_DAYS = 30

# 假名（平假名/片假名/半角片假名）。中文汉字不在这里，避免把中文曲名全过滤掉。
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")

# 宴会场谱面不参与开字母：曲名通常很长、包含特殊符号，开起来体验很差
_UTAGE_GENRES = {"宴会場", "宴会场", "utage"}

# 每群一局：group_id -> 对局状态
_GAMES: Dict[str, Dict[str, Any]] = {}

# 每日答对榜缓存，避免每次查询都读盘
_SCORES: Optional[Dict[str, Any]] = None
_SCORES_LOCK = asyncio.Lock()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"[guess] 环境变量 {name}={raw!r} 不是整数，改用默认值 {default}")
        return default
    return max(low, min(high, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


SONG_COUNT = _env_int("GUESS_SONG_COUNT", DEFAULT_SONG_COUNT, MIN_SONG_COUNT, MAX_SONG_COUNT)
FILTER_JAPANESE = _env_bool("GUESS_FILTER_JAPANESE", True)


# ========== 纯工具函数 ==========

def _clean_title(value: Any) -> str:
    """曲名去掉换行，避免把发送格式撑坏"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _maskable(char: str) -> bool:
    """字母、数字、汉字、假名等需要猜的字符；空格和标点直接显示"""
    return bool(char) and char.isalnum()


def _mask_title(title: str) -> str:
    return "".join("?" if _maskable(char) else char for char in title)


def _normalize(text: str) -> str:
    """用于猜歌匹配：全角转半角、大小写不敏感、压缩空白"""
    text = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _guess_keys(text: str) -> List[str]:
    """同时生成带空格和去空格两种键，兼容「曲名里有空格」的情况"""
    normalized = _normalize(text)
    keys = [normalized]
    compact = re.sub(r"\s+", "", normalized)
    if compact and compact != normalized:
        keys.append(compact)
    return keys


def _register_key(keys: Dict[str, Set[int]], raw: Any, index: int) -> None:
    for key in _guess_keys(str(raw or "")):
        if key:
            keys.setdefault(key, set()).add(index)


def _reveal_letter(mask: str, title: str, char: str) -> Tuple[str, int]:
    """把 title 里等于 char 的字符在 mask 中揭开，返回 (新 mask, 揭开数量)"""
    folded = char.casefold()
    revealed = 0
    output = []
    for mask_char, title_char in zip(mask, title):
        if _maskable(title_char) and title_char.casefold() == folded:
            output.append(title_char)
            revealed += 1
        else:
            output.append(mask_char)
    return "".join(output), revealed


def _elapsed(state: Dict[str, Any]) -> str:
    seconds = max(0, int((datetime.now() - state["started_at"]).total_seconds()))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _board(state: Dict[str, Any], reveal: bool = False) -> str:
    songs: List[Music] = state["songs"]
    lines = [f"【开字母】{len(songs)} 首"]
    for index, music in enumerate(songs):
        title = _clean_title(music.get("title"))
        if reveal or state["solved"][index]:
            shown = title
        else:
            shown = state["masks"][index]
        mark = "✅" if state["solved"][index] else "❓"
        lines.append(f"{mark}{index + 1}. {shown}")
    opened = "、".join(sorted(state["opened"])) or "无"
    lines.append(f"已开字符：{opened}")
    return "\n".join(lines)


def _summary(state: Dict[str, Any]) -> str:
    solved = sum(1 for value in state["solved"] if value)
    return f"用时 {_elapsed(state)}｜猜对 {solved}/{len(state['songs'])}"


def _group_id(event: Event) -> Optional[str]:
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return None
    return str(group_id)


def _is_active(event: Event) -> bool:
    group_id = _group_id(event)
    return bool(group_id and group_id in _GAMES)


# ========== 筛选参数 ==========

def _parse_ds_spec(spec: str):
    """解析定数参数：13.5 / 13.5-14.0 / >=13.5 / <=14.0"""
    spec = spec.replace("～", "-").replace("~", "-").strip()
    match = re.fullmatch(
        r"(>=|<=|>|<)?\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?",
        spec,
    )
    if not match:
        return None
    op, first, second = match.groups()
    low = float(first)
    if second is not None:
        if op:
            return None
        high = float(second)
        return (min(low, high), max(low, high))
    if op == ">=":
        return (low, 15.0)
    if op == "<=":
        return (1.0, low)
    if op == ">":
        return (low + 0.01, 15.0)
    if op == "<":
        return (1.0, low - 0.01)
    return low


def _parse_filters(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    options: Dict[str, Any] = {"level": None, "ds": None, "type": None, "version": ""}
    for token in re.split(r"\s+", text.strip()):
        if not token:
            continue
        lowered = token.casefold()
        if lowered == "dx":
            options["type"] = ["DX"]
        elif lowered in ("sd", "标准"):
            options["type"] = ["SD"]
        elif token.startswith("等级"):
            level = token[2:].strip()
            if not level:
                return None, "等级后面要写具体等级，例如：等级13+"
            options["level"] = level
        elif token.startswith("定数"):
            parsed = _parse_ds_spec(token[2:].strip())
            if parsed is None:
                return None, "定数格式不对，例如：定数13.5、定数13.5-14.0、定数>=13.5"
            options["ds"] = parsed
        elif token.startswith("版本"):
            version = token[2:].strip()
            if not version:
                return None, "版本后面要写版本名的一部分，例如：版本PRiSM"
            options["version"] = version
        else:
            return None, f"看不懂参数「{token}」，可以输入「开字母帮助」查看用法"
    return options, ""


def _build_candidates(options: Dict[str, Any]) -> List[Music]:
    """按参数筛曲，并按曲名去重，避免同一首歌的 SD/DX 同时出现"""
    kwargs = {}
    if options.get("level") is not None:
        kwargs["level"] = options["level"]
    if options.get("ds") is not None:
        kwargs["ds"] = options["ds"]
    if options.get("type") is not None:
        kwargs["type"] = options["type"]

    pool = total_list.filter(**kwargs)
    version_filter = str(options.get("version") or "").casefold()
    seen: Set[str] = set()
    candidates: List[Music] = []

    for music in pool:
        title = _clean_title(music.get("title"))
        if not title or len(title) > 80:
            continue
        genre = str((music.get("basic_info") or {}).get("genre") or "").strip()
        if genre in _UTAGE_GENRES or "宴会" in genre:
            continue
        if FILTER_JAPANESE and _KANA_RE.search(title):
            continue
        if sum(1 for char in title if _maskable(char)) < 2:
            continue
        if version_filter:
            version = str((music.get("basic_info") or {}).get("from") or "")
            if version_filter not in version.casefold():
                continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(music)

    return candidates


async def _build_answer_keys(songs: List[Music]) -> Dict[str, Set[int]]:
    """建立「曲名 / 别名 / id -> 曲目下标」的精确匹配表"""
    keys: Dict[str, Set[int]] = {}
    for index, music in enumerate(songs):
        raw_id = str(music.get("id") or "")
        _register_key(keys, raw_id, index)
        _register_key(keys, _clean_title(music.get("title")), index)
        try:
            base_id = int(raw_id) % 10000
            _register_key(keys, str(base_id), index)
        except (TypeError, ValueError):
            pass

    try:
        mapping = await song_alias.ensure_loaded()
    except Exception as exc:  # noqa: BLE001 - 别名加载失败不应该让游戏开不起来
        logger.warning(f"[guess] 别名库不可用，仅按曲名/id 匹配：{exc}")
        mapping = {}

    for index, music in enumerate(songs):
        try:
            song_id = int(music.get("id"))
        except (TypeError, ValueError):
            continue
        aliases = mapping.get(song_id) or mapping.get(song_id % 10000) or []
        for alias in aliases:
            _register_key(keys, alias, index)

    return keys


# ========== 每日答对榜 ==========

def _read_scores_sync() -> Dict[str, Any]:
    if not SCORE_PATH.is_file():
        return {}
    try:
        data = json.loads(SCORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_scores_sync(data: Dict[str, Any]) -> None:
    SCORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SCORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SCORE_PATH)


async def _load_scores() -> Dict[str, Any]:
    global _SCORES
    if _SCORES is None:
        _SCORES = await asyncio.to_thread(_read_scores_sync)
    return _SCORES


async def _record_score(group_id: str, user_id: str, count: int) -> None:
    if count <= 0:
        return
    async with _SCORES_LOCK:
        data = await _load_scores()
        today = datetime.now().strftime("%Y-%m-%d")
        group_scores = data.setdefault(today, {}).setdefault(group_id, {})
        group_scores[user_id] = int(group_scores.get(user_id, 0)) + count

        # 只保留最近 N 天，避免文件无限增长
        for old_date in sorted(data)[:-SCORE_KEEP_DAYS]:
            data.pop(old_date, None)

        try:
            await asyncio.to_thread(_write_scores_sync, data)
        except OSError as exc:
            logger.warning(f"[guess] 写入答对榜失败：{exc}")


# ========== 命令 ==========

guess_start = on_command("开字母", aliases={"猜字母"}, priority=5)
guess_letter = on_regex(r"^开(?!歌)(\S)$", priority=6)
guess_song = on_command("开歌", aliases={"猜歌"}, priority=6, rule=Rule(_is_active))
guess_stop = on_fullmatch(("不玩了", "结束开字母"), priority=5, rule=Rule(_is_active))
guess_rank = on_command("开字母榜", aliases={"猜歌榜"}, priority=5)
guess_help = on_command("开字母帮助", aliases={"猜字母帮助"}, priority=5)


HELP_TEXT = """【开字母帮助】
开字母是从歌名里找歌的小游戏：系统随机抽若干首歌，把歌名变成 ?，你每开一个字母，所有歌名里这个字母都会显示出来。

命令：
· 开字母 [参数]  开始一局
· 开x            揭开字母 x（例如：开a、开星）
· 开歌 <曲名/别名/id>  提交答案
· 不玩了          结束并公布答案
· 开字母榜        查看本群今日答对榜

可选参数：
· 等级13+        只抽指定等级的曲子
· 定数13.5       只抽指定定数；也支持 13.5-14.0、>=13.5、<=14.0
· dx / sd        只抽 DX 谱或标准谱
· 版本PRiSM      按版本名筛选（部分匹配）

例子：
· 开字母
· 开字母 等级13+
· 开字母 定数13.5-14.0 dx
· 开字母 版本PRiSM

说明：
· 仅群聊可用，一个群同时只能开一局；
· 默认排除宴会场谱面；
· 默认会过滤含日文假名的标题，可用 GUESS_FILTER_JAPANESE=false 关闭；
· 开字母不需要下载曲绘、音频或谱面视频。"""


@guess_help.handle()
async def _handle_help():
    await guess_help.finish(HELP_TEXT)


@guess_start.handle()
async def _handle_start(event: Event, args: Message = CommandArg()):
    group_id = _group_id(event)
    if group_id is None:
        await guess_start.finish("开字母只能在群里玩哦。")

    text = args.extract_plain_text().strip()
    if text in ("帮助", "help", "说明"):
        await guess_start.finish(HELP_TEXT)

    current = _GAMES.get(group_id)
    if current is not None:
        if text in ("状态", "查看"):
            await guess_start.finish(_board(current))
        await guess_start.finish(
            "本群已经有一局开字母了，先发送「不玩了」结束当前对局吧。\n" + _board(current)
        )

    if text in ("状态", "查看"):
        await guess_start.finish("本群还没有进行中的开字母，发送「开字母」开始一局。")

    options, error = _parse_filters(text)
    if error:
        await guess_start.finish(f"{error}\n\n{HELP_TEXT}")

    pool = _build_candidates(options or {})
    if len(pool) < MIN_SONG_COUNT:
        await guess_start.finish(
            f"符合条件的乐曲只有 {len(pool)} 首，至少要 {MIN_SONG_COUNT} 首才能开局。\n"
            "可以放宽等级/定数/版本条件再试。"
        )

    songs = random.sample(pool, min(SONG_COUNT, len(pool)))
    keys = await _build_answer_keys(songs)
    state: Dict[str, Any] = {
        "songs": songs,
        "masks": [_mask_title(_clean_title(music.get("title"))) for music in songs],
        "solved": [False] * len(songs),
        "opened": set(),
        "keys": keys,
        "started_at": datetime.now(),
        "params": text,
    }
    _GAMES[group_id] = state
    logger.info(f"[guess] 群 {group_id} 开始开字母：{len(songs)} 首，参数={text or '无'}")

    await guess_start.finish(
        _board(state)
        + "\n\n发送「开x」揭开字母，发送「开歌xxx」猜歌，发送「不玩了」看答案。"
    )


@guess_letter.handle()
async def _handle_letter(event: Event):
    group_id = _group_id(event)
    if group_id is None:
        return
    state = _GAMES.get(group_id)
    if state is None:
        return

    match = re.match(r"^开(?!歌)(\S)$", event.get_plaintext().strip())
    if not match:
        return
    char = match.group(1)
    folded = char.casefold()

    if folded in state["opened"]:
        await guess_letter.finish("这个字符已经开过了哦，换一个吧。")

    revealed = 0
    for index, music in enumerate(state["songs"]):
        title = _clean_title(music.get("title"))
        state["masks"][index], count = _reveal_letter(state["masks"][index], title, char)
        revealed += count

    if revealed == 0:
        await guess_letter.finish(f"本轮标题里没有「{char}」，换一个字符试试。")

    state["opened"].add(folded)
    await guess_letter.finish(_board(state))


@guess_song.handle()
async def _handle_guess(event: Event, args: Message = CommandArg()):
    group_id = _group_id(event)
    if group_id is None or group_id not in _GAMES:
        return
    state = _GAMES[group_id]

    text = args.extract_plain_text().strip()
    if not text:
        await guess_song.finish("用法：开歌 <曲名 / 别名 / 歌曲id>")

    indices: Set[int] = set()
    for key in _guess_keys(text):
        indices.update(state["keys"].get(key, set()))

    if not indices:
        await guess_song.finish("❌ 不在本轮答案里，再想想～")

    new_titles: List[str] = []
    for index in sorted(indices):
        if state["solved"][index]:
            continue
        state["solved"][index] = True
        state["masks"][index] = _clean_title(state["songs"][index].get("title"))
        new_titles.append(state["masks"][index])

    if not new_titles:
        await guess_song.finish("这首已经猜出来了，换一首吧～")

    await _record_score(group_id, str(event.get_user_id()), len(new_titles))

    if all(state["solved"]):
        _GAMES.pop(group_id, None)
        await guess_song.finish(
            f"🎉 全部猜对！{_summary(state)}\n{_board(state, reveal=True)}"
        )

    await guess_song.finish(
        "✅ 猜对了：" + "、".join(new_titles) + "\n" + _board(state)
    )


@guess_stop.handle()
async def _handle_stop(event: Event):
    group_id = _group_id(event)
    if group_id is None:
        return
    state = _GAMES.pop(group_id, None)
    if state is None:
        return
    await guess_stop.finish(
        f"本次开字母结束，{_summary(state)}\n{_board(state, reveal=True)}"
    )


@guess_rank.handle()
async def _handle_rank(event: Event):
    group_id = _group_id(event)
    if group_id is None:
        await guess_rank.finish("开字母榜只能在群里看哦。")

    today = datetime.now().strftime("%Y-%m-%d")
    data = await _load_scores()
    group_scores = (data.get(today) or {}).get(group_id) or {}
    ranking = sorted(
        ((str(uid), int(count)) for uid, count in group_scores.items()),
        key=lambda item: (-item[1], item[0]),
    )[:10]

    if not ranking:
        await guess_rank.finish(f"📭 {today} 本群还没有人答对，快来开一局吧。")

    parts = [MessageSegment.text(f"【开字母榜】{today}\n")]
    for index, (user_id, count) in enumerate(ranking, 1):
        parts.append(MessageSegment.text(f"{index}. "))
        try:
            parts.append(MessageSegment.at(int(user_id)))
        except (TypeError, ValueError):
            parts.append(MessageSegment.text(user_id))
        parts.append(MessageSegment.text(f"：{count} 首\n"))
    parts.append(MessageSegment.text("统计口径：今日猜对的歌曲数"))
    await guess_rank.finish(Message(parts))
