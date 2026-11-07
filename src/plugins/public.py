# src/plugins/public.py
import base64
import os

from nonebot import on_command, on_notice
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import Message, Event, Bot, MessageSegment
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor
from nonebot.log import logger
from src.libraries.config_env import load_project_env

# 让 .env 里的 HELP_IMAGE 等配置生效（nonebot 不会把 .env 写进环境变量）
load_project_env()

# 帮助图：默认出图，失败自动回退文字
HELP_IMAGE = os.getenv("HELP_IMAGE", "true").lower() in ("1", "true", "yes", "on")
HELP_IMAGE_FORMAT = (os.getenv("HELP_IMAGE_FORMAT") or "jpg").strip().lower()


def _env_positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


HELP_COLUMNS = min(4, _env_positive_int("HELP_COLUMNS", 2))
HELP_WIDTH = max(1400, _env_positive_int("HELP_WIDTH", 2300))


@event_preprocessor
async def preprocessor(bot, event, state):
    if hasattr(event, 'message_type') and event.message_type == "private" and event.sub_type != "friend":
        raise IgnoredException("not reply group temp message")

        
help = on_command("help", aliases={"帮助", "指令", "菜单"})

# 帮助内容（图片版按分组排版；文字版作为兜底）
HELP_SECTIONS = [
    {
        "heading": "查歌 / 查谱",
        "rows": [
            {"label": "查歌 <标题片段 / 艺术家>", "value": "生图列出匹配乐曲（附标签）"},
            {"label": "[绿黄红紫白]id<歌曲编号>", "value": "生图查看曲目与谱面信息（附标签）"},
            {"label": "XXX是什么歌", "value": "按别名查歌（生图 + 标签）"},
            {"label": "定数查歌 <定数> / <下限> <上限>", "value": "按定数范围查曲"},
            {"label": "分数线 <难度+歌曲id> <分数线>", "value": "该分数线的容错（分数线 帮助）"},
        ],
    },
    {
        "heading": "随机 / 运势",
        "rows": [
            {"label": "今日舞萌", "value": "查看今天的舞萌运势"},
            {"label": "mai什么", "value": "随机一首歌（生图 + 标签）"},
            {"label": "随个[dx/标准][绿黄红紫白]<定数>", "value": "按条件随机（生图 + 标签）"},
        ],
    },
    {
        "heading": "猜歌小游戏",
        "rows": [
            {"label": "开字母 [等级13+] [定数13.5-14.0] [dx/sd]",
             "value": "开始一局开字母猜歌", "value_color": (150, 226, 150, 255)},
            {"label": "开x", "value": "揭开字母 x（如 开a、开星）", "value_color": (150, 226, 150, 255)},
            {"label": "开歌 <曲名/别名/id>", "value": "提交答案", "value_color": (150, 226, 150, 255)},
            {"label": "不玩了", "value": "结束并公布答案", "value_color": (150, 226, 150, 255)},
            {"label": "开字母榜", "value": "查看本群今日答对榜", "value_color": (150, 226, 150, 255)},
        ],
        "note": "仅群聊；默认排除宴会场谱面；开字母不需要曲绘/音频/谱面等额外资源",
    },
    {
        "heading": "谱面标签（DXTag）",
        "rows": [
            {"label": "标签 <曲名> [难度]", "value": "键盘 / 星星 / 技巧 / 体力 / 爆发五轴"},
            {"label": "标签列表", "value": "查看本地官谱谱库收录情况"},
            {"label": "标签帮助", "value": "标签功能说明"},
        ],
        "note": "标签由谱面音符结构计算，需要管理员放入的本地官谱 maidata",
    },
    {
        "heading": "推分 / 成绩",
        "rows": [
            {"label": "推荐 [目标Rating/定数范围] [鸟加/鸟/ss+/无] [第x页]",
             "value": "自动推荐推分曲目（推荐帮助）",
             "value_color": (150, 226, 150, 255)},
            {"label": "上分 15000 / 上分 曲名 白 100.5",
             "value": "上分模拟 / 目标拆解",
             "value_color": (150, 226, 150, 255)},
            {"label": "b50 / b50 @某人 / b50 用户名",
             "value": "查询 B50（水鱼优先，失败自动尝试落雪）",
             "value_color": (150, 226, 150, 255)},
            {"label": "底力分析 / 底力分析 @某人 / 底力分析 用户名",
             "value": "统计 B50 五轴最大/平均/最小（雷达图）",
             "value_color": (150, 226, 150, 255)},
        ],
    },
    {
        "heading": "账号 / 数据源",
        "rows": [
            {"label": "dfbind / 绑定水鱼", "value": "水鱼设备码 OAuth 绑定", "value_color": (150, 226, 150, 255)},
            {"label": "lxbind / 绑定落雪", "value": "落雪授权码 OAuth 绑定", "value_color": (150, 226, 150, 255)},
            {"label": "数据源 自动/水鱼/落雪", "value": "切换自己的首选数据源"},
            {"label": "账号 / 绑定状态", "value": "查看当前绑定与数据源"},
            {"label": "解绑水鱼 / 解绑落雪", "value": "解除对应 OAuth 绑定"},
        ],
        "note": "绑定后 b50、推荐、底力分析、上分模拟会优先使用你选择的数据源，失败自动回退另一个",
    },
    {
        "heading": "服务器 / 出勤",
        "rows": [
            {"label": "status / 状态 / mai状态", "value": "查询华立服务器状态"},
            {"label": "天气 / 出勤 [城市]", "value": "天气与出勤建议（默认杭州）"},
        ],
    },
    {
        "heading": "订阅推送",
        "rows": [
            {"label": "订阅 状态", "value": "服务器从正常变为异常时推送", "value_color": (150, 226, 150, 255)},
            {"label": "订阅 新曲", "value": "水鱼曲库出现新曲时推送", "value_color": (150, 226, 150, 255)},
            {"label": "订阅 天气 杭州", "value": "降雨/高温/雾霾大风提醒", "value_color": (150, 226, 150, 255)},
            {"label": "订阅列表", "value": "查看本群订阅"},
            {"label": "取消订阅 状态/新曲/天气/全部", "value": "移除订阅"},
        ],
        "note": "按群订阅，重启后保留；状态只在正常 -> 异常时推送",
    },
    {
        "heading": "机厅人数（nearcade）",
        "rows": [
            {"label": "<别名><人数>", "value": "上报机厅当前人数（如 m1）", "value_color": (150, 226, 150, 255)},
            {"label": "<别名>j / <别名>几", "value": "查询当前人数与最近 3 小时上报", "value_color": (150, 226, 150, 255)},
        ],
        "note": "别名在 arcade.json 里配置（如 m = 魔力空间（上城天街店））；上报需在 .env 配置 NEARCADE_TOKEN",
    },
    {
        "heading": "娱乐 / 其他",
        "rows": [
            {"label": "音晶爆炸 / X X XXX", "value": "有惊喜"},
            {"label": "bilibili 视频链接", "value": "自动爬取视频信息并发送视频"},
            {"label": "入群 / 退群", "value": "自动欢迎与退群提醒"},
        ],
    },
]


@help.handle()
async def _(bot: Bot, event: Event, state: T_State):
    if HELP_IMAGE:
        payload = None
        try:
            from src.libraries.card_image import image_to_bytes, render_card

            image = await render_card(
                title="mai-bot 命令帮助",
                subtitle="直接发送对应命令即可；[ ] 为可选项，< > 为参数",
                badge="帮助",
                badge_color=(180, 150, 255, 255),
                sections=HELP_SECTIONS,
                footer="数据来源：水鱼查分器 / 华立监控平台",
                accent=(150, 118, 255, 255),
                columns=HELP_COLUMNS,
                width=HELP_WIDTH,
            )
            payload = image_to_bytes(image, HELP_IMAGE_FORMAT, 88)
        except Exception as e:
            logger.exception(f"[public] 帮助图生成失败，回退文字：{e}")
        if payload:
            logger.info(f"[public] 已生成帮助图：{len(payload) / 1024:.0f} KB")
            await help.finish(Message([
                MessageSegment("image", {"file": "base64://" + base64.b64encode(payload).decode()})
            ]))
            return

    help_str = '''可用命令如下：
今日舞萌 查看今天的舞萌运势
mai什么 随机一首歌（生图 + 标签）
随个[dx/标准][绿黄红紫白]<定数> 随机一首指定条件的乐曲（生图 + 标签）
查歌 <标题片段/艺术家/别名> 生图列出匹配乐曲（附标签）
[绿黄红紫白]id<歌曲编号> 生图查看曲目与谱面信息（附标签）
XXX是什么歌 按别名查歌（生图 + 标签）
定数查歌 <定数>  查询定数对应的乐曲
定数查歌 <定数下限> <定数上限>
开字母 [等级13+] [定数13.5-14.0] [dx/sd] 开始开字母猜歌，开x 揭字母，开歌<曲名/别名/id> 猜歌，不玩了 看答案
开字母榜 查看本群今日答对榜
分数线 <难度+歌曲id> <分数线> 详情请输入“分数线 帮助”查看
标签 <曲名> [难度] 查看键盘/星星/技巧/体力/爆发五轴（DXTag）
标签列表 查看本地官谱谱库收录情况
标签帮助 标签功能说明
dfbind / 绑定水鱼、lxbind / 绑定落雪 完成 OAuth 绑定；数据源 自动/水鱼/落雪 切换首选数据源
status或状态,mai状态 查询华立服务器状态
天气+出勤建议 发送“天气”或“出勤”即可查询，默认是杭州天气，后面空格加上城市名可查询所在地天气
订阅 状态/新曲/天气 杭州 添加主动推送；订阅列表 查看；取消订阅 状态/新曲/天气/全部 移除
<机厅别名><人数> 上报机厅当前人数（如 m1）
<机厅别名>j 或 <机厅别名>几 查询机厅当前人数与最近3小时上报（别名在 arcade.json 配置）
入群/退群相关消息发送
可以尝试输入“音晶爆炸”或者“X X XXX”，有惊喜
推荐 [目标Rating/定数范围] [鸟加/鸟/ss+/无] [第x页] 按查分器Rating自动推荐推分曲目（如“推荐”“推荐 15000 鸟”“推荐 第2页”，输入“推荐帮助”看详情）
上分 15000 按当前B50算还差多少ra、打哪几首最划算；上分 曲名 白 100.5 单曲模拟
b50 查询 b50（水鱼优先，失败自动尝试落雪；支持 b50 @某人 / b50 用户名）
底力分析 [@某人/用户名] 统计 b50 内所有谱面的键盘/星星/技巧/体力/爆发最大、平均、最小值（雷达图）
发送bilibili视频链接将自动爬取视频信息并发送视频'''
    await help.send(help_str)


async def _group_poke(bot: Bot, event: Event) -> bool:
    value = (event.notice_type == "notify" and event.sub_type == "poke" and event.target_id == int(bot.self_id))
    return value


poke = on_notice(rule=_group_poke, priority=10, block=True)


@poke.handle()
async def _(bot: Bot, event: Event, state: T_State):
    if event.__getattribute__('group_id') is None:
        event.__delattr__('group_id')
    await poke.send(Message([
        MessageSegment("poke",  {
           "qq": f"{event.sender_id}"
       })
    ]))
