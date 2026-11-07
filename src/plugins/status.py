# src/plugins/status.py
import base64
import os
from typing import Any, Dict, List

import httpx
from datetime import datetime
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.rule import to_me
from src.libraries.config_env import load_project_env

# 让 .env 里的 STATUS_IMAGE 等配置生效（nonebot 不会把 .env 写进环境变量）
load_project_env()

API_URL = "https://mai.chongxi.us/api/bot"

# 状态图：默认出图，失败自动回退文字
STATUS_IMAGE = os.getenv("STATUS_IMAGE", "true").lower() in ("1", "true", "yes", "on")
STATUS_IMAGE_FORMAT = (os.getenv("STATUS_IMAGE_FORMAT") or "jpg").strip().lower()

GREEN = (120, 226, 150, 255)
YELLOW = (255, 208, 96, 255)
RED = (255, 130, 130, 255)
ORANGE = (255, 170, 90, 255)
GRAY = (176, 180, 202, 255)

# 总判定 verdict -> (徽章文案, 颜色)；recovering 用浅绿表示"性能恢复中"
RECOVERING = (168, 230, 140, 255)
VERDICT_STYLE = {
    "normal": ("一切正常", GREEN),
    "recovering": ("性能恢复中", RECOVERING),
    "degraded": ("性能下降", YELLOW),
    "outage": ("服务宕机", RED),
    "maintenance": ("维护中", ORANGE),
    "nodata": ("无数据", GRAY),
}
# 兼容旧字段 incident_level / status（接口仍在返回）
LEVEL_STYLE = {
    "normal": ("一切正常", GREEN),
    "ok": ("一切正常", GREEN),
    "warning": ("部分异常", YELLOW),
    "degraded": ("服务降级", YELLOW),
    "unstable": ("不稳定", YELLOW),
    "partial": ("部分异常", YELLOW),
    "anomaly": ("不稳定", YELLOW),
    "error": ("服务异常", RED),
    "down": ("服务不可用", RED),
    "outage": ("服务宕机", RED),
    "empty": ("服务异常", RED),
    "nodata": ("无数据", GRAY),
    "maintenance": ("维护中", ORANGE),
}
# 单个服务：ok / down / degraded / recovering / maintenance / nodata（新接口）
STATE_STYLE = {
    "ok": ("正常", GREEN),
    "down": ("宕机", RED),
    "degraded": ("性能下降", YELLOW),
    "recovering": ("性能恢复中", RECOVERING),
    "maintenance": ("维护中", ORANGE),
    "nodata": ("无数据", GRAY),
    # 以下为旧取值兜底
    "warning": ("警告", YELLOW),
    "slow": ("延迟高", YELLOW),
    "error": ("异常", RED),
    "unknown": ("未知", GRAY),
}

# 文字回退用的图标：键同时覆盖新 verdict 与旧 incident_level/status 取值
LEVEL_ICON = {
    "normal": "🟢",
    "ok": "🟢",
    "recovering": "🟩",
    "degraded": "🟡",
    "unstable": "🟡",
    "anomaly": "🟡",
    "warning": "🟡",
    "partial": "🟡",
    "outage": "🔴",
    "down": "🔴",
    "error": "🔴",
    "empty": "🔴",
    "maintenance": "🟠",
    "nodata": "⚪",
}
STATE_ICON = {
    "ok": "🟢",
    "recovering": "🟩",
    "degraded": "🟡",
    "maintenance": "🟠",
    "down": "⛔",
    "nodata": "⚪",
    # 旧取值兜底
    "warning": "🟡",
    "slow": "🟡",
    "error": "🔴",
}


STATE_KEYWORD = {"recovering": "恢复", "down": "宕机", "maintenance": "维护"}


def _service_note(service: Dict[str, Any]) -> str:
    """服务备注：优先用接口给的 state_text，若 duration_text 已含同样的关键动作词则不重复"""
    state = str(service.get("state") or "")
    state_text = str(service.get("state_text") or STATE_STYLE.get(state, ("", GRAY))[0])
    duration = str(service.get("duration_text") or "")
    keyword = STATE_KEYWORD.get(state, "")
    if duration and keyword and keyword in duration:
        return duration
    return " ".join(x for x in (state_text, duration) if x)


def _format_timestamp(value: Any) -> str:
    """接口的 timestamp（ISO，带 +08:00）-> 10-14 21:13"""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%m-%d %H:%M")


def build_status_card(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """把接口数据整理成 card_image.render_card 的参数"""
    verdict = str(data.get("verdict") or "")
    version = data.get("version", "")
    status_text = str(data.get("status_text") or "")
    verdict_text = str(data.get("verdict_text") or "")
    # 徽章优先看新的 verdict，其次旧字段 incident_level，都没有就用接口给的文案
    badge_style = VERDICT_STYLE.get(verdict) or LEVEL_STYLE.get(str(data.get("incident_level") or ""))
    if badge_style is None:
        badge_style = (verdict_text or status_text or "状态", GRAY)
    badge, badge_color = badge_style
    subtitle = "｜".join(x for x in (status_text, verdict_text) if x) or "华立服务器监控平台"

    sections: List[Dict[str, Any]] = []

    # 公告（有内容才显示，接口为空/null 时忽略）
    broadcast = data.get("broadcast")
    broadcast_msg = ""
    if isinstance(broadcast, dict):
        broadcast_msg = str(broadcast.get("msg") or "")
    elif broadcast:
        broadcast_msg = str(broadcast)
    if broadcast_msg:
        sections.append({
            "heading": "公告",
            "rows": [{"label": broadcast_msg, "value": "", "color": ORANGE}],
        })

    services = data.get("services") or []
    if services:
        rows = []
        for service in services:
            state = str(service.get("state") or "")
            state_text, color = STATE_STYLE.get(state, ("未知", GRAY))
            # 新接口直接给了状态文案和"已宕机 X 分钟"这类时长描述，优先用它
            state_text = str(service.get("state_text") or state_text)
            latency = service.get("latency")
            rows.append({
                "label": str(service.get("name") or service.get("key") or "未知"),
                "value": f"{latency} ms" if isinstance(latency, (int, float)) else "—",
                "note": _service_note(service) or state_text,
                "color": color,
                "value_color": color,
            })
        sections.append({"heading": "服务状态", "rows": rows})

    overview_rows = []
    if verdict_text:
        overview_rows.append({"label": "总判定", "value": verdict_text, "value_color": badge_color})
    if data.get("summary"):
        overview_rows.append({"label": "摘要", "value": str(data["summary"])})
    if version:
        overview_rows.append({"label": "数据版本", "value": f"v{version}"})
    updated = _format_timestamp(data.get("timestamp"))
    if updated:
        overview_rows.append({"label": "数据时间", "value": updated})
    if overview_rows:
        sections.append({"heading": "总体", "rows": overview_rows})

    latency = data.get("latency") or {}
    sections.append({
        "heading": "网络指标",
        "rows": [
            {"label": "当前延迟", "value": f"{latency.get('current_ms', 0)} ms"},
            {"label": "服务器负载", "value": str(latency.get("load_text") or "未知")},
            {"label": "延迟波动", "value": str(latency.get("volatility_text") or "—")},
        ],
    })

    reports = data.get("reports") or {}
    anomaly = int(reports.get("anomaly_count", 0) or 0)
    report_rows = [
        {"label": "异常上报", "value": f"{anomaly} 条", "value_color": RED if anomaly else GREEN},
        {"label": "正常上报", "value": f"{reports.get('normal_count', 0)} 条", "value_color": GREEN},
    ]
    if reports.get("last_report_ago_minutes") is not None:
        report_rows.append({"label": "最近上报", "value": f"{reports.get('last_report_ago_minutes')} 分钟前"})
    if reports.get("data_empty"):
        report_rows.append({"label": "数据状态", "value": "暂无数据", "value_color": GRAY})
    sections.append({"heading": "上报统计（近 1 小时）", "rows": report_rows})

    footer = "数据来源：华立服务器死了吗 by Chongxi"
    if prefix:
        footer = f"{prefix}｜{footer}"
    return {
        "title": "舞萌DX 游戏服务器状态",
        "subtitle": subtitle,
        "badge": badge,
        "badge_color": badge_color,
        "sections": sections,
        "footer": footer,
        "accent": badge_color,
    }


async def send_status_image(matcher, data: Dict[str, Any], prefix: str = "") -> bool:
    """渲染状态图并发送；出图失败返回 False（由调用方回退文字）"""
    if not STATUS_IMAGE:
        return False
    try:
        from src.libraries.card_image import image_to_bytes, render_card

        image = await render_card(**build_status_card(data, prefix))
        payload = image_to_bytes(image, STATUS_IMAGE_FORMAT, 88)
    except Exception as e:
        logger.exception(f"[status] 状态图生成失败，回退文字：{e}")
        return False

    logger.info(f"[status] 已生成状态图：{len(payload) / 1024:.0f} KB")
    # finish 会抛 FinishedException 结束处理器，这里不要再套 try
    await matcher.finish(Message([
        MessageSegment("image", {"file": "base64://" + base64.b64encode(payload).decode()})
    ]))
    return True

# 创建命令处理器
status = on_command("status", aliases={"服务器状态", "状态", "mai状态"}, priority=10)

@status.handle()
async def handle_status(bot: Bot, event: Event, args: CommandArg = CommandArg()):
    """处理 /status 命令"""
    try:
        # 发送正在请求的提示
        await status.send("🔄 正在获取服务器状态，请稍候...")
        
        # 请求API
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(API_URL)
            
            if response.status_code == 200:
                # 解析响应数据
                data = response.json()

                # 优先出图，失败回退文字
                if await send_status_image(status, data):
                    return
                await status.finish(format_status_message(data))
            else:
                await status.finish(f"❌ 获取服务器状态失败：HTTP {response.status_code}")
                
    except httpx.TimeoutException:
        await status.finish("⏰ 请求超时，请稍后重试")
    except httpx.RequestError as e:
        logger.error(f"请求API失败: {e}")
        await status.finish("🌐 网络请求失败，请检查网络连接")

def format_status_message(data: dict) -> str:
    """格式化状态消息（文字回退，字段与状态图保持一致）"""
    message_parts = ["【舞萌DX游戏服务器状态】"]

    version = data.get("version", "")
    status_text = str(data.get("status_text") or "")
    verdict_text = str(data.get("verdict_text") or "")

    # 总判定：优先新字段 verdict，其次旧字段 incident_level / status
    verdict = str(data.get("verdict") or "")
    style = VERDICT_STYLE.get(verdict)
    if style is not None:
        verdict_label = style[0]
    else:
        legacy = str(data.get("incident_level") or data.get("status") or "")
        verdict_label = LEVEL_STYLE.get(legacy, (verdict_text or status_text or "未知", GRAY))[0]
    icon = LEVEL_ICON.get(verdict) or LEVEL_ICON.get(str(data.get("incident_level") or "")) or "⚪"
    message_parts.append(f"{icon} {verdict_label}")
    subtitle = "｜".join(x for x in (status_text, verdict_text) if x)
    if subtitle:
        message_parts.append(subtitle)

    # ========== 公告（接口为空 / null 时不显示） ==========
    broadcast = data.get("broadcast")
    broadcast_msg = ""
    if isinstance(broadcast, dict):
        broadcast_msg = str(broadcast.get("msg") or "")
    elif broadcast:
        broadcast_msg = str(broadcast)
    if broadcast_msg:
        message_parts.append(f"\n📢 公告：{broadcast_msg}")

    # ========== 服务状态 ==========
    services = data.get("services") or []
    if services:
        message_parts.append("\n【服务状态】")
        for service in services:
            name = str(service.get("name") or service.get("key") or "未知")
            state = str(service.get("state") or "")
            note = _service_note(service)
            latency = service.get("latency")
            latency_text = f"{latency}ms" if isinstance(latency, (int, float)) else "—"
            message_parts.append(f"{STATE_ICON.get(state, '⚪')} {name}  {latency_text}  {note}".rstrip())

    # ========== 总体 ==========
    summary = str(data.get("summary") or "")
    updated = _format_timestamp(data.get("timestamp"))
    if verdict_text or summary or version or updated:
        message_parts.append("\n【总体】")
        if verdict_text:
            message_parts.append(f"总判定：{verdict_text}")
        if summary:
            message_parts.append(f"摘要：{summary}")
        if version:
            message_parts.append(f"数据版本：v{version}")
        if updated:
            message_parts.append(f"数据时间：{updated}")

    # ========== 网络指标 ==========
    latency_info = data.get("latency") or {}
    current_latency = latency_info.get("current_ms", 0)
    load_text = latency_info.get("load_text", "未知")
    volatility_text = latency_info.get("volatility_text", "")
    metrics = [f"当前延迟：{current_latency}ms", f"服务器负载：{load_text}"]
    if volatility_text:
        metrics.append(str(volatility_text))
    message_parts.append("\n⏱ " + "｜".join(metrics))

    # ========== 异常上报 ==========
    reports = data.get("reports") or {}
    anomaly_count = reports.get("anomaly_count", 0)
    normal_count = reports.get("normal_count", 0)
    message_parts.append(f"\n💬 近 1 小时：{anomaly_count} 条异常，{normal_count} 条正常上报")
    if reports.get("last_report_ago_minutes") is not None:
        message_parts.append(f"最近上报：{reports.get('last_report_ago_minutes')} 分钟前")
    if reports.get("data_empty"):
        message_parts.append("数据状态：暂无数据")

    # 说明：新接口已废弃 recent_logs（只会返回一条"请查阅新版文档"的提示），故不再展示

    # ========== 数据来源 ==========
    message_parts.append("\n📡 数据来源：华立服务器死了吗 by Chongxi")

    return "\n".join(message_parts)

# ========== 可选：添加定时自动推送功能 ==========
from nonebot import get_driver
import asyncio

# 缓存数据
_cache_data = None
_cache_time = None

@get_driver().on_startup
async def startup():
    """启动时获取一次缓存"""
    global _cache_data, _cache_time
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(API_URL)
            if response.status_code == 200:
                _cache_data = response.json()
                _cache_time = datetime.now()
                logger.info("✅ 成功获取服务器状态缓存")
    except Exception as e:
        logger.error(f"❌ 初始化缓存失败: {e}")

# 添加一个带缓存的命令（可选）
status_cache = on_command("状态缓存", aliases={"缓存状态"}, priority=10)

@status_cache.handle()
async def handle_cached_status(bot: Bot, event: Event):
    """使用缓存数据快速响应"""
    global _cache_data, _cache_time
    
    if _cache_data and _cache_time:
        # 如果缓存时间在5分钟内
        if (datetime.now() - _cache_time).seconds < 300:
            minutes = int((datetime.now() - _cache_time).seconds / 60)
            # 缓存数据同样出图（页脚标注是缓存）
            if await send_status_image(status_cache, _cache_data, prefix=f"使用缓存数据（{minutes} 分钟前）"):
                return
            message = format_status_message(_cache_data)
            await status_cache.finish(f"📦 使用缓存数据（{minutes}分钟前）：\n\n{message}")
        else:
            await status_cache.finish("⏰ 缓存已过期，请使用 /status 获取最新数据")
    else:
        await status_cache.finish("❌ 暂无缓存数据")

# ========== 错误处理和帮助信息 ==========
status_help = on_command("status帮助", aliases={"状态帮助"}, priority=10)


STATUS_HELP_SECTIONS = [
    {
        "heading": "命令",
        "rows": [
            {"label": "status / 服务器状态", "value": "获取最新服务器状态"},
            {"label": "状态缓存 / 缓存状态", "value": "用缓存快速查询（5 分钟内有效）"},
            {"label": "status帮助 / 状态帮助", "value": "显示本帮助"},
        ],
    },
    {
        "heading": "状态说明",
        "rows": [
            {"label": "一切正常", "value": "服务可用", "color": GREEN, "value_color": GREEN},
            {"label": "性能恢复中", "value": "异常后正在恢复", "color": RECOVERING, "value_color": RECOVERING},
            {"label": "性能下降", "value": "部分服务变慢", "color": YELLOW, "value_color": YELLOW},
            {"label": "服务宕机 / 离线", "value": "服务不可用", "color": RED, "value_color": RED},
            {"label": "维护中", "value": "官方维护", "color": ORANGE, "value_color": ORANGE},
            {"label": "无数据", "value": "暂无监控数据", "color": GRAY, "value_color": GRAY},
        ],
        "note": "数据来自华立服务器监控平台",
    },
]


@status_help.handle()
async def handle_help(bot: Bot, event: Event):
    """显示帮助信息"""
    if STATUS_IMAGE:
        payload = None
        try:
            from src.libraries.card_image import image_to_bytes, render_card

            image = await render_card(
                title="状态查询帮助",
                subtitle="查询舞萌DX 华立服务器状态",
                badge="帮助",
                badge_color=GRAY,
                sections=STATUS_HELP_SECTIONS,
                footer="数据来源：华立服务器死了吗 by Chongxi",
                accent=(90, 150, 255, 255),
            )
            payload = image_to_bytes(image, STATUS_IMAGE_FORMAT, 88)
        except Exception as e:
            logger.exception(f"[status] 帮助图生成失败，回退文字：{e}")
        if payload:
            await status_help.finish(Message([
                MessageSegment("image", {"file": "base64://" + base64.b64encode(payload).decode()})
            ]))
            return

    help_text = """
📖 舞萌DX状态查询命令帮助：

/status 或 /服务器状态 - 获取最新服务器状态
/状态缓存 或 /缓存状态 - 使用缓存数据快速查询（5分钟内有效）
/status帮助 或 /状态帮助 - 显示本帮助

🔍 总判定：
🟢 一切正常  🟩 性能恢复中  🟡 性能下降  🔴 服务宕机  🟠 维护中  ⚪ 无数据

🔧 服务状态里 ⛔ 表示宕机，延迟显示 — 表示当前没有测到延迟数据

💡 提示：数据来自华立服务器监控平台
    """
    await status_help.finish(help_text.strip())
