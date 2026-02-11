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

## 📋 版本对比

| 功能 | V1.0 (DouYinCommand.py) | V2.0 (downloader.py) |
|------|------------------------|---------------------|
| 单个视频下载 | ✅ 完全正常 | ✅ 可用（已增强重试与兜底） |
| 用户主页下载 | ✅ 正常 | ✅ 完全正常 |
| Cookie 管理 | 手动配置 | 自动获取 |
| Cookie 兜底 | ❌ | ✅ 自动合并 config/cookies.txt/cookies.pkl |
| 图文 403 处理 | 一般 | ✅ 多 URL 自动回退 |
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
