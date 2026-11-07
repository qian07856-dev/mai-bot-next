# src/plugins/bilibili.py
"""
B站（bilibili）视频解析插件

功能：
1. 监听群聊 / 私聊消息，只要消息里包含 bilibili 视频链接就自动解析
   （支持 BV号 / av号 / www.bilibili.com/video/... 链接 / b23.tv 短链 / QQ分享卡片）；
2. 爬取视频基本信息（标题、UP主、时长、播放量、点赞、收藏、投币、弹幕、转发、封面图）并发送；
3. 下载视频文件并发送到聊天中，默认清晰度跟着登录态走：
   配了 BILI_SESSDATA 默认按 1080P 下（走 DASH，需要本机有 ffmpeg 合并），
   没配则默认 720P（免登录能拿到的上限）；两者都能用 BILI_QUALITY 覆盖。

依赖：
- httpx（requirements.txt 中已有，无需额外安装）
- ffmpeg（可选，用于把 DASH 的视频流 / 音频流合并；建议安装，否则只能拿到低清晰度单文件）

可选配置（写在项目根目录的 .env 中，不配也能跑）：
    BILI_SESSDATA=你的SESSDATA        # 登录态cookie，不登录一般只能拿到 480P/720P
    BILI_COOKIE=完整cookie字符串       # 需要更完整的cookie时使用（优先级高于 SESSDATA）
    BILI_SAVE_DIR=D:/data/bilibili    # 视频/封面的保存目录，默认 项目根目录/data/bilibili
    BILI_MAX_VIDEO_MB=500             # 超过这个体积的视频不发（QQ上传限制），默认 500
    BILI_QUALITY=80                   # 覆盖默认清晰度：80=1080P，64=720P，32=480P
                                      # 不写这项时自动判断：有 SESSDATA 用 1080P，没有用 720P
    BILI_KEEP_FILE=true               # 发送后是否保留视频文件，默认 false（发送完就删）
    BILI_SEND_VIDEO=true              # 是否下载并发送视频文件，默认 true
    BILI_DEDUP_SECONDS=600            # 同一个视频在同一会话内的处理冷却时间，默认 600 秒
    BILI_DOWNLOAD_THREADS=8           # 每个文件用几条连接分片下载，默认 8（1 表示不加速）
    BILI_MIN_CHUNK_MB=4               # 单个分片最小体积，默认 4MB
    BILI_MIRROR_PROBE=true            # 是否对 CDN 节点/公共镜像测速择优，默认 true
    BILI_MIRROR_HOSTS=a.com,b.com     # 自定义镜像节点，留空用内置列表
    BILI_PROBE_KB=512                 # 每个候选节点探测多少字节，默认 512KB
    BILI_PROBE_MIN_MB=16              # 小于这个体积就不测速了（小文件没必要），默认 16
    BILI_FAST_THRESHOLD_MBS=20        # 原始节点已超过这个速度就不切镜像，默认 20 MB/s
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.log import logger
from src.libraries.tool import hide_local_paths

# 让 .env 里的 BILI_* 配置生效（nonebot 不会把 .env 写进环境变量）
from src.libraries.config_env import load_project_env

load_project_env()

# ========== 基础配置 ==========


def _env_num(name: str, default, cast=int):
    """
    读取数字型环境变量：没写、写空值或写成非数字时都回退到默认值。
    （.env 里留一行空的 BILI_XXX= 不至于让插件整个加载失败）
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        logger.warning(f"[bilibili] 环境变量 {name}={raw!r} 不是合法数字，改用默认值 {default}")
        return default


# 请求 bilibili 接口时需要的请求头（缺少 Referer 会被拦截）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
}

# 下载视频流时用的请求头（DASH 地址带了防盗链，必须带 Referer）
STREAM_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

SESSDATA = os.getenv("BILI_SESSDATA", "").strip()
COOKIE = os.getenv("BILI_COOKIE", "").strip() or (f"SESSDATA={SESSDATA}" if SESSDATA else "")
if COOKIE:
    HEADERS["Cookie"] = COOKIE
    STREAM_HEADERS["Cookie"] = COOKIE

SAVE_DIR = Path(os.getenv("BILI_SAVE_DIR") or (Path.cwd() / "data" / "bilibili"))

MAX_VIDEO_MB = _env_num("BILI_MAX_VIDEO_MB", 500)
# 期望清晰度：80=1080P，64=720P，32=480P
# 默认跟着登录态走：配了 BILI_SESSDATA 默认冲 1080P，没配就默认 720P（免登录上限）
PREFER_QUALITY = _env_num("BILI_QUALITY", 80 if COOKIE else 64)
MAX_BASE64_MB = 30  # 本地文件路径发送失败时，小于这个体积才用 base64 兜底
KEEP_FILE = os.getenv("BILI_KEEP_FILE", "false").lower() in ("1", "true", "yes", "on")
SEND_VIDEO = os.getenv("BILI_SEND_VIDEO", "true").lower() in ("1", "true", "yes", "on")
# 聊天里是否显示本机绝对路径：默认 false（只显示相对项目根目录的写法，绝对路径只写进控制台日志）
SHOW_PATH = os.getenv("BILI_SHOW_PATH", "false").lower() in ("1", "true", "yes", "on")
DEDUP_SECONDS = _env_num("BILI_DEDUP_SECONDS", 600.0, float)
MAX_CONCURRENT = _env_num("BILI_CONCURRENT", 1)

# ffmpeg 路径（没有的话 DASH 音视频合并不了，只能发单文件 mp4）
FFMPEG_PATH = shutil.which("ffmpeg")

# ---------- 下载加速配置 ----------
# 公共镜像节点：B 站播放地址的签名只跟路径/参数有关，换 host 可以直接用
DEFAULT_MIRROR_HOSTS = [
    "upos-sz-mirrorcos.bilivideo.com",
    "upos-sz-mirrorali.bilivideo.com",
    "upos-sz-mirroraliov.bilivideo.com",
    "upos-sz-mirrorhw.bilivideo.com",
    "upos-tf-all-hw.bilivideo.com",
    "upos-tf-all-tx.bilivideo.com",
]

DOWNLOAD_THREADS = max(1, min(32, _env_num("BILI_DOWNLOAD_THREADS", 8)))  # 每个文件的分片连接数
MIN_CHUNK_BYTES = max(1, _env_num("BILI_MIN_CHUNK_MB", 4)) * 1024 * 1024   # 单个分片最小体积
PROBE_BYTES = max(64, _env_num("BILI_PROBE_KB", 512)) * 1024              # 每个候选节点探测多少字节
PROBE_MIN_BYTES = max(0, _env_num("BILI_PROBE_MIN_MB", 16)) * 1024 * 1024  # 小于这个体积就不找镜像了
PROBE_TIMEOUT = _env_num("BILI_PROBE_TIMEOUT", 6.0, float)
FAST_THRESHOLD_MBS = _env_num("BILI_FAST_THRESHOLD_MBS", 20.0, float)     # 首个节点够快就不再试镜像
MIRROR_PROBE = os.getenv("BILI_MIRROR_PROBE", "true").lower() in ("1", "true", "yes", "on")
MIRROR_HOSTS = [
    h.strip()
    for h in os.getenv("BILI_MIRROR_HOSTS", ",".join(DEFAULT_MIRROR_HOSTS)).split(",")
    if h.strip()
]
MIRROR_CACHE_TTL = 1800.0  # 选好的节点缓存半小时

# 清晰度代码 -> 名称
QUALITY_NAME = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60 高帧率",
    112: "1080P+ 高码率",
    80: "1080P 高清",
    74: "720P60 高帧率",
    64: "720P 高清",
    32: "480P 清晰",
    16: "360P 流畅",
}

# 允许使用的清晰度（按优先级从高到低，默认 1080P 优先）
QUALITY_ORDER = [80, 112, 116, 74, 64, 32, 16]

# 启动时提示一下环境情况，方便排查为什么拿不到 1080P
if not FFMPEG_PATH:
    logger.warning(
        "[bilibili] 未检测到 ffmpeg：只能发送 720P/480P 的单文件视频。"
        "想要 1080P，请安装 ffmpeg 并加入系统 PATH。"
    )
if not COOKIE:
    logger.warning(
        "[bilibili] 未配置 BILI_SESSDATA：未登录态一般只能拿到 480P/720P。"
        "在 .env 里加一行 BILI_SESSDATA=你的SESSDATA 即可解锁 1080P。"
    )


# ========== 链接识别 ==========

# 匹配消息里的 URL（QQ 分享卡片的 json 里斜杠会被转义成 \/，先还原再匹配）
URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\\]\[{}]+")
# BV 号：BV + 10位字符，且前后不能紧挨字母数字
# （否则图片 base64、表情包 key、文件名里的片段会被误判成视频号）
BV_PATTERN = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
# av 号：av + 数字，同样要求前后是边界
AV_PATTERN = re.compile(r"(?<![0-9A-Za-z])[Aa][Vv](\d{1,12})(?![0-9A-Za-z])")
# 短链域名
SHORT_LINK_HOSTS = ("b23.tv", "bili2233.cn", "b23.wtf")

# 只有这些段里的内容才参与链接识别；图片/表情/语音/视频等媒体段一律不看
# （表情包的 CQ 段里带 base64 的 key，图片段里带文件名和 URL，容易被误判成 av 号）
TEXT_SEGMENT_TYPES = ("text", "json", "xml")


def _collect_text(message) -> str:
    """
    从消息里取出"可搜索文本"：只取 text 段和 json/xml 分享卡片段。

    以前是整个 CQ 字符串都参与匹配，结果图片文件名、表情包 base64 key 里的
    片段（比如 "Av8"）被当成 av 号，导致收到图片/表情包就回复"解析失败"。
    """
    try:
        segments = list(message)
    except TypeError:
        return str(message)

    parts: List[str] = []
    for segment in segments:
        seg_type = getattr(segment, "type", "")
        if seg_type not in TEXT_SEGMENT_TYPES:
            continue
        data = getattr(segment, "data", None) or {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        else:
            # QQ 分享卡片：B 站小程序卡片的链接藏在 json 里。
            # 直接取原始的 json 字符串，别再用 json.dumps 序列化一遍（会把 \/ 变成 \\/，
            # 导致卡片里的短链还原不出来）
            if isinstance(data, dict):
                raw_json = data.get("data")
                if isinstance(raw_json, str):
                    parts.append(raw_json)
                parts.extend(str(v) for k, v in data.items() if k != "data" and isinstance(v, str))
            else:
                parts.append(str(data))
    return " ".join(parts)


def _normalize_text(raw: str) -> str:
    """把消息文本里被转义的字符还原，方便正则匹配"""
    text = raw.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    # 兼容被重复转义的情况（例如 \\/ 这种）：把连续反斜杠 + 斜杠压成一个斜杠
    text = re.sub(r"\\+/", "/", text)
    return text


def _find_raw_ref(raw: str) -> Optional[Tuple[str, str]]:
    """
    从任意文本里找出 bilibili 视频引用（只做正则匹配，不发网络请求）

    返回值：("bv", "BV1xx411c7mD") / ("av", "12345") / ("url", "https://b23.tv/xxx")
    找不到返回 None
    """
    text = _normalize_text(raw)

    # 1. 先看消息里的链接
    for url in URL_PATTERN.findall(text):
        host = urllib.parse.urlparse(url).netloc.lower()

        # 短链（b23.tv 之类）需要解析跳转，交给 handle 里处理
        if host in SHORT_LINK_HOSTS or host.endswith(tuple(f".{h}" for h in SHORT_LINK_HOSTS)):
            return "url", url

        # 普通视频链接
        if "bilibili.com" in host or host.endswith("bilibili.com"):
            bv = BV_PATTERN.search(url)
            if bv:
                return "bv", bv.group(0)
            av = AV_PATTERN.search(url)
            if av:
                return "av", av.group(1)

    # 2. 再退化到直接在文本里找 BV号 / av号（很多人只发一个 BV 号）
    bv = BV_PATTERN.search(text)
    if bv:
        return "bv", bv.group(0)

    av = AV_PATTERN.search(text)
    if av:
        return "av", av.group(1)

    return None


def _find_page_index(raw: str) -> int:
    """从链接里取分P参数 ?p=2（默认第 1 P）"""
    text = _normalize_text(raw)
    match = re.search(r"[?&]p=(\d+)", text)
    if match:
        try:
            return max(1, int(match.group(1)))
        except ValueError:
            return 1
    return 1


# ========== wbi 签名（新版 playurl 接口必须带签名） ==========

_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_wbi_keys: Optional[Tuple[str, str]] = None
_wbi_keys_time: float = 0.0


async def _get_wbi_keys(client: httpx.AsyncClient) -> Tuple[str, str]:
    """获取 img_key / sub_key（缓存 6 小时）"""
    global _wbi_keys, _wbi_keys_time

    if _wbi_keys and (time.time() - _wbi_keys_time) < 6 * 3600:
        return _wbi_keys

    resp = await client.get("https://api.bilibili.com/x/web-interface/nav", headers=HEADERS)
    data = resp.json().get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_url = wbi_img.get("img_url", "")
    sub_url = wbi_img.get("sub_url", "")
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]

    if not img_key or not sub_key:
        raise RuntimeError("获取 wbi 签名密钥失败")

    _wbi_keys = (img_key, sub_key)
    _wbi_keys_time = time.time()
    return _wbi_keys


def _sign_wbi(params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
    """给请求参数加上 wts 和 w_rid 签名"""
    raw_key = img_key + sub_key
    mixin_key = "".join(raw_key[i] for i in _MIXIN_KEY_ENC_TAB)[:32]

    signed = dict(params)
    signed["wts"] = int(time.time())
    # 过滤掉 !'()* 这几个字符，再按 key 排序拼接
    signed = {
        k: "".join(ch for ch in str(v) if ch not in "!'()*")
        for k, v in sorted(signed.items())
    }
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed


# ========== bilibili 接口封装 ==========


async def _resolve_ref(
    client: httpx.AsyncClient,
    ref: Tuple[str, str],
    depth: int = 0,
) -> Dict[str, Any]:
    """把识别出来的引用统一成 {"bvid": ..., "aid": ..., "page": ...}"""
    kind, value = ref

    if kind == "url":
        if depth >= 3:
            raise ValueError("短链跳转层数过多，无法解析")
        # 短链：跟着跳转拿真实链接
        resp = await client.get(value, headers=HEADERS, follow_redirects=True)
        final_url = str(resp.url)
        logger.info(f"[bilibili] 短链 {value} -> {final_url}")
        page = _find_page_index(final_url)
        inner = _find_raw_ref(final_url)
        if inner is None:
            raise ValueError("短链没有跳转到 bilibili 视频页")
        result = await _resolve_ref(client, inner, depth + 1)
        result["page"] = page
        return result

    if kind == "bv":
        return {"bvid": value, "aid": None, "page": 1}

    return {"bvid": None, "aid": int(value), "page": 1}


async def _get_video_info(client: httpx.AsyncClient, bvid: Optional[str], aid: Optional[int]) -> Dict[str, Any]:
    """获取视频基本信息（标题、封面、UP主、统计、分P等）"""
    params = {"bvid": bvid} if bvid else {"aid": aid}
    resp = await client.get(
        "https://api.bilibili.com/x/web-interface/view",
        params=params,
        headers=HEADERS,
    )
    payload = resp.json()

    if payload.get("code") != 0:
        raise ValueError(f"获取视频信息失败：{payload.get('message') or payload.get('code')}")

    return payload["data"]


async def _get_playurl(
    client: httpx.AsyncClient,
    bvid: Optional[str],
    aid: Optional[int],
    cid: int,
    fnval: int,
    quality: int,
) -> Dict[str, Any]:
    """获取播放地址（fnval=4048 走 DASH 音视频分离，fnval=1 走单文件 mp4）"""
    img_key, sub_key = await _get_wbi_keys(client)

    params: Dict[str, Any] = {
        "cid": cid,
        "qn": quality,
        "fnval": fnval,
        "fnver": 0,
        "fourk": 1,
        "platform": "pc",
        "otype": "json",
    }
    if bvid:
        params["bvid"] = bvid
    else:
        params["avid"] = aid

    resp = await client.get(
        "https://api.bilibili.com/x/player/wbi/playurl",
        params=_sign_wbi(params, img_key, sub_key),
        headers=HEADERS,
    )
    payload = resp.json()
    if payload.get("code") != 0:
        raise ValueError(f"获取播放地址失败：{payload.get('message') or payload.get('code')}")

    return payload["data"]


def _pick_quality(videos) -> Optional[Dict[str, Any]]:
    """
    从 DASH 视频流里挑一条：BILI_QUALITY 排最前（默认：登录 1080P / 未登录 720P），
    其次按 QUALITY_ORDER 降级。
    """
    if not videos:
        return None

    order = []
    if PREFER_QUALITY in QUALITY_NAME:
        order.append(PREFER_QUALITY)
    order.extend(q for q in QUALITY_ORDER if q != PREFER_QUALITY)

    for quality in order:
        same = [v for v in videos if int(v.get("id", 0)) == quality]
        if same:
            # 同一清晰度有多个编码（avc/hevc/av1），优先 avc 兼容性最好
            same.sort(key=lambda v: (v.get("codecid") != 7, int(v.get("bandwidth", 0))))
            return same[0]

    # 都不在候选里就用码率最高的
    return max(videos, key=lambda v: int(v.get("bandwidth", 0)))


def _pick_audio(audios) -> Optional[Dict[str, Any]]:
    """挑一条码率最高的音频流"""
    if not audios:
        return None
    return max(audios, key=lambda a: int(a.get("bandwidth", 0)))


# ========== 下载 / 合并 / 发送 ==========


# ---------- 节点测速 ----------

# {原始节点: (选中的节点, 记录时间)}
_MIRROR_CACHE: Dict[str, Tuple[str, float]] = {}


def _netloc(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def _dedup_urls(urls: List[str]) -> List[str]:
    """按 host 去重，保持先后顺序"""
    out: List[str] = []
    seen = set()
    for url in urls:
        if not url:
            continue
        host = _netloc(url)
        if host in seen:
            continue
        seen.add(host)
        out.append(url)
    return out


def _expand_mirror_urls(urls: List[str]) -> List[str]:
    """原文地址 + 备用地址 + 换 host 的公共镜像，按 host 去重"""
    candidates = _dedup_urls(list(urls))
    base = candidates[0]
    parsed = urllib.parse.urlsplit(base)
    known = {_netloc(u) for u in candidates}

    for host in MIRROR_HOSTS:
        if host in known:
            continue
        known.add(host)
        candidates.append(
            urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
        )

    return candidates


async def _probe_speed(client: httpx.AsyncClient, url: str, probe_bytes: int) -> Optional[float]:
    """拉一小段数据测速，返回 MB/s（失败或超时返回 None）"""
    headers = dict(STREAM_HEADERS)
    headers["Range"] = f"bytes=0-{probe_bytes - 1}"
    start = time.perf_counter()
    size = 0

    try:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                return None
            async for chunk in resp.aiter_bytes(64 * 1024):
                size += len(chunk)
                if size >= probe_bytes:
                    break
    except Exception:
        return None

    cost = time.perf_counter() - start
    if size <= 0 or cost <= 0:
        return None
    return size / cost / 1048576


async def _rank_urls(client: httpx.AsyncClient, urls: List[str], size: Optional[int], label: str = "") -> List[str]:
    """
    给下载地址排序：先小流量测速，返回"最快节点在前"的候选列表。

    - 关掉 BILI_MIRROR_PROBE 或没有镜像候选时，直接返回原顺序
    - 第一个节点就够快（>= BILI_FAST_THRESHOLD_MBS）时不做镜像测速
    - 最近 30 分钟给同一个原始节点选过的结果会走缓存
    """
    if not MIRROR_PROBE:
        return _dedup_urls(urls)

    cache_key = _netloc(urls[0])
    cached = _MIRROR_CACHE.get(cache_key)
    if cached and time.time() - cached[1] < MIRROR_CACHE_TTL:
        chosen_host = cached[0]
        if any(_netloc(u) == chosen_host for u in urls):
            return _dedup_urls(urls)
        candidates = _expand_mirror_urls(urls)
        candidates.sort(key=lambda u: _netloc(u) != chosen_host)
        logger.info(f"[bilibili] {label}沿用最近选中的节点：{chosen_host}")
        return candidates

    candidates = _expand_mirror_urls(urls)
    if len(candidates) <= 1:
        return candidates

    # 第一步：先测原始节点，够快就不折腾
    base_speed = await _probe_speed(client, candidates[0], PROBE_BYTES)
    if base_speed is not None and base_speed >= FAST_THRESHOLD_MBS:
        logger.info(f"[bilibili] {label}节点 {_netloc(candidates[0])} 速度 {base_speed:.1f} MB/s，够快，不切换")
        _MIRROR_CACHE[cache_key] = (_netloc(candidates[0]), time.time())
        return candidates

    # 第二步：并发测所有候选
    others = candidates[1:]
    results = await asyncio.gather(
        *[asyncio.wait_for(_probe_speed(client, u, PROBE_BYTES), timeout=PROBE_TIMEOUT + 2) for u in others],
        return_exceptions=True,
    )
    scored: List[Tuple[float, str]] = []
    if base_speed is not None:
        scored.append((base_speed, candidates[0]))
    for url, speed in zip(others, results):
        if isinstance(speed, float):
            scored.append((speed, url))

    if not scored:
        logger.warning(f"[bilibili] {label}节点测速全部失败，继续用原始地址")
        return candidates

    scored.sort(reverse=True, key=lambda item: item[0])
    best_speed, best_url = scored[0]
    detail = "，".join(f"{_netloc(u)} {s:.1f}MB/s" for s, u in scored[:4])
    logger.info(f"[bilibili] {label}节点测速：{detail} → 选用 {_netloc(best_url)}（{best_speed:.1f} MB/s）")

    _MIRROR_CACHE[cache_key] = (_netloc(best_url), time.time())
    return [url for _, url in scored] + [u for u in candidates if u not in [x for _, x in scored]]


# ---------- 分片多连接下载 ----------


async def _probe_size(client: httpx.AsyncClient, url: str) -> Tuple[Optional[int], bool]:
    """探测文件大小以及服务端是否支持 Range 分片，返回 (大小, 是否支持 Range)"""
    headers = dict(STREAM_HEADERS)
    headers["Range"] = "bytes=0-0"
    try:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            if resp.status_code == 206:
                total = (resp.headers.get("content-range") or "").rsplit("/", 1)[-1]
                return (int(total) if total.isdigit() else None), True
            if resp.status_code == 200:
                length = resp.headers.get("content-length")
                return (int(length) if length and length.isdigit() else None), False
    except Exception as e:
        logger.warning(f"[bilibili] 探测下载地址失败（{_netloc(url)}）：{e}")
    return None, False


def _split_ranges(size: int, threads: int) -> List[Tuple[int, int]]:
    """把文件切成若干分片，返回 [(起始字节, 结束字节), ...]"""
    threads = max(1, min(threads, max(1, (size + MIN_CHUNK_BYTES - 1) // MIN_CHUNK_BYTES)))
    step = size // threads
    ranges: List[Tuple[int, int]] = []
    start = 0
    for index in range(threads):
        end = size - 1 if index == threads - 1 else start + step - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


async def _progress_reporter(counter: Dict[str, int], total: int, label: str, interval: float = 5.0) -> None:
    """下载过程中定期汇报进度和平均速度"""
    start = time.perf_counter()
    while True:
        await asyncio.sleep(interval)
        done = counter.get("done", 0)
        cost = time.perf_counter() - start
        if cost <= 0:
            continue
        logger.info(
            f"[bilibili] {label}下载中：{done / 1048576:.1f}/{total / 1048576:.1f} MB，"
            f"平均 {done / cost / 1048576:.1f} MB/s"
        )


async def _download_chunk(
    client: httpx.AsyncClient,
    urls: List[str],
    part: Path,
    start: int,
    end: int,
    counter: Dict[str, int],
    label: str,
) -> int:
    """下载一个分片到独立的临时文件（失败会换节点重试）"""
    headers = dict(STREAM_HEADERS)
    headers["Range"] = f"bytes={start}-{end}"
    expect = end - start + 1
    last_error: Optional[Exception] = None

    for url in urls[:3]:
        for _ in range(2):
            written = 0
            try:
                part.parent.mkdir(parents=True, exist_ok=True)
                with open(part, "wb") as fp:
                    async with client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            fp.write(chunk)
                            written += len(chunk)
                            counter["done"] += len(chunk)

                if written == expect:
                    return written

                last_error = ValueError(f"分片体积不符：{written}/{expect}")
            except Exception as e:
                last_error = e
            # 只有失败 / 体积不符才会走到这里，把这一轮计过的字节扣回去再重试
            counter["done"] -= written

    raise last_error or RuntimeError("分片下载失败")


async def _download_multi(
    client: httpx.AsyncClient,
    urls: List[str],
    dest: Path,
    size: int,
    label: str,
    threads: int,
) -> int:
    """分片并发下载，最后按顺序拼接成完整文件"""
    ranges = _split_ranges(size, threads)
    parts = [dest.with_name(f"{dest.name}.part{index}") for index in range(len(ranges))]
    counter = {"done": 0}
    logger.info(f"[bilibili] {label}启用 {len(ranges)} 连接分片下载，文件 {size / 1048576:.1f} MB")

    reporter = asyncio.create_task(_progress_reporter(counter, size, label))
    started = time.perf_counter()
    try:
        results = await asyncio.gather(
            *[
                _download_chunk(client, urls, part, start, end, counter, label)
                for part, (start, end) in zip(parts, ranges)
            ]
        )
    finally:
        reporter.cancel()
        with suppress(asyncio.CancelledError):
            await reporter

    total = sum(results)
    if total != size:
        raise ValueError(f"分片合计体积不符：{total}/{size}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        for part in parts:
            with open(part, "rb") as fp:
                shutil.copyfileobj(fp, out, 1024 * 1024)
            part.unlink(missing_ok=True)

    cost = time.perf_counter() - started
    logger.info(
        f"[bilibili] {label}完成：{size / 1048576:.1f} MB，用时 {cost:.1f}s"
        f"（平均 {size / cost / 1048576 if cost else 0:.1f} MB/s）"
    )
    return dest.stat().st_size


async def _download_stream(client: httpx.AsyncClient, urls: List[str], dest: Path, label: str) -> int:
    """单连接顺序下载（小文件，或服务端不支持分片时的兜底）"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Optional[Exception] = None

    for url in urls:
        size = 0
        started = time.perf_counter()
        last_log = started
        try:
            with open(dest, "wb") as fp:
                async with client.stream("GET", url, headers=STREAM_HEADERS, follow_redirects=True) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        if not chunk:
                            continue
                        fp.write(chunk)
                        size += len(chunk)
                        now = time.perf_counter()
                        if label and now - last_log > 10:
                            last_log = now
                            logger.info(
                                f"[bilibili] {label}下载中：{size / 1048576:.1f} MB，"
                                f"平均 {size / (now - started) / 1048576:.1f} MB/s"
                            )

            if size:
                cost = time.perf_counter() - started
                logger.info(
                    f"[bilibili] {label}完成（单连接 {_netloc(url)}）：{size / 1048576:.1f} MB，"
                    f"用时 {cost:.1f}s（平均 {size / cost / 1048576 if cost else 0:.1f} MB/s）"
                )
                return size

            last_error = ValueError("下载内容为空")
        except Exception as e:
            last_error = e
            logger.warning(f"[bilibili] {label}从 {_netloc(url)} 下载失败：{e}")

    raise last_error or RuntimeError("下载失败")


async def _download_file(
    client: httpx.AsyncClient,
    urls: Any,
    dest: Path,
    label: str = "",
    probe: bool = True,
) -> int:
    """
    下载入口：先择优选节点，再优先用多连接分片下载，失败或不支持 Range 时退回单连接。

    urls 可以传字符串，也可以传 [主地址, 备用地址...]
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = _dedup_urls(list(urls))
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1. 探测大小 + 是否支持分片
    size, supports_range = await _probe_size(client, urls[0])

    # 2. 择优选节点（小文件不值得测速，封面图也不需要）
    if probe and (size is None or size >= PROBE_MIN_BYTES):
        urls = await _rank_urls(client, urls, size, label)

    # 3. 多连接分片下载
    if supports_range and size and size >= MIN_CHUNK_BYTES and DOWNLOAD_THREADS > 1:
        try:
            return await _download_multi(client, urls, dest, size, label, DOWNLOAD_THREADS)
        except Exception as e:
            logger.warning(f"[bilibili] {label}多连接下载失败，改用单连接重试：{e}")
            for part in dest.parent.glob(f"{dest.name}.part*"):
                part.unlink(missing_ok=True)

    # 4. 单连接兜底
    return await _download_stream(client, urls, dest, label)


async def _merge_av(video: Path, audio: Path, dest: Path) -> bool:
    """用 ffmpeg 把 DASH 的视频流和音频流合并成 mp4"""
    if not FFMPEG_PATH:
        return False

    proc = await asyncio.create_subprocess_exec(
        FFMPEG_PATH, "-y",
        "-i", str(video),
        "-i", str(audio),
        "-c", "copy",
        "-movflags", "+faststart",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"[bilibili] ffmpeg 合并失败：{stderr.decode('utf-8', 'ignore')[-500:]}")
        return False

    return dest.exists() and dest.stat().st_size > 0


def _safe_filename(title: str, suffix: str) -> str:
    """把视频标题转成安全的文件名"""
    # [] 也要去掉：CQ 码里中括号需要转义，避免部分实现解析文件名出错
    name = re.sub(r'[\\/:*?"<>|\[\]\r\n\t]+', "_", title).strip(" .")
    return f"{name[:60] or 'bilibili_video'}{suffix}"


def _video_segment(path: Path) -> MessageSegment:
    """
    构造视频消息段。
    直接传文件路径（file:///）比 base64 更省内存，NapCat / go-cqhttp 都支持。
    这里用 MessageSegment("video", {...}) 的写法，避免适配器版本差异导致找不到 video() 方法。
    """
    return MessageSegment("video", {"file": f"file:///{path.as_posix()}"})


async def _send_video(matcher, path: Path) -> str:
    """
    发送视频文件，本地路径失败时用 base64 兜底。

    返回状态：
      "sent"      已发送
      "too_large" 超过体积上限，主动不发（文件由调用方决定去留）
      "failed"    发送失败（文件由调用方决定去留）
    """
    size = path.stat().st_size
    size_mb = size / 1048576

    if size_mb > MAX_VIDEO_MB:
        logger.warning(f"[bilibili] 视频 {size_mb:.1f} MB 超过上限 {MAX_VIDEO_MB} MB，不发送")
        return "too_large"

    try:
        await matcher.send(_video_segment(path))
        logger.info(f"[bilibili] 视频发送完成：{path.name}（{size_mb:.1f} MB）")
        return "sent"
    except Exception as e:
        logger.warning(f"[bilibili] 用本地路径发送视频失败，尝试 base64：{e}")

    if size_mb <= MAX_BASE64_MB:
        try:
            data = base64.b64encode(path.read_bytes()).decode()
            await matcher.send(MessageSegment("video", {"file": f"base64://{data}"}))
            logger.info(f"[bilibili] 视频已用 base64 发送：{path.name}")
            return "sent"
        except Exception as e:
            logger.error(f"[bilibili] base64 发送视频失败：{e}")

    return "failed"


def _preserve_file(path: Path) -> Tuple[Path, bool]:
    """
    把文件挪到不会被自动清理的目录，返回 (最终路径, 是否挪成功)。
    挪成功后临时目录里就不再保留这份文件，提示里的路径才是真实存在的。
    """
    try:
        target_dir = SAVE_DIR / "saved"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            target = target_dir / f"{int(time.time())}_{path.name}"
        shutil.move(str(path), str(target))
        logger.info(f"[bilibili] 文件已保留到：{target}")
        return target, True
    except Exception as e:
        logger.error(f"[bilibili] 保留文件失败：{e}")
        return path, False


async def _report_unsent(matcher, path: Path, reason: str) -> bool:
    """
    没能发送时：先把文件挪到保留目录，再如实告诉用户路径。
    返回是否需要连整个临时目录一起保留（挪文件失败时该目录就是唯一副本）。
    """
    kept, moved = _preserve_file(path)
    if moved:
        # 路径只写进控制台日志，聊天里默认只给相对路径
        logger.info(f"[bilibili] 文件已保留在：{kept}")
        await matcher.send(f"⚠️ {reason}。\n📁 文件已保留在机器人的保留目录：{_display_path(kept)}")
        return False

    logger.info(f"[bilibili] 文件留在临时目录：{path.parent}")
    await matcher.send(f"⚠️ {reason}，文件暂时留在临时目录：{_display_path(path.parent)}")
    return True


# ========== 工具函数 ==========


def _display_path(path: Any) -> str:
    """
    生成"发给聊天看的"路径：
      · 默认（BILI_SHOW_PATH=false）只显示相对项目根目录的写法，例如 data/bilibili/saved/x.mp4
      · 不在项目内的路径直接遮成 <本机路径>
      · 设 BILI_SHOW_PATH=true 时显示完整绝对路径
    """
    if SHOW_PATH:
        return str(path)
    try:
        return Path(path).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except Exception:
        return hide_local_paths(str(path))


def _fmt_count(count: Any) -> str:
    """播放量等数字按中文习惯格式化：12345 -> 1.2万"""
    if count is None:
        return "0"
    try:
        num = int(count)
    except (TypeError, ValueError):
        return str(count)

    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def _fmt_duration(seconds: Any) -> str:
    """秒数 -> hh:mm:ss / mm:ss"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "未知"

    hour, rem = divmod(total, 3600)
    minute, second = divmod(rem, 60)
    if hour:
        return f"{hour}:{minute:02d}:{second:02d}"
    return f"{minute}:{second:02d}"


def _get_semaphore() -> asyncio.Semaphore:
    """下载并发锁（延迟创建，避免和事件循环绑定问题）"""
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _download_semaphore


_download_semaphore: Optional[asyncio.Semaphore] = None

# 处理冷却：{(会话, BV号): 上次处理时间}
_recent: Dict[str, float] = {}


def _session_key(event: Event) -> str:
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"group_{group_id}"
    return f"user_{getattr(event, 'user_id', 'unknown')}"


def _is_duplicated(key: str) -> bool:
    """同一个视频在同一会话里短时间内只处理一次，避免刷屏 + 重复下载"""
    now = time.time()
    last = _recent.get(key)

    # 顺手清理过期记录
    for old_key in [k for k, t in _recent.items() if now - t > DEDUP_SECONDS]:
        _recent.pop(old_key, None)

    if last and now - last < DEDUP_SECONDS:
        return True

    _recent[key] = now
    return False


def _build_info_message(info: Dict[str, Any], page_title: str, page: int) -> str:
    """拼装视频信息文本"""
    stat = info.get("stat") or {}
    owner = info.get("owner") or {}
    title = info.get("title", "未知标题")
    if page_title and page_title != title:
        title = f"{title}（P{page} {page_title}）"

    lines = [
        "📺 【B站视频】",
        f"标题：{title}",
        f"UP主：{owner.get('name', '未知')}",
        f"时长：{_fmt_duration(info.get('duration'))}",
        "",
        f"▶️ 播放：{_fmt_count(stat.get('view'))}    "
        f"👍 点赞：{_fmt_count(stat.get('like'))}    "
        f"⭐ 收藏：{_fmt_count(stat.get('favorite'))}",
        f"🪙 投币：{_fmt_count(stat.get('coin'))}    "
        f"💬 弹幕：{_fmt_count(stat.get('danmaku'))}    "
        f"🔁 转发：{_fmt_count(stat.get('share'))}",
        f"📅 投稿时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(info.get('pubdate', 0)))}",
        f"🔗 https://www.bilibili.com/video/{info.get('bvid', '')}",
    ]
    return "\n".join(lines)


async def _download_cover(client: httpx.AsyncClient, info: Dict[str, Any], work_dir: Path) -> Optional[Path]:
    """下载封面图到本地（用本地文件发送，比让 QQ 去拉外链更稳）"""
    pic_url = info.get("pic")
    if not pic_url:
        return None

    suffix = Path(urllib.parse.urlparse(pic_url).path).suffix or ".jpg"
    dest = work_dir / f"cover{suffix}"
    try:
        # 封面图很小，不用测速找镜像
        await _download_file(client, pic_url, dest, probe=False)
        return dest
    except Exception as e:
        logger.warning(f"[bilibili] 下载封面失败：{e}")
        return None


async def _download_video_file(
    client: httpx.AsyncClient,
    bvid: Optional[str],
    aid: Optional[int],
    cid: int,
    title: str,
    work_dir: Path,
) -> Tuple[Optional[Path], str]:
    """
    下载视频文件，返回 (文件路径, 清晰度说明)

    两条路的清晰度上限不一样：
    1. DASH（音视频分离）+ ffmpeg 合并 -> 登录态下最高 1080P/4K，但免登录时只有 480P
    2. fnval=1 单文件 mp4            -> 自带音轨，免登录也能拿到 720P

    所以只有"配了 BILI_SESSDATA 且本机有 ffmpeg"时才优先 DASH，
    否则优先单文件 mp4：避免出现"装了 ffmpeg 但没登录，清晰度反而从 720P 掉到 480P"。
    """
    prefer_dash = (FFMPEG_PATH is not None) and bool(COOKIE)
    if not prefer_dash and FFMPEG_PATH and not COOKIE:
        logger.info("[bilibili] 未配置 BILI_SESSDATA，优先走单文件 mp4（免登录 720P），避免 DASH 只有 480P")
    order = [_download_dash, _download_single] if prefer_dash else [_download_single, _download_dash]

    for downloader in order:
        result = await downloader(client, bvid, aid, cid, title, work_dir)
        if result:
            return result

    return None, "未知"


async def _download_dash(
    client: httpx.AsyncClient,
    bvid: Optional[str],
    aid: Optional[int],
    cid: int,
    title: str,
    work_dir: Path,
) -> Optional[Tuple[Path, str]]:
    """DASH 音视频分离 + ffmpeg 合并（清晰度最高，需要 ffmpeg）"""
    try:
        play = await _get_playurl(client, bvid, aid, cid, fnval=4048, quality=max(PREFER_QUALITY, 80))
        dash = play.get("dash") or {}
        video_stream = _pick_quality(dash.get("video") or [])
        audio_stream = _pick_audio(dash.get("audio") or [])

        if video_stream:
            quality_id = int(video_stream.get("id", 0))
            quality_desc = QUALITY_NAME.get(quality_id, f"id={quality_id}")
            if quality_id < 80:
                quality_desc += "（未登录态只能拿到这个清晰度，配置 BILI_SESSDATA 可解锁 1080P）"

            video_urls = [video_stream.get("baseUrl")] + list(video_stream.get("backupUrl") or [])
            video_file = work_dir / "video.m4s"
            logger.info(f"[bilibili] 开始下载视频流：{quality_desc}")

            if audio_stream:
                audio_urls = [audio_stream.get("baseUrl")] + list(audio_stream.get("backupUrl") or [])
                audio_file = work_dir / "audio.m4s"
                logger.info("[bilibili] 开始下载音频流")
                # 视频流和音频流同时下，省掉一半等待时间
                await asyncio.gather(
                    _download_file(client, video_urls, video_file, "视频流"),
                    _download_file(client, audio_urls, audio_file, "音频流"),
                )

                merged = work_dir / _safe_filename(title, ".mp4")
                if await _merge_av(video_file, audio_file, merged):
                    return merged, quality_desc

                logger.warning("[bilibili] 没有找到 ffmpeg（或合并失败），改用单文件接口试试")
            else:
                await _download_file(client, video_urls, video_file, "视频流")
                # 没有音频流：直接当无声视频发（少见）
                no_audio = work_dir / _safe_filename(title, ".mp4")
                video_file.replace(no_audio)
                return no_audio, quality_desc + "（无音轨）"
    except Exception as e:
        logger.warning(f"[bilibili] DASH 下载失败：{e}")

    return None


async def _download_single(
    client: httpx.AsyncClient,
    bvid: Optional[str],
    aid: Optional[int],
    cid: int,
    title: str,
    work_dir: Path,
) -> Optional[Tuple[Path, str]]:
    """fnval=1 单文件 mp4（自带音轨，免登录也能拿到 720P）"""
    try:
        # 单文件接口按配置的目标清晰度请求（默认 720P），避免登录后拿到超出目标的体积
        play = await _get_playurl(client, bvid, aid, cid, fnval=1, quality=PREFER_QUALITY)
        durl = play.get("durl") or []
        if not durl:
            raise ValueError("单文件接口没有返回 durl")

        quality_id = int(play.get("quality", 0))
        quality_desc = QUALITY_NAME.get(quality_id, f"id={quality_id}")
        urls = [durl[0].get("url")] + list(durl[0].get("backup_url") or [])

        dest = work_dir / _safe_filename(title, ".mp4")
        logger.info(f"[bilibili] 开始下载单文件视频：{quality_desc}")
        await _download_file(client, urls, dest, "视频文件")
        return dest, quality_desc + "（单文件模式）"
    except Exception as e:
        logger.warning(f"[bilibili] 单文件下载失败：{e}")
        return None


# ========== 消息监听 ==========

# 注意：优先级必须小于 10（数值越小越先执行）。
# 因为 chat.py 里有一个无规则的 on_message(priority=10, block=True) 拦截器，
# 它会终止传播，任何优先级 >= 10 的匹配器（例如后面的插件）都收不到普通消息。
# block=False 保证我们只是"顺便回复"，不影响其它插件。
bili = on_message(priority=6, block=False)


@bili.handle()
async def handle_bilibili(bot: Bot, event: Event):
    """监听到包含 bilibili 视频链接的消息就解析并下载"""
    # 忽略机器人自己发的消息，避免死循环
    if str(getattr(event, "user_id", "")) == str(bot.self_id):
        return

    # 只从文本/分享卡片段里找链接，媒体段（图片、表情包等）不参与，避免误判
    raw = _collect_text(event.get_message())
    ref = _find_raw_ref(raw)
    if ref is None:
        return

    page = _find_page_index(raw)
    session = _session_key(event)
    # 用毫秒时间戳建临时目录，避免同一秒内的两个请求互相覆盖
    work_dir = SAVE_DIR / "cache" / f"{int(time.time() * 1000)}_{getattr(event, 'user_id', 'x')}"
    dedup_key: Optional[str] = None  # 失败时清掉，方便群友重发
    video_sent = False
    processed = False      # 视频已下载完（决定冷却是否保留）
    keep_work_dir = False  # 文件挪不出去时，临时目录不能删

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 1. 把链接（含短链）解析成 bvid / aid
            target = await _resolve_ref(client, ref)
            if page > 1:
                target["page"] = page
            bvid, aid = target.get("bvid"), target.get("aid")

            # 2. 拉取视频信息
            info = await _get_video_info(client, bvid, aid)
            bvid, aid = info.get("bvid"), info.get("aid")

            dedup_key = f"{session}_{bvid}"
            if _is_duplicated(dedup_key):
                logger.info(f"[bilibili] {bvid} 在 {DEDUP_SECONDS} 秒内处理过，跳过")
                dedup_key = None
                return

            # 3. 找到对应分P（cid / 分P标题）
            pages = info.get("pages") or []
            page_index = min(max(target.get("page", 1), 1), len(pages) or 1) - 1
            page_info = pages[page_index] if pages else {}
            cid = page_info.get("cid") or info.get("cid")
            page_title = page_info.get("part", "")

            logger.info(
                f"[bilibili] 解析 {bvid}：{info.get('title')} "
                f"（UP：{(info.get('owner') or {}).get('name')}，cid={cid}）"
            )

            # 4. 发送视频基本信息 + 封面
            work_dir.mkdir(parents=True, exist_ok=True)
            info_text = _build_info_message(info, page_title, page_index + 1)
            cover = await _download_cover(client, info, work_dir)
            if cover:
                await bili.send(Message([
                    MessageSegment.text(info_text + "\n\n"),
                    MessageSegment("image", {"file": f"file:///{cover.as_posix()}"}),
                ]))
            else:
                await bili.send(info_text)

            if not SEND_VIDEO:
                return

            # 5. 下载视频文件
            target_quality = QUALITY_NAME.get(PREFER_QUALITY, "视频")
            if FFMPEG_PATH and COOKIE:
                hint = f"⏳ 正在下载 {target_quality} 视频文件，请稍候…（视频较长时会更久）"
            else:
                hint = (
                    f"⏳ 正在下载视频文件（目标 {target_quality}），请稍候…"
                    "未配置 BILI_SESSDATA 时免登录清晰度有限，实际清晰度以下载结果为准"
                )
            await bili.send(hint)

            async with _get_semaphore():
                video_path, quality_desc = await _download_video_file(
                    client, bvid, aid, cid, info.get("title", bvid), work_dir
                )

            if not video_path or not video_path.exists():
                await bili.send("❌ 视频下载失败，可能是清晰度需要登录或接口被限流，稍后再试试~")
                return

            logger.info(f"[bilibili] 视频下载完成：{video_path}（{quality_desc}）")
            await bili.send(f"📥 实际清晰度：{quality_desc}")
            processed = True

            status = await _send_video(bili, video_path)
            if status == "sent":
                video_sent = True
            elif status == "too_large":
                # 体积超限不发，但文件要真的留在本地（之前提示了却没留）
                keep_work_dir = await _report_unsent(
                    bili, video_path, f"视频体积 {video_path.stat().st_size / 1048576:.1f} MB 超过上限 {MAX_VIDEO_MB} MB，没有发送"
                )
            else:
                # 发送失败时允许重发链接重试
                processed = False
                keep_work_dir = await _report_unsent(bili, video_path, "视频发送失败")

    except ValueError as e:
        # 接口层面的常规错误（视频不存在、被删、需要登录等）
        logger.warning(f"[bilibili] 解析失败：{e}")
        await bili.send(f"❌ 解析失败：{hide_local_paths(str(e))}")
    except httpx.HTTPError as e:
        logger.error(f"[bilibili] 网络请求失败：{e}")
        await bili.send("🌐 网络请求失败，请稍后重试~")
    except Exception as e:
        logger.exception(f"[bilibili] 处理异常：{e}")
        await bili.send(f"❌ 处理出错了：{hide_local_paths(str(e))}")
    finally:
        # 处理失败时解除冷却，允许重新发送链接重试
        if dedup_key and not processed:
            _recent.pop(dedup_key, None)

        # 清理临时文件（封面、音视频流）
        if KEEP_FILE:
            logger.info(f"[bilibili] 文件已保留在：{work_dir}")
        elif keep_work_dir:
            logger.warning(f"[bilibili] 文件没能挪出临时目录，保留该目录：{work_dir}")
        else:
            if video_sent:
                # 给 NapCat / go-cqhttp 留一点读取文件的时间，避免刚发送就删除
                await asyncio.sleep(2)
            shutil.rmtree(work_dir, ignore_errors=True)


# ========== 帮助命令 ==========

bili_help = on_command("b站帮助", aliases={"B站帮助", "bili帮助", "视频解析帮助"}, priority=10)


@bili_help.handle()
async def handle_bili_help(bot: Bot, event: Event):
    """显示 bilibili 插件帮助"""
    login_state = "已配置 SESSDATA（可以拿 1080P）" if COOKIE else "未配置 SESSDATA（通常只能拿 480P/720P）"
    if FFMPEG_PATH:
        # 默认只说"已安装"，不暴露本机目录（BILI_SHOW_PATH=true 时才带路径）
        ffmpeg_state = f"已安装（{_display_path(FFMPEG_PATH)}）" if SHOW_PATH else "已安装"
    else:
        ffmpeg_state = "未安装（会走单文件 mp4，免登录约 720P）"
    help_text = f"""
📺 B站视频解析帮助：

直接在群里发 bilibili 视频链接即可，机器人会自动：
  1️⃣ 爬取视频信息（标题/UP主/播放量/点赞/收藏/投币/封面图）并发送
  2️⃣ 下载视频文件并发送（多连接分片 + 节点测速择优，尽量跑满带宽）

支持的链接形式：
  https://www.bilibili.com/video/BV1xx411c7mD
  https://www.bilibili.com/video/av170001
  https://b23.tv/xxxxxxx（短链）
  BV1xx411c7mD（只发 BV 号也行）
  QQ 分享的B站小程序卡片
  带分P的链接：...?p=2（会解析对应的那一 P）

当前状态：
  🔑 登录态：{login_state}
  🎬 ffmpeg：{ffmpeg_state}
  🎯 目标清晰度：{QUALITY_NAME.get(PREFER_QUALITY, PREFER_QUALITY)}
  ⚡ 分片下载：{DOWNLOAD_THREADS} 连接/文件（节点测速：{"开启" if MIRROR_PROBE else "关闭"}）
  📁 保存目录：{_display_path(SAVE_DIR)}
  📦 体积上限：{MAX_VIDEO_MB} MB
  📬 超限/失败的文件保存在：{_display_path(SAVE_DIR / "saved")}
"""
    await bili_help.finish(help_text.strip())
