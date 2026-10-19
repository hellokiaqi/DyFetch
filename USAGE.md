# 使用指南

本文档给出从零开始的最短操作路径。完整说明见 [README.md](README.md)。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

需要浏览器兜底时再安装 Playwright：

```bash
pip install playwright
playwright install chromium
```

### 2. 获取 Cookie

```bash
python cookie_extractor.py
```

按提示完成登录后，会生成 `cookies.txt`、`cookies.pkl`、`storage_state.json`。

### 3. 开始下载

命令行方式：

```bash
python downloader.py -c config_simple.yml -u "作品或主页链接"
```

图形界面方式：

```bash
python ui.py
```

## 常用命令

### 单个视频 / 图文

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/video/1234567890123456789"
python downloader.py -c config_simple.yml -u "https://www.douyin.com/note/1234567890123456789"
```

### 主页链接中只下载某一个作品

链接里带 `modal_id` / `vid` 时，只会下载该作品：

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/MS4wLjABAAAAeEVIdxtFC-y5UG_8coku6gHwWJ4itLMnGtYNULV4VZ8?from_tab_name=main&modal_id=7628190562495340004&vid=7628190562495340004"
```

### 我的主页（点赞 / 收藏）

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7628190562495340004&showTab=like"
```

### 整页批量下载到指定目录

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/SEC_USER_ID" --path "./Downloaded/"
```

### 为历史视频补切帧

```bash
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/SEC_USER_ID" --slice-backfill
```

## `config_simple.yml` 示例

```yaml
link:
  - https://www.douyin.com/user/SEC_USER_ID

path: ./Downloaded/

music: true
cover: true
avatar: false
json: true

slice_videos: true
slice_fps: 10
slice_subdir: frames
slice_backfill_on_run: false
auto_install_ffmpeg: true

mode:
  - post

number:
  post: 0
  like: 0

thread: 5
retry_times: 3

cookies: auto
# 也可以直接写 Cookie 字符串或字典
```

## 图形界面说明

`ui.py` 顶部为下载参数区，中部左侧是任务记录、右侧是实时日志。

- 链接输入框：粘贴作品或主页链接
- 配置文件：下拉选择项目内的 `*.yml`
- 保存目录：可浏览选择，"打开"直接定位到该目录
- 开始下载 / 停止：运行期间开始按钮禁用，可随时停止
- 任务记录：双击载入，右键可命名、打开目录或删除，"刷新"同步磁盘文件
- 日志：按等级着色，可清空或另存为 `.log` 文件
- 底部：实时状态、进度条与成功/失败/跳过/耗时统计

快捷键：`Ctrl+Enter` 开始下载，`Esc` 停止当前任务。

## 减少重复登录

登录过期后接口只会返回空数据，但本项目会自动延续登录态：

- **自动续期**：每次请求后把服务端下发的 `Set-Cookie` 写回
  `cookies.txt` / `cookies.pkl` / `storage_state.json`，抖音会在有效期内滚动延长会话，
  只要定期使用就不必重新扫码。
- **查看有效期**：启动日志会打印 `登录态有效期至 YYYY-MM-DD（剩余 N 天）`，
  图形界面右上角也会常驻显示；过期会标黄。
- **手动续期**（不下载任何内容）：

```bash
python downloader.py -c config_simple.yml --refresh-session
```

或在界面「下载设置 → 续期登录」点一下。

### 主页批量下载

mode: post 时会把主页里的作品全部下载；
umber.post 可限制数量（0 = 不限）。
即使登录过期，程序也会自动打开浏览器逐屏抓取作品链接逐个下载，
只是比接口方式慢一些（接口方式每个作品不到 1 秒，浏览器方式约 6~8 秒）。

建议每 2~3 周续期一次。若超过有效期一直没用，或修改过密码/在其他设备退出登录，
仍需重新运行 `python cookie_extractor.py`。

## 故障排查

### 返回空数据

依次检查：

1. Cookie 是否过期
2. 目标内容是否需要登录
3. 是否需要开启浏览器兜底

### 主页作品抓不全

程序会自动依次尝试：API 分页 → `iteminfo` 兜底 → `locate_item_id` → Playwright。
仍不完整时，可开启 `user_posts_browser_recovery` 并把
`user_posts_browser_headless` 设为 `false`，观察浏览器行为。

### 切帧没有输出

确认 `ffmpeg` 可用，或在配置中显式指定 `ffmpeg_bin`。

## 运行产物

以下文件由程序在运行时生成，可安全删除后重新生成：

- `cookies.txt`
- `cookies.pkl`
- `storage_state.json`
- `downloader.log`
- `logs/`
- `data.db`
- `Downloaded/`
- `my_videos/`
- `my_videos_fix/`
- `ui_history.json`
- `ui_state.json`
