# src/libraries/song_alias.py
"""
曲目别名库（用于「XXX是什么歌」和查歌）。

数据来源优先级：
    1. data/aliases.json（本地文件，离线可用）
    2. Yuzu 别名接口（ALIAS_API 可覆盖，默认沿用 maimai.py 的地址）

aliases.json 支持下面几种写法，任选一种：
    {"11568": ["不知死活", "internet overdose"]}
    {"content": [{"SongID": 11568, "Alias": ["不知死活"]}]}
    {"不知死活": 11568}
    [{"id": 11568, "aliases": ["不知死活"]}]

别名按 SongID % 10000 同时挂到 SD / DX 两种编号上，和 maimai.py 的做法一致。
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import httpx

from src.libraries.config_env import load_project_env

load_project_env()

ROOT = Path(__file__).resolve().parents[2]
ALIAS_PATH = Path(os.getenv("ALIAS_FILE") or (ROOT / "data" / "aliases.json"))
DEFAULT_API = "https://www.yuzuchan.moe/api/maimaidx/maimaidxalias"
CACHE_SECONDS = int(os.getenv("ALIAS_CACHE_DAYS", "7")) * 86400
FETCH_TIMEOUT = float(os.getenv("ALIAS_TIMEOUT", "6"))
AUTO_FETCH = os.getenv("ALIAS_AUTO_FETCH", "true").lower() in ("1", "true", "yes", "on")

_BY_ID: Optional[Dict[int, List[str]]] = None
_LOAD_FAILED = False


def _add(mapping: Dict[int, List[str]], song_id: int, aliases: Iterable[str]) -> None:
    cleaned = [str(alias).strip() for alias in aliases if str(alias).strip()]
    if not cleaned:
        return
    for key in {int(song_id), int(song_id) % 10000, (int(song_id) % 10000) + 10000}:
        bucket = mapping.setdefault(key, [])
        for alias in cleaned:
            if alias not in bucket:
                bucket.append(alias)


def parse(payload) -> Dict[int, List[str]]:
    mapping: Dict[int, List[str]] = {}
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        payload = payload["content"]

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            song_id = item.get("SongID") or item.get("id") or item.get("song_id")
            aliases = item.get("Alias") or item.get("aliases") or item.get("alias")
            if song_id is None or aliases is None:
                continue
            if isinstance(aliases, str):
                aliases = [aliases]
            try:
                _add(mapping, int(song_id), aliases)
            except (TypeError, ValueError):
                continue
        return mapping

    if isinstance(payload, dict):
        # {"alias": song_id} 反查表
        reverse = all(isinstance(v, (int, str)) and str(v).isdigit() for v in payload.values())
        if reverse and all(not str(k).isdigit() for k in payload):
            for alias, song_id in payload.items():
                try:
                    _add(mapping, int(song_id), [alias])
                except (TypeError, ValueError):
                    continue
            return mapping
        for key, value in payload.items():
            if not str(key).isdigit():
                continue
            aliases = [value] if isinstance(value, str) else value
            if isinstance(aliases, list):
                _add(mapping, int(key), aliases)
    return mapping


def _read_local() -> Dict[int, List[str]]:
    if not ALIAS_PATH.is_file():
        return {}
    try:
        return parse(json.loads(ALIAS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _save(payload) -> None:
    try:
        ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ALIAS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(ALIAS_PATH)
    except OSError:
        pass


async def ensure_loaded() -> Dict[int, List[str]]:
    """加载别名；本地没有就尝试拉一次，失败只记一次，不影响查歌"""
    global _BY_ID, _LOAD_FAILED
    if _BY_ID is not None:
        return _BY_ID

    local = _read_local()
    if local and ALIAS_PATH.stat().st_mtime + CACHE_SECONDS > time.time():
        _BY_ID = local
        return local

    if AUTO_FETCH and not _LOAD_FAILED:
        url = os.getenv("ALIAS_API") or DEFAULT_API
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, headers={"User-Agent": "mai-bot/1.0"})
            if response.status_code == 200:
                payload = response.json()
                fetched = parse(payload)
                if fetched:
                    _save(payload)
                    _BY_ID = fetched
                    return fetched
        except Exception:  # noqa: BLE001 - 网络问题不该阻断查歌
            pass
        _LOAD_FAILED = True

    _BY_ID = local
    return local


async def aliases_for(song_id) -> List[str]:
    mapping = await ensure_loaded()
    try:
        key = int(song_id)
    except (TypeError, ValueError):
        return []
    return mapping.get(key) or mapping.get(key % 10000) or []


async def find_song_ids(keyword: str) -> List[int]:
    """按别名找歌曲：先精确，再包含；返回原始 ID（<10000）"""
    mapping = await ensure_loaded()
    keyword = str(keyword or "").strip().casefold()
    if not keyword:
        return []
    exact: Set[int] = set()
    fuzzy: Set[int] = set()
    for song_id, aliases in mapping.items():
        for alias in aliases:
            folded = alias.casefold()
            if folded == keyword:
                exact.add(song_id)
            elif keyword in folded:
                fuzzy.add(song_id)
    # 统一成 <10000 的基础 ID，调用方再决定用 SD 还是 DX
    return sorted({song_id % 10000 for song_id in (exact or fuzzy)})
