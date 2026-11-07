# mai-bot-next

基于 NoneBot2 + OneBot v11 的 QQ 舞萌 DX 机器人，使用 NapCatQQ 登录 QQ。



## 功能

- `b50`、查歌、查谱、定数查歌、分数线
- 水鱼 / 落雪双数据源查询，自动回退
- `推荐`、`上分` 模拟、`底力分析` 五轴雷达图
- `开字母` 猜歌小游戏，默认排除宴会场
- 机厅人数上报 / 查询（nearcade）
- 华立服务器状态查询、状态缓存
- 订阅服务器状态、新曲、天气
- 入群欢迎、退群提醒、bilibili 视频解析

## 环境要求

- Python 3.10+
- Windows / Linux
- NapCatQQ
- 可选：水鱼 / 落雪 OAuth、落雪开发者 Token、nearcade Token

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>
```

### 2. 准备静态资源

本仓库不附带以下资源：

```text
src/static/
data/aliases.json
```

请自行准备有权使用的静态资源，放到 `src/static`；把 `aliases.json` 放到 `data/`。缺少字体或曲绘时，B50、底力分析等出图功能会失败。

### 静态资源下载（YuchanBot）

资源来自 [YuchanBot / maimaiDX](https://github.com/Yuri-YuzuChaN/maimaiDX)，下载后解压，将 `static` 目录里的内容合并到本项目的 `src/static/`。

完整资源包（CN1.55）：

- [Cloudreve](https://cloud.yuzuchan.moe/f/34s7/Resource%20CN1.55.7z)
- [OneDrive](https://yuzuai-my.sharepoint.com/:u:/g/personal/yuzu_yuzuchan_moe/IQBGKHie6MAaTZy3rME7Q-ruAVKgXDCKROqz5e25KtMeeVY?e=53eC6a)
- [OpenList](https://share.yuzuchan.moe/d/downloads/Resource%20CN1.55.7z?sign=4wMRn_9n6YZiEVV2vELKCEOj9zsgxScnmgtjsEL3C6g=:0)

3.1.5 增量更新（新曲绘、镜/廻牌等）：

- [Cloudreve](https://cloud.yuzuchan.moe/f/x3f3/Resource%203.1.5%20UPDATE.7z)
- [OneDrive](https://yuzuai-my.sharepoint.com/:u:/g/personal/yuzu_yuzuchan_moe/IQBfiyOSmOXEQ6BU4NFfZdxmATlmua1cd1zGUlW3txCwTy8?e=uwrb3h)
- [OpenList](https://share.yuzuchan.moe/d/downloads/Resource%203.1.5%20UPDATE.7z?sign=lpaSBMx8zCkfIYcyWWVVPu5ozDfs45_1-RMLVeAynH4=:0)

如果资源包内带有 `aliases.json`，请同时放到本项目的 `data/aliases.json`。

> 资源版权归 YuchanBot 原作者及 SEGA 所有，仅供学习交流，请勿用于商业用途。

### 3. 安装依赖

Windows：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. 创建配置

```powershell
copy .env.example .env
copy arcade.example.json arcade.json
```

Linux：

```bash
cp .env.example .env
cp arcade.example.json arcade.json
```

编辑 `.env`，把所有 `<你的...>` 占位符换成自己的值。

至少需要：

```dotenv
PORT=10219
ONEBOT_ACCESS_TOKEN=<你的NapCat访问令牌>
```

可选配置：

```dotenv
# 官谱目录，DXTag / 底力分析需要
MAIDATA_DIR=<你的官谱目录>

# 机厅人数上报
NEARCADE_TOKEN=<你的nearcadeToken>

# 水鱼 OAuth
DIVINGFISH_CLIENT_ID=<你的水鱼ClientID>
DIVINGFISH_CLIENT_SECRET=<你的水鱼ClientSecret>

# 落雪 OAuth
LX_CLIENT_ID=<你的落雪ClientID>
LX_CLIENT_SECRET=<你的落雪ClientSecret>
LX_REDIRECT_URI=http://127.0.0.1/callback

# 落雪开发者查询回退
LXNS_DEVELOPER_TOKEN=<你的落雪开发者Token>
```

### 5. 安装并连接 NapCatQQ

1. 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载对应系统的版本。
2. 启动 NapCatQQ，用手机 QQ 扫码登录 bot 账号。
3. 打开「网络配置」，新建一个 **WebSocket 客户端**：

```text
URL:      ws://127.0.0.1:10219/onebot/v11/ws
Token:    <你的NapCat访问令牌>
消息格式: array
```

4. 确认 `.env` 里的 `ONEBOT_ACCESS_TOKEN` 和 NapCat 里的 Token 一致。
5. 启动 bot：

```bash
python bot.py
```

看到类似日志即连接成功：

```text
[INFO] nonebot | WebSocket Connection from NapCat Accepted!
```

> 不要把 OneBot 端口直接暴露到公网。远程连接请使用防火墙、VPN 或反向代理。

## 首次配置

### 欢迎图片

在 D 盘准备一张欢迎图片：

```text
D:/data/welcome.jpg
```

所有群统一使用这张图，不再对特定群做单独判断。路径可以在 `.env` 里覆盖：

```dotenv
WELCOME_IMAGE=D:/data/welcome.jpg
```

### 机厅人数

编辑 `arcade.json`：

```json
{
  "arcades": [
    {
      "group_id": "<你的群号>",
      "name": "<nearcade机厅全名>",
      "alias": "m"
    }
  ]
}
```

- `name` 必须和 nearcade 的机厅全名一致，可在 https://nearcade.cn 搜索
- 上报需要 `NEARCADE_TOKEN`
- 查询不需要 Token

用法：

```text
m1          上报当前 1 人（0 表示没人了，最大 50）
m1j / m1几  查询当前人数和最近 3 小时的上报
```

### 服务器状态

```text
status / 服务器状态 / 状态 / mai状态
状态缓存 / 缓存状态
status帮助 / 状态帮助
```

数据来源：`https://mai.chongxi.us/api/bot`。

状态图会显示：

- 总判定：正常 / 性能恢复中 / 性能下降 / 服务宕机 / 维护中 / 无数据
- 六项服务：NET、会员、标题、公众号(NET)、公众号(二维码)、计费
- 当前延迟、服务器负载、延迟波动
- 近 1 小时异常 / 正常上报
- 数据版本和数据时间

当前接口数据版本为 **v27.0**，插件已兼容 `verdict`、`verdict_text`、`services`、`latency`、`reports` 等字段。出图失败会自动回退文字。

## 常用命令

| 命令 | 功能 |
| --- | --- |
| `帮助` | 查看命令帮助 |
| `b50 [@某人/用户名]` | 查询 B50 |
| `推荐 15000 鸟加` | 推荐推分曲目 |
| `上分 15000` | 上分模拟 |
| `底力分析 [@某人/用户名]` | 五轴底力雷达图 |
| `开字母` / `开x` / `开歌` / `不玩了` | 开字母猜歌 |
| `dfbind` / `lxbind` | 绑定水鱼 / 落雪 |
| `数据源 自动/水鱼/落雪` | 切换首选数据源 |
| `订阅 状态/新曲/天气 杭州` | 添加主动推送 |
| `status` | 查询服务器状态 |

## 安全与隐私

- 不要提交 `.env`
- 不要提交 `data/accounts.json`、`data/subscriptions.json`、`data/guess_scores.json`
- 不要提交 `arcade.json`、`src/static`、`video_assets`
- 如果 Token 泄露，立即在对应平台重置
- 本项目不附带任何个人 API Key、Auth Key 或账号数据

## 素材与许可证

- 代码许可证见 [LICENSE](LICENSE)（MIT）
- 舞萌 DX 游戏素材、歌曲曲绘、字体、BGM 版权归各自权利人所有


## 常见问题

**启动时报缺少字体 / 曲绘 / aliases.json？**

说明 `src/static` 或 `data/aliases.json` 没有准备好。把静态资源放到 `src/static`，把别名库放到 `data/`。

**NapCat 连接不上？**

检查：

- NapCat 里配置的是 WebSocket 客户端
- URL 是 `ws://127.0.0.1:10219/onebot/v11/ws`
- Token 与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致
- bot 已经先启动，端口没有被占用

**状态图或 B50 图发不出来？**

会回退文字。检查静态资源、字体和 `.env` 配置。
