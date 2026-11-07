# src/libraries/oauth_accounts.py
"""
水鱼 / 落雪 OAuth 绑定与成绩读取。

水鱼：设备码绑定 + on-behalf-of 换票
    https://maimai.diving-fish.com/manual/docs/developer/oauth-quickstart
落雪：授权码 + refresh token
    https://maimai.lxns.net/docs/oauth-guide

水鱼只需要在 .env 配置应用凭据，用户绑定后不保存用户令牌；
落雪需要保存 refresh token（会轮换），文件在 data/accounts.json。
"""

import hashlib
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from src.libraries import accounts
from src.libraries.config_env import load_project_env
from src.libraries.maimaidx_music import total_list

load_project_env()

DF_AUTH = (os.getenv("DIVINGFISH_AUTH_URL") or "https://auth.diving-fish.com").rstrip("/")
DF_API = "https://www.diving-fish.com/api/maimaidxprober"
DF_CLIENT_ID = (os.getenv("DIVINGFISH_CLIENT_ID") or "").strip()
DF_CLIENT_SECRET = (os.getenv("DIVINGFISH_CLIENT_SECRET") or "").strip()
DF_SCOPE = (os.getenv("DIVINGFISH_SCOPE") or "prober.records.read").strip()

LX_AUTH = (os.getenv("LXNS_AUTH_URL") or "https://maimai.lxns.net").rstrip("/")
LX_API = (os.getenv("LXNS_API_BASE") or "https://maimai.lxns.net/api/v0").rstrip("/")
LX_CLIENT_ID = (os.getenv("LX_CLIENT_ID") or "").strip()
LX_CLIENT_SECRET = (os.getenv("LX_CLIENT_SECRET") or "").strip()
LX_REDIRECT_URI = (os.getenv("LX_REDIRECT_URI") or "").strip()
LX_NO_CALLBACK = (os.getenv("LX_NO_CALLBACK") or "false").strip().lower() in (
    "1", "true", "yes", "on",
)
LX_SCOPE = (os.getenv("LX_SCOPE") or "read_player").strip()

_DF_TOKENS: Dict[str, Tuple[str, float]] = {}
_LX_PENDING: Dict[str, Dict[str, Any]] = {}


def mask_qq(qq: str) -> str:
    text = str(qq or "")
    if len(text) <= 4:
        return f"QQ {text}"
    return f"QQ {text[:2]}****{text[-2:]}"


def subject_ref(qq: str) -> str:
    return hashlib.sha256(f"{DF_CLIENT_ID}:{qq}".encode()).hexdigest()


def divingfish_ready() -> Tuple[bool, str]:
    if not DF_CLIENT_ID or not DF_CLIENT_SECRET:
        return False, "未配置 DIVINGFISH_CLIENT_ID / DIVINGFISH_CLIENT_SECRET"
    return True, ""


def lxns_ready() -> Tuple[bool, str]:
    if not LX_CLIENT_ID or not LX_CLIENT_SECRET:
        return False, "未配置 LX_CLIENT_ID / LX_CLIENT_SECRET"
    if not LX_NO_CALLBACK and not LX_REDIRECT_URI:
        return False, "未配置 LX_REDIRECT_URI，或设置 LX_NO_CALLBACK=true 使用无回调模式"
    return True, ""


# ========== 水鱼 OAuth ==========

async def start_divingfish_binding(qq: str) -> Tuple[Optional[Dict[str, Any]], str]:
    ready, error = divingfish_ready()
    if not ready:
        return None, error

    payload = {
        "client_id": DF_CLIENT_ID,
        "client_secret": DF_CLIENT_SECRET,
        "scope": DF_SCOPE,
        "subject_ref": subject_ref(qq),
        "binding_label": mask_qq(qq),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{DF_AUTH}/oauth/device_authorization", data=payload)
    except httpx.TimeoutException:
        return None, "水鱼绑定请求超时"
    except Exception as exc:  # noqa: BLE001
        return None, f"水鱼绑定请求失败：{exc}"

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200:
        message = data.get("error_description") or data.get("error") or f"HTTP {response.status_code}"
        return None, f"水鱼绑定失败：{message}"
    if not data.get("verification_uri_complete"):
        return None, "水鱼没有返回绑定链接"

    await accounts.bind_divingfish(
        qq,
        subject_ref=payload["subject_ref"],
        label=payload["binding_label"],
        pending=True,
    )
    return data, ""


async def _divingfish_access_token(qq: str) -> Tuple[Optional[str], str]:
    ready, error = divingfish_ready()
    if not ready:
        return None, error
    cached = _DF_TOKENS.get(str(qq))
    if cached and cached[1] > time.time():
        return cached[0], ""

    payload = {
        "grant_type": "urn:diving-fish:params:oauth:grant-type:on-behalf-of",
        "client_id": DF_CLIENT_ID,
        "client_secret": DF_CLIENT_SECRET,
        "subject": f"ref:{subject_ref(qq)}",
        "scope": DF_SCOPE,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{DF_AUTH}/oauth/token", data=payload)
    except Exception as exc:  # noqa: BLE001
        return None, f"水鱼换票失败：{exc}"

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code == 200 and data.get("access_token"):
        token = str(data["access_token"])
        expires = float(data.get("expires_in") or 300)
        _DF_TOKENS[str(qq)] = (token, time.time() + max(0, expires - 30))
        return token, ""

    error_code = str(data.get("error") or "")
    description = str(data.get("error_description") or data.get("message") or "")
    if error_code == "consent_required":
        return None, "还没有绑定水鱼，请先发送「dfbind」完成授权"
    if error_code == "invalid_client":
        return None, "水鱼应用凭据无效，请检查 DIVINGFISH_CLIENT_ID / CLIENT_SECRET"
    if error_code == "invalid_scope":
        return None, f"水鱼应用没有申请到 {DF_SCOPE} 权限"
    return None, f"水鱼换票失败：{description or error_code or response.status_code}"


def _music_meta(song_id: int, diff_index: int) -> Dict[str, Any]:
    music = total_list.by_id(str(song_id)) or total_list.by_id(str(song_id + 10000))
    if music is None:
        return {"title": "", "ds": 0.0, "level": "", "is_new": False}
    levels = list(music.get("level") or [])
    ds_list = list(music.get("ds") or [])
    basic = music.get("basic_info") or {}
    return {
        "title": str(music.get("title") or ""),
        "ds": float(ds_list[diff_index]) if diff_index < len(ds_list) else 0.0,
        "level": str(levels[diff_index]) if diff_index < len(levels) else "",
        "is_new": bool(basic.get("is_new")),
    }


def _normalize_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """水鱼 /player/records 的 record -> 现有绘图/推荐使用的 score 结构"""
    if not isinstance(record, dict):
        return None
    song_id = int(record.get("song_id") or 0)
    if song_id <= 0 or song_id > 100000:
        return None
    base_id = song_id % 10000 if song_id > 10000 else song_id
    chart_type = "DX" if song_id > 10000 else "SD"
    diff_index = int(record.get("level_index") or 0)
    meta = _music_meta(base_id, diff_index)
    return {
        "song_id": str(base_id),
        "title": meta["title"],
        "type": chart_type,
        "level_index": diff_index,
        "level": meta["level"] or str(record.get("level") or ""),
        "ds": meta["ds"],
        "achievements": float(record.get("achievements") or 0),
        "ra": int(record.get("ra") or 0),
        "fc": str(record.get("fc") or ""),
        "fs": str(record.get("fs") or ""),
        "rate": str(record.get("rate") or ""),
        "dxScore": int(record.get("dxScore") or 0),
        "is_new": meta["is_new"],
    }


def _b50_from_scores(scores: List[Dict[str, Any]], name: str, rating: int, source: str) -> Dict[str, Any]:
    """
    水鱼/落雪原始成绩列表 -> B50 的 B35/B15。

    B35/B15 按「曲目是否属于当前版本」拆分，而不是按 SD/DX 谱面类型拆分：
        sd = 非当前版本 Best 35（B35）
        dx = 当前版本 Best 15（B15）
    否则所有标准谱都会被错误塞进 B35，所有 DX 谱都会进 B15。
    """
    current = [s for s in scores if s.get("is_new")]
    old = [s for s in scores if not s.get("is_new")]

    # 曲库暂时没有 is_new 标记时退回按谱面类型拆分，至少保证 B15 不为空
    if not current:
        current = [s for s in scores if s["type"] == "DX"]
        old = [s for s in scores if s not in current]

    sd = sorted(old, key=lambda s: s["ra"], reverse=True)[:35]
    dx = sorted(current, key=lambda s: s["ra"], reverse=True)[:15]
    total = sum(s["ra"] for s in sd + dx)
    return {
        "nickname": name,
        "username": name,
        "rating": int(rating or total),
        "charts": {"sd": sd, "dx": dx},
        "_source": source,
    }


async def fetch_divingfish_oauth(qq: str) -> Tuple[Optional[Dict[str, Any]], str]:
    token, error = await _divingfish_access_token(qq)
    if not token:
        return None, error

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{DF_API}/player/records",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # noqa: BLE001
        return None, f"水鱼成绩请求失败：{exc}"

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code == 401:
        _DF_TOKENS.pop(str(qq), None)
        return None, "水鱼令牌已过期，请重试"
    if response.status_code == 403:
        return None, f"水鱼授权缺少权限：{data.get('message') or 'prober.records.read'}"
    if response.status_code != 200:
        return None, f"水鱼成绩请求失败：HTTP {response.status_code} {data.get('message') or ''}"

    records = data.get("records") or []
    scores = [item for record in records if (item := _normalize_record(record))]
    if not scores:
        return None, "水鱼没有返回成绩记录"

    name = str(data.get("nickname") or data.get("username") or mask_qq(qq))
    result = _b50_from_scores(scores, name, int(data.get("rating") or 0), "divingfish-oauth")
    await accounts.confirm_divingfish(qq)
    return result, ""


# ========== 落雪 OAuth ==========

async def start_lxns_binding(qq: str) -> Tuple[Optional[Dict[str, Any]], str]:
    ready, error = lxns_ready()
    if not ready:
        return None, error

    state = secrets.token_urlsafe(24)
    _LX_PENDING[str(qq)] = {
        "state": state,
        "created_at": time.time(),
        "expires": time.time() + 600,
    }
    params = {
        "response_type": "code",
        "client_id": LX_CLIENT_ID,
        "scope": LX_SCOPE,
        "state": state,
    }
    if not LX_NO_CALLBACK:
        params["redirect_uri"] = LX_REDIRECT_URI
    return {
        "url": f"{LX_AUTH}/oauth/authorize?{urlencode(params)}",
        "state": state,
        "no_callback": LX_NO_CALLBACK,
    }, ""


def pending_lxns(qq: str) -> bool:
    item = _LX_PENDING.get(str(qq))
    if not item:
        return False
    if item["expires"] < time.time():
        _LX_PENDING.pop(str(qq), None)
        return False
    return True


def cancel_lxns_binding(qq: str) -> None:
    _LX_PENDING.pop(str(qq), None)


async def finish_lxns_binding(qq: str, code: str, state: str = "") -> Tuple[bool, str]:
    ready, error = lxns_ready()
    if not ready:
        return False, error

    pending = _LX_PENDING.get(str(qq))
    if not pending or pending["expires"] < time.time():
        _LX_PENDING.pop(str(qq), None)
        return False, "授权会话已过期，请重新发送「lxbind」"
    if state and state != pending["state"]:
        return False, "state 校验失败，请重新发送「lxbind」"

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": LX_CLIENT_ID,
        "client_secret": LX_CLIENT_SECRET,
    }
    if not LX_NO_CALLBACK:
        payload["redirect_uri"] = LX_REDIRECT_URI
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{LX_API}/oauth/token", json=payload)
    except Exception as exc:  # noqa: BLE001
        return False, f"落雪授权请求失败：{exc}"

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("access_token"):
        message = data.get("error_description") or data.get("error") or f"HTTP {response.status_code}"
        return False, f"落雪授权失败：{message}"

    await accounts.bind_lxns(qq, data)
    _LX_PENDING.pop(str(qq), None)
    return True, "落雪绑定成功"


async def _refresh_lxns(qq: str, refresh_token: str) -> Tuple[Optional[str], str]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": LX_CLIENT_ID,
        "client_secret": LX_CLIENT_SECRET,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(f"{LX_API}/oauth/token", json=payload)
    except Exception as exc:  # noqa: BLE001
        return None, f"落雪刷新令牌失败：{exc}"

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("access_token"):
        message = data.get("error_description") or data.get("error") or f"HTTP {response.status_code}"
        return None, f"落雪授权已失效，请重新发送「lxbind」：{message}"

    await accounts.update_lxns_tokens(qq, data)
    return str(data["access_token"]), ""


async def lxns_access_token(qq: str) -> Tuple[Optional[str], str]:
    """供 maimai.py 免费上传复用当前落雪 OAuth access token"""
    return await _lxns_access_token(qq)


async def _lxns_access_token(qq: str) -> Tuple[Optional[str], str]:
    user = await accounts.get_user(qq)
    lx = user.get("lxns") if isinstance(user.get("lxns"), dict) else {}
    if not lx:
        return None, "还没有绑定落雪，请先发送「lxbind」完成授权"
    token = str(lx.get("access_token") or "")
    if token and float(lx.get("expires_at") or 0) > time.time():
        return token, ""
    refresh_token = str(lx.get("refresh_token") or "")
    if not refresh_token:
        return None, "落雪授权信息不完整，请重新发送「lxbind」"
    return await _refresh_lxns(qq, refresh_token)


def _normalize_lxns(score: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(score, dict):
        return None
    song_id = int(score.get("id") or 0)
    score_type = str(score.get("type") or "").lower()
    if song_id <= 0 or song_id > 100000 or score_type == "utage":
        return None
    diff_index = int(score.get("level_index") or 0)
    base_id = song_id % 10000 if song_id > 10000 else song_id
    chart_type = "DX" if score_type == "dx" else "SD"
    meta = _music_meta(base_id, diff_index)
    achievements = float(score.get("achievements") or 0)
    ds = float(meta["ds"])
    ra = int(score.get("dx_rating") or 0)
    if ra <= 0 and ds > 0:
        ra = int(ds * (min(100.5, achievements) / 100) * 22.4)
    return {
        "song_id": str(base_id),
        "title": meta["title"],
        "type": chart_type,
        "level_index": diff_index,
        "level": meta["level"] or str(score.get("level") or ""),
        "ds": ds,
        "achievements": achievements,
        "ra": ra,
        "fc": str(score.get("fc") or ""),
        "fs": str(score.get("fs") or ""),
        "rate": str(score.get("rate") or ""),
        "dxScore": int(score.get("dx_score") or 0),
        "is_new": meta["is_new"],
    }


async def _lxns_get(client: httpx.AsyncClient, qq: str, url: str) -> Tuple[Optional[Dict[str, Any]], str]:
    token, error = await _lxns_access_token(qq)
    if not token:
        return None, error
    response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 401:
        user = await accounts.get_user(qq)
        lx = user.get("lxns") if isinstance(user.get("lxns"), dict) else {}
        token, error = await _refresh_lxns(qq, str(lx.get("refresh_token") or ""))
        if not token:
            return None, error
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        try:
            message = (response.json() or {}).get("message") or ""
        except ValueError:
            message = ""
        return None, f"落雪请求失败：HTTP {response.status_code} {message}".strip()
    try:
        payload = response.json()
    except ValueError:
        return None, "落雪返回了非 JSON 数据"
    if isinstance(payload, dict) and not payload.get("success", True):
        return None, str(payload.get("message") or "落雪返回失败")
    data = payload.get("data") if isinstance(payload, dict) else payload
    return data if isinstance(data, (dict, list)) else None, ""


async def fetch_lxns_oauth(qq: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            player_info, error = await _lxns_get(client, qq, f"{LX_API}/user/maimai/player")
            if player_info is None:
                return None, error
            scores_data, error = await _lxns_get(client, qq, f"{LX_API}/user/maimai/player/scores")
            if scores_data is None:
                return None, error
    except Exception as exc:  # noqa: BLE001
        return None, f"落雪成绩请求失败：{exc}"

    if isinstance(scores_data, dict):
        raw_scores = scores_data.get("scores") or scores_data.get("records") or []
    elif isinstance(scores_data, list):
        raw_scores = scores_data
    else:
        raw_scores = []

    scores = [item for raw in raw_scores if (item := _normalize_lxns(raw))]
    if not scores:
        return None, "落雪没有返回成绩"

    name = str(player_info.get("name") or player_info.get("nickname") or mask_qq(qq))
    rating = int(player_info.get("rating") or 0)
    return _b50_from_scores(scores, name, rating, "lxns-oauth"), ""
