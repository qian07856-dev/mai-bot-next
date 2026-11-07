import asyncio
import base64
import os
import re

from nonebot import on_command, on_regex
from nonebot.log import logger
from nonebot.params import CommandArg, EventMessage
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from src.libraries import maidata_store, song_alias
from src.libraries.tool import hash
from src.libraries.maimaidx_music import *
from src.libraries.image import *
from src.libraries.maimai_best_50 import generate50
# 新版 B50 绘图（移植自 Yuri-YuzuChaN/nonebot-plugin-maimaidx）
from src.libraries.maimai_best50_new import generate50 as generate50_new
from src.libraries.song_image import (
    DIFF_LABELS,
    image_to_bytes,
    render_search,
    render_song,
)

from src.libraries.config_env import load_project_env
from src.libraries.player_source import fetch_player_any

# 让 .env 里的 B50_ENGINE 等配置生效（nonebot 不会把 .env 写进环境变量）
load_project_env()


def song_txt(music: Music):
    return Message([
        MessageSegment("text", {
            "text": f"{music.id}. {music.title}\n"
        }),
        MessageSegment("image", {
            "file": f"https://www.diving-fish.com/covers/{get_cover_len5_id(music.id)}.png"
        }),
        MessageSegment("text", {
            "text": f"\n{'/'.join(music.level)}"
        })
    ])


def inner_level_q(ds1, ds2=None):
    result_set = []
    diff_label = ['Bas', 'Adv', 'Exp', 'Mst', 'ReM']
    if ds2 is not None:
        music_data = total_list.filter(ds=(ds1, ds2))
    else:
        music_data = total_list.filter(ds=ds1)
    for music in sorted(music_data, key = lambda i: int(i['id'])):
        for i in music.diff:
            result_set.append((music['id'], music['title'], music['ds'][i], diff_label[i], music['level'][i]))
    return result_set


inner_level = on_command('inner_level ', aliases={'定数查歌 '})


@inner_level.handle()
async def _(event: Event, message: Message = CommandArg()):
    argv = str(message).strip().split(" ")
    if len(argv) > 2 or len(argv) == 0:
        await inner_level.finish("命令格式为\n定数查歌 <定数>\n定数查歌 <定数下限> <定数上限>")
        return
    if len(argv) == 1:
        result_set = inner_level_q(float(argv[0]))
    else:
        result_set = inner_level_q(float(argv[0]), float(argv[1]))
    if len(result_set) > 50:
        await inner_level.finish(f"结果过多（{len(result_set)} 条），请缩小搜索范围。")
        return
    s = ""
    for elem in result_set:
        s += f"{elem[0]}. {elem[1]} {elem[3]} {elem[4]}({elem[2]})\n"
    await inner_level.finish(s.strip())


# ========== 查歌 / 随机谱面：生图 + 标签 ==========

SEARCH_LIMIT = max(1, min(20, int(os.getenv("SEARCH_LIMIT", "12"))))
SONG_IMAGE_FORMAT = (os.getenv("SONG_IMAGE_FORMAT") or "jpg").strip().lower()


def music_view(music) -> dict:
    """把水鱼曲目对象摊平成绘图用的普通字典"""
    basic = music.get("basic_info") or {}
    return {
        "id": music.get("id"),
        "title": music.get("title") or "",
        "artist": basic.get("artist") or "",
        "genre": basic.get("genre") or "",
        "bpm": basic.get("bpm") or "",
        "version": basic.get("from") or "",
        "level": list(music.get("level") or []),
        "ds": list(music.get("ds") or []),
        "type": music.get("type") or "",
    }


async def search_songs(keyword: str):
    """对齐 maimai.py 的 by_keywords：标题 / 艺术家 / 别名，先精确再模糊"""
    kw = keyword.casefold()
    try:
        alias_ids = set(await song_alias.find_song_ids(keyword))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[song] 别名库不可用，仅按标题搜索：{exc}")
        alias_ids = set()

    exact, fuzzy = [], []
    for music in total_list:
        title = str(music.get("title") or "").casefold()
        artist = str((music.get("basic_info") or {}).get("artist") or "").casefold()
        try:
            base_id = int(music.get("id")) % 10000
        except (TypeError, ValueError):
            base_id = -1
        if kw == title or kw == artist:
            exact.append(music)
        elif kw in title or kw in artist or base_id in alias_ids:
            fuzzy.append(music)
    pool = exact if exact else fuzzy
    return sorted(pool, key=lambda item: int(item.get("id") or 0))


async def compute_five(items, slot_of) -> list:
    """items 是曲目列表；slot_of(music) 给出难度槽位（1~6）"""
    await asyncio.to_thread(maidata_store.build_index)
    jobs, positions = [], []
    for position, music in enumerate(items):
        slot = slot_of(music)
        chart = maidata_store.find_chart(
            music.get("id"), music.get("type") or "", music.get("title") or "", slot
        )
        if chart is not None:
            jobs.append((chart, slot))
            positions.append(position)
    results = [None] * len(items)
    if not jobs:
        return results
    computed = await asyncio.to_thread(maidata_store.score_slots, jobs)
    for position, scores in zip(positions, computed):
        results[position] = scores
    return results


async def send_song_image(matcher, image) -> None:
    payload = image_to_bytes(image, SONG_IMAGE_FORMAT, 90)
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


def highest_diff(music) -> int:
    return max(0, len(music.get("ds") or []) - 1)


def slot_of(music_or_index) -> int:
    """水鱼难度序号（0=Basic）→ maidata / DXTag 槽位（2=BASIC）"""
    if isinstance(music_or_index, int):
        return music_or_index + 2
    return highest_diff(music_or_index) + 2


async def song_aliases(music) -> list:
    try:
        return await song_alias.aliases_for(music.get("id"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[song] 取别名失败：{exc}")
        return []


def build_search_rows(songs, fives) -> list:
    rows = []
    for music, scores in zip(songs, fives):
        view = music_view(music)
        diff_index = highest_diff(music)
        rows.append(
            {
                "id": view["id"],
                "title": view["title"],
                "artist": view["artist"],
                "type": view["type"],
                "diffs": [
                    (DIFF_LABELS[i] if i < len(DIFF_LABELS) else str(i), level)
                    for i, level in enumerate(view["level"])
                ],
                "five_diff": diff_index if scores else None,
                "scores": scores,
            }
        )
    return rows


search_music = on_regex(r"^查歌\s*(.*)$", priority=6)


@search_music.handle()
async def handle_search_music(event: Event, message: Message = EventMessage()):
    text = message.extract_plain_text().strip()
    keyword = re.sub(r"^查歌\s*", "", text).strip()
    if not keyword:
        await search_music.finish("用法：查歌 <标题片段 / 艺术家>")

    found = await search_songs(keyword)
    if not found:
        await search_music.finish(f"没有找到包含「{keyword}」的乐曲。")

    shown = found[:SEARCH_LIMIT]
    if len(shown) > 3:
        await search_music.send("🔍 正在计算标签，请稍候…")
    fives = await compute_five(shown, slot_of)

    rows = build_search_rows(shown, fives)

    try:
        image = await render_search(keyword, rows, len(found))
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[song] 查歌出图失败，回退文字：{exc}")
        lines = [f"{m['id']}. {m['title']}" for m in shown]
        await search_music.finish("\n".join(lines))
    await send_song_image(search_music, image)


alias_question = on_regex(r"^(.+?)是什么歌\s*$", priority=6)


@alias_question.handle()
async def handle_alias_question(event: Event, message: Message = EventMessage()):
    text = message.extract_plain_text().strip()
    keyword = re.sub(r"是什么歌\s*$", "", text).strip()
    if not keyword:
        await alias_question.finish("用法：XXX是什么歌")

    ids = await song_alias.find_song_ids(keyword)
    if not ids:
        await alias_question.finish(
            f"别名库里没有「{keyword}」。\n可以试试「查歌 {keyword}」按标题搜。"
        )

    songs = []
    for base in ids:
        # 同名曲目优先给 DX 谱面，没有 DX 再退回标准谱
        music = total_list.by_id(str(base + 10000)) or total_list.by_id(str(base))
        if music is not None:
            songs.append(music)
    if not songs:
        await alias_question.finish("别名对上了，但当前曲库里没有这首歌。")

    if len(songs) == 1:
        music = songs[0]
        diff_index = highest_diff(music)
        fives = await compute_five([music], lambda item: slot_of(diff_index))
        aliases = await song_aliases(music)
        try:
            image = await render_song(
                music_view(music),
                chart_type=music.get("type") or "",
                diff_index=diff_index,
                scores=fives[0],
                aliases=aliases,
                footer=f"别名：{keyword}｜数据来源：水鱼查分器 / DXTag 谱面标签",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[song] 别名出图失败：{exc}")
            await alias_question.finish(f"{music.get('id')}. {music.get('title')}")
        await send_song_image(alias_question, image)

    shown = songs[:SEARCH_LIMIT]
    fives = await compute_five(shown, slot_of)
    try:
        image = await render_search(f"别名：{keyword}", build_search_rows(shown, fives), len(songs))
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[song] 别名列表出图失败：{exc}")
        await alias_question.finish("\n".join(f"{m['id']}. {m['title']}" for m in shown))
    await send_song_image(alias_question, image)


mr = on_regex(r"^mai什么\s*$", priority=6)


@mr.handle()
async def handle_random_music():
    music = total_list.random()
    diff_index = highest_diff(music)
    fives = await compute_five([music], lambda item: slot_of(diff_index))
    aliases = await song_aliases(music)
    try:
        image = await render_song(
            music_view(music),
            chart_type=music.get("type") or "",
            diff_index=diff_index,
            scores=fives[0],
            aliases=aliases,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[song] 随机出图失败：{exc}")
        await mr.finish(f"{music.get('id')}. {music.get('title')}")
    await send_song_image(mr, image)


spec_rand = on_regex(
    r"^随个(?:(dx|sd|标准))?([绿黄红紫白])?([0-9]+(?:\.[0-9]+)?\+?)\s*$",
    priority=6,
    flags=re.IGNORECASE,
)


@spec_rand.handle()
async def handle_spec_rand(event: Event, message: Message = EventMessage()):
    text = message.extract_plain_text().strip()
    match = re.match(
        r"^随个(?:(dx|sd|标准))?([绿黄红紫白])?([0-9]+(?:\.[0-9]+)?\+?)\s*$",
        text,
        re.IGNORECASE,
    )
    if not match:
        await spec_rand.finish("用法：随个[dx/标准][绿黄红紫白]<定数>，例如 随个白13+、随个dx紫12")

    kind, color, token = match.groups()
    kinds = {"dx": ["DX"], "sd": ["SD"], "标准": ["SD"]}.get((kind or "").casefold())
    types = kinds or ["SD", "DX"]
    diff = [DIFF_LABELS.index(color)] if color else ...

    candidates = total_list.filter(level=token, type=types, diff=diff)
    if not candidates and re.fullmatch(r"\d+(?:\.\d+)?", token):
        candidates = total_list.filter(ds=float(token), type=types, diff=diff)
    if not candidates:
        await spec_rand.finish("没有符合条件的乐曲哦。")

    music = candidates.random()
    if color:
        diff_index = DIFF_LABELS.index(color)
    else:
        diff_index = highest_diff(music)
    fives = await compute_five([music], lambda item: slot_of(diff_index))
    aliases = await song_aliases(music)
    try:
        image = await render_song(
            music_view(music),
            chart_type=music.get("type") or "",
            diff_index=diff_index,
            scores=fives[0],
            aliases=aliases,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[song] 随个出图失败：{exc}")
        await spec_rand.finish(f"{music.get('id')}. {music.get('title')}")
    await send_song_image(spec_rand, image)


query_chart = on_regex(r"^([绿黄红紫白]?)id([0-9]+)", priority=6)


@query_chart.handle()
async def handle_query_chart(event: Event, message: Message = EventMessage()):
    text = message.extract_plain_text().strip()
    match = re.match(r"^([绿黄红紫白]?)id([0-9]+)", text)
    if not match:
        await query_chart.finish("用法：id<编号> 或 <绿黄红紫白>id<编号>")
    color, raw_id = match.groups()

    music = total_list.by_id(raw_id)
    if music is None:
        await query_chart.finish(f"没有找到编号 {raw_id} 的乐曲。")

    levels = list(music.get("level") or [])
    if color:
        diff_index = DIFF_LABELS.index(color)
        if diff_index >= len(levels):
            await query_chart.finish(f"《{music['title']}》没有 {color} 难度。")
    else:
        diff_index = highest_diff(music)

    fives = await compute_five([music], lambda item: slot_of(diff_index))
    aliases = await song_aliases(music)
    chart = music["charts"][diff_index]
    try:
        image = await render_song(
            music_view(music),
            chart_type=music.get("type") or "",
            diff_index=diff_index,
            scores=fives[0],
            aliases=aliases,
            notes=list(chart.get("notes") or []),
            charter=str(chart.get("charter") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[song] id 出图失败：{exc}")
        await query_chart.finish(f"{music['id']}. {music['title']}")
    await send_song_image(query_chart, image)


wm_list = ['拼机', '推分', '越级', '下埋', '夜勤', '练底力', '练手法', '打旧框', '干饭', '抓绝赞', '收歌']


jrwm = on_command('今日舞萌', aliases={'今日mai'})


@jrwm.handle()
async def _(event: Event, message: Message = CommandArg()):
    qq = int(event.get_user_id())
    h = hash(qq)
    rp = h % 100
    wm_value = []
    for i in range(11):
        wm_value.append(h & 3)
        h >>= 2
    s = f"今日人品值：{rp}\n"
    for i in range(11):
        if wm_value[i] == 3:
            s += f'宜 {wm_list[i]}\n'
        elif wm_value[i] == 0:
            s += f'忌 {wm_list[i]}\n'
    s += "千雪提醒您：打机时不要大力拍打或滑动哦\n今日推荐歌曲："
    music = total_list[h % len(total_list)]
    await jrwm.finish(Message([MessageSegment("text", {"text": s})] + song_txt(music)))

query_score = on_command('分数线')


@query_score.handle()
async def _(event: Event, message: Message = CommandArg()):
    r = "([绿黄红紫白])(id)?([0-9]+)"
    argv = str(message).strip().split(" ")
    if len(argv) == 1 and argv[0] == '帮助':
        s = '''此功能为查找某首歌分数线设计。
命令格式：分数线 <难度+歌曲id> <分数线>
例如：分数线 紫799 100
命令将返回分数线允许的 TAP GREAT 容错以及 BREAK 50落等价的 TAP GREAT 数。
以下为 TAP GREAT 的对应表：
GREAT/GOOD/MISS
TAP\t1/2.5/5
HOLD\t2/5/10
SLIDE\t3/7.5/15
TOUCH\t1/2.5/5
BREAK\t5/12.5/25(外加200落)'''
        await query_score.send(Message([
            MessageSegment("image", {
                "file": f"base64://{str(image_to_base64(text_to_image(s)), encoding='utf-8')}"
            })
        ]))
    elif len(argv) == 2:
        try:
            grp = re.match(r, argv[0]).groups()
            level_labels = ['绿', '黄', '红', '紫', '白']
            level_labels2 = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:MASTER']
            level_index = level_labels.index(grp[0])
            chart_id = grp[2]
            line = float(argv[1])
            music = total_list.by_id(chart_id)
            chart: Dict[Any] = music['charts'][level_index]
            tap = int(chart['notes'][0])
            slide = int(chart['notes'][2])
            hold = int(chart['notes'][1])
            touch = int(chart['notes'][3]) if len(chart['notes']) == 5 else 0
            brk = int(chart['notes'][-1])
            total_score = 500 * tap + slide * 1500 + hold * 1000 + touch * 500 + brk * 2500
            break_bonus = 0.01 / brk
            break_50_reduce = total_score * break_bonus / 4
            reduce = 101 - line
            if reduce <= 0 or reduce >= 101:
                raise ValueError
            await query_chart.send(f'''{music['title']} {level_labels2[level_index]}
分数线 {line}% 允许的最多 TAP GREAT 数量为 {(total_score * reduce / 10000):.2f}(每个-{10000 / total_score:.4f}%),
BREAK 50落(一共{brk}个)等价于 {(break_50_reduce / 100):.3f} 个 TAP GREAT(-{break_50_reduce / total_score * 100:.4f}%)''')
        except Exception:
            await query_chart.send("格式错误，输入“分数线 帮助”以查看帮助信息")


# ========== b50 查分图片（新版：旧曲 Best 35 + 新曲 Best 15） ==========
best_50_pic = on_command('b50')


# B50 绘制引擎：new = 新版（参考插件版式，默认）；old = 旧版绘制
USE_NEW_B50 = (os.getenv("B50_ENGINE") or "new").strip().lower() != "old"


async def render_b50(payload, player_data):
    """优先用新版绘制，渲染失败时回退旧版"""
    if USE_NEW_B50:
        img, status = await generate50_new(payload, player_data=player_data)
        if img is not None:
            return img, status
    return await generate50(payload, player_data=player_data)


@best_50_pic.handle()
async def _(event: Event, message: Message = CommandArg()):
    username = str(message).strip()
    at_qq = None
    for segment in event.get_message():
        if segment.type == "at":
            value = str(segment.data.get("qq") or "")
            if value and value != "all":
                at_qq = value
                break
    if at_qq:
        username = ""
        qq = at_qq
    else:
        qq = None if username else str(event.get_user_id())
    player, error = await fetch_player_any(qq=qq, username=username or None)
    if not player:
        await best_50_pic.finish(
            "❌ 水鱼和落雪都没有查到这位玩家的成绩。\n"
            f"原因：{error or '未知错误'}\n"
            "落雪查询需要在 .env 配置 LXNS_DEVELOPER_TOKEN。"
        )
        return

    payload = {'qq': qq} if qq else {'username': username, 'b50': True}
    img, success = await render_b50(payload, player)
    if img is None:
        await best_50_pic.send(f"❌ B50 图片生成失败（状态 {success}），请稍后重试。")
        return
    await best_50_pic.send(Message([
        MessageSegment("image", {
            "file": f"base64://{str(image_to_base64(img), encoding='utf-8')}"
        })
    ]))
