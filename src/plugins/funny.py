# src/plugins/funny.py
import random
import re
from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

# ========== 配置 ==========
# 音晶爆炸回复列表（用于随机输出）
YINJING_RESPONSES = [
    "Only feels like nothing could be better when I'm with you",
    "Dancing all night.Pray we're next together,it's a breakthrough",
    "X X XXX",
    "x x xxx",
    "XX X XXX",
    "xx x xxx",
    "X X X XXXX",
    "x x x xxxx",
    "XX XX XXXXX",
    "xx xx xxxxx",
    "XXX",
    "xxx",
    "XXX X",
    "xxx x",
    "XXX XXX",
    "xxx xxx",
    "XXXXX",
    "xxxxx",
    "XXXXXXX",
    "xxxxxxx",
    "XX X XX",
    "xx x xx",
    "XX",
    "xx",
    "XX",
    "xx",
    "XX",
    "xx",
    "XX",
    "xx",
    "XXXXXXXXXXXXXXXX!",
]

# 大写节奏 -> 小写节奏 映射表
CASE_MAP = {
    "X X XXX": "x x xxx",
    "XX X XXX": "xx x xxx",
    "X X X XXXX": "x x x xxxx",
    "XX XX XXXXX": "xx xx xxxxx",
    "XXX": "xxx",
    "XXX X": "xxx x",
    "XXX XXX": "xxx xxx",
    "XXXXX": "xxxxx",
    "XXXXXXX": "xxxxxxx",
    "XX X XX": "xx x xx",
    "XX": "xx",
    "XXXXXXXXXXXXXXXX!": "xxxxxxxxxxxxxxxx!",
}

# 所有大写节奏的集合（用于快速匹配）
UPPER_PATTERNS = set(CASE_MAP.keys())

# 所有小写节奏的集合（用于判断是否是小写）
LOWER_PATTERNS = set(CASE_MAP.values())


# ========== 规则：检测是否匹配大写节奏 ==========
def is_upper_pattern(text: str) -> bool:
    """检测文本是否匹配大写节奏（忽略首尾空格）"""
    text = text.strip()
    # 直接匹配
    if text in UPPER_PATTERNS:
        return True
    # 如果文本包含空格，尝试去除多余空格后匹配
    normalized = re.sub(r'\s+', ' ', text).strip()
    return normalized in UPPER_PATTERNS


def is_lower_pattern(text: str) -> bool:
    """检测文本是否匹配小写节奏（忽略首尾空格）"""
    text = text.strip()
    if text in LOWER_PATTERNS:
        return True
    normalized = re.sub(r'\s+', ' ', text).strip()
    return normalized in LOWER_PATTERNS


# ========== 消息监听器：直接匹配节奏文本 ==========
@on_message(priority=5, block=False).handle()
async def handle_rhythm_match(bot: Bot, event: Event):
    """监听所有消息，匹配节奏文本"""
    
    # 获取消息内容
    raw_msg = event.get_plaintext() if hasattr(event, 'get_plaintext') else str(event.get_message())
    raw_msg = raw_msg.strip()
    
    if not raw_msg:
        return
    
    # 如果消息以 / 开头，让命令处理器处理（不拦截命令）
    if raw_msg.startswith("/"):
        return
    
    # ========== 1. 检查是否匹配大写节奏 ==========
    if is_upper_pattern(raw_msg):
        # 标准化文本，去除多余空格
        normalized = re.sub(r'\s+', ' ', raw_msg).strip()
        # 转换为小写
        response = CASE_MAP.get(normalized)
        if response:
            logger.info(f"节奏匹配（大写 -> 小写）: {normalized} -> {response}")
            await bot.send(event=event, message=response)
            return
    
    # ========== 2. 检查是否匹配小写节奏（不触发） ==========
    if is_lower_pattern(raw_msg):
        logger.info(f"小写节奏不触发: {raw_msg}")
        # 不回复，静默忽略
        return


# ========== 命令处理器：/音晶爆炸 ==========
yinjing_baozha = on_command("音晶爆炸", aliases={"音晶", "爆炸"}, priority=10)

@yinjing_baozha.handle()
async def handle_yinjing_baozha(bot: Bot, event: Event, args: CommandArg = CommandArg()):
    """处理 /音晶爆炸 命令"""
    
    user_input = args.extract_plain_text().strip()
    
    # 如果输入了大写节奏，转换为小写输出
    if user_input in CASE_MAP:
        response = CASE_MAP[user_input]
        await yinjing_baozha.finish(response)
        return
    
    # 如果没有输入参数或输入其他内容，随机输出
    response = random.choice(YINJING_RESPONSES)
    await yinjing_baozha.finish(response)


# ========== 帮助命令 ==========
funny_help = on_command("funny帮助", aliases={"娱乐帮助"}, priority=10)

@funny_help.handle()
async def handle_funny_help(bot: Bot, event: Event):
    """显示娱乐功能帮助"""
    help_text = """
🎮 娱乐功能帮助：

【直接发送节奏文本】
发送以下任意大写节奏，机器人会自动回复小写版本：
  X X XXX          → x x xxx
  XX X XXX         → xx x xxx
  X X X XXXX       → x x x xxxx
  XX XX XXXXX      → xx xx xxxxx
  XXX              → xxx
  XXX X            → xxx x
  XXX XXX          → xxx xxx
  XXXXX            → xxxxx
  XXXXXXX          → xxxxxxx
  XX X XX          → xx x xx
  XX               → xx
  XXXXXXXXXXXXXXXX! → xxxxxxxxxxxxxxxx!

【命令方式】
/音晶爆炸          - 随机输出一句音晶台词
/音晶              - 同上
/爆炸              - 同上

💡 注意：发送小写节奏（如「x x xxx」）不会触发回复
    """
    await funny_help.finish(help_text.strip())