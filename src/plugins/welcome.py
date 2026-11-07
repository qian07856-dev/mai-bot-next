# src/plugins/welcome.py
import os
import random
from pathlib import Path
from nonebot import on_notice, on_command
from nonebot.adapters.onebot.v11 import Bot, Event, GroupIncreaseNoticeEvent, GroupDecreaseNoticeEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

# ========== 配置 ==========
# 所有群统一使用一张欢迎图；路径可在 .env 中用 WELCOME_IMAGE 覆盖
DATA_DIR = Path(os.getenv("WELCOME_DIR") or "D:/data")
DEFAULT_IMAGE = Path(os.getenv("WELCOME_IMAGE") or str(DATA_DIR / "welcome.jpg"))

# 欢迎语列表（随机选取）
WELCOME_MESSAGES = [
    "欢迎新人喵~群地位-1……",
    "新人来了！群地位-1！",
    "欢迎欢迎！群地位-1！",
    "好耶！新人！群地位-1！",
    "新群友加入！群地位-1！",
    "欢迎新人！记得看群规哦~群地位-1！",
    "又来一个！群地位-1！",
    "欢迎光临！群地位-1！",
    "新人快跑！群地位-1！",
    "欢迎新人！群地位-1（传统艺能）",
    "新人报到，群地位-1！",
    "欢迎！群地位-1！",
    "新人来了，群地位-1！",
    "好耶，是新人！群地位-1！",
    "欢迎新人喵~群地位-1！",
]

# 退群消息模板
LEAVE_MESSAGES = [
    "退群了！惋惜惋惜，让我们永远记住TA",
    "悄悄地走了，让我们永远记住TA",
    "离开了我们，让我们永远记住TA",
    "退群了！群地位+1……让我们永远记住TA",
    "走了走了，让我们永远记住TA",
]

# 需要监控的群号（留空则监控所有群）
MONITOR_GROUPS = []  # 如果为空，则监控所有群

# 是否发送图片
SEND_IMAGE = True


# ========== 入群事件监听 ==========
welcome_notice = on_notice(priority=1, block=False)

@welcome_notice.handle()
async def handle_group_increase(bot: Bot, event: Event):
    """处理群成员增加事件"""
    
    # 只处理群成员增加事件
    if not isinstance(event, GroupIncreaseNoticeEvent):
        return
    
    # 排除机器人自己入群（防止自己欢迎自己）
    if str(event.user_id) == str(bot.self_id):
        logger.info(f"机器人自身入群，跳过欢迎")
        return
    
    group_id = event.group_id
    
    if MONITOR_GROUPS and group_id not in MONITOR_GROUPS:
        logger.info(f"群 {group_id} 不在监控列表中，跳过欢迎")
        return
    
    user_id = event.user_id
    user_name = str(user_id)
    
    try:
        user_info = await bot.get_stranger_info(user_id=user_id)
        user_name = user_info.get("nickname", str(user_id))
    except Exception as e:
        logger.warning(f"获取用户信息失败: {e}")
    
    await send_welcome_message(bot, group_id, user_id)
    logger.info(f"群 {group_id} 欢迎新人 {user_name}({user_id})")


# ========== 退群事件监听 ==========
leave_notice = on_notice(priority=1, block=False)

@leave_notice.handle()
async def handle_group_decrease(bot: Bot, event: Event):
    """处理群成员减少事件"""
    
    # 只处理群成员减少事件
    if not isinstance(event, GroupDecreaseNoticeEvent):
        return
    
    # 排除机器人自己退群
    if str(event.user_id) == str(bot.self_id):
        logger.info(f"机器人自身退群，跳过通知")
        return
    
    group_id = event.group_id
    
    if MONITOR_GROUPS and group_id not in MONITOR_GROUPS:
        logger.info(f"群 {group_id} 不在监控列表中，跳过退群通知")
        return
    
    user_id = event.user_id
    user_name = str(user_id)
    
    # 尝试获取用户昵称（退群后可能获取不到，用 QQ 号代替）
    try:
        user_info = await bot.get_stranger_info(user_id=user_id)
        user_name = user_info.get("nickname", str(user_id))
    except Exception as e:
        logger.warning(f"获取用户信息失败: {e}")
        user_name = str(user_id)
    
    # 发送退群通知
    await send_leave_message(bot, group_id, user_id, user_name)
    logger.info(f"群 {group_id} 成员 {user_name}({user_id}) 退群")


# ========== 主动触发命令 ==========
welcome_cmd = on_command("welcome", aliases={"欢迎"}, priority=10)

@welcome_cmd.handle()
async def handle_welcome_cmd(bot: Bot, event: Event, args: CommandArg = CommandArg()):
    """处理 /welcome 或 /欢迎 命令，@发送者本人"""
    
    if not hasattr(event, "group_id") or not event.group_id:
        await welcome_cmd.finish("❌ 该命令仅在群聊中可用~")
        return
    
    group_id = event.group_id
    user_id = event.user_id
    
    # 命令触发者如果是机器人自己，不处理
    if str(user_id) == str(bot.self_id):
        await welcome_cmd.finish("❌ 机器人不需要测试欢迎自己~")
        return
    
    if MONITOR_GROUPS and group_id not in MONITOR_GROUPS:
        await welcome_cmd.finish("❌ 本群未开启欢迎功能~")
        return
    
    await send_welcome_message(bot, group_id, user_id)
    logger.info(f"群 {group_id} 中 {user_id} 主动触发了欢迎")


# ========== 退群测试命令 ==========
leave_cmd = on_command("quit", aliases={"退群"}, priority=10)

@leave_cmd.handle()
async def handle_leave_cmd(bot: Bot, event: Event, args: CommandArg = CommandArg()):
    """处理 /quit 或 /退群 命令，模拟退群通知"""
    
    if not hasattr(event, "group_id") or not event.group_id:
        await leave_cmd.finish("❌ 该命令仅在群聊中可用~")
        return
    
    group_id = event.group_id
    user_id = event.user_id
    
    if MONITOR_GROUPS and group_id not in MONITOR_GROUPS:
        await leave_cmd.finish("❌ 本群未开启退群通知功能~")
        return
    
    # 获取发送者信息
    try:
        user_info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        user_name = user_info.get("nickname", str(user_id))
    except Exception as e:
        logger.warning(f"获取用户信息失败: {e}")
        user_name = str(user_id)
    
    # 发送退群通知
    await send_leave_message(bot, group_id, user_id, user_name, is_test=True)
    logger.info(f"群 {group_id} 中 {user_name}({user_id}) 主动触发了退群测试")


# ========== 发送消息函数 ==========
async def send_welcome_message(bot: Bot, group_id: int, user_id: int):
    """发送欢迎消息"""
    
    welcome_text = random.choice(WELCOME_MESSAGES)
    
    message_parts = []
    # @用户 + 空格（确保@后面的文字不会紧贴着@显示）
    message_parts.append(MessageSegment.at(user_id))
    message_parts.append(MessageSegment.text(" "))
    message_parts.append(MessageSegment.text(welcome_text))
    
    if SEND_IMAGE:
        image_segment = await get_welcome_image(group_id)
        if image_segment:
            message_parts.append(image_segment)
    
    try:
        await bot.send_group_msg(
            group_id=group_id,
            message=message_parts
        )
    except Exception as e:
        logger.error(f"发送欢迎消息失败: {e}")


async def send_leave_message(bot: Bot, group_id: int, user_id: int, user_name: str, is_test: bool = False):
    """发送退群通知"""
    
    leave_text = random.choice(LEAVE_MESSAGES)
    
    # 构建消息
    message_parts = []
    
    # 用户名 + QQ号
    message_parts.append(MessageSegment.text(f'"{user_name}"（{user_id}）'))
    message_parts.append(MessageSegment.text(leave_text))
    
    # 如果是测试，添加标记
    if is_test:
        message_parts.append(MessageSegment.text("（测试消息）"))
    
    try:
        await bot.send_group_msg(
            group_id=group_id,
            message=message_parts
        )
    except Exception as e:
        logger.error(f"发送退群通知失败: {e}")


async def get_welcome_image(group_id: int = None) -> MessageSegment:
    """获取欢迎图片：所有群统一一张图"""
    if DEFAULT_IMAGE.exists():
        try:
            logger.info(f"使用默认图片: {DEFAULT_IMAGE}")
            return MessageSegment.image(DEFAULT_IMAGE)
        except Exception as e:
            logger.error(f"读取默认图片失败: {e}")
    
    logger.warning(f"未找到欢迎图片，请检查 D:/data/welcome.jpg 是否存在")
    return None


# ========== 可选：入群时发送群规 ==========
SEND_RULES = False
RULES_TEXT = """
📋 群规速览：
1. 禁止人身攻击
2. 禁止刷屏
3. 禁止广告
4. 友好交流
5. 多打歌，多上分！

祝你在群里玩得开心~
"""

@welcome_notice.handle()
async def send_rules(bot: Bot, event: GroupIncreaseNoticeEvent):
    """发送群规（可选）"""
    if not SEND_RULES:
        return
    
    try:
        await bot.send_group_msg(
            group_id=event.group_id,
            message=RULES_TEXT.strip()
        )
    except Exception as e:
        logger.error(f"发送群规失败: {e}")