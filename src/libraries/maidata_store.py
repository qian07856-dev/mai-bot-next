# src/libraries/maidata_store.py
"""
本地 maidata 谱库：把「歌曲 + 难度」映射到谱面文件，并缓存标签结果。

谱库根目录取 .env 的 MAIDATA_DIR（兼容 DXTAG_CHART_DIR），默认 data/maidata。
目录结构不用固定，索引会按下面的顺序推断歌曲 ID：

    &shortid= / &id= 字段  →  文件名是数字  →  所在文件夹名是数字

都推不出来时按 &title 和水鱼曲名匹配。SD/DX 用 &cabinet= 或目录名区分。

索引和标签结果都落盘到 data/dxtag/，谱面改动后会按 mtime 自动失效。
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.libraries import dxtag_engine as engine
from src.libraries.config_env import load_project_env

# 单独被导入时也要能读到 .env 里的 MAIDATA_DIR
load_project_env()

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "dxtag"
INDEX_PATH = CACHE_DIR / "maidata_index.json"
SCORE_PATH = CACHE_DIR / "five_cache.json"

# DXTag / maidata 槽位：1=EASY 2=BASIC 3=ADVANCED 4=EXPERT 5=MASTER 6=Re:MASTER 7=UTAGE
SLOT_NAMES = {
    1: "EASY", 2: "BASIC", 3: "ADVANCED", 4: "EXPERT",
    5: "MASTER", 6: "Re:MASTER", 7: "UTAGE",
}

_LOCK = threading.Lock()
_INDEX: Optional["MaidataIndex"] = None
_SCORE_CACHE: Optional[Dict[str, Dict[str, float]]] = None
_SCORE_DIRTY = False
_LAST_SIGNATURE_CHECK = 0.0
_SIGNATURE_TTL = 30.0  # 30 秒内重复查询不再重扫目录


def chart_root() -> Path:
    raw = (
        os.getenv("MAIDATA_DIR")
        or os.getenv("DXTAG_CHART_DIR")
        or (ROOT / "data" / "maidata")
    )
    return Path(raw).expanduser()


@dataclass(frozen=True)
class ChartFile:
    path: Path
    song_id: Optional[int]
    title: str
    chart_type: str
    slots: Tuple[int, ...]
    mtime: float

    @property
    def key(self) -> str:
        return f"{self.path.as_posix()}|{int(self.mtime)}"


@dataclass
class MaidataIndex:
    root: Path
    by_id: Dict[int, List[ChartFile]]
    by_title: Dict[str, List[ChartFile]]
    entries: List[ChartFile]
    total_files: int
    signature: Tuple[Tuple[str, int, int], ...]


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="ignore")


_FIELD_RE = re.compile(r"^&([A-Za-z0-9_]+)=(.*)$")
_DIGITS = re.compile(r"\d{1,6}")


def parse_chart(path: Path, text: str) -> Optional[ChartFile]:
    fields: Dict[str, str] = {}
    slots: List[int] = []
    # 谱面正文很长，&inote_N 可能排到几百行之后，这里必须扫全文
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line.startswith("&"):
            continue
        match = _FIELD_RE.match(line)
        if not match:
            continue
        name, value = match.group(1).lower(), match.group(2).strip()
        fields[name] = value
        if name.startswith("inote_"):
            suffix = name[len("inote_"):]
            if suffix.isdigit() and 1 <= int(suffix) <= 7:
                slots.append(int(suffix))
    if not slots:
        return None

    song_id: Optional[int] = None
    for field in ("shortid", "id", "songid"):
        if value := fields.get(field):
            found = _DIGITS.search(value)
            if found:
                song_id = int(found.group(0))
                break
    if song_id is None:
        for candidate in (path.stem, path.parent.name):
            found = _DIGITS.search(candidate)
            if found and candidate.strip() == found.group(0):
                song_id = int(found.group(0))
                break

    cabinet = (fields.get("cabinet") or fields.get("type") or "").upper()
    if "DX" in cabinet:
        chart_type = "DX"
    elif "STD" in cabinet or "STANDARD" in cabinet or "SD" in cabinet:
        chart_type = "SD"
    else:
        parent = path.parent.name.upper()
        chart_type = "DX" if "DX" in parent else "SD"

    title = re.sub(r"^\[(?:ST|DX|標準|标准)\]\s*", "", fields.get("title", ""), flags=re.I)
    return ChartFile(
        path=path,
        song_id=song_id,
        title=title,
        chart_type=chart_type,
        slots=tuple(sorted(set(slots))),
        mtime=path.stat().st_mtime,
    )


def _iter_candidates(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    for dirpath, _dirnames, names in os.walk(root):
        for name in names:
            lower = name.lower()
            if lower.endswith((".maidata", ".txt")):
                yield Path(dirpath) / name


def _signature(root: Path) -> Tuple[Tuple[str, int, int], ...]:
    rows = []
    for path in _iter_candidates(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((str(path.relative_to(root)), int(stat.st_mtime), stat.st_size))
    rows.sort()
    return tuple(rows)


def _load_disk_index(root: Path, signature) -> Optional[MaidataIndex]:
    if not INDEX_PATH.is_file():
        return None
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("root") != str(root):
        return None
    stored = tuple(tuple(row) for row in data.get("signature") or ())
    if stored != signature:
        return None

    by_id: Dict[int, List[ChartFile]] = {}
    by_title: Dict[str, List[ChartFile]] = {}
    entries: List[ChartFile] = []
    for row in data.get("entries") or []:
        path = root / row["path"]
        if not path.is_file():
            return None
        chart = ChartFile(
            path=path,
            song_id=row.get("id"),
            title=row.get("title") or "",
            chart_type=row.get("type") or "SD",
            slots=tuple(row.get("slots") or ()),
            mtime=path.stat().st_mtime,
        )
        entries.append(chart)
        if chart.song_id is not None:
            for key in {int(chart.song_id), int(chart.song_id) % 10000}:
                by_id.setdefault(key, []).append(chart)
        if chart.title:
            by_title.setdefault(chart.title.casefold(), []).append(chart)
    return MaidataIndex(root, by_id, by_title, entries, len(entries), signature)


def _save_disk_index(index: MaidataIndex) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "path": str(chart.path.relative_to(index.root)),
                "id": chart.song_id,
                "title": chart.title,
                "type": chart.chart_type,
                "slots": list(chart.slots),
            }
            for chart in index.entries
        ]
        payload = {
            "root": str(index.root),
            "signature": [list(row) for row in index.signature],
            "entries": entries,
        }
        tmp = INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(INDEX_PATH)
    except OSError:
        pass


def build_index(force: bool = False) -> MaidataIndex:
    """扫描谱库（按目录签名缓存，只有谱面变动时才重新读文件头）"""
    global _INDEX, _LAST_SIGNATURE_CHECK
    with _LOCK:
        root = chart_root()
        now = time.monotonic()
        if (
            not force
            and _INDEX is not None
            and _INDEX.root == root
            and now - _LAST_SIGNATURE_CHECK < _SIGNATURE_TTL
        ):
            return _INDEX
        signature = _signature(root)
        _LAST_SIGNATURE_CHECK = now
        if not force and _INDEX and _INDEX.root == root and _INDEX.signature == signature:
            return _INDEX

        cached = None if force else _load_disk_index(root, signature)
        if cached is not None:
            _INDEX = cached
            return cached

        by_id: Dict[int, List[ChartFile]] = {}
        by_title: Dict[str, List[ChartFile]] = {}
        entries: List[ChartFile] = []
        total = 0
        for path in _iter_candidates(root):
            try:
                if path.stat().st_size > 8 * 1024 * 1024:
                    continue
                text = _decode(path.read_bytes())
            except OSError:
                continue
            if "&inote_" not in text:
                # 标签库之类的 json/txt 直接跳过
                continue
            chart = parse_chart(path, text)
            if chart is None:
                continue
            total += 1
            entries.append(chart)
            if chart.song_id is not None:
                for key in {int(chart.song_id), int(chart.song_id) % 10000}:
                    by_id.setdefault(key, []).append(chart)
            if chart.title:
                by_title.setdefault(chart.title.casefold(), []).append(chart)

        index = MaidataIndex(root, by_id, by_title, entries, total, signature)
        _INDEX = index
        _save_disk_index(index)
        return index


def _type_matches(chart: ChartFile, chart_type: str) -> bool:
    return not chart_type or chart.chart_type == chart_type.upper()


def find_chart(
    song_id: Optional[int],
    chart_type: str = "",
    title: str = "",
    slot: int = 0,
) -> Optional[ChartFile]:
    """找某个难度的谱面文件；先按 ID+类型，再退回曲名匹配"""
    index = build_index()
    candidates: List[ChartFile] = []
    if song_id is not None:
        candidates = index.by_id.get(int(song_id), [])
    if not candidates and title:
        candidates = index.by_title.get(title.casefold(), [])

    typed = [c for c in candidates if _type_matches(c, chart_type)]
    pool = typed or candidates
    if slot:
        for chart in pool:
            if slot in chart.slots:
                return chart
        return None
    return pool[0] if pool else None


def read_chart(chart: ChartFile) -> str:
    return _decode(chart.path.read_bytes())


def _load_score_cache() -> Dict[str, Dict[str, float]]:
    global _SCORE_CACHE
    if _SCORE_CACHE is not None:
        return _SCORE_CACHE
    data: Dict[str, Dict[str, float]] = {}
    if SCORE_PATH.is_file():
        try:
            raw = json.loads(SCORE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
    _SCORE_CACHE = data
    return data


def _save_score_cache() -> None:
    global _SCORE_DIRTY
    if not _SCORE_DIRTY or _SCORE_CACHE is None:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SCORE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_SCORE_CACHE, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SCORE_PATH)
        _SCORE_DIRTY = False
    except OSError:
        pass


def score_slots(jobs: Sequence[Tuple[ChartFile, int]]) -> List[Optional[Dict[str, float]]]:
    """批量算标签，命中缓存的不重复计算；返回与输入等长的 scores 列表"""
    global _SCORE_DIRTY
    cache = _load_score_cache()
    results: List[Optional[Dict[str, float]]] = []
    pending: List[Tuple[int, ChartFile, int]] = []
    for position, (chart, slot) in enumerate(jobs):
        key = f"{chart.key}|{slot}"
        hit = cache.get(key)
        results.append(hit)
        if hit is None:
            pending.append((position, chart, slot))

    if pending:
        texts: List[Tuple[str, int]] = []
        usable: List[Tuple[int, ChartFile, int]] = []
        for item in pending:
            try:
                texts.append((read_chart(item[1]), item[2]))
                usable.append(item)
            except OSError:
                continue
        workers = int(os.getenv("DXTAG_WORKERS") or 0) or min(6, os.cpu_count() or 4)
        workers = max(1, min(8, workers))
        computed = engine.score_many(texts, workers=workers)
        for (position, chart, slot), outcome in zip(usable, computed):
            if isinstance(outcome, Exception):
                continue
            scores = {axis: float(value) for axis, value in outcome["scores"].items()}
            results[position] = scores
            cache[f"{chart.key}|{slot}"] = scores
            _SCORE_DIRTY = True
        _save_score_cache()

    return results


def index_summary() -> Tuple[Path, int, int]:
    index = build_index()
    return index.root, index.total_files, len(index.by_id)
