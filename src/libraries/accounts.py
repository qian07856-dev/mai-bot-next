# src/libraries/accounts.py
"""
查分器账号绑定与数据源偏好。

数据结构（data/accounts.json）：
{
  "users": {
    "123456": {
      "source": "auto",                 # auto / divingfish / lxns
      "divingfish": {
        "subject_ref": "...",           # sha256(client_id:qq)
        "sub": "12345",                 # 可选，绑定轮询拿到的水鱼用户 ID
        "label": "QQ 12****56",
        "bound_at": "..."
      },
      "lxns": {
        "refresh_token": "...",
        "access_token": "...",
        "expires_at": 0,
        "bound_at": "..."
      }
    }
  }
}

注意：落雪 refresh token 是敏感凭据，文件仅保存在本机 data/ 目录，已被 .gitignore 忽略。
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.libraries.config_env import load_project_env

load_project_env()

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT_PATH = ROOT / "data" / "accounts.json"

VALID_SOURCES = {"auto", "divingfish", "lxns"}
SOURCE_ALIASES = {
    "auto": "auto",
    "自动": "auto",
    "水鱼": "divingfish",
    "divingfish": "divingfish",
    "diving-fish": "divingfish",
    "df": "divingfish",
    "落雪": "lxns",
    "lxns": "lxns",
    "lx": "lxns",
}

_ACCOUNTS: Optional[Dict[str, Any]] = None
_LOCK = asyncio.Lock()


def _empty() -> Dict[str, Any]:
    return {"users": {}}


def _normalize(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("users", {})
    if not isinstance(data["users"], dict):
        data["users"] = {}
    return data


def _read_sync() -> Dict[str, Any]:
    if not ACCOUNT_PATH.is_file():
        return _empty()
    try:
        return _normalize(json.loads(ACCOUNT_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _empty()


def _write_sync(data: Dict[str, Any]) -> None:
    ACCOUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACCOUNT_PATH)


async def _load() -> Dict[str, Any]:
    global _ACCOUNTS
    if _ACCOUNTS is None:
        _ACCOUNTS = await asyncio.to_thread(_read_sync)
    return _ACCOUNTS


async def _save(data: Dict[str, Any]) -> None:
    global _ACCOUNTS
    _ACCOUNTS = data
    await asyncio.to_thread(_write_sync, data)


def normalize_source(value: str) -> Optional[str]:
    return SOURCE_ALIASES.get(str(value or "").strip().casefold())


def source_label(source: str) -> str:
    return {
        "auto": "自动（水鱼优先，失败落雪）",
        "divingfish": "水鱼",
        "lxns": "落雪",
    }.get(source, source)


async def get_user(qq: str) -> Dict[str, Any]:
    data = await _load()
    user = data["users"].get(str(qq))
    return user if isinstance(user, dict) else {}


async def get_source(qq: str) -> str:
    user = await get_user(qq)
    source = str(user.get("source") or "auto")
    return source if source in VALID_SOURCES else "auto"


async def set_source(qq: str, source: str) -> None:
    normalized = normalize_source(source)
    if normalized is None:
        raise ValueError("invalid source")
    async with _LOCK:
        data = await _load()
        user = data["users"].setdefault(str(qq), {})
        user["source"] = normalized
        await _save(data)


async def bind_divingfish(
    qq: str,
    subject_ref: str,
    sub: Optional[str] = None,
    label: str = "",
    pending: bool = False,
) -> None:
    async with _LOCK:
        data = await _load()
        user = data["users"].setdefault(str(qq), {})
        user["divingfish"] = {
            "subject_ref": subject_ref,
            "sub": str(sub or ""),
            "label": label,
            "pending": bool(pending),
            "bound_at": datetime.now().isoformat(timespec="seconds"),
        }
        await _save(data)


async def confirm_divingfish(qq: str) -> None:
    async with _LOCK:
        data = await _load()
        user = data["users"].get(str(qq))
        if not isinstance(user, dict):
            return
        df = user.get("divingfish")
        if isinstance(df, dict):
            df["pending"] = False
            df["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
            await _save(data)


async def bind_lxns(qq: str, token_data: Dict[str, Any]) -> None:
    async with _LOCK:
        data = await _load()
        user = data["users"].setdefault(str(qq), {})
        now = datetime.now()
        expires_in = int(token_data.get("expires_in") or 0)
        user["lxns"] = {
            "refresh_token": str(token_data.get("refresh_token") or ""),
            "access_token": str(token_data.get("access_token") or ""),
            "expires_at": now.timestamp() + max(0, expires_in - 30),
            "bound_at": now.isoformat(timespec="seconds"),
        }
        await _save(data)


async def update_lxns_tokens(qq: str, token_data: Dict[str, Any]) -> None:
    async with _LOCK:
        data = await _load()
        user = data["users"].setdefault(str(qq), {})
        current = user.get("lxns") if isinstance(user.get("lxns"), dict) else {}
        now = datetime.now()
        expires_in = int(token_data.get("expires_in") or 0)
        current.update({
            "refresh_token": str(token_data.get("refresh_token") or current.get("refresh_token") or ""),
            "access_token": str(token_data.get("access_token") or ""),
            "expires_at": now.timestamp() + max(0, expires_in - 30),
            "updated_at": now.isoformat(timespec="seconds"),
        })
        user["lxns"] = current
        await _save(data)


async def unbind(qq: str, provider: str) -> bool:
    async with _LOCK:
        data = await _load()
        user = data["users"].get(str(qq))
        if not isinstance(user, dict):
            return False
        if provider == "all":
            data["users"].pop(str(qq), None)
            await _save(data)
            return True
        if provider not in ("divingfish", "lxns") or provider not in user:
            return False
        user.pop(provider, None)
        if not any(key in user for key in ("divingfish", "lxns")):
            data["users"].pop(str(qq), None)
        await _save(data)
        return True


async def summary(qq: str) -> Dict[str, Any]:
    user = await get_user(qq)
    df = user.get("divingfish") if isinstance(user.get("divingfish"), dict) else {}
    return {
        "source": user.get("source") or "auto",
        "divingfish": bool(df),
        "divingfish_pending": bool(df.get("pending")),
        "lxns": isinstance(user.get("lxns"), dict),
    }
