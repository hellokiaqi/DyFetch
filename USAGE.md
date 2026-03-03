# 抖音下载器使用说明

## 🚀 快速开始

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置 Cookie（首次使用需要）
```bash
# 自动获取（推荐）
python cookie_extractor.py

# 或手动获取
python get_cookies_manual.py
```

### 3. 开始下载

#### V1.0 稳定版（推荐用于单个视频）
```bash
# 编辑 config.yml 配置文件
# 然后运行
python DouYinCommand.py
```

#### V2.0 增强版（推荐用于用户主页）
```bash
# 下载用户主页
python downloader.py -u "https://www.douyin.com/user/xxxxx"

# 自动获取 Cookie 并下载
python downloader.py --auto-cookie -u "https://www.douyin.com/user/xxxxx"

# 全量下载并指定输出目录（请确保 number.post=0）
python downloader.py -c config_simple.yml -u "https://www.douyin.com/user/xxxxx" --path "./my_videos/"
```

### 4. 用简易 UI 运行（省事）

```bash
python downloader_ui.py
```

- 支持一键运行 `downloader.py`，不需要手敲整条命令
- 自动记录 URL、配置路径、输出路径
- 自动提取并保存 `user_id`，后续可直接选择复用
- `Username` 为手动维护：在 `Manual Username` 输入昵称后，点 `Set Name` 写入选中记录
- `Refresh` 仅刷新本地记录，不再自动请求昵称，避免接口重试刷屏
- `Saved Records` 支持横向滚动，长 URL 可左右拖动查看

### 图文/片段作品输出规则（V2.1）

- `clip_<index>.mp4`：图文作品中的视频片段下载成功时输出
- `image_<index>.jpg`：纯图片，或片段 URL 全部失败后的回退结果
- 下载器会优先尝试非水印地址；如果源站未提供无水印地址，回退结果可能带水印

## 📋 版本对比

| 功能 | V1.0 (DouYinCommand.py) | V2.0 (downloader.py) |
|------|------------------------|---------------------|
| 单个视频下载 | ✅ 完全正常 | ✅ 可用（已增强重试与兜底） |
| 用户主页下载 | ✅ 正常 | ✅ 完全正常 |
| Cookie 管理 | 手动配置 | 自动获取 |
| Cookie 兜底 | ❌ | ✅ 自动合并 config/cookies.txt/cookies.pkl |
| 图文 403 处理 | 一般 | ✅ 多 URL 自动回退 |
| 图文片段视频 | ❌ | ✅ 优先下载 `clip_<index>.mp4`，失败回退 `image_<index>.jpg` |
| 水印控制策略 | 一般 | ✅ 优先非水印地址，必要时回退可用地址 |
| 使用复杂度 | 简单 | 中等 |
| 稳定性 | 高 | 中等 |

## 🎯 推荐使用场景

- **下载单个视频**：使用 V1.0
- **下载用户主页**：使用 V2.0
- **批量下载**：使用 V2.0
- **学习研究**：两个版本都可以

## 📞 获取帮助

- 查看详细文档：`README.md`
- 报告问题：[GitHub Issues](https://github.com/jiji262/douyin-downloader/issues)

## 🔁 失败补下载

如果出现“已找到全部作品，但少量失败（例如 11 个）”，按这个流程处理：

1. 在 `config_simple.yml` 中设置 `number.post: 0`（全量）。
2. 使用同一条命令再次运行，保持同一个 `--path`。
3. 程序会自动跳过已下载文件，只补失败项。
4. 若仍有失败，查看 `downloader.log` 中对应状态码（常见为 403），再重跑一次通常还能补齐一部分。
5. 若某些图文作品目录下只有 `image_<index>.jpg` 没有 `clip_<index>.mp4`，通常表示该片段未返回可用视频地址或被源站限制。
