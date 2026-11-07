# src/libraries/cover_source.py
"""
曲绘补全：本地没有封面时按顺序从多个源下载并缓存。

源优先级：
    1. 落雪 LXNS  https://assets.lxns.net/maimai/jacket/{base_id}.png
       （新曲更全，DX 曲目用去掉 10000 偏移的基准 ID）
    2. 水鱼       https://www.diving-fish.com/covers/{5位ID}.png
    3. 自定义模板 COVER_URL_TEMPLATE，例如 https://example.com/{id}.png

下载成功会写入 src/static/mai/cover/{5位ID}.png，后续直接命中本地。
"""

import asyncio
import os
from pathlib import Path
from typing import Iterable

import httpx

from src.libraries.maimai_best50_new import COVER_DIR
from src.libraries.maimaidx_music import get_cover_len5_id

HEADERS = {"User-Agent": "mai-bot/1.0"}
TIMEOUT = float(os.getenv("COVER_TIMEOUT", "8"))
CONCURRENCY = max(1, min(8, int(os.getenv("COVER_CONCURRENCY", "4"))))
MIN_BYTES = 2000

def base_id(song_id) -> str:
    return str(int(song_id) % 10000)


def local_path(song_id) -> Path:
    return COVER_DIR / f"{get_cover_len5_id(song_id)}.png"


def _candidates(song_id) -> list[str]:
    cover5 = get_cover_len5_id(song_id)
    base = base_id(song_id)
    urls = [
        f"https://assets.lxns.net/maimai/jacket/{base}.png",
        f"https://www.diving-fish.com/covers/{cover5}.png",
    ]
    if base != cover5:
        urls.append(f"https://assets.lxns.net/maimai/jacket/{cover5}.png")
    template = os.getenv("COVER_URL_TEMPLATE")
    if template:
        urls.insert(0, template.format(id=cover5, base=base))
    return urls


def _looks_like_image(payload: bytes) -> bool:
    if len(payload) < MIN_BYTES:
        return False
    return payload[:8].startswith(b"\x89PNG") or payload[:3] == b"\xff\xd8\xff"


async def _download_one(client: httpx.AsyncClient, song_id) -> bool:
    target = local_path(song_id)
    if target.is_file() and target.stat().st_size >= MIN_BYTES:
        return True

    for url in _candidates(song_id):
        try:
            response = await client.get(url)
            if response.status_code == 200 and _looks_like_image(response.content):
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".png.tmp")
                tmp.write_bytes(response.content)
                tmp.replace(target)
                return True
        except Exception:  # noqa: BLE001 - 单个源失败继续试下一个
            continue
    return False


async def ensure_covers(song_ids: Iterable) -> int:
    """给一批曲子补封面，返回本次成功下载的数量"""
    ids = []
    seen = set()
    for song_id in song_ids:
        try:
            key = int(song_id)
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        ids.append(key)

    if not ids:
        return 0

    downloaded = 0
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True, headers=HEADERS
    ) as client:
        async def worker(song_id):
            nonlocal downloaded
            async with semaphore:
                existed = local_path(song_id).is_file()
                if await _download_one(client, song_id) and not existed:
                    downloaded += 1

        await asyncio.gather(*(worker(song_id) for song_id in ids))

    return downloaded
