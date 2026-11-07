# src/libraries/maimai_best50_new.py
"""
新版 B50 绘图（移植自 Yuri-YuzuChaN/nonebot-plugin-maimaidx 的 core/image/best50.py，MIT License）

版式与坐标、素材尺寸都按参考插件的新版 UI（CIRCLE 主题，1400x1600）来画：
  · 顶部：logo + 名牌 + 段位铭牌 + rating 区块（01~12 档 + >=14000 星级绶带）+ 称号条
  · 旧曲 Best 35：5 列 × 7 行，起始 y=235
  · 新曲 Best 15：5 列 × 3 行，起始 y=1085
每格画出封面、难度底色、评价图标、FC/FS 图标、DX 星数、曲名、达成率、DX 分数与定数→ra。

数据来源仍是水鱼查分器（/query/player 带 b50 参数）；参考插件里来自落雪的成绩字段
（职业等级 class_rank、头像 icon、牌子 trophy）我们没有，按它的兜底分支处理：
头像用 QQ 头像、称号条显示 B35+B15=rating、职业等级不画。
"""

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ========== 资源路径（与参考插件的 resources 对应） ==========

STATIC_DIR = Path("src/static")
FONT_DIR = STATIC_DIR / "font"
PIC_DIR = STATIC_DIR / "mai" / "pic"
PLATE_DIR = STATIC_DIR / "mai" / "plate_version"
COVER_DIR = STATIC_DIR / "mai" / "cover"
THEME = "circle"  # 新版 UI 主题目录
THEME_DIR = PIC_DIR / THEME

FONT_TITLE = FONT_DIR / "ResourceHanRoundedCN-Bold.ttf"  # 中文标题（参考插件里的 SIYUAN）
FONT_NUM = FONT_DIR / "Torus SemiBold.otf"               # 数字（参考插件里的 TBFONT）
FONT_FOT = FONT_DIR / "FOT-NewRodin Pro EB.otf"          # 备用（参考插件里的 FOTNEWRODIN）

B50_API = "https://www.diving-fish.com/api/maimaidxprober/query/player"
MUSIC_DATA_API = "https://www.diving-fish.com/api/maimaidxprober/music_data"
COVER_URL = "https://www.diving-fish.com/covers/{}.png"
QQ_LOGO_URL = "https://q1.qlogo.cn/g"
HEADERS = {"User-Agent": "mai-bot-b50/1.0", "Referer": "https://www.diving-fish.com/maimaidx/prober/"}

# ========== 常量（对应参考插件的 constants.py） ==========

RANK_MAP = {
    "d": "D", "c": "C", "b": "B", "bb": "BB", "bbb": "BBB", "a": "A", "aa": "AA",
    "aaa": "AAA", "s": "S", "sp": "Sp", "ss": "SS", "ssp": "SSp",
    "sss": "SSS", "sssp": "SSSp",
}
COMBO_MAP = {"fc": "FC", "fcp": "FCp", "ap": "AP", "app": "APp"}
SYNC_MAP = {"fs": "FS", "fsp": "FSp", "fdx": "FSD", "fdxp": "FSDp", "fsd": "FSD", "fsdp": "FSDp", "sync": "Sync"}

# 难度底色上的文字颜色（与参考插件的 AssetsImage 一致）
DIFF_TEXT_COLOR = [
    (255, 255, 255, 255), (255, 255, 255, 255), (255, 255, 255, 255),
    (255, 255, 255, 255), (138, 0, 226, 255),
]
ID_TEXT_COLOR = [
    (129, 217, 85, 255), (245, 189, 21, 255), (255, 129, 141, 255),
    (159, 81, 220, 255), (138, 0, 226, 255),
]
DEFAULT_TEXT_COLOR = (124, 129, 255, 255)

RA_TIER_STEPS = (
    (1000, "01"), (2000, "02"), (4000, "03"), (7000, "04"), (10000, "05"),
    (12000, "06"), (13000, "07"), (14000, "08"), (14500, "09"), (15000, "10"),
    (16000, "11"),
)
RA_STAR_THRESHOLDS = (14000, 14250, 14500, 14750, 15000, 15250, 15500, 15750, 16000, 16250, 16500, 16750)
RA_STAR_NUM = (1, 2, 1, 2, 1, 2, 3, 4, 1, 2, 3, 4)


def get_char_width(o: int) -> int:
    """半角/全角宽度（与参考插件一致），用于标题截断"""
    widths = [
        (126, 1), (159, 0), (687, 1), (710, 0), (711, 1), (727, 0), (733, 1), (879, 0),
        (1154, 1), (1161, 0), (4347, 1), (4447, 2), (7467, 1), (7521, 0), (8369, 1), (8426, 0),
        (9000, 1), (9002, 2), (11021, 1), (12350, 2), (12351, 1), (12438, 2), (12442, 0),
        (19893, 2), (19967, 1), (55203, 2), (63743, 1), (64106, 2), (65039, 1), (65059, 0),
        (65131, 2), (65279, 1), (65376, 2), (65500, 1), (65510, 2), (120831, 1), (262141, 2),
        (1114109, 1),
    ]
    if o in (0xE, 0xF):
        return 0
    for num, wid in widths:
        if o <= num:
            return wid
    return 1


def coloum_width(s: str) -> int:
    return sum(get_char_width(ord(ch)) for ch in s)


def change_column_width(s: str, limit: int) -> str:
    res = 0
    out = []
    for ch in s:
        res += get_char_width(ord(ch))
        if res <= limit:
            out.append(ch)
    return "".join(out)


class DrawText:
    """参考插件 core/image/tools.py 里的 DrawText"""

    def __init__(self, image: ImageDraw.ImageDraw, font: Path) -> None:
        self._img = image
        self._font = str(font)

    def draw(
        self,
        pos_x: int,
        pos_y: int,
        size: int,
        text: Any,
        color: Tuple[int, int, int, int] = (255, 255, 255, 255),
        anchor: str = "lt",
        stroke_width: int = 0,
        stroke_fill: Tuple[int, int, int, int] = (0, 0, 0, 0),
    ) -> None:
        font = ImageFont.truetype(self._font, size)
        self._img.text(
            (pos_x, pos_y), str(text), color, font, anchor,
            stroke_width=stroke_width, stroke_fill=stroke_fill,
        )


def dx_score(dx: float) -> int:
    """DX 分数百分比 -> 星星数（参考插件 utils/calc.py）"""
    if dx < 85:
        return 0
    if dx < 90:
        return 1
    if dx < 93:
        return 2
    if dx < 95:
        return 3
    if dx < 97:
        return 4
    return 5


def rating_tier(rating: int) -> str:
    num = "12"
    for limit, value in RA_TIER_STEPS:
        if rating < limit:
            num = value
            break
    return num


def rating_star_num(rating: int) -> Optional[int]:
    if rating < RA_STAR_THRESHOLDS[0]:
        return None
    index = 0
    for i, limit in enumerate(RA_STAR_THRESHOLDS):
        if rating >= limit:
            index = i
        else:
            break
    return RA_STAR_NUM[index]


@dataclass
class PlayedResult:
    song_id: int
    song_name: str
    level_index: int
    level: str
    level_value: float
    type: str
    rating: int
    achievements: float
    fc: str
    fs: str
    rate: str
    dx_score: int
    dx_max: int


def _open(path: Path, size: Optional[Tuple[int, int]] = None) -> Image.Image:
    img = Image.open(path).convert("RGBA")
    if size:
        img = img.resize(size)
    return img


def build_rating_block(rating: int) -> Image.Image:
    """新版 rating 区块：档位贴图 + 5 位数字 + (>=14000) 星级绶带"""
    plate = _open(THEME_DIR / f"UI_CMN_DXRating_{rating_tier(rating)}.png")
    star_num = rating_star_num(rating)
    star_img = _open(THEME_DIR / f"UI_CMN_DXRating_Star_0{star_num}.png") if star_num else None

    if star_img is not None:
        plate_size, star_size = (170, 35), (21, 35)
        num_x, num_y, num_gap, num_size = 80, 10, 13, (14, 17)
    else:
        plate_size, star_size = (186, 35), (0, 0)
        num_x, num_y, num_gap, num_size = 85, 8, 15, (17, 20)

    width = plate_size[0] + (star_size[0] - 15 if star_img is not None else 0)
    block = Image.new("RGBA", (width, plate_size[1]), (0, 0, 0, 0))
    block.alpha_composite(plate.resize(plate_size), (0, 0))
    for n, ch in enumerate(f"{min(rating, 99999):05d}"):
        digit = _open(PIC_DIR / f"UI_NUM_Drating_{ch}.png", num_size)
        block.alpha_composite(digit, (num_x + num_gap * n, num_y))
    if star_img is not None:
        block.alpha_composite(star_img.resize(star_size), (plate_size[0] - 15, 0))
    return block


def song_cover(song_id: int) -> Image.Image:
    """封面（本地有就用本地，没有则用占位图；缺图由 generate50 事先补下）"""
    from src.libraries.maimaidx_music import get_cover_len5_id

    path = COVER_DIR / f"{get_cover_len5_id(song_id)}.png"
    if path.exists():
        return _open(path, (75, 75))
    return _open(PIC_DIR / "UI_Icon_509506.png", (75, 75))


class Best50Image:
    """新版 B50 绘图（对应参考插件的 PlayerBest50）"""

    def __init__(self, player: Dict[str, Any], sd: List[PlayedResult], dx: List[PlayedResult], qq: Optional[str] = None):
        self.player = player
        self.sd = sd
        self.dx = dx
        self.qq = qq
        self.rating = int(player.get("rating") or 0)
        self.name = player.get("nickname") or player.get("username") or ""
        self.sd_total = sum(item.rating for item in sd)
        self.dx_total = sum(item.rating for item in dx)
        self.img = _open(THEME_DIR / "b50.png")
        self._title_bg = _open(THEME_DIR / "title.png")
        self._title_lengthen_bg = _open(THEME_DIR / "title_lengthen.png")
        self._dx_star_bg = [_open(PIC_DIR / f"UI_GAM_Gauge_DXScoreIcon_0{n}.png") for n in range(1, 6)]
        self._diff_bg = [
            _open(PIC_DIR / "b50_score_basic.png"),
            _open(PIC_DIR / "b50_score_advanced.png"),
            _open(PIC_DIR / "b50_score_expert.png"),
            _open(PIC_DIR / "b50_score_master.png"),
            _open(PIC_DIR / "b50_score_remaster.png"),
        ]
        draw = ImageDraw.Draw(self.img)
        self._sy = DrawText(draw, FONT_TITLE)
        self._tb = DrawText(draw, FONT_NUM)
        self._fot = DrawText(draw, FONT_FOT)

    # ---------- 单行 ----------
    def _row(self, info: PlayedResult, x: int, y: int) -> None:
        level_index = min(max(int(info.level_index), 0), 4)
        self.img.alpha_composite(self._diff_bg[level_index], (x, y))
        self.img.alpha_composite(song_cover(info.song_id), (x + 12, y + 12))
        self.img.alpha_composite(_open(PIC_DIR / f"{str(info.type).upper()}.png", (37, 14)), (x + 51, y + 91))

        rank = RANK_MAP.get(str(info.rate).lower(), "D")
        rank_path = THEME_DIR / f"UI_TTR_Rank_{rank}.png"
        if rank_path.exists():
            self.img.alpha_composite(_open(rank_path, (63, 28)), (x + 92, y + 78))

        if info.fc:
            combo = COMBO_MAP.get(str(info.fc).lower())
            combo_path = PIC_DIR / f"UI_MSS_MBase_Icon_{combo}.png"
            if combo and combo_path.exists():
                self.img.alpha_composite(_open(combo_path, (34, 34)), (x + 154, y + 77))
        if info.fs:
            sync = SYNC_MAP.get(str(info.fs).lower())
            sync_path = PIC_DIR / f"UI_MSS_MBase_Icon_{sync}.png"
            if sync and sync_path.exists():
                self.img.alpha_composite(_open(sync_path, (34, 34)), (x + 185, y + 77))

        if info.dx_max:
            stars = dx_score(info.dx_score / info.dx_max * 100)
            if stars:
                self.img.alpha_composite(self._dx_star_bg[stars - 1].resize((47, 26)), (x + 217, y + 80))
            self._tb.draw(x + 219, y + 65, 15, f"{info.dx_score}/{info.dx_max}",
                          DIFF_TEXT_COLOR[level_index], anchor="mm")

        self._tb.draw(x + 26, y + 98, 13, info.song_id, ID_TEXT_COLOR[level_index], anchor="mm")

        title = info.song_name
        if coloum_width(title) > 18:
            title = change_column_width(title, 17) + "..."
        self._sy.draw(x + 93, y + 14, 14, title, DIFF_TEXT_COLOR[level_index], anchor="lm")
        self._tb.draw(x + 93, y + 38, 30, f"{info.achievements:.4f}%", DIFF_TEXT_COLOR[level_index], anchor="lm")
        self._tb.draw(x + 93, y + 65, 15, f"{info.level_value} -> {info.rating}",
                      DIFF_TEXT_COLOR[level_index], anchor="lm")

    def _draw_list(self, data: List[PlayedResult], dx: bool = False) -> None:
        """对应参考插件的 whiledraw：5 列，行距 114，列距 276，起始 x=16"""
        gap, dx_step, start_x = 114, 276, 16
        initial_y = 1085 if dx else 235
        for num, info in enumerate(data):
            row, col = divmod(num, 5)
            self._row(info, start_x + col * dx_step, initial_y + row * gap)

    # ---------- 整图 ----------
    async def render(self) -> Image.Image:
        # logo
        self.img.alpha_composite(_open(THEME_DIR / "logo.png", (249, 120)), (14, 60))
        # 名牌底板
        plate_name = str(self.player.get("plate") or "")
        plate_path = PLATE_DIR / f"{plate_name}.png"
        if not plate_name or not plate_path.exists():
            plate_path = PIC_DIR / "UI_Plate_550101.png"
        self.img.alpha_composite(_open(plate_path, (800, 130)), (300, 60))
        # 头像：没有游戏头像就用 QQ 头像
        icon = None
        if self.qq:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(QQ_LOGO_URL, params={"b": "qq", "nk": self.qq, "s": 100},
                                           headers=HEADERS) as resp:
                        if resp.status == 200:
                            import io

                            icon = Image.open(io.BytesIO(await resp.read())).convert("RGBA").resize((120, 120))
            except Exception:
                icon = None
        if icon is None:
            icon = _open(PIC_DIR / "UI_Icon_509506.png", (120, 120))
        self.img.alpha_composite(icon, (305, 65))
        # 段位铭牌（水鱼 additional_rating）
        course_rank = int(self.player.get("additional_rating") or 0)
        if course_rank > 0:
            num = f"{course_rank if course_rank <= 10 else course_rank + 1:02d}"
            dani_path = PIC_DIR / f"UI_DNM_DaniPlate_{num}.png"
            if dani_path.exists():
                self.img.alpha_composite(_open(dani_path, (80, 32)), (625, 120))
        # rating 区块 + 星级
        self.img.alpha_composite(build_rating_block(self.rating), (435, 72))
        # 名字 + DX 图标
        self.img.alpha_composite(_open(PIC_DIR / "Name.png"), (435, 115))
        # 称号条（没有牌子数据就显示 B35+B15=rating）
        band = _open(PIC_DIR / "UI_CMN_Shougou_Rainbow.png", (270, 27))
        self.img.alpha_composite(band, (435, 160))
        self._tb.draw(570, 172, 14, f"B35: {self.sd_total} + B15: {self.dx_total} = {self.rating}",
                      (0, 0, 0, 255), anchor="mm")
        self._sy.draw(445, 135, 20, self.name, (0, 0, 0, 255), anchor="lm")
        source = "LXNS" if str(self.player.get("_source") or "").startswith("lxns") else "diving-fish"
        self._sy.draw(700, 1570, 22, f"Data from {source}. Generated by mai-bot", DEFAULT_TEXT_COLOR,
                      anchor="mm", stroke_width=5, stroke_fill=(255, 255, 255, 255))
        # 两个列表
        self._draw_list(self.sd, dx=False)
        self._draw_list(self.dx, dx=True)
        return self.img


def _to_result(item: Dict[str, Any], chart_notes: Dict[Tuple[int, int], int]) -> PlayedResult:
    song_id = int(item.get("song_id"))
    level_index = int(item.get("level_index") or 0)
    dx_max = chart_notes.get((song_id, level_index), 0)
    return PlayedResult(
        song_id=song_id,
        song_name=str(item.get("title") or ""),
        level_index=level_index,
        level=str(item.get("level") or ""),
        level_value=float(item.get("ds") or 0),
        type=str(item.get("type") or ""),
        rating=int(item.get("ra") or 0),
        achievements=float(item.get("achievements") or 0),
        fc=str(item.get("fc") or ""),
        fs=str(item.get("fs") or ""),
        rate=str(item.get("rate") or ""),
        dx_score=int(item.get("dxScore") or 0),
        dx_max=dx_max,
    )


async def _fetch_chart_notes(client: aiohttp.ClientSession) -> Dict[Tuple[int, int], int]:
    """从水鱼曲库算每张谱的 DX 分数上限（音符总数 × 3）"""
    notes: Dict[Tuple[int, int], int] = {}
    async with client.get(MUSIC_DATA_API, headers=HEADERS) as resp:
        if resp.status != 200:
            return notes
        data = await resp.json()
    for music in data:
        song_id = int(music["id"])
        for index, chart in enumerate(music.get("charts") or []):
            counts = chart.get("notes") or []
            try:
                notes[(song_id, index)] = int(sum(counts)) * 3
            except (TypeError, ValueError):
                continue
    return notes


async def _ensure_covers(client: aiohttp.ClientSession, song_ids: List[int]) -> None:
    """补齐缺失封面（落雪优先，水鱼兜底；client 参数保留兼容旧调用）"""
    from src.libraries.cover_source import ensure_covers

    await ensure_covers(song_ids)


async def generate50(
    payload: Dict,
    player_data: Optional[Dict] = None,
) -> Tuple[Optional[Image.Image], float]:
    """
    查询水鱼并画新版 B50（与旧版 generate50 同签名，方便直接替换）

    player_data 不为空时直接使用已经查询/归一化的数据（水鱼或落雪），不再请求水鱼。
    返回 (图片, 状态)：状态 0 成功、400 查无此人、403 隐私设置、其它为 HTTP 状态码；
    任何渲染异常都会退回 None + 状态码，由调用方决定是否回退旧引擎。
    """
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
        if player_data is None:
            async with client.post(B50_API, json=payload, headers=HEADERS) as resp:
                if resp.status != 200:
                    return None, resp.status
                data = await resp.json()
        else:
            data = player_data

        charts = data.get("charts") or {}
        sd_raw = charts.get("sd") or []
        dx_raw = charts.get("dx") or []
        notes = await _fetch_chart_notes(client)
        await _ensure_covers(client, [int(c.get("song_id") or 0) for c in sd_raw + dx_raw])

    sd = [_to_result(c, notes) for c in sd_raw]
    dx = [_to_result(c, notes) for c in dx_raw]
    qq = str(payload.get("qq")) if payload.get("qq") else None
    image = Best50Image(data, sd, dx, qq)
    return await image.render(), 0
