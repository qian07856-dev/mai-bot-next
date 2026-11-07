# src/plugins/accounts_cmd.py
"""
查分器账号绑定与数据源切换。

命令：
    dfbind / 绑定水鱼      设备码 OAuth 绑定水鱼
    lxbind / 绑定落雪      授权码 OAuth 绑定落雪
    数据源 [自动/水鱼/落雪]  查看或切换自己的数据源
    账号 / 绑定状态         查看绑定情况
    解绑水鱼 / 解绑落雪     解除绑定
"""

import re
from urllib.parse import parse_qs, urlparse

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Event, Message
from nonebot.params import CommandArg
from nonebot.rule import Rule

from src.libraries import accounts, oauth_accounts


def _qq(event: Event) -> str:
    return str(event.get_user_id())


def _lx_pending(event: Event) -> bool:
    return oauth_accounts.pending_lxns(_qq(event))


def _pending_lx(event: Event) -> bool:
    if not _lx_pending(event):
        return False
    text = event.get_plaintext().strip()
    # 只接管疑似授权码/回调链接，避免把「数据源」等普通指令吃掉
    return "code=" in text or bool(re.fullmatch(r"[A-Za-z0-9_\-]{8,}", text))


def _extract_lx_code(text: str) -> tuple[str, str]:
    """从完整回调链接或纯授权码里取 code / state"""
    text = str(text or "").strip()
    if "code=" in text:
        query = parse_qs(urlparse(text).query) if "://" in text else parse_qs(text.lstrip("?"))
        return (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    return text, ""


def _source_text(summary: dict) -> str:
    source = accounts.source_label(str(summary.get("source") or "auto"))
    if not summary.get("divingfish"):
        df = "未绑定"
    elif summary.get("divingfish_pending"):
        df = "待授权（请打开 dfbind 链接并同意）"
    else:
        df = "已绑定"
    lx = "已绑定" if summary.get("lxns") else "未绑定"
    return f"当前数据源：{source}\n水鱼 OAuth：{df}\n落雪 OAuth：{lx}"


dfbind = on_command("dfbind", aliases={"绑定水鱼"}, priority=6)
lxbind = on_command("lxbind", aliases={"绑定落雪"}, priority=6)
source_cmd = on_command("数据源", aliases={"切换数据源", "source"}, priority=6)
account_cmd = on_command("账号", aliases={"绑定状态", "我的账号"}, priority=6)
unbind_df = on_command("解绑水鱼", priority=6)
unbind_lx = on_command("解绑落雪", priority=6)
cancel_lx = on_command("取消", aliases={"退出"}, priority=4, rule=Rule(_lx_pending))

lx_code_listener = on_message(priority=5, block=False, rule=Rule(_pending_lx))


@dfbind.handle()
async def _handle_dfbind(event: Event):
    qq = _qq(event)
    data, error = await oauth_accounts.start_divingfish_binding(qq)
    if not data:
        await dfbind.finish(f"❌ 水鱼绑定失败：{error}")

    url = data.get("verification_uri_complete") or data.get("verification_uri") or ""
    await dfbind.finish(
        "请打开下面的链接完成水鱼授权：\n"
        f"{url}\n\n"
        "授权页会显示绑定身份（QQ 掩码），确认无误后点击同意。\n"
        "完成后直接发送 b50 即可。"
    )


async def _finish_lxbind(event: Event, code: str, state: str = "") -> None:
    ok, message = await oauth_accounts.finish_lxns_binding(_qq(event), code, state)
    if ok:
        await lxbind.finish(f"✅ {message}\n现在可以使用 b50 / 推荐 / 底力分析了。")
    await lxbind.finish(f"❌ {message}")


@lxbind.handle()
async def _handle_lxbind(event: Event, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if text in ("帮助", "help", "说明"):
        await lxbind.finish(
            "落雪绑定：\n"
            "1. 发送「lxbind」获取授权链接\n"
            "2. 打开链接并同意授权\n"
            "3. 把授权码或完整回调链接发回来\n\n"
            "需要先在 .env 配置 LX_CLIENT_ID / LX_CLIENT_SECRET / LX_REDIRECT_URI。"
        )
    if text:
        code, state = _extract_lx_code(text)
        if not code:
            await lxbind.finish("没有找到授权码，请重新发送「lxbind」获取授权链接。")
        await _finish_lxbind(event, code, state)
        return

    data, error = await oauth_accounts.start_lxns_binding(_qq(event))
    if not data:
        await lxbind.finish(f"❌ 落雪绑定失败：{error}")

    if data.get("no_callback"):
        finish_text = (
            "请打开下面的链接完成落雪授权：\n"
            f"{data['url']}\n\n"
            "当前为「无回调地址」模式：授权完成后页面会直接显示授权码。\n"
            "把授权码复制发给我即可，不会出现 127.0.0.1 无法访问的问题。\n"
            "授权码约 60 秒有效；发送「取消」可退出。"
        )
    else:
        finish_text = (
            "请打开下面的链接完成落雪授权：\n"
            f"{data['url']}\n\n"
            "授权完成后，把页面显示的授权码，或完整回调链接发给我。\n"
            "如果浏览器提示 127.0.0.1 无法访问，这是正常的：\n"
            "直接复制地址栏里的完整链接发回来即可。\n"
            "授权码约 60 秒有效；发送「取消」可退出。"
        )
    await lxbind.finish(finish_text)


@lx_code_listener.handle()
async def _handle_lx_code(event: Event):
    text = event.get_plaintext().strip()
    if not text:
        return
    code, state = _extract_lx_code(text)
    if not code:
        return
    ok, message = await oauth_accounts.finish_lxns_binding(_qq(event), code, state)
    if ok:
        await lx_code_listener.finish(f"✅ {message}\n现在可以使用 b50 / 推荐 / 底力分析了。")
    await lx_code_listener.finish(f"❌ {message}")


@source_cmd.handle()
async def _handle_source(event: Event, args: Message = CommandArg()):
    qq = _qq(event)
    text = args.extract_plain_text().strip()
    if not text:
        await source_cmd.finish(
            _source_text(await accounts.summary(qq))
            + "\n\n发送「数据源 自动 / 水鱼 / 落雪」切换。"
        )

    source = accounts.normalize_source(text)
    if source is None:
        await source_cmd.finish("用法：数据源 自动 / 水鱼 / 落雪")
    await accounts.set_source(qq, source)
    await source_cmd.finish(
        f"✅ 已切换数据源：{accounts.source_label(source)}\n"
        "查询时会优先使用该数据源，失败仍会回退另一个源。"
    )


@account_cmd.handle()
async def _handle_account(event: Event):
    await account_cmd.finish(
        _source_text(await accounts.summary(_qq(event)))
        + "\n\n绑定：dfbind / lxbind\n解绑：解绑水鱼 / 解绑落雪"
    )


@unbind_df.handle()
async def _handle_unbind_df(event: Event):
    ok = await accounts.unbind(_qq(event), "divingfish")
    await unbind_df.finish("✅ 已解除水鱼绑定。" if ok else "本账号没有绑定水鱼。")


@unbind_lx.handle()
async def _handle_unbind_lx(event: Event):
    ok = await accounts.unbind(_qq(event), "lxns")
    await unbind_lx.finish("✅ 已解除落雪绑定。" if ok else "本账号没有绑定落雪。")


@cancel_lx.handle()
async def _handle_cancel_lx(event: Event):
    oauth_accounts.cancel_lxns_binding(_qq(event))
    await cancel_lx.finish("已取消落雪绑定。")
