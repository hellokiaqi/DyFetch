# 抖音下载器

拿来下载抖音视频、图文、作者主页和合集的工具，命令行和桌面界面都能用。下载完可以按秒抽帧。

核心是 `downloader.py`，界面是 `ui.py`。

## 能下什么

- 单个视频、单个图文，以及带 `modal_id` 的作者主页链接（只下这一个作品）
- 作者主页批量下载：作品全下、点赞、合集
- 合集页、音乐页下的作品
- 短链接，`v.douyin.com` 那种

另外可以顺带存下背景音乐、封面、作者头像和原始 JSON，视频可以按秒抽帧到 `frames/`。

接口拿不到东西的时候会退到浏览器去取，所以公开作品即使登录过期也能下，代价是慢一些（每个作品六到八秒）。

## 安装

```bash
pip install -r requirements.txt
```

切帧默认用 `imageio-ffmpeg` 自带的 ffmpeg，不用另外装。如果系统里已经有 `ffmpeg`，也可以直接用它。

接口全挂的时候要靠浏览器兜底，需要 Playwright：

```bash
pip install playwright
playwright install chromium
```

`cut.py` 是单独的逐帧导出工具，用的是 OpenCV，需要额外装：

```bash
pip install opencv-python tqdm
```

## 先准备 Cookie

抖音的接口基本都要登录态。两种方式：

**自动取（推荐）**

```bash
python cookie_extractor.py
```

跑完会生成 `cookies.txt`、`cookies.pkl`、`storage_state.json`。后面程序会自己读，不用管。

**手工填**

在配置里直接写 `cookies:`，字符串或者字典都行。`config_simple.yml` 默认是 `cookies: auto`，表示先从本地文件读，读不到再开浏览器取。

## 用法

单个视频：

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/video/1234567890123456789"
```

整个主页：

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/MS4wLjABAAAA..."
```

主页链接里带 `modal_id` 或 `vid` 时只下那一个作品，不会整页翻。

下载自己的点赞/收藏：

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/self?...&showTab=like"
```

换个输出目录、或者给旧目录补切帧：

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/SEC_USER_ID" --path "./Downloaded/"
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/SEC_USER_ID" --slice-backfill
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `-c, --config` | 配置文件，默认 `config.yml`，没有就用 `config_simple.yml` |
| `-u, --url` | 一个或多个链接，可以一次性传多个 |
| `-p, --path` | 输出目录，覆盖配置里的 `path` |
| `--auto-cookie` | 用 Playwright 取 Cookie |
| `--cookie` | 直接把 Cookie 字符串传进来 |
| `--slice-backfill` / `--no-slice-backfill` | 运行前是否给旧目录补切帧 |
| `--refresh-session` | 刷新并保存登录态，不下载内容 |

图形界面：

```bash
python ui.py
```

![界面预览](docs/ui-preview.png)

界面上可以做这些事：链接贴在输入框回车就进队列，可以攒多个一起跑；日志按级别过滤、能搜关键字、能导出；「下载设置」里改抽帧帧率、数量上限、下载模式、并发和重试；右上角能切浅色深色主题。`Ctrl+Enter` 开始，`Esc` 停止。

界面上的设置会写进独立的 `ui_run_config.yml`，不会改你原来的配置文件。

## 输出结构

```
<path>/<作者昵称>/<发布时间>_<作品描述>_<作品ID>/
├── clip_1.mp4              图文作品的视频片段，或者视频作品的分段
├── image_1.jpg             图文作品的图片
├── <文件夹名>.mp4          单个视频作品
├── <文件夹名>_music.mp3    背景音乐
├── <文件夹名>_cover.jpg    封面
├── <文件夹名>_data.json    原始元数据
└── frames/                 切帧结果
    └── clip_01_000001.jpg
```

文件夹名带作品 ID，同一个作品重复下载不会串到别的目录里。已经存在的文件会跳过，可以放心重复跑。

## 配置

`config_simple.yml` 里常用的几项：

```yaml
link:                     # 链接列表，和 -u 二选一
  - https://www.douyin.com/user/SEC_USER_ID

path: ./Downloaded/       # 输出目录

music: true               # 存背景音乐
cover: true               # 存封面
avatar: false             # 存作者头像
json: true                # 存原始 JSON

slice_videos: true        # 是否切帧
slice_fps: 10             # 每秒抽几帧
slice_subdir: frames      # 帧目录名
slice_backfill_on_run: false   # 运行前给旧目录补切帧
auto_install_ffmpeg: true      # 找不到 ffmpeg 时自动装
# ffmpeg_bin: ffmpeg           # 也可以自己指定

mode:                     # 主页下什么
  - post                  # post / like / mix

number:                   # 数量上限，0 是不限
  post: 0
  like: 0

thread: 5                 # 并发
retry_times: 3            # 重试次数

cookies: auto             # auto / Cookie 字符串 / Cookie 字典

start_time: ""            # 时间过滤，格式 YYYY-MM-DD，含首尾两天
end_time: ""
```

浏览器兜底相关的几个开关：

```yaml
user_posts_browser_recovery: true    # 主页作品抓不全时用浏览器补
user_posts_browser_headless: false   # 兜底浏览器要不要无头

single_aweme_browser_recovery: true  # 单作品接口没数据时用浏览器取
single_aweme_browser_headless: true  # 单作品兜底默认无头，快
```

## 关于登录

登录过期之后接口会返回 200 但内容是空的，很容易被当成程序坏了。所以程序启动时会先看 `sid_guard`，过期会直接说：

```
⚠ Cookie 登录状态已于 2026-04-05 22:23 过期，请重新运行 cookie_extractor.py 登录
```

不想反复扫码的话，程序会帮你把登录态续下去：每次请求后都会把服务端下发的 `Set-Cookie`（新的 `sessionid`、`sid_guard` 之类）写回 `cookies.txt`、`cookies.pkl` 和 `storage_state.json`。抖音在有效期内会不断往后推到期时间，只要隔一段时间用一次，基本就不用再登。

启动时还会打印一行 `登录态有效期至 2026-11-18（剩余 30 天）`，界面右上角也一直显示着。想主动续一次：

```bash
python downloader.py -c config_simple.yml --refresh-session
```

或者点界面「下载设置 → 续期登录」。这个命令不下任何东西，只去浏览器里把最新登录态读出来写回文件。

自动续期失败之后六小时内不会再试（记在 `.session_refresh_state.json`），免得每次运行都白等半分钟。重新登录过的话可以用 `--refresh-session` 强制刷新。

有两种情况绕不过去：超过有效期一直没用，或者改了密码、在别的设备上退出登录、触发了风控。这些只能重新登录。

## 主页能下到什么

`mode: post` 会把主页里的作品都下下来，`number.post` 可以限数量（0 为不限）。

登录有效时走接口分页，每个作品不到一秒；登录过期时会打开浏览器一屏一屏地抓作品链接，再逐个打开作品页取详情，慢不少。实测一个 26 条作品的主页，未登录状态下下到 24 条，0 失败，大概四分钟。

点赞和合集仍然依赖接口，需要有效登录。

## 常见问题

**下了 0 个，或者接口一直 403**

先看启动时那行 Cookie 过期的提示。是过期的话跑一次 `python cookie_extractor.py`。不是过期的话，检查 `number.post` 是不是设成了 0 以外的值，以及目标内容是不是需要登录才能看。

**主页作品下不全**

程序会依次尝试：接口分页、用 `iteminfo` 补、按 `locate_item_id` 定位、最后开浏览器逐屏翻。前三种都要登录态，最后一种不用但慢。想稳一点就把 `user_posts_browser_headless` 设成 `false`，浏览器窗口看得见，遇到登录墙也能手动登。

**切帧没输出**

先确认 `ffmpeg -version` 能跑，或者在配置里写死 `ffmpeg_bin`。`auto_install_ffmpeg` 开着的话，缺 ffmpeg 时程序会试着装 `imageio-ffmpeg`。

**界面里中文是乱码**

界面和子进程都按 UTF-8 走。如果系统区域设置比较特殊，启动前 `set PYTHONUTF8=1` 再试。

## 项目结构

```
douyin-downloader/
├── downloader.py          主程序，下载逻辑都在这里
├── ui.py                  桌面界面
├── cut.py                 逐帧导出工具（OpenCV，可选）
├── cookie_extractor.py    取 Cookie
├── get_cookies_manual.py  手工取 Cookie 的辅助脚本
├── DouYinCommand.py       旧版命令行封装，留着兼容
├── config_simple.yml      日常用的配置
├── config.yml             完整配置样例
├── config.example.yml     配置模板
├── apiproxy/              接口封装和请求策略
├── utils/                 日志等公共工具
├── tests/                 回归脚本
└── dy-downloader/         早期模块化版本，留着参考
```

`dy-downloader/` 是早先拆成模块的那一版，功能没有现在的 `downloader.py` 全，依赖也不太一样，只作参考。

## 测试

主程序的回归脚本，跑在临时目录里，不会动你的数据和 Cookie：

```bash
python tests/regress_downloader.py            # 离线，18 项
python tests/regress_downloader.py --network  # 额外验证真实接口
```

覆盖链接识别、ID 提取、文件命名、时间过滤、增量下载、数据库、切帧和补切帧，以及 Cookie 自动续期。

`dy-downloader/` 自带 pytest 用例，需要单独装依赖，而且必须在它自己的目录下跑，否则根目录的 `utils/` 会挡住同名的包：

```bash
cd dy-downloader
pip install -r requirements.txt
python -m pytest tests -q
```

## 运行产物

下面这些是跑起来才会有的，删掉也能重新生成，都已经在 `.gitignore` 里：

```
cookies.txt / cookies.pkl / storage_state.json     登录态
downloader.log / logs/ / ui_startup.log            日志
data.db                                            增量下载用的数据库
Downloaded/ / my_videos/ / my_videos_fix/          下载内容
ui_history.json / ui_state.json / ui_run_config.yml 界面状态
.session_refresh_state.json                        自动续期的尝试记录
```
