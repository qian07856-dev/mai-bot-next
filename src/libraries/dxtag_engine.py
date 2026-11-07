# src/libraries/dxtag_engine.py
"""
DXTag 标签评分引擎封装。

DXTag 是分析 Simai 谱面的 Node/TypeScript 项目（MIT，Copyright (c) 2026 尘言）：
https://github.com/kckc7887/DXTag

这里不改写它的算法：直接执行它的 esbuild 单文件产物
（src/static/dxtag/dxtag.js），由 py-mini-racer 内嵌的 V8 跑，所以运行时不需要
Node.js。产物怎么重新生成见 src/static/dxtag/README.md。

对外只暴露三件事：
    available_slots(text)      谱面里正文非空的难度槽位
    score_chart(text, slot)    单谱标签结果
    score_chart_async(...)     上面两个的线程化版本（给 asyncio 用）
"""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

ALGORITHM_VERSION = "1.1.0"

BUNDLE_PATH = Path(__file__).resolve().parent.parent / "static" / "dxtag" / "dxtag.js"

# 放进 JS 上下文一次的入口函数，后面只用 call 传参，避免每次拼接源码
_BRIDGE = r"""
var __dxtag_score = function (text, slot) {
  return JSON.stringify(DXTag.scoreMaidata(text, [slot]));
};
var __dxtag_slots = function (text) {
  return JSON.stringify(DXTag.listDifficulties(text));
};
var __dxtag_version = function () {
  return DXTag.ALGORITHM_VERSION;
};
"""

# V8 isolate 不跨线程共享：每个工作线程各自持有上下文，这样批量算标签可以并行
_LOCAL = threading.local()


class DxtagError(RuntimeError):
    """DXTag 执行失败（谱面解析错误等）"""


class DxtagUnavailable(DxtagError):
    """运行环境缺失（没装 py-mini-racer、找不到打包产物）"""


def _clean_message(text: str) -> str:
    """把 V8 抛出来的 'Uncaught Error: xxx' 收拾成一句人话"""
    msg = str(text or "").strip()
    for prefix in ("Uncaught Error:", "Uncaught TypeError:", "Uncaught RangeError:"):
        if msg.startswith(prefix):
            msg = msg[len(prefix):].strip()
            break
    return msg


def _get_context():
    """惰性创建并复用当前线程的 V8 上下文（加载产物约 10ms）"""
    context = getattr(_LOCAL, "context", None)
    if context is not None:
        return context

    try:
        from py_mini_racer.py_mini_racer import MiniRacer
    except Exception as exc:  # noqa: BLE001 - ImportError 之外还可能是 DLL 加载失败
        raise DxtagUnavailable(
            "缺少 py-mini-racer，请先执行 pip install py-mini-racer"
        ) from exc

    if not BUNDLE_PATH.is_file():
        raise DxtagUnavailable(f"缺少 DXTag 运行文件：{BUNDLE_PATH.name}")

    source = BUNDLE_PATH.read_text(encoding="utf-8")
    context = MiniRacer()
    try:
        context.eval(source, timeout=30_000)
        context.eval(_BRIDGE, timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        raise DxtagUnavailable(f"DXTag 运行文件加载失败：{_clean_message(exc)}") from exc

    _LOCAL.context = context
    return context


def _run(expression: str, *args: Any) -> Any:
    context = _get_context()
    try:
        raw = context.call(expression, *args, timeout=30_000)
    except Exception as exc:  # noqa: BLE001
        raise DxtagError(_clean_message(exc)) from exc
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DxtagError("DXTag 返回了无法解析的结果") from exc


def available_slots(text: str) -> List[int]:
    """谱面里正文非空的难度槽位（升序）"""
    if not str(text or "").strip():
        raise DxtagError("谱面内容为空")
    slots = _run("__dxtag_slots", str(text))
    return [int(slot) for slot in slots or []]


def score_chart(text: str, slot: int) -> Dict[str, Any]:
    """计算单谱标签，返回 DXTag 的 ChartScores 结构"""
    if not str(text or "").strip():
        raise DxtagError("谱面内容为空")
    result = _run("__dxtag_score", str(text), int(slot))
    if not result:
        raise DxtagError(f"难度 {slot} 未算出结果")
    return result[0]


def algorithm_version() -> str:
    try:
        return str(_run("__dxtag_version"))
    except DxtagError:
        return ALGORITHM_VERSION


async def available_slots_async(text: str) -> List[int]:
    return await asyncio.to_thread(available_slots, text)


async def score_chart_async(text: str, slot: int) -> Dict[str, Any]:
    return await asyncio.to_thread(score_chart, text, slot)


def score_many(jobs: Iterable[Tuple[str, int]], workers: int = 4) -> List[Any]:
    """批量算标签（多线程、每线程一个 V8 上下文）。

    返回与输入等长的列表，成功项是 ChartScores 字典，失败项是 DxtagError 实例。
    """
    items = list(jobs)
    if not items:
        return []
    if len(items) == 1:
        try:
            return [score_chart(*items[0])]
        except DxtagError as exc:
            return [exc]

    def run(job: Tuple[str, int]) -> Any:
        try:
            return score_chart(*job)
        except DxtagError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as pool:
        return list(pool.map(run, items))


async def score_many_async(
    jobs: Sequence[Tuple[str, int]], workers: int = 4
) -> List[Any]:
    return await asyncio.to_thread(score_many, jobs, workers)
