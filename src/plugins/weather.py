# src/plugins/weather.py
import re
import httpx
from datetime import datetime
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg

# 高德地图API Key
AMAP_API_KEY = "dd54346507553273545a35222f4be8b2"

# 创建命令处理器
weather = on_command("天气", aliases={"weather", "天气预报", "出勤天气", "出勤", "出勤建议"}, priority=10)

# 城市名称到高德adcode的映射
CITY_CODE_MAP = {
    "北京": "110000",
    "海淀": "110108",
    "朝阳": "110105",
    "上海": "310000",
    "闵行": "310112",
    "宝山": "310113",
    "嘉定": "310114",
    "浦东": "310115",
    "天津": "120000",
    "重庆": "500000",
    "哈尔滨": "230100",
    "长春": "220100",
    "沈阳": "210100",
    "大连": "210200",
    "鞍山": "210300",
    "呼和浩特": "150100",
    "包头": "150200",
    "石家庄": "130100",
    "保定": "130600",
    "唐山": "130200",
    "廊坊": "131000",
    "太原": "140100",
    "大同": "140200",
    "西安": "610100",
    "咸阳": "610400",
    "延安": "610600",
    "济南": "370100",
    "青岛": "370200",
    "淄博": "370300",
    "烟台": "370600",
    "潍坊": "370700",
    "乌鲁木齐": "650100",
    "克拉玛依": "650200",
    "拉萨": "540100",
    "日喀则": "540200",
    "西宁": "630100",
    "兰州": "620100",
    "银川": "640100",
    "郑州": "410100",
    "洛阳": "410300",
    "开封": "410200",
    "南京": "320100",
    "无锡": "320200",
    "苏州": "320500",
    "徐州": "320300",
    "常州": "320400",
    "杭州": "330100",
    "宁波": "330200",
    "温州": "330300",
    "嘉兴": "330400",
    "湖州": "330500",
    "绍兴": "330600",
    "金华": "330700",
    "合肥": "340100",
    "芜湖": "340200",
    "蚌埠": "340300",
    "福州": "350100",
    "厦门": "350200",
    "泉州": "350500",
    "漳州": "350600",
    "南昌": "360100",
    "九江": "360400",
    "赣州": "360700",
    "长沙": "430100",
    "湘潭": "430300",
    "株洲": "430200",
    "衡阳": "430400",
    "岳阳": "430600",
    "贵阳": "520100",
    "遵义": "520300",
    "成都": "510100",
    "绵阳": "510700",
    "南充": "511300",
    "宜宾": "511500",
    "广州": "440100",
    "深圳": "440300",
    "珠海": "440400",
    "佛山": "440600",
    "惠州": "441300",
    "东莞": "441900",
    "中山": "442000",
    "昆明": "530100",
    "大理": "532900",
    "丽江": "530700",
    "南宁": "450100",
    "桂林": "450300",
    "柳州": "450200",
    "海口": "460100",
    "三亚": "460200",
    "香港": "810000",
    "澳门": "820000",
    "台北": "710000",
    "高雄": "710000",
    "台中": "710000",
}

# 默认城市
DEFAULT_CITY = "杭州"

# 出勤建议配置
ATTENDANCE_ADVICE = {
    "晴": "☀️ 天气晴朗，适合出勤！记得做好防晒，带上水杯~",
    "多云": "⛅ 多云天气，适合出勤！温度适宜，冲吧！",
    "阴": "☁️ 阴天，适合出勤！不会太晒，打歌状态up！",
    "小雨": "🌦️ 有小雨，建议带伞出勤。室内机厅不受影响，冲！",
    "中雨": "🌧️ 中雨，建议带伞。出勤注意路上安全~",
    "大雨": "🌧️ 大雨，建议带好雨具。出勤注意安全，可以考虑改天~",
    "雷阵雨": "⛈️ 有雷阵雨，建议推迟出勤或注意安全！",
    "雪": "❄️ 下雪了！路面可能湿滑，出勤注意保暖和防滑！",
    "雾": "🌫️ 有雾，能见度低，出勤注意交通安全！",
}

# 温度建议
TEMPERATURE_ADVICE = [
    (35, "🥵 太热了！注意防暑，建议室内活动，多补水！"),
    (30, "☀️ 天气较热，注意防晒，带好水杯~"),
    (25, "🌤️ 温度适宜，非常适合出勤！"),
    (20, "🌤️ 温度舒适，绝佳出勤天气！"),
    (10, "🧥 温度偏凉，建议带件外套~"),
    (0, "🧣 天气寒冷，注意保暖！"),
    (-10, "🥶 非常冷！多穿衣服，注意防寒！"),
]


@weather.handle()
async def handle_weather(bot: Bot, event: Event, args: Message = CommandArg()):
    """处理天气查询命令"""
    
    # ========== 方法1：尝试从 CommandArg 提取 ==========
    city_input = args.extract_plain_text().strip()
    
    # ========== 方法2：如果提取失败，从原始消息中提取 ==========
    if not city_input:
        raw_msg = event.get_plaintext() if hasattr(event, 'get_plaintext') else ""
        # 匹配各种命令前缀后面的内容
        match = re.search(r'(?:天气|weather|出勤|天气预报|出勤天气|出勤建议)\s*(.+)', raw_msg)
        if match:
            city_input = match.group(1).strip()
    
    # ========== 方法3：如果还不行，尝试从原始消息中直接取 ==========
    if not city_input:
        raw_msg = str(event.get_message()) if hasattr(event, 'get_message') else ""
        # 去掉 / 前缀
        for prefix in ["/天气", "/weather", "/出勤", "/天气预报", "/出勤天气", "/出勤建议"]:
            if raw_msg.startswith(prefix):
                city_input = raw_msg[len(prefix):].strip()
                break
        # 如果还是以 / 开头但没匹配上，尝试去掉第一个 /
        if not city_input and raw_msg.startswith("/"):
            parts = raw_msg.split(" ", 1)
            if len(parts) > 1:
                city_input = parts[1].strip()
    
    # 记录解析结果，方便调试
    logger.info(f"解析到的城市参数: '{city_input}'")
    
    # 默认城市
    if not city_input:
        city = DEFAULT_CITY
    else:
        city = city_input
    
    try:
        await weather.send(f"🔍 正在查询 {city} 的天气...")
        
        # 获取城市编码
        city_code = CITY_CODE_MAP.get(city)
        if city_code is None:
            # 尝试模糊匹配
            for key in CITY_CODE_MAP:
                if city in key or key in city:
                    city_code = CITY_CODE_MAP[key]
                    city = key
                    break
        
        if city_code is None:
            await weather.finish(f"❌ 未找到城市「{city}」的天气信息，请检查城市名称是否正确~")
            return
        
        # 调用高德天气API
        weather_data = await fetch_weather(city_code, city)
        
        if weather_data is None:
            await weather.finish(f"❌ 获取 {city} 天气信息失败，请稍后重试~")
            return
        
        message = format_weather_message(weather_data, city)
        await weather.finish(message)
        
    except httpx.TimeoutException:
        await weather.finish("⏰ 请求超时，请稍后重试~")
    except httpx.RequestError as e:
        logger.error(f"请求天气API失败: {e}")
        await weather.finish("🌐 网络请求失败，请检查网络连接~")


async def fetch_weather(city_code: str, city: str) -> dict:
    """
    获取天气数据
    使用高德地图天气API
    """
    try:
        url = "https://restapi.amap.com/v3/weather/weatherInfo"
        params = {
            "key": AMAP_API_KEY,
            "city": city_code,
            "extensions": "base"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get("status") == "1" and data.get("lives"):
                    return {
                        "source": "amap",
                        "data": data,
                        "city": city
                    }
                else:
                    logger.warning(f"高德天气API返回错误: {data.get('info', '未知错误')}")
                    return None
            else:
                logger.warning(f"高德天气API返回HTTP错误: {response.status_code}")
                return None
                
    except Exception as e:
        logger.error(f"高德天气API请求失败: {e}")
        return None


def format_weather_message(weather_data: dict, city: str) -> str:
    """格式化天气消息"""
    
    data = weather_data.get("data")
    lives = data.get("lives", [])
    
    if not lives:
        return "❌ 无法解析天气数据~"
    
    live = lives[0]
    
    # 提取数据
    temp = live.get("temperature", "N/A")
    weather_desc = live.get("weather", "未知")
    wind_dir = live.get("winddirection", "未知")
    wind_power = live.get("windpower", "未知")
    humidity = live.get("humidity", "N/A")
    province = live.get("province", "")
    report_time = live.get("reporttime", "")
    
    # 构建消息
    message_parts = []
    
    # 标题
    location = f"{province} {city}" if province else city
    message_parts.append(f"🌤️ {location} 实时天气")
    message_parts.append("=" * 20)
    
    # 当前天气
    message_parts.append(f"🌡️ 温度：{temp}°C")
    message_parts.append(f"☁️ 天气：{weather_desc}")
    message_parts.append(f"💧 湿度：{humidity}%")
    message_parts.append(f"💨 风向：{wind_dir} {wind_power}级")
    
    # 出勤建议
    message_parts.append("")
    message_parts.append("=" * 20)
    message_parts.append(get_attendance_advice(temp, weather_desc))
    
    # 更新时间
    if report_time:
        message_parts.append("")
        message_parts.append(f"🕐 更新时间：{report_time}")
    else:
        message_parts.append("")
        message_parts.append(f"🕐 更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    message_parts.append("📡 数据来源：高德地图")
    
    return "\n".join(message_parts)


def get_attendance_advice(temp, weather_desc: str) -> str:
    """根据天气生成出勤建议"""
    
    advice_parts = ["🎯 出勤建议："]
    
    # 根据天气描述给出建议
    weather_advice = None
    for key, advice in ATTENDANCE_ADVICE.items():
        if key in weather_desc:
            weather_advice = advice
            break
    
    if weather_advice:
        advice_parts.append(weather_advice)
    
    # 根据温度给出建议
    try:
        temp_num = int(temp)
        for threshold, advice in TEMPERATURE_ADVICE:
            if temp_num >= threshold:
                advice_parts.append(advice)
                break
    except:
        pass
    
    # 根据当前时间给出机厅营业建议
    now = datetime.now()
    hour = now.hour
    
    if hour < 10:
        advice_parts.append("🏪 现在时间较早（<10:00），大部分机厅可能还没开门，建议10点后再出发~")
    elif hour >= 22:
        advice_parts.append("🌙 现在时间较晚（>=22:00），大部分机厅可能已经关门了，注意时间安排~")
    elif 10 <= hour < 12:
        advice_parts.append("🌅 上午时段，机厅刚开门不久，人少环境好，适合推分！")
    elif 12 <= hour < 14:
        advice_parts.append("🍜 午间时段，可以顺便吃个饭再战！")
    elif 14 <= hour < 18:
        advice_parts.append("☀️ 下午时段，黄金出勤时间！冲！")
    elif 18 <= hour < 20:
        advice_parts.append("🌆 晚间时段，吃完饭来打几把消消食~")
    else:
        advice_parts.append("🌙 晚上时段，注意结束时间，别错过末班车~")
    
    # 工作日/周末建议
    is_weekday = now.weekday() < 5
    if is_weekday:
        if 9 <= hour < 18:
            advice_parts.append("💼 工作日白天，机厅人少，适合静心推分！")
        else:
            advice_parts.append("📅 工作日晚上，机厅人可能不多，适合练习~")
    else:
        advice_parts.append("🎉 周末！机厅人多热闹，约上群友一起出勤！")
    
    return "\n".join(advice_parts)


# ========== 帮助命令 ==========
weather_help = on_command("天气帮助", aliases={"天气指令"}, priority=10)

@weather_help.handle()
async def handle_weather_help(bot: Bot, event: Event):
    """显示天气命令帮助"""
    help_text = """
📖 天气查询命令帮助：

/天气 [城市名] - 查询指定城市的天气
/weather [城市名] - 同上（英文）
/出勤 [城市名] - 查询天气并给出出勤建议
/出勤建议 [城市名] - 同上

🌰 示例：
/天气              # 查询杭州天气（默认）
/天气 上海          # 查询上海天气
/出勤 广州          # 查询广州天气并给出出勤建议

💡 提示：
- 不指定城市时，默认为杭州
- 支持中文城市名（如北京、上海、广州等）
- 出勤建议会根据天气、温度、时间和工作日/周末综合生成
- 10:00前会提示机厅可能还没开门
- 22:00后会提示机厅可能已关门
- 数据来源：高德地图
    """
    await weather_help.finish(help_text.strip())