#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
抖音下载器 - 统一增强版
支持视频、图文、作者主页、合集等多种链接的批量下载
"""

import asyncio
import contextlib
import json
import logging
import io
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set
from urllib.parse import urlparse, quote
import argparse
import importlib
import yaml

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 第三方库
try:
    import aiohttp
    import requests
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn
    from rich.table import Table
    from rich.panel import Panel
    from rich.live import Live
    from rich import print as rprint
except ImportError as e:
    print(f"请安装必要的依赖: pip install aiohttp requests rich pyyaml")
    sys.exit(1)

# 本地模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 项目内部模块
from apiproxy.douyin import douyin_headers
from apiproxy.douyin.urls import Urls
from apiproxy.douyin.result import Result
from apiproxy.common.utils import Utils
from apiproxy.douyin.auth.cookie_manager import AutoCookieManager
from apiproxy.douyin.database import DataBase

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('downloader.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Rich console
console = Console()


class ContentType:
    """内容类型枚举"""
    VIDEO = "video"
    IMAGE = "image" 
    USER = "user"
    MIX = "mix"
    MUSIC = "music"
    LIVE = "live"


class DownloadStats:
    """下载统计"""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = time.time()
    
    @property
    def success_rate(self):
        # 主页不限数量时 total 只是预估值，可能远小于实际下载数，
        # 这里以「实际处理过的条数」为分母，保证成功率不会出现 2400% 这种值
        processed = max(self.total, self.success + self.failed + self.skipped)
        return (self.success / processed * 100) if processed > 0 else 0
    
    @property
    def elapsed_time(self):
        return time.time() - self.start_time
    
    def to_dict(self):
        return {
            'total': self.total,
            'success': self.success,
            'failed': self.failed,
            'skipped': self.skipped,
            'success_rate': f"{self.success_rate:.1f}%",
            'elapsed_time': f"{self.elapsed_time:.1f}s"
        }


class RateLimiter:
    """速率限制器"""
    def __init__(self, max_per_second: float = 2):
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second
        self.last_request = 0
    
    async def acquire(self):
        """获取一个执行许可（按配置速率等待）"""
        current = time.time()
        time_since_last = current - self.last_request
        if time_since_last < self.min_interval:
            await asyncio.sleep(self.min_interval - time_since_last)
        self.last_request = time.time()


class RetryManager:
    """重试管理器"""
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self.retry_delays = [1, 2, 5]  # 重试间隔（秒）
    
    async def execute_with_retry(self, func, *args, **kwargs):
        """执行函数，失败时自动重试"""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                    logger.warning(f"第 {attempt + 1} 次尝试失败: {e}, {delay} 秒后重试...")
                    await asyncio.sleep(delay)
        raise last_error


class UnifiedDownloader:
    """统一下载器"""
    
    def __init__(self, config_path: str = "config.yml"):
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.urls_helper = Urls()
        self.result_helper = Result()
        self.utils = Utils()
        
        # 统计信息
        self.stats = DownloadStats()
        self.rate_limiter = RateLimiter(max_per_second=2)
        self.retry_manager = RetryManager(max_retries=self.config.get('retry_times', 3))
        
        # Cookie 初始化（支持自动获取与本地文件回退）
        self.cookies = self.config.get('cookies') if 'cookies' in self.config else self.config.get('cookie')
        self.auto_cookie = bool(self.config.get('auto_cookie')) or (isinstance(self.config.get('cookie'), str) and self.config.get('cookie') == 'auto') or (isinstance(self.config.get('cookies'), str) and self.config.get('cookies') == 'auto')
        self.headers = {**douyin_headers}
        # 禁用 brotli 压缩：为兼容 aiohttp，请求头不声明 br
        self.headers['accept-encoding'] = 'gzip, deflate'
        # 增量下载与数据库
        self.increase_cfg: Dict[str, Any] = self.config.get('increase', {}) or {}
        self.enable_database: bool = bool(self.config.get('database', True))
        self.db: Optional[DataBase] = DataBase() if self.enable_database else None
        
        # 保存路径
        self.save_path = Path(self.config.get('path', './Downloaded'))
        self.save_path.mkdir(parents=True, exist_ok=True)
        
        # 视频切帧配置（兼容旧键 slice_clips）
        slice_videos_cfg = self.config.get('slice_videos')
        if slice_videos_cfg is None:
            slice_videos_cfg = self.config.get('slice_clips', True)
        self.slice_videos = bool(slice_videos_cfg)
        self.slice_fps = int(self.config.get('slice_fps', 10) or 10)
        if self.slice_fps <= 0:
            self.slice_fps = 10
        self.slice_subdir = self._safe_name(
            self.config.get('slice_subdir', 'frames'),
            default='frames',
            max_len=40,
        )
        self.slice_backfill_on_run = bool(self.config.get('slice_backfill_on_run', False))
        self.ffmpeg_bin = str(self.config.get('ffmpeg_bin', 'ffmpeg') or 'ffmpeg')
        self.auto_install_ffmpeg = bool(self.config.get('auto_install_ffmpeg', True))
        self.auto_install_playwright = bool(self.config.get('auto_install_playwright', True))
        self._ffmpeg_checked = False
        self._ffmpeg_path: Optional[str] = None
        self._cookies_expired = False
        self._last_cookie_write = 0.0
        self._cookie_write_pending = False
        self.login_expiry_at: Optional[float] = None
        self._imageio_ffmpeg_checked = False
        
    def _load_config(self, config_path: str) -> Dict:
        """加载配置文件"""
        if not os.path.exists(config_path):
            # 配置文件缺失时回退到 config.yml / config_simple.yml
            alt_path = 'config_simple.yml'
            if os.path.exists(alt_path):
                config_path = alt_path
            else:
                # 两者都不存在时返回空字典，由调用方使用默认值
                return {}
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 兼容旧键名：links/link、output_dir/path、cookie/cookies
        if 'links' in config and 'link' not in config:
            config['link'] = config['links']
        if 'output_dir' in config and 'path' not in config:
            config['path'] = config['output_dir']
        if 'cookie' in config and 'cookies' not in config:
            config['cookies'] = config['cookie']
        if isinstance(config.get('cookies'), str) and config.get('cookies') == 'auto':
            config['auto_cookie'] = True
        
        # 链接只支持列表形式，字符串会在 run() 中统一转换
        # 若两者都没有，后续会在运行时提示
        
        return config
    
    def _parse_cookie_string(self, cookie_str: str) -> Dict[str, str]:
        """Parse cookie header string into a key/value dictionary."""
        cookie_map: Dict[str, str] = {}
        if not cookie_str:
            return cookie_map
        for item in cookie_str.split(';'):
            item = item.strip()
            if not item or '=' not in item:
                continue
            key, value = item.split('=', 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            cookie_map[key] = value
        return cookie_map

    def _cookies_to_dict(self, cookies: Any) -> Dict[str, str]:
        """Normalize cookies from dict/list/string into a dictionary."""
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if k and v is not None}
        if isinstance(cookies, list):
            normalized: Dict[str, str] = {}
            for item in cookies:
                try:
                    name = str(item.get('name', '')).strip()
                    value = item.get('value')
                    if name and value is not None:
                        normalized[name] = str(value)
                except Exception:
                    continue
            return normalized
        if isinstance(cookies, str):
            return self._parse_cookie_string(cookies)
        return {}

    def _load_cookie_text_file(self, file_path: str) -> Dict[str, str]:
        """Load cookie string from a local text file."""
        if not os.path.exists(file_path):
            return {}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw = f.read().strip()
        except Exception:
            try:
                with open(file_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                    raw = f.read().strip()
            except Exception:
                return {}
        return self._parse_cookie_string(raw)

    def _load_cookie_pickle(self, file_path: str) -> Dict[str, str]:
        """Load cookie data from legacy cookie manager pickle file."""
        if not os.path.exists(file_path):
            return {}
        try:
            with open(file_path, 'rb') as f:
                data = pickle.load(f)
            if isinstance(data, dict) and 'cookies' in data:
                return self._cookies_to_dict(data.get('cookies'))
            return self._cookies_to_dict(data)
        except Exception:
            return {}

    def _build_cookie_string(self) -> str:
        """Build Cookie header string."""
        cookie_map = self._cookies_to_dict(self.cookies)
        return '; '.join([f'{k}={v}' for k, v in cookie_map.items() if k and v is not None])

    @staticmethod
    def _cookie_expiry_at(cookie_map: Dict[str, str]) -> Optional[float]:
        """从 sid_guard 解析登录态到期时间戳（无法解析返回 None）。

        sid_guard 的格式为 <sessionid>|<签发时间戳>|<有效期秒数>|<其他>。
        """
        raw = (cookie_map or {}).get('sid_guard') or ''
        if not raw:
            return None
        try:
            from urllib.parse import unquote
            parts = unquote(str(raw)).split('|')
            if len(parts) < 3:
                return None
            return float(int(parts[1]) + int(parts[2]))
        except Exception:
            return None

    @classmethod
    def _cookie_expiry_hint(cls, cookie_map: Dict[str, str]) -> Optional[str]:
        """登录态已过期时返回中文提示，未过期返回 None。"""
        expire_at = cls._cookie_expiry_at(cookie_map)
        if expire_at is None or expire_at > time.time():
            return None
        expired = datetime.fromtimestamp(expire_at).strftime('%Y-%m-%d %H:%M')
        return f"Cookie 登录状态已于 {expired} 过期，请重新运行 cookie_extractor.py 登录"

    # ---------------------------------------------------------------- Cookie 自动续期

    AUTO_REFRESH_COOLDOWN_HOURS = 6.0
    AUTO_REFRESH_STATE = '.session_refresh_state.json'

    def _skip_auto_refresh_reason(self) -> Optional[float]:
        """若最近一次自动刷新失败且在冷却期内，返回已过去的小时数。"""
        try:
            path = Path(self.AUTO_REFRESH_STATE)
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding='utf-8')) or {}
            if data.get('success'):
                return None
            elapsed = (time.time() - float(data.get('at') or 0)) / 3600.0
            if 0 <= elapsed < self.AUTO_REFRESH_COOLDOWN_HOURS:
                return elapsed
        except Exception:
            return None
        return None

    def _remember_auto_refresh(self, success: bool) -> None:
        try:
            Path(self.AUTO_REFRESH_STATE).write_text(
                json.dumps({'at': time.time(), 'success': bool(success)}), encoding='utf-8'
            )
        except Exception:
            pass


    def _cookie_persist_paths(self) -> Dict[str, Path]:
        return {
            'txt': Path('cookies.txt'),
            'pkl': Path('cookies.pkl'),
            'state': Path('storage_state.json'),
        }

    def _absorb_response_cookies(self, response) -> None:
        """把服务端 Set-Cookie 合并进本地 Cookie，实现登录态自动续期。

        抖音在登录有效期内会在响应里滚动下发新的 sessionid / sid_guard，
        以前这些 Cookie 被直接丢弃，导致会话到期后只能重新扫码登录。
        """
        try:
            jar = getattr(response, 'cookies', None)
            if not jar:
                return
            current = self._cookies_to_dict(self.cookies)
            changed: Dict[str, str] = {}
            for name, morsel in jar.items():
                value = getattr(morsel, 'value', morsel)
                if not name or value is None or value == '':
                    continue
                if str(current.get(name)) != str(value):
                    changed[str(name)] = str(value)
            if not changed:
                return
            current.update(changed)
            self.cookies = current
            self.headers['Cookie'] = self._build_cookie_string()
            logger.info(f"收到服务端续期 Cookie: {', '.join(sorted(changed)[:6])}")
            self._persist_cookies(current)
        except Exception as exc:
            logger.debug("absorb response cookies failed: %s", exc)

    def _persist_cookies(self, cookie_map: Dict[str, str]) -> None:
        """把最新 Cookie 写回 cookies.txt / cookies.pkl / storage_state.json。"""
        now = time.time()
        if now - self._last_cookie_write < 5:
            self._cookie_write_pending = True
            return
        self._last_cookie_write = now
        self._cookie_write_pending = False
        paths = self._cookie_persist_paths()

        cookie_str = '; '.join(f'{k}={v}' for k, v in cookie_map.items() if k and v is not None)
        try:
            paths['txt'].write_text(cookie_str, encoding='utf-8')
        except Exception as exc:
            logger.warning(f"写入 cookies.txt 失败: {exc}")

        try:
            existing: Dict[str, Any] = {}
            if paths['pkl'].exists():
                with open(paths['pkl'], 'rb') as fh:
                    existing = pickle.load(fh) or {}
                if not isinstance(existing, dict):
                    existing = {}
            records: List[Dict[str, Any]] = []
            seen = set()
            for item in (existing.get('cookies') or []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name', ''))
                if name in cookie_map:
                    item = dict(item)
                    item['value'] = cookie_map[name]
                if name:
                    seen.add(name)
                records.append(item)
            for name, value in cookie_map.items():
                if name in seen:
                    continue
                records.append({
                    'name': name, 'value': value, 'domain': '.douyin.com', 'path': '/',
                    'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'None',
                })
            payload = {
                'cookies': records,
                'created_at': now,
                'last_used': now,
                'login_method': existing.get('login_method') or 'refresh',
                'is_valid': True,
            }
            with open(paths['pkl'], 'wb') as fh:
                pickle.dump(payload, fh)
        except Exception as exc:
            logger.warning(f"写入 cookies.pkl 失败: {exc}")

        # storage_state.json 供浏览器兜底复用：只更新已有条目的值，保留 origins
        try:
            if paths['state'].exists():
                state = json.loads(paths['state'].read_text(encoding='utf-8'))
                if isinstance(state, dict) and isinstance(state.get('cookies'), list):
                    touched = 0
                    for item in state['cookies']:
                        name = str(item.get('name', ''))
                        if name in cookie_map and item.get('value') != cookie_map[name]:
                            item['value'] = cookie_map[name]
                            touched += 1
                    if touched:
                        paths['state'].write_text(
                            json.dumps(state, ensure_ascii=False), encoding='utf-8'
                        )
        except Exception as exc:
            logger.debug("update storage_state failed: %s", exc)

        self.login_expiry_at = self._cookie_expiry_at(cookie_map)
        if self.login_expiry_at:
            days = (self.login_expiry_at - now) / 86400
            when = datetime.fromtimestamp(self.login_expiry_at).strftime('%Y-%m-%d')
            logger.info(f"Cookie 已回写，登录态有效期至 {when}（剩余 {days:.0f} 天）")

    def _report_session_validity(self, cookie_map: Dict[str, str]) -> None:
        """打印登录态有效期，方便判断距离下次登录还有多久。"""
        expire_at = self._cookie_expiry_at(cookie_map)
        self.login_expiry_at = expire_at
        if not expire_at:
            return
        days = (expire_at - time.time()) / 86400
        when = datetime.fromtimestamp(expire_at).strftime('%Y-%m-%d')
        if days >= 0:
            console.print(f"[green]登录态有效期至 {when}（剩余 {days:.0f} 天）[/green]")
        else:
            console.print(f"[yellow]登录态已于 {when} 过期（{abs(days):.0f} 天前）[/yellow]")

    async def _fetch_cookies_via_browser(self, force: bool = True, headless: bool = False) -> Dict[str, str]:
        """用 Playwright 打开浏览器读取当前登录态 Cookie（失败返回空字典）。

        force=True 时强制重新访问抖音，即使 cookies.pkl 看起来"很新"，
        这样浏览器里已经滚新的会话才能被读回来。
        """
        try:
            async with AutoCookieManager(cookie_file='cookies.pkl', headless=headless) as cm:
                if force and not cm._need_refresh() and cm.current_cookies is not None:
                    # 让管理器认为需要刷新，从而真实地去读一次浏览器里的最新登录态
                    cm.current_cookies.created_at = 0
                cookies_list = await cm.get_cookies()
            if not cookies_list:
                return {}
            cookie_map = self._cookies_to_dict(cookies_list)
            if not any(
                cookie_map.get(k)
                for k in ('sessionid', 'sessionid_ss', 'sid_tt', 'passport_auth_status')
            ):
                logger.warning("浏览器返回的 Cookie 不含登录字段，登录态可能已失效")
            return cookie_map
        except Exception as exc:
            logger.warning(f"通过浏览器获取 Cookie 失败: {exc}")
            return {}

    def _load_cookie_records_from_pickle(self, file_path: str = 'cookies.pkl') -> List[Dict[str, Any]]:
        """Load browser cookie records with domain/path metadata for Playwright reuse."""
        if not os.path.exists(file_path):
            return []
        try:
            with open(file_path, 'rb') as f:
                data = pickle.load(f)
            records = data.get('cookies') if isinstance(data, dict) else data
            if not isinstance(records, list):
                return []
            normalized: List[Dict[str, Any]] = []
            for item in records:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name', '')).strip()
                value = item.get('value')
                domain = str(item.get('domain', '')).strip()
                path = str(item.get('path', '/') or '/')
                if not name or value is None or not domain:
                    continue
                cookie_record: Dict[str, Any] = {
                    'name': name,
                    'value': str(value),
                    'domain': domain,
                    'path': path,
                }
                expires = item.get('expires')
                if isinstance(expires, (int, float)) and expires > 0:
                    cookie_record['expires'] = expires
                if 'httpOnly' in item:
                    cookie_record['httpOnly'] = bool(item.get('httpOnly'))
                if 'secure' in item:
                    cookie_record['secure'] = bool(item.get('secure'))
                same_site = item.get('sameSite')
                if same_site in ('Strict', 'Lax', 'None'):
                    cookie_record['sameSite'] = same_site
                normalized.append(cookie_record)
            return normalized
        except Exception as e:
            logger.warning(f"Failed to load browser cookie records from {file_path}: {e}")
            return []

    def _build_playwright_cookie_records(self) -> List[Dict[str, Any]]:
        """Build Playwright cookie records from available cookie sources."""
        records = self._load_cookie_records_from_pickle('cookies.pkl')
        if records:
            return records

        cookie_map = self._cookies_to_dict(self.cookies)
        generated: List[Dict[str, Any]] = []
        for name, value in cookie_map.items():
            if not name or value is None:
                continue
            generated.append({
                'name': str(name),
                'value': str(value),
                'domain': '.douyin.com',
                'path': '/',
            })
        return generated

    def _get_playwright_storage_state_path(self) -> str:
        """Return storage state path used by browser-assisted recovery."""
        return str(self.config.get('playwright_storage_state', 'storage_state.json') or 'storage_state.json')

    def _python_has_module(self, python_executable: str, module_name: str) -> bool:
        """Check whether a Python executable can import a module."""
        if not python_executable or not os.path.exists(python_executable):
            return False
        try:
            result = subprocess.run(
                [
                    python_executable,
                    '-c',
                    (
                        'import importlib.util, sys; '
                        f'sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)'
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _iter_candidate_playwright_pythons(self) -> List[str]:
        """Return likely Python executables that may already have Playwright."""
        candidates: List[str] = []

        def add_candidate(path_like: Any):
            if not path_like:
                return
            try:
                candidate = str(Path(path_like))
            except Exception:
                return
            if candidate not in candidates and os.path.exists(candidate):
                candidates.append(candidate)

        add_candidate(self.config.get('playwright_python'))
        add_candidate(sys.executable)

        conda_prefix = os.environ.get('CONDA_PREFIX')
        if conda_prefix:
            add_candidate(Path(conda_prefix) / 'python.exe')

        executable_path = Path(sys.executable)
        root_candidates = {
            executable_path.parent.parent,
            Path.home() / 'anaconda3',
            Path.home() / 'miniconda3',
            Path('D:/anaconda3'),
            Path('C:/Users') / os.environ.get('USERNAME', '') / 'anaconda3',
            Path('C:/Users') / os.environ.get('USERNAME', '') / 'miniconda3',
        }
        for root in root_candidates:
            try:
                envs_dir = Path(root) / 'envs'
            except Exception:
                continue
            if not envs_dir.exists():
                continue
            for python_path in envs_dir.glob('*/python.exe'):
                add_candidate(python_path)

        return candidates

    def _install_playwright_for_python(self, python_executable: str) -> bool:
        """Install Playwright into the given Python environment if allowed."""
        if not self.auto_install_playwright or not python_executable:
            return False

        try:
            logger.info("Installing Playwright into interpreter: %s", python_executable)
            install_result = subprocess.run(
                [python_executable, '-m', 'pip', 'install', 'playwright'],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if install_result.returncode != 0:
                logger.warning(
                    "Playwright install failed for %s: %s",
                    python_executable,
                    (install_result.stderr or install_result.stdout or '').strip()[:400],
                )
                return False

            browser_result = subprocess.run(
                [python_executable, '-m', 'playwright', 'install', 'chromium'],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
            if browser_result.returncode != 0:
                logger.warning(
                    "Playwright browser install failed for %s: %s",
                    python_executable,
                    (browser_result.stderr or browser_result.stdout or '').strip()[:400],
                )
            return self._python_has_module(python_executable, 'playwright')
        except Exception as e:
            logger.warning("Auto-install Playwright failed for %s: %s", python_executable, e)
            return False

    def _find_playwright_python(self) -> Optional[str]:
        """Find a Python interpreter that can run Playwright helper recovery."""
        for candidate in self._iter_candidate_playwright_pythons():
            if self._python_has_module(candidate, 'playwright'):
                return candidate

        if self._install_playwright_for_python(sys.executable):
            return sys.executable

        return None

    async def _fetch_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Fetch user profile summary for counts and nickname."""
        platform_variants = [
            ("Win32", "Chrome", "145.0.0.0"),
            ("MacIntel", "Chrome", "122.0.0.0"),
        ]
        cookie_map = self._cookies_to_dict(self.cookies)
        ms_token = cookie_map.get('msToken', '') or ''

        for browser_platform, browser_name, browser_version in platform_variants:
            try:
                params_list = [
                    f'sec_user_id={user_id}',
                    'device_platform=webapp',
                    'aid=6383',
                    'channel=channel_pc_web',
                    'publish_video_strategy_type=2',
                    'source=channel_pc_web',
                    'personal_center_strategy=1',
                    'profile_other_record_enable=1',
                    'land_to=1',
                    'update_version_code=170400',
                    'pc_client_type=1',
                    'pc_libra_divert=Windows',
                    'support_h265=1',
                    'support_dash=1',
                    'cpu_core_num=8',
                    'version_code=170400',
                    'version_name=17.4.0',
                    'cookie_enabled=true',
                    'screen_width=1440',
                    'screen_height=1200',
                    'browser_language=zh-CN',
                    f'browser_platform={browser_platform}',
                    f'browser_name={browser_name}',
                    f'browser_version={browser_version}',
                    'browser_online=true',
                    'engine_name=Blink',
                    f'engine_version={browser_version}',
                    'os_name=Windows',
                    'os_version=10',
                    'device_memory=8',
                    'platform=PC',
                    'downlink=10',
                    'effective_type=4g',
                    'round_trip_time=0',
                ]
                if ms_token:
                    params_list.append(f'msToken={quote(ms_token, safe="")}')

                params = '&'.join(params_list)
                try:
                    full_url = f"{self.urls_helper.USER_DETAIL}{self.utils.getXbogus(params)}"
                except Exception as e:
                    logger.warning(f"X-Bogus build failed for user detail: {e}; fallback to unsigned request")
                    full_url = f"{self.urls_helper.USER_DETAIL}{params}"

                req_headers = dict(self.headers)
                if cookie_map:
                    req_headers.pop('Cookie', None)
                async with aiohttp.ClientSession(cookies=cookie_map or None) as session:
                    async with session.get(full_url, headers=req_headers, timeout=10) as response:
                        self._absorb_response_cookies(response)
                        body = await response.read()
                        if response.status != 200 or not body:
                            continue
                        data = json.loads(body.decode('utf-8', errors='ignore'))
                        if data.get('status_code') != 0:
                            continue
                        user = data.get('user')
                        if isinstance(user, dict):
                            return user
            except Exception as e:
                logger.warning(f"Fetch user profile failed (platform={browser_platform}): {e}")

        return None

    async def _browser_profile_requires_login(self, page) -> bool:
        """Check whether the user profile page is still showing the login wall."""
        try:
            body_text = await page.evaluate("() => document.body.innerText || ''")
        except Exception:
            return False
        return '登录后查看更多作品' in body_text or '立即登录' in body_text

    async def _capture_browser_profile_links(self, page) -> Dict[str, str]:
        """Collect visible profile work links from the browser DOM."""
        try:
            hrefs = await page.evaluate("""
                () => Array.from(
                    document.querySelectorAll('a[href*="/video/"], a[href*="/note/"]')
                ).map((node) => node.href).filter(Boolean)
            """)
        except Exception:
            return {}

        links: Dict[str, str] = {}
        for href in hrefs or []:
            aweme_id = self.extract_id_from_url(href)
            if aweme_id:
                links[aweme_id] = href
        return links

    async def _recover_user_posts_via_local_playwright(
        self,
        user_id: str,
        expected_count: int = 0,
        existing_ids: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Recover user posts in-process with Playwright."""
        from playwright.async_api import async_playwright

        existing_ids = set(existing_ids or set())
        storage_state_path = self._get_playwright_storage_state_path()
        headless = bool(self.config.get('user_posts_browser_headless', False))
        login_timeout = int(self.config.get('user_posts_browser_login_timeout', 300) or 300)
        max_scroll_rounds = int(self.config.get('user_posts_browser_max_scroll_rounds', 120) or 120)
        collected_awemes: Dict[str, Dict[str, Any]] = {}
        collected_links: Dict[str, str] = {}

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=['--disable-blink-features=AutomationControlled']
            )
            context_kwargs: Dict[str, Any] = {
                'viewport': {'width': 1440, 'height': 1200},
                'locale': 'zh-CN',
                'timezone_id': 'Asia/Shanghai',
                'user_agent': self.headers.get(
                    'User-Agent',
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
                ),
            }
            if os.path.exists(storage_state_path):
                context_kwargs['storage_state'] = storage_state_path

            context = await browser.new_context(**context_kwargs)
            if 'storage_state' not in context_kwargs:
                cookie_records = self._build_playwright_cookie_records()
                if cookie_records:
                    await context.add_cookies(cookie_records)

            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            page = await context.new_page()

            async def on_response(response):
                if '/aweme/v1/web/aweme/post/' not in response.url:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                aweme_list = data.get('aweme_list') or []
                if not isinstance(aweme_list, list):
                    return
                for aweme in aweme_list:
                    aweme_id = self._get_aweme_id_from_info(aweme)
                    if aweme_id:
                        collected_awemes[aweme_id] = aweme

            page.on('response', on_response)

            try:
                await page.goto(
                    f'https://www.douyin.com/user/{user_id}?from_tab_name=main',
                    wait_until='domcontentloaded',
                    timeout=60000,
                )
                await page.wait_for_timeout(5000)

                # 关键：先抓取「已经渲染出来」的作品链接，再判断登录墙。
                # 未登录时页面会提示"登录后查看更多作品"，但首屏作品链接通常已经存在，
                # 以前一遇到登录提示就直接放弃，导致主页批量下载拿不到任何作品。
                early_links = await self._capture_browser_profile_links(page)
                collected_links.update(early_links)

                if not early_links and not collected_awemes and await self._browser_profile_requires_login(page):
                    if headless:
                        logger.warning(
                            "Browser recovery hit the login wall with no visible posts; "
                            "set user_posts_browser_headless: false and login once in the opened browser"
                        )
                        return {'awemes': [], 'links': {}}

                    console.print(
                        "[yellow]The profile still hides old posts in browser mode. "
                        "Please complete Douyin login in the opened browser window...[/yellow]"
                    )
                    deadline = time.time() + login_timeout
                    while time.time() < deadline:
                        if not await self._browser_profile_requires_login(page):
                            break
                        await page.wait_for_timeout(2000)

                    if await self._browser_profile_requires_login(page):
                        logger.warning("Browser recovery timed out waiting for profile login access")
                        return {'awemes': [], 'links': {}}
                elif early_links:
                    logger.info(
                        "Browser recovery: 首屏已抓到 %s 个作品链接（未登录也可继续翻页）",
                        len(early_links),
                    )

                    try:
                        await context.storage_state(path=storage_state_path)
                        logger.info("Saved browser storage state to %s", storage_state_path)
                    except Exception as e:
                        logger.warning("Failed to save browser storage state: %s", e)

                stagnant_rounds = 0
                last_seen = -1
                for _ in range(max_scroll_rounds):
                    visible_links = await self._capture_browser_profile_links(page)
                    collected_links.update(visible_links)

                    current_seen = len(set(collected_awemes.keys()) | set(collected_links.keys()))
                    if expected_count and current_seen >= expected_count:
                        break

                    clicked_more = False
                    try:
                        clicked_more = await page.evaluate("""
                            () => {
                                const labels = ['看更多', '查看更多', '展开更多'];
                                const nodes = Array.from(document.querySelectorAll('button, div, span'));
                                const target = nodes.find((node) => labels.includes((node.textContent || '').trim()));
                                if (!target) return false;
                                target.click();
                                return true;
                            }
                        """)
                    except Exception:
                        clicked_more = False

                    if not clicked_more:
                        try:
                            await page.evaluate("""
                                () => {
                                    const container =
                                        document.querySelector('.parent-route-container.route-scroll-container') ||
                                        document.querySelector('.route-scroll-container') ||
                                        document.scrollingElement ||
                                        document.documentElement;
                                    container.scrollBy(0, Math.max(1800, (container.clientHeight || 0) - 120));
                                    window.scrollTo(0, document.body.scrollHeight);
                                }
                            """)
                        except Exception:
                            pass
                        try:
                            await page.mouse.wheel(0, 3200)
                        except Exception:
                            pass

                    await page.wait_for_timeout(1800)

                    if current_seen <= last_seen:
                        stagnant_rounds += 1
                    else:
                        stagnant_rounds = 0
                        last_seen = current_seen

                    if stagnant_rounds >= 20:
                        break

                visible_links = await self._capture_browser_profile_links(page)
                collected_links.update(visible_links)

                unique_seen = len(set(collected_awemes.keys()) | set(collected_links.keys()))
                logger.info(
                    "Browser recovery collected %s post payloads and %s visible links",
                    len(collected_awemes),
                    len(collected_links),
                )
                if expected_count and unique_seen < expected_count:
                    logger.warning(
                        "Browser recovery is still short of the profile count: got %s unique ids, expected %s",
                        unique_seen,
                        expected_count,
                    )

                return {
                    'awemes': [
                        aweme for aweme_id, aweme in collected_awemes.items()
                        if aweme_id not in existing_ids
                    ],
                    'links': {
                        aweme_id: href
                        for aweme_id, href in collected_links.items()
                        if aweme_id not in existing_ids and aweme_id not in collected_awemes
                    },
                }
            finally:
                await context.close()
                await browser.close()

    async def _run_browser_recovery_subprocess(
        self,
        user_id: str,
        expected_count: int = 0,
        existing_ids: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Run browser recovery through another Python interpreter that has Playwright."""
        helper_python = self._find_playwright_python()
        if not helper_python:
            logger.warning(
                "Playwright is unavailable for browser-assisted recovery; current interpreter: %s",
                sys.executable,
            )
            return {'awemes': [], 'links': {}}

        request_payload = {
            'config_path': str(Path(self.config_path).resolve()),
            'user_id': user_id,
            'expected_count': expected_count,
            'existing_ids': sorted(existing_ids or []),
        }
        login_timeout = int(self.config.get('user_posts_browser_login_timeout', 300) or 300)
        max_scroll_rounds = int(self.config.get('user_posts_browser_max_scroll_rounds', 120) or 120)
        timeout_seconds = max(180, login_timeout + max_scroll_rounds * 5 + 120)

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
            json.dump(request_payload, tmp_file, ensure_ascii=False)
            tmp_input_path = tmp_file.name

        try:
            result = subprocess.run(
                [
                    helper_python,
                    __file__,
                    '--browser-recover-user-posts-helper',
                    tmp_input_path,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout_seconds,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
        except Exception as e:
            logger.warning("External browser recovery helper failed to start: %s", e)
            return {'awemes': [], 'links': {}}
        finally:
            try:
                os.remove(tmp_input_path)
            except OSError:
                pass

        if result.stderr:
            logger.info("Browser helper stderr: %s", result.stderr.strip()[:800])

        if result.returncode != 0:
            logger.warning(
                "Browser helper exited with code %s via %s; stdout=%s stderr=%s",
                result.returncode,
                helper_python,
                (result.stdout or '').strip()[:400],
                (result.stderr or '').strip()[:400],
            )
            return {'awemes': [], 'links': {}}

        raw_stdout = (result.stdout or '').strip()
        if not raw_stdout:
            return {'awemes': [], 'links': {}}

        try:
            return json.loads(raw_stdout)
        except json.JSONDecodeError:
            logger.warning("Browser helper returned non-JSON output: %s", raw_stdout[:400])
            return {'awemes': [], 'links': {}}

    async def _recover_user_posts_via_browser(
        self,
        user_id: str,
        expected_count: int = 0,
        existing_ids: Optional[Set[str]] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """Use a browser session to recover user posts when API pagination stalls."""
        existing_ids = set(existing_ids or set())
        recovery_result: Dict[str, Any] = {'awemes': [], 'links': {}}

        try:
            import playwright.async_api  # noqa: F401
            recovery_result = await self._recover_user_posts_via_local_playwright(
                user_id=user_id,
                expected_count=expected_count,
                existing_ids=existing_ids,
            )
        except ImportError:
            recovery_result = await self._run_browser_recovery_subprocess(
                user_id=user_id,
                expected_count=expected_count,
                existing_ids=existing_ids,
            )
        except Exception as e:
            logger.warning("Local browser recovery failed: %s", e)
            recovery_result = {'awemes': [], 'links': {}}

        recovered_awemes: List[Dict[str, Any]] = []
        recovered_ids: Set[str] = set()

        for aweme in recovery_result.get('awemes') or []:
            aweme_id = self._get_aweme_id_from_info(aweme)
            if not aweme_id or aweme_id in existing_ids or aweme_id in recovered_ids:
                continue
            recovered_awemes.append(aweme)
            recovered_ids.add(aweme_id)

        if expected_count and len(existing_ids) + len(recovered_ids) >= expected_count:
            return recovered_awemes

        link_only_ids = [
            aweme_id for aweme_id in (recovery_result.get('links') or {}).keys()
            if aweme_id not in existing_ids and aweme_id not in recovered_ids
        ]
        if link_only_ids:
            logger.info(
                "Browser recovery found %s extra visible post links without payloads; "
                "fetching details one by one",
                len(link_only_ids),
            )
            links_map = recovery_result.get('links') or {}
            for aweme_id in link_only_ids:
                if limit and len(recovered_awemes) >= limit:
                    logger.info("已达到本次下载上限 %s，停止继续抓取作品详情", limit)
                    break
                detail = None
                try:
                    detail = await self._fetch_aweme_from_user_page_locate(user_id, aweme_id)
                except Exception as e:
                    logger.warning("Locate-item recovery for aweme %s failed: %s", aweme_id, e)

                # 接口/定位都拿不到时，直接打开作品页用浏览器取（未登录也能成功）
                if not detail and self._single_aweme_browser_recovery_enabled():
                    page_url = links_map.get(aweme_id) or f"https://www.douyin.com/video/{aweme_id}"
                    try:
                        logger.info("改用浏览器打开作品页获取详情: %s", aweme_id)
                        detail = await self._fetch_aweme_via_local_playwright(
                            page_url, aweme_id, headless=self._single_aweme_browser_headless()
                        )
                    except Exception as e:
                        logger.warning("浏览器获取作品 %s 详情失败: %s", aweme_id, e)
                        detail = None

                detail_id = self._get_aweme_id_from_info(detail or {}) or aweme_id
                if not detail or detail_id in existing_ids or detail_id in recovered_ids:
                    continue
                recovered_awemes.append(detail)
                recovered_ids.add(detail_id)
                if expected_count and len(existing_ids) + len(recovered_ids) >= expected_count:
                    break

        return recovered_awemes

    async def _initialize_cookies_and_headers(self):
        """Initialize cookies and headers with local fallback cookie files."""
        if isinstance(self.cookies, str) and self.cookies.strip().lower() == 'auto':
            self.cookies = None

        config_cookie_map = self._cookies_to_dict(self.cookies)
        local_cookie_map: Dict[str, str] = {}

        txt_cookie_map = self._load_cookie_text_file('cookies.txt')
        if txt_cookie_map:
            local_cookie_map.update(txt_cookie_map)
            logger.info(f"Loaded {len(txt_cookie_map)} cookies from cookies.txt")

        pkl_cookie_map = self._load_cookie_pickle('cookies.pkl')
        if pkl_cookie_map:
            for k, v in pkl_cookie_map.items():
                local_cookie_map.setdefault(k, v)
            logger.info(f"Loaded {len(pkl_cookie_map)} cookies from cookies.pkl")

        # cookies.txt 与 cookies.pkl 可能一新一旧：登录相关字段要以「更晚过期」的为准，
        # 否则旧的 cookies.txt 会覆盖浏览器新刷出来的会话，导致接口一直 403。
        login_keys = (
            'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard',
            'passport_auth_status', 'passport_auth_status_ss', 'uid_tt', 'msToken',
        )
        txt_expiry = self._cookie_expiry_at(txt_cookie_map)
        pkl_expiry = self._cookie_expiry_at(pkl_cookie_map)
        fresher = None
        if txt_expiry and pkl_expiry:
            fresher = pkl_cookie_map if pkl_expiry > txt_expiry else txt_cookie_map
        elif pkl_expiry and not txt_expiry:
            fresher = pkl_cookie_map
        if fresher is not None:
            applied = 0
            for key in login_keys:
                value = fresher.get(key)
                if value and local_cookie_map.get(key) != value:
                    local_cookie_map[key] = value
                    applied += 1
            if applied and fresher is pkl_cookie_map:
                logger.info(
                    "cookies.pkl 的登录态比 cookies.txt 更新，已优先使用 pkl 的 %s 个字段", applied
                )

        has_login_cookie = any(
            config_cookie_map.get(k)
            for k in ('sessionid', 'sessionid_ss', 'sid_tt', 'passport_auth_status')
        )

        cookie_map = dict(config_cookie_map)
        if not cookie_map and local_cookie_map:
            cookie_map = dict(local_cookie_map)
            logger.info("Using local cookies because config cookie is empty")
        elif cookie_map and local_cookie_map and not has_login_cookie:
            # Partial config cookies often return aweme_list=null; enrich with local browser cookies.
            cookie_map.update(local_cookie_map)
            logger.info("Merged config cookies with local cookies")

        if cookie_map and self._cookie_expiry_hint(cookie_map) and self.auto_cookie:
            # 本地已有 Cookie 但登录态已过期：cookies 为 auto 时先尝试用浏览器刷新，
            # 避免用户只看到一堆 403 却不知道为什么。
            skipped_hours = self._skip_auto_refresh_reason()
            if skipped_hours is not None:
                logger.info(
                    "%.1f 小时前刚尝试过自动刷新且未取到登录态，本次跳过；"
                    "重新登录后可运行 --refresh-session 强制刷新",
                    skipped_hours,
                )
            else:
                logger.info("本地 Cookie 已过期，尝试用浏览器刷新登录态")
                refreshed = await self._fetch_cookies_via_browser(headless=True)
                if refreshed and any(
                    refreshed.get(k)
                    for k in ('sessionid', 'sessionid_ss', 'sid_tt', 'passport_auth_status')
                ):
                    cookie_map = refreshed
                    self._remember_auto_refresh(success=True)
                    logger.info("浏览器刷新得到有效登录态")
                else:
                    self._remember_auto_refresh(success=False)
                    if refreshed:
                        logger.warning(
                            "浏览器中的登录态也已失效（缺少 sessionid 等字段），将使用现有 Cookie 继续"
                        )
                    logger.warning(
                        "自动刷新未取到登录态，%.0f 小时内不再重复尝试（可用 --refresh-session 强制刷新）",
                        self.AUTO_REFRESH_COOLDOWN_HOURS,
                    )

        if cookie_map:
            self.cookies = cookie_map
            cookie_str = '; '.join([f'{k}={v}' for k, v in cookie_map.items() if k and v is not None])
            self.headers['Cookie'] = cookie_str
            from apiproxy.douyin import douyin_headers
            douyin_headers['Cookie'] = cookie_str
            # 提前发现登录态过期，避免用户只看到莫名的 403
            expiry_hint = self._cookie_expiry_hint(cookie_map)
            self._cookies_expired = bool(expiry_hint)
            if expiry_hint:
                logger.warning(expiry_hint)
                console.print(f"[yellow]⚠ {expiry_hint}[/yellow]")
            self._report_session_validity(cookie_map)
            return

        if self.auto_cookie:
            try:
                console.print("[cyan]Auto fetching cookies...[/cyan]")
                cookie_map = await self._fetch_cookies_via_browser()
                if cookie_map:
                    self.cookies = cookie_map
                    cookie_str = '; '.join(
                        [f'{k}={v}' for k, v in cookie_map.items() if k and v is not None]
                    )
                    if cookie_str:
                        self.headers['Cookie'] = cookie_str
                        from apiproxy.douyin import douyin_headers
                        douyin_headers['Cookie'] = cookie_str
                        console.print("[green]Cookie fetched successfully[/green]")
                        return
                console.print("[yellow]Auto cookie fetch failed, continue without cookie[/yellow]")
            except Exception as e:
                logger.warning(f"Auto cookie fetch failed: {e}")
                console.print("[yellow]Auto cookie fetch failed, continue without cookie[/yellow]")

    def detect_content_type(self, url: str) -> ContentType:
        """根据链接判断内容类型（视频 / 图文 / 主页 / 合集 / 音乐 / 直播）"""
        # 图文作品链接形如 /note/<id>，需先于单作品标记判断，否则会被识别成视频
        if '/note/' in url:
            return ContentType.IMAGE
        if self._url_has_single_aweme_marker(url):
            return ContentType.VIDEO
        elif '/user/' in url:
            return ContentType.USER
        elif '/collection/' in url or '/mix/' in url:
            return ContentType.MIX
        elif '/music/' in url:
            return ContentType.MUSIC
        elif 'live.douyin.com' in url:
            return ContentType.LIVE
        else:
            return ContentType.VIDEO  # 默认按视频作品处理
    
    async def resolve_short_url(self, url: str) -> str:
        """解析 v.douyin.com 短链接，返回真实地址"""
        if 'v.douyin.com' in url:
            try:
                # 使用同步请求获取重定向后的地址
                response = requests.get(url, headers=self.headers, allow_redirects=True, timeout=10)
                final_url = response.url
                logger.info(f"解析短链接: {url} -> {final_url}")
                return final_url
            except Exception as e:
                logger.warning(f"解析短链接失败: {e}")
        return url

    def _url_has_single_aweme_marker(self, url: str) -> bool:
        """Whether the URL clearly points to a single aweme page/modal."""
        single_aweme_patterns = (
            r'/video/\d+',
            r'/note/\d+',
            r'(^|[?&])modal_id=\d+',
            r'(^|[?&])aweme_id=\d+',
            r'(^|[?&])item_id=\d+',
        )
        return any(re.search(pattern, url) for pattern in single_aweme_patterns)
    
    def extract_id_from_url(self, url: str, content_type: ContentType = None) -> Optional[str]:
        """从链接中提取 ID
        
        Args:
            url: 作品或主页链接
            content_type: 内容类型，留空时自动判断
        """
        # 主页链接优先提取 sec_user_id
        if content_type == ContentType.USER:
            user_patterns = [
                r'/user/([\w.-]+)',
                r'sec_uid=([\w.-]+)'
            ]
            
            for pattern in user_patterns:
                match = re.search(pattern, url)
                if match:
                    user_id = match.group(1)
                    logger.info(f"提取到用户ID: {user_id}")
                    return user_id

        # 作品 ID 的常见参数形式
        video_patterns = [
            r'/video/(\d+)',
            r'/note/(\d+)',
            r'modal_id=(\d+)',
            r'aweme_id=(\d+)',
            r'item_id=(\d+)'
        ]
        
        for pattern in video_patterns:
            match = re.search(pattern, url)
            if match:
                video_id = match.group(1)
                logger.info(f"提取到作品ID: {video_id}")
                return video_id

        if content_type is None and '/user/' in url:
            user_patterns = [
                r'/user/([\w.-]+)',
                r'sec_uid=([\w.-]+)'
            ]
            
            for pattern in user_patterns:
                match = re.search(pattern, url)
                if match:
                    user_id = match.group(1)
                    logger.info(f"提取到用户ID: {user_id}")
                    return user_id
        
        # 兜底匹配
        other_patterns = [
            r'/collection/(\d+)',
            r'/music/(\d+)'
        ]
        
        for pattern in other_patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        # 无法从链接中识别出 ID
        number_match = re.search(r'(\d{15,20})', url)
        if number_match:
            video_id = number_match.group(1)
            logger.info(f"从URL中提取到数字ID: {video_id}")
            return video_id
        
        logger.error(f"无法从URL提取ID: {url}")
        return None

    def _get_aweme_id_from_info(self, info: Dict) -> Optional[str]:
        """从 aweme 数据中取出 aweme_id"""
        try:
            if not isinstance(info, dict):
                return None
            value = info.get('aweme_id')
            if not value and isinstance(info.get('aweme'), dict):
                value = info.get('aweme', {}).get('aweme_id')
            if value is None or value == '':
                return None
            return str(value)
        except Exception:
            return None

    def _aweme_id_matches(self, info: Dict, expected_aweme_id: str) -> bool:
        """Return whether an aweme payload belongs to the requested aweme id."""
        actual_aweme_id = self._get_aweme_id_from_info(info)
        return bool(actual_aweme_id and str(actual_aweme_id) == str(expected_aweme_id))

    def _build_post_folder_name(self, create_time: str, desc: str, aweme_id: Optional[str]) -> str:
        """Build a unique folder name for a post."""
        base = f"{create_time}_{desc}" if desc else create_time
        suffix = f"_{aweme_id}" if aweme_id else ""
        max_base_len = max(20, 120 - len(suffix))
        folder_name = self._safe_name(base, default=create_time, max_len=max_base_len)
        return self._safe_name(f"{folder_name}{suffix}", default=create_time, max_len=120)

    def _get_sec_uid_from_info(self, info: Dict) -> Optional[str]:
        """从 aweme 数据中取出作者的 sec_uid"""
        try:
            return info.get('author', {}).get('sec_uid')
        except Exception:
            return None

    def _should_skip_increment(self, context: str, info: Dict, mix_id: Optional[str] = None, music_id: Optional[str] = None, sec_uid: Optional[str] = None) -> bool:
        """按增量配置判断该作品是否已下载过，已下载则跳过"""
        if not self.db:
            return False
        aweme_id = self._get_aweme_id_from_info(info)
        if not aweme_id:
            return False

        try:
            if context == 'post' and self.increase_cfg.get('post', False):
                sec = sec_uid or self._get_sec_uid_from_info(info) or ''
                return bool(self.db.get_user_post(sec, int(aweme_id)) if aweme_id.isdigit() else None)
            if context == 'like' and self.increase_cfg.get('like', False):
                sec = sec_uid or self._get_sec_uid_from_info(info) or ''
                return bool(self.db.get_user_like(sec, int(aweme_id)) if aweme_id.isdigit() else None)
            if context == 'mix' and self.increase_cfg.get('mix', False):
                sec = sec_uid or self._get_sec_uid_from_info(info) or ''
                mid = mix_id or ''
                return bool(self.db.get_mix(sec, mid, int(aweme_id)) if aweme_id.isdigit() else None)
            if context == 'music' and self.increase_cfg.get('music', False):
                mid = music_id or ''
                return bool(self.db.get_music(mid, int(aweme_id)) if aweme_id.isdigit() else None)
        except Exception:
            return False
        return False

    def _record_increment(self, context: str, info: Dict, mix_id: Optional[str] = None, music_id: Optional[str] = None, sec_uid: Optional[str] = None):
        """记录已下载的作品，供增量下载判断使用"""
        if not self.db:
            return
        aweme_id = self._get_aweme_id_from_info(info)
        if not aweme_id or not aweme_id.isdigit():
            return
        try:
            if context == 'post':
                sec = sec_uid or self._get_sec_uid_from_info(info) or ''
                self.db.insert_user_post(sec, int(aweme_id), info)
            elif context == 'like':
                sec = sec_uid or self._get_sec_uid_from_info(info) or ''
                self.db.insert_user_like(sec, int(aweme_id), info)
            elif context == 'mix':
                sec = sec_uid or self._get_sec_uid_from_info(info) or ''
                mid = mix_id or ''
                self.db.insert_mix(sec, mid, int(aweme_id), info)
            elif context == 'music':
                mid = music_id or ''
                self.db.insert_music(mid, int(aweme_id), info)
        except Exception:
            pass
    
    async def download_single_video(self, url: str, progress=None) -> bool:
        """下载单个作品（视频或图文）"""
        try:
            # 解析短链接
            url = await self.resolve_short_url(url)
            
            # 提取作品 ID
            video_id = self.extract_id_from_url(url, ContentType.VIDEO)
            if not video_id:
                logger.error(f"无法从URL提取ID: {url}")
                return False
            
            # 链接中没有作品 ID 时，从主页页面中定位作品
            if not video_id and '/user/' not in url:
                # 退化为从跳转地址的路径中提取作品 ID
                video_id = url.split('/')[-2] if url.endswith('/') else url.split('/')[-1]
                logger.info(f"尝试从短链接路径提取ID: {video_id}")
            
            if not video_id:
                logger.error(f"无法从URL提取视频ID: {url}")
                return False
            
            # 限速等待
            await self.rate_limiter.acquire()
            
            # 获取作品信息
            if progress:
                progress.update(task_id=progress.task_ids[-1], description="获取作品信息...")
            
            video_info = None
            # 登录态已过期时接口基本只会返回空数据，直接走浏览器更快也更稳
            if self._cookies_expired and self._single_aweme_browser_recovery_enabled():
                console.print("[cyan]检测到 Cookie 已过期，优先用浏览器获取作品信息…[/cyan]")
                try:
                    video_info = await self._fetch_aweme_via_local_playwright(
                        url, video_id, headless=self._single_aweme_browser_headless()
                    )
                except Exception as exc:
                    logger.warning(f"浏览器优先获取失败，回退接口: {exc}")
                    video_info = None

            if not video_info:
                video_info = await self.retry_manager.execute_with_retry(
                    self._fetch_video_info, video_id
                )

            if not video_info and '/user/' in url and self._url_has_single_aweme_marker(url):
                logger.info(
                    "Direct aweme detail APIs returned empty; try recovering aweme %s from modal URL context",
                    video_id,
                )
                video_info = await self._fetch_aweme_from_modal_url(url, video_id)

            # 接口在没有有效登录态时常常返回空数据（200 + 空响应体），
            # 但浏览器加载作品页仍能拿到 aweme/detail，因此用浏览器兜底。
            if not video_info and self._single_aweme_browser_recovery_enabled():
                console.print(
                    "[yellow]接口未返回作品信息，改用浏览器兜底获取（无需登录即可下载公开作品）…[/yellow]"
                )
                try:
                    video_info = await self._fetch_aweme_via_local_playwright(
                        url, video_id, headless=self._single_aweme_browser_headless()
                    )
                except Exception as exc:
                    logger.warning(f"浏览器兜底获取作品信息失败: {exc}")
                    video_info = None
                if video_info:
                    logger.info("浏览器兜底成功获取作品信息: %s", video_id)

            if not video_info:
                logger.error(f"无法获取视频信息: {video_id}")
                console.print(
                    "[red]接口与浏览器均未能获取该作品，若为私密/需登录内容，"
                    "请重新运行 cookie_extractor.py 登录后再试。[/red]"
                )
                self.stats.failed += 1
                return False
            
            # 下载作品文件
            if progress:
                progress.update(task_id=progress.task_ids[-1], description="下载作品文件...")
            
            success = await self._download_media_files(video_info, progress)
            
            if success:
                self.stats.success += 1
                logger.info(f"下载成功: {url}")
            else:
                self.stats.failed += 1
                logger.error(f"下载失败: {url}")
            
            return success
            
        except Exception as e:
            logger.error(f"下载单个作品异常 {url}: {e}")
            self.stats.failed += 1
            return False
        finally:
            self.stats.total += 1
    
    async def _fetch_video_info(self, video_id: str) -> Optional[Dict]:
        """获取作品详情"""
        try:
            # 优先复用 DouYinCommand.py 中的 Douyin 类
            from apiproxy.douyin.douyin import Douyin
            
            # 创建 Douyin 实例
            dy = Douyin(database=False)
            
            # 把当前 cookies 注入 douyin_headers
            if hasattr(self, 'cookies') and self.cookies:
                cookie_str = self._build_cookie_string()
                if cookie_str:
                    from apiproxy.douyin import douyin_headers
                    douyin_headers['Cookie'] = cookie_str
                    logger.info(f"已将 Cookie 注入 Douyin 请求头: {cookie_str[:100]}...")
            
            try:
                # 使用该类自身的请求封装
                result = dy.getAwemeInfo(video_id)
                if result:
                    if self._aweme_id_matches(result, video_id):
                        logger.info(f"Douyin 类成功返回作品信息: {result.get('desc', '')[:30]}")
                        return result
                    logger.warning(
                        "Douyin class returned mismatched aweme: requested=%s, actual=%s",
                        video_id,
                        self._get_aweme_id_from_info(result),
                    )
                else:
                    logger.error("Douyin class returned empty result")
                    
            except Exception as e:
                logger.error(f"Douyin 类获取单作品信息失败: {e}")
                
        except Exception as e:
            logger.error(f"导入 Douyin 类失败: {e}")
            import traceback
            traceback.print_exc()
        
        # Douyin 类失败时，退化为直接用 aiohttp 请求备用接口
        try:
            fallback_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
            logger.info(f"尝试备用接口获取作品信息: {fallback_url}")
            
            # 组装备用接口请求头
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Referer': 'https://www.douyin.com/',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(fallback_url, headers=headers, timeout=15) as response:
                    logger.info(f"备用接口响应状态码: {response.status}")
                    if response.status != 200:
                        logger.error(f"备用接口请求失败，状态码: {response.status}")
                        return None
                    
                    text = await response.text()
                    logger.info(f"备用接口响应内容长度: {len(text)}")
                    
                    if not text:
                        logger.error("备用接口响应为空")
                        return None
                    
                    try:
                        data = json.loads(text)
                        
                        item_list = (data or {}).get('item_list') or []
                        if item_list:
                            for aweme_detail in item_list:
                                if self._aweme_id_matches(aweme_detail, video_id):
                                    logger.info("备用接口成功返回作品信息")
                                    return aweme_detail
                            logger.error("备用接口返回了作品列表，但没有匹配目标 aweme_id")
                        else:
                            logger.error("备用接口未返回 item_list")
                            
                    except json.JSONDecodeError as e:
                        logger.error(f"备用接口 JSON 解析失败: {e}")
                        logger.error(f"备用接口原始响应: {text}")
                        return None
                        
        except Exception as e:
            logger.error(f"备用接口获取作品信息失败: {e}")
        
        return None

    async def _fetch_aweme_from_user_page_locate(self, user_id: str, aweme_id: str) -> Optional[Dict]:
        """Fetch a single aweme through the user-page list API with locate_item_id."""
        if not user_id or user_id == 'self' or not aweme_id:
            return None

        platform_variants = [
            ("Win32", "Chrome", "145.0.0.0", "Windows"),
            ("MacIntel", "Chrome", "122.0.0.0", "Mac"),
        ]
        cookie_map = self._cookies_to_dict(self.cookies)
        ms_token = cookie_map.get('msToken', '') or ''

        for browser_platform, browser_name, browser_version, os_name in platform_variants:
            try:
                params_list = [
                    'device_platform=webapp',
                    'aid=6383',
                    'channel=channel_pc_web',
                    f'sec_user_id={user_id}',
                    'max_cursor=0',
                    f'locate_item_id={aweme_id}',
                    'locate_query=false',
                    'show_live_replay_strategy=1',
                    'need_time_list=1',
                    'time_list_query=0',
                    'whale_cut_token=',
                    'cut_version=1',
                    'count=18',
                    'publish_video_strategy_type=2',
                    'from_user_page=1',
                    'update_version_code=170400',
                    'pc_client_type=1',
                    'pc_libra_divert=Windows',
                    'support_h265=1',
                    'support_dash=0',
                    'cpu_core_num=8',
                    'version_code=290100',
                    'version_name=29.1.0',
                    'cookie_enabled=true',
                    'screen_width=1440',
                    'screen_height=1200',
                    'browser_language=zh-CN',
                    f'browser_platform={browser_platform}',
                    f'browser_name={browser_name}',
                    f'browser_version={browser_version}',
                    'browser_online=true',
                    'engine_name=Blink',
                    f'engine_version={browser_version}',
                    f'os_name={os_name}',
                    'os_version=10',
                    'device_memory=8',
                    'platform=PC',
                    'downlink=10',
                    'effective_type=4g',
                    'round_trip_time=0',
                ]
                if ms_token:
                    params_list.append(f'msToken={quote(ms_token, safe="")}')
                params = '&'.join(params_list)
                api_url = self.urls_helper.USER_POST
                try:
                    full_url = f"{api_url}{self.utils.getXbogus(params)}"
                except Exception:
                    full_url = f"{api_url}{params}"

                req_headers = dict(self.headers)
                if cookie_map:
                    req_headers.pop('Cookie', None)
                async with aiohttp.ClientSession(cookies=cookie_map or None) as session:
                    async with session.get(full_url, headers=req_headers, timeout=15) as response:
                        self._absorb_response_cookies(response)
                        if response.status != 200:
                            continue
                        body = await response.read()
                        if not body:
                            continue
                        data = json.loads(body.decode('utf-8', errors='ignore'))
                        aweme_list = data.get('aweme_list') or []
                        if not isinstance(aweme_list, list):
                            continue
                        for aweme in aweme_list:
                            if self._get_aweme_id_from_info(aweme) == aweme_id:
                                logger.info(
                                    "Recovered aweme %s from user page locate API (user=%s)",
                                    aweme_id,
                                    user_id,
                                )
                                return aweme
            except Exception as e:
                logger.warning(
                    "Locate-item user page API failed for aweme %s (user=%s): %s",
                    aweme_id,
                    user_id,
                    e,
                )

        return None

    def _extract_target_aweme_from_payload(self, data: Dict[str, Any], aweme_id: str) -> Optional[Dict]:
        """Extract target aweme from a browser/network response payload."""
        if not isinstance(data, dict):
            return None

        aweme_detail = data.get('aweme_detail')
        if isinstance(aweme_detail, dict) and self._get_aweme_id_from_info(aweme_detail) == aweme_id:
            return aweme_detail

        aweme_list = data.get('aweme_list') or []
        if isinstance(aweme_list, list):
            for aweme in aweme_list:
                if self._get_aweme_id_from_info(aweme) == aweme_id:
                    return aweme
        return None

    def _single_aweme_browser_recovery_enabled(self) -> bool:
        """接口拿不到作品时，是否允许用浏览器兜底。"""
        return bool(self.config.get('single_aweme_browser_recovery', True))

    def _single_aweme_browser_headless(self) -> bool:
        """浏览器兜底默认无头运行，可用 single_aweme_browser_headless 覆盖。"""
        value = self.config.get('single_aweme_browser_headless')
        if value is None:
            return True
        return bool(value)

    async def _fetch_aweme_via_local_playwright(
        self, url: str, aweme_id: str, headless: Optional[bool] = None
    ) -> Optional[Dict]:
        """Open the target URL in Playwright and capture the target aweme from network responses."""
        from playwright.async_api import async_playwright

        storage_state_path = self._get_playwright_storage_state_path()
        if headless is None:
            headless = bool(self.config.get(
                'single_aweme_browser_headless',
                self.config.get('user_posts_browser_headless', False),
            ))
        login_timeout = int(self.config.get('single_aweme_browser_login_timeout', 300) or 300)
        found_aweme: Optional[Dict] = None

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=['--disable-blink-features=AutomationControlled']
            )
            context_kwargs: Dict[str, Any] = {
                'viewport': {'width': 1440, 'height': 1200},
                'locale': 'zh-CN',
                'timezone_id': 'Asia/Shanghai',
                'user_agent': self.headers.get(
                    'User-Agent',
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
                ),
            }
            if os.path.exists(storage_state_path):
                context_kwargs['storage_state'] = storage_state_path

            context = await browser.new_context(**context_kwargs)
            if 'storage_state' not in context_kwargs:
                cookie_records = self._build_playwright_cookie_records()
                if cookie_records:
                    await context.add_cookies(cookie_records)

            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            page = await context.new_page()

            async def on_response(response):
                nonlocal found_aweme
                if found_aweme is not None:
                    return
                if '/aweme/' not in response.url:
                    return
                try:
                    data = await response.json()
                except Exception:
                    return
                found_aweme = self._extract_target_aweme_from_payload(data, aweme_id)

            page.on('response', on_response)

            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(4000)

                if found_aweme is None and await self._browser_profile_requires_login(page):
                    if headless:
                        logger.warning(
                            "Single-aweme browser recovery still hits login wall in headless mode; "
                            "set single_aweme_browser_headless: false and login once"
                        )
                        return None

                    console.print(
                        "[yellow]Please complete Douyin login in the opened browser window "
                        "to recover this single aweme...[/yellow]"
                    )
                    deadline = time.time() + login_timeout
                    while time.time() < deadline and found_aweme is None:
                        if not await self._browser_profile_requires_login(page):
                            await page.wait_for_timeout(3000)
                            if found_aweme is not None:
                                break
                        await page.wait_for_timeout(1500)

                    if found_aweme is None and await self._browser_profile_requires_login(page):
                        logger.warning("Single-aweme browser recovery timed out waiting for login access")
                        return None

                    try:
                        await context.storage_state(path=storage_state_path)
                        logger.info("Saved browser storage state to %s", storage_state_path)
                    except Exception as e:
                        logger.warning("Failed to save browser storage state: %s", e)

                for _ in range(8):
                    if found_aweme is not None:
                        break
                    try:
                        await page.mouse.wheel(0, 1800)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1500)

                return found_aweme
            finally:
                await context.close()
                await browser.close()

    async def _run_browser_fetch_aweme_subprocess(self, url: str, aweme_id: str) -> Optional[Dict]:
        """Run single-aweme browser recovery through another Python interpreter that has Playwright."""
        helper_python = self._find_playwright_python()
        if not helper_python:
            logger.warning(
                "Playwright is unavailable for single-aweme browser recovery; current interpreter: %s",
                sys.executable,
            )
            return None

        request_payload = {
            'config_path': str(Path(self.config_path).resolve()),
            'url': url,
            'aweme_id': aweme_id,
        }
        login_timeout = int(self.config.get('single_aweme_browser_login_timeout', 300) or 300)
        timeout_seconds = max(180, login_timeout + 180)

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as tmp_file:
            json.dump(request_payload, tmp_file, ensure_ascii=False)
            tmp_input_path = tmp_file.name

        try:
            result = subprocess.run(
                [
                    helper_python,
                    __file__,
                    '--browser-fetch-aweme-helper',
                    tmp_input_path,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout_seconds,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
        except Exception as e:
            logger.warning("External single-aweme browser helper failed to start: %s", e)
            return None
        finally:
            try:
                os.remove(tmp_input_path)
            except OSError:
                pass

        if result.stderr:
            logger.info("Single-aweme browser helper stderr: %s", result.stderr.strip()[:800])

        if result.returncode != 0:
            logger.warning(
                "Single-aweme browser helper exited with code %s via %s; stdout=%s stderr=%s",
                result.returncode,
                helper_python,
                (result.stdout or '').strip()[:400],
                (result.stderr or '').strip()[:400],
            )
            return None

        raw_stdout = (result.stdout or '').strip()
        if not raw_stdout:
            return None
        try:
            return json.loads(raw_stdout)
        except json.JSONDecodeError:
            logger.warning("Single-aweme browser helper returned non-JSON output: %s", raw_stdout[:400])
            return None

    async def _fetch_aweme_from_modal_url(self, url: str, aweme_id: str) -> Optional[Dict]:
        """Recover aweme info from a user-page modal URL."""
        user_id = self.extract_id_from_url(url, ContentType.USER)
        if user_id and user_id != 'self':
            located_aweme = await self._fetch_aweme_from_user_page_locate(user_id, aweme_id)
            if located_aweme:
                return located_aweme

        try:
            import playwright.async_api  # noqa: F401
            return await self._fetch_aweme_via_local_playwright(url, aweme_id)
        except ImportError:
            return await self._run_browser_fetch_aweme_subprocess(url, aweme_id)
        except Exception as e:
            logger.warning("Local single-aweme browser recovery failed: %s", e)
            return None
    
    def _build_detail_params(self, aweme_id: str) -> str:
        """构建作品详情接口的参数串"""
        # 复用 douyinapi.py 中的参数模板
        params = [
            f'aweme_id={aweme_id}',
            'device_platform=webapp',
            'aid=6383'
        ]
        return '&'.join(params)
    
    async def _download_media_files(self, video_info: Dict, progress=None) -> bool:
        """Download media assets. Required assets must succeed."""
        try:
            is_image = bool(video_info.get('images'))

            author_name = self._safe_name(video_info.get('author', {}).get('nickname', 'unknown'), default='unknown', max_len=40)
            desc = self._safe_name(video_info.get('desc', ''), default='', max_len=50)
            raw_create_time = video_info.get('create_time')
            dt_obj = None
            if isinstance(raw_create_time, (int, float)):
                dt_obj = datetime.fromtimestamp(raw_create_time)
            elif isinstance(raw_create_time, str) and raw_create_time:
                for fmt in ('%Y-%m-%d %H.%M.%S', '%Y-%m-%d_%H-%M-%S', '%Y-%m-%d %H:%M:%S'):
                    try:
                        dt_obj = datetime.strptime(raw_create_time, fmt)
                        break
                    except Exception:
                        pass
            if dt_obj is None:
                dt_obj = datetime.fromtimestamp(time.time())
            create_time = dt_obj.strftime('%Y-%m-%d_%H-%M-%S')

            aweme_id = self._get_aweme_id_from_info(video_info)
            folder_name = self._build_post_folder_name(create_time, desc, aweme_id)
            save_dir = self.save_path / author_name / folder_name
            save_dir.mkdir(parents=True, exist_ok=True)

            success = True

            if is_image:
                images = video_info.get('images', [])
                downloaded_videos: List[Path] = []
                for i, image_info in enumerate(images):
                    clip_urls = self._get_image_clip_url_candidates(image_info)
                    if clip_urls:
                        clip_path = save_dir / f"clip_{i+1}.mp4"
                        if await self._download_first_success(clip_urls, clip_path, required=False):
                            logger.info(f"Downloaded clip {i+1}/{len(images)}: {clip_path.name}")
                            downloaded_videos.append(clip_path)
                            continue
                        logger.warning(f"Clip {i+1}/{len(images)} failed, fallback to image")

                    image_urls = self._get_image_url_candidates(image_info)
                    image_path = save_dir / f"image_{i+1}.jpg"
                    if image_urls and await self._download_first_success(image_urls, image_path, required=True):
                        logger.info(f"Downloaded image {i+1}/{len(images)}: {image_path.name}")
                    else:
                        logger.error(f"Failed to download image {i+1}/{len(images)}")
                        success = False

                if downloaded_videos and self.slice_videos:
                    # Frame extraction is CPU-heavy; move to thread to avoid blocking event loop.
                    await asyncio.to_thread(self._slice_videos_for_post, save_dir, downloaded_videos)
            else:
                video_urls = self._get_video_url_candidates(video_info)
                file_path = save_dir / f"{folder_name}.mp4"
                if video_urls and await self._download_first_success(video_urls, file_path, required=True):
                    logger.info(f"Downloaded video: {file_path.name}")
                    if self.slice_videos:
                        await asyncio.to_thread(self._slice_videos_for_post, save_dir, [file_path])
                else:
                    logger.error("Failed to download video after trying all candidate URLs")
                    success = False

                if self.config.get('music', True):
                    music_urls = self._get_music_url_candidates(video_info)
                    if music_urls:
                        music_path = save_dir / f"{folder_name}_music.mp3"
                        await self._download_first_success(music_urls, music_path, required=False)

            if self.config.get('cover', True):
                cover_urls = self._get_cover_url_candidates(video_info)
                if cover_urls:
                    cover_path = save_dir / f"{folder_name}_cover.jpg"
                    await self._download_first_success(cover_urls, cover_path, required=False)

            if self.config.get('json', True):
                json_path = save_dir / f"{folder_name}_data.json"
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(video_info, f, ensure_ascii=False, indent=2)

            return success

        except Exception as e:
            logger.error(f"Download media files failed: {e}")
            return False

    def _unique_urls(self, urls: List[str]) -> List[str]:
        """Remove duplicates and empty urls while preserving order."""
        result: List[str] = []
        seen = set()
        for url in urls or []:
            if not isinstance(url, str):
                continue
            u = url.strip()
            if not u or u in seen:
                continue
            seen.add(u)
            result.append(u)
        return result

    async def _download_first_success(self, urls: List[str], save_path: Path, required: bool = True) -> bool:
        """Try candidate URLs in order and stop at the first success."""
        for idx, url in enumerate(self._unique_urls(urls), start=1):
            if await self._download_file(url, save_path, required=required):
                return True
            logger.warning(f"Candidate URL {idx} failed for {save_path.name}")
        return False

    def _resolve_ffmpeg_path(self) -> Optional[str]:
        """Resolve ffmpeg executable once."""
        if self._ffmpeg_checked:
            return self._ffmpeg_path

        self._ffmpeg_checked = True
        candidate = (self.ffmpeg_bin or 'ffmpeg').strip()
        if not candidate:
            candidate = 'ffmpeg'

        if os.path.isabs(candidate) and os.path.exists(candidate):
            self._ffmpeg_path = candidate
            return self._ffmpeg_path

        resolved = shutil.which(candidate)
        if resolved:
            self._ffmpeg_path = resolved
            return self._ffmpeg_path

        imageio_ffmpeg_path = self._resolve_ffmpeg_via_imageio()
        if imageio_ffmpeg_path:
            self._ffmpeg_path = imageio_ffmpeg_path
            return self._ffmpeg_path

        logger.warning(
            "Clip slicing is enabled but ffmpeg is not found. "
            "Set ffmpeg_bin in config or add ffmpeg to PATH."
        )
        self._ffmpeg_path = None
        return None

    def _resolve_ffmpeg_via_imageio(self) -> Optional[str]:
        """
        Resolve ffmpeg from imageio-ffmpeg package.
        If package is missing and auto_install_ffmpeg is enabled, install it automatically.
        """
        if self._imageio_ffmpeg_checked:
            return None
        self._imageio_ffmpeg_checked = True

        def _get_imageio_ffmpeg_exe() -> Optional[str]:
            try:
                module = importlib.import_module("imageio_ffmpeg")
                exe = module.get_ffmpeg_exe()
                if exe and os.path.exists(exe):
                    return exe
            except Exception:
                return None
            return None

        exe_path = _get_imageio_ffmpeg_exe()
        if exe_path:
            logger.info(f"Using ffmpeg from imageio-ffmpeg: {exe_path}")
            return exe_path

        if not self.auto_install_ffmpeg:
            return None

        logger.info(
            "ffmpeg not found in PATH, installing imageio-ffmpeg into current Python environment..."
        )
        install_cmd = [sys.executable, "-m", "pip", "install", "imageio-ffmpeg"]
        try:
            proc = subprocess.run(
                install_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            logger.warning(f"Auto install imageio-ffmpeg failed to start: {exc}")
            return None

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            logger.warning(
                f"Auto install imageio-ffmpeg failed (exit={proc.returncode}): "
                f"{err[:300] if err else 'unknown error'}"
            )
            return None

        exe_path = _get_imageio_ffmpeg_exe()
        if exe_path:
            logger.info(f"Using auto-installed ffmpeg: {exe_path}")
            return exe_path

        logger.warning("imageio-ffmpeg installed but ffmpeg executable path was not resolved.")
        return None

    def _frame_prefix_for_video(self, video_path: Path, fallback_index: int = 1) -> str:
        """Build stable frame filename prefix for a video file."""
        stem = video_path.stem
        match = re.match(r'^clip_(\d+)$', stem, flags=re.I)
        if match:
            return f"clip_{int(match.group(1)):02d}_"

        safe_stem = re.sub(r'[^0-9A-Za-z_-]+', '_', stem).strip('_')
        if not safe_stem:
            safe_stem = f"{fallback_index:02d}"
        if len(safe_stem) > 40:
            safe_stem = safe_stem[:40]
        return f"video_{safe_stem}_"

    def _slice_single_video_to_jpg(self, video_path: Path, frame_dir: Path, frame_prefix: str) -> int:
        """
        Slice one video into jpg frames.
        Returns the number of extracted frames.
        """
        ffmpeg = self._resolve_ffmpeg_path()
        if not ffmpeg:
            return 0

        if not video_path.exists():
            return 0

        existing = list(frame_dir.glob(f"{frame_prefix}*.jpg"))
        if existing:
            logger.info(f"Skip slicing existing frames: {video_path.name}")
            return len(existing)

        output_pattern = frame_dir / f"{frame_prefix}%06d.jpg"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps={self.slice_fps}",
            str(output_pattern),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            stderr_text = (proc.stderr or "").strip()
            logger.warning(
                f"Video slicing failed for {video_path.name}: "
                f"{stderr_text or f'ffmpeg exit code {proc.returncode}'}"
            )
            return 0

        produced = len(list(frame_dir.glob(f"{frame_prefix}*.jpg")))
        if produced > 0:
            logger.info(
                f"Sliced {video_path.name} at {self.slice_fps} fps -> {produced} frame(s)"
            )
        return produced

    def _slice_videos_for_post(self, save_dir: Path, video_files: List[Path]) -> int:
        """Slice all downloaded videos for one post into one shared subfolder."""
        if not video_files or not self.slice_videos:
            return 0

        frame_dir = save_dir / self.slice_subdir
        frame_dir.mkdir(parents=True, exist_ok=True)

        total_frames = 0
        for idx, video_path in enumerate(video_files, start=1):
            frame_prefix = self._frame_prefix_for_video(video_path, idx)
            total_frames += self._slice_single_video_to_jpg(video_path, frame_dir, frame_prefix)

        if total_frames > 0:
            logger.info(
                f"Video slicing completed, total {total_frames} frame(s) in: {frame_dir}"
            )
        return total_frames

    def _slice_backfill_existing_videos(self) -> int:
        """
        Backfill slicing for existing post folders:
        if a folder contains mp4 files but no slice subfolder, generate frames.
        """
        if not self.slice_videos or not self.slice_backfill_on_run:
            return 0

        if not self.save_path.exists():
            return 0

        folder_to_videos: Dict[Path, List[Path]] = {}
        for mp4_file in self.save_path.rglob("*.mp4"):
            if not mp4_file.is_file():
                continue
            folder_to_videos.setdefault(mp4_file.parent, []).append(mp4_file)

        processed_folders = 0
        total_frames = 0
        for folder, videos in folder_to_videos.items():
            frame_dir = folder / self.slice_subdir
            if frame_dir.exists():
                # User requested: only process folders that do not have slice folder yet.
                continue

            videos_sorted = sorted(videos, key=lambda p: p.name.lower())
            total_frames += self._slice_videos_for_post(folder, videos_sorted)
            processed_folders += 1

        if processed_folders > 0:
            logger.info(
                f"Backfill slicing checked {processed_folders} folder(s), "
                f"generated {total_frames} frame(s)"
            )
        return total_frames

    def _extract_addr_url_candidates(self, addr: Any) -> List[str]:
        """Extract URL candidates from an address object."""
        if not isinstance(addr, dict):
            return []
        url_list = addr.get('url_list') or []
        if not isinstance(url_list, list):
            return []

        candidates: List[str] = []
        for url in url_list:
            if not isinstance(url, str):
                continue
            u = url.strip()
            if not u:
                continue
            if 'playwm' in u:
                candidates.append(u.replace('playwm', 'play'))
            candidates.append(u)
        return self._unique_urls(candidates)

    def _is_watermark_like_url(self, url: str) -> bool:
        """Heuristic check whether an URL is likely watermarked."""
        lowered = (url or '').lower()
        markers = (
            'tplv-dy-water',
            'playwm',
            'watermark',
            'download_suffix_logo',
            '/logo/',
        )
        return any(marker in lowered for marker in markers)

    def _prefer_non_watermark_urls(self, urls: List[str]) -> List[str]:
        """Keep original order but move likely-watermarked URLs to the end."""
        clean: List[str] = []
        fallback: List[str] = []
        for url in self._unique_urls(urls):
            if self._is_watermark_like_url(url):
                fallback.append(url)
            else:
                clean.append(url)
        return clean + fallback

    def _get_image_clip_url_candidates(self, image_info: Dict) -> List[str]:
        """Get candidate clip URLs for image posts that contain video fragments."""
        if not isinstance(image_info, dict):
            return []
        video = image_info.get('video') or {}
        if not isinstance(video, dict):
            return []

        candidates: List[str] = []
        for key in ('play_addr_h264', 'play_addr', 'play_addr_lowbr', 'download_addr', 'download_suffix_logo_addr'):
            candidates.extend(self._extract_addr_url_candidates(video.get(key) or {}))
        return self._prefer_non_watermark_urls(candidates)

    def _get_image_url_candidates(self, image_info: Dict) -> List[str]:
        """Get candidate image URLs in preferred order."""
        if not isinstance(image_info, dict):
            return []
        candidates: List[str] = []
        for key in ('watermark_free_download_url_list', 'url_list', 'download_url_list'):
            value = image_info.get(key) or []
            if isinstance(value, list):
                candidates.extend(value)
        return self._prefer_non_watermark_urls(candidates)

    def _get_video_url_candidates(self, video_info: Dict) -> List[str]:
        """Get candidate video URLs with no-watermark variants first."""
        video = video_info.get('video', {}) or {}
        if not isinstance(video, dict):
            return []

        candidates: List[str] = []
        for key in ('play_addr_h264', 'play_addr', 'play_addr_lowbr', 'download_addr', 'download_suffix_logo_addr'):
            candidates.extend(self._extract_addr_url_candidates(video.get(key) or {}))
        return self._prefer_non_watermark_urls(candidates)

    def _get_music_url_candidates(self, video_info: Dict) -> List[str]:
        music = video_info.get('music', {}) or {}
        play_url = music.get('play_url', {}) or {}
        return self._unique_urls(play_url.get('url_list') or [])

    def _get_cover_url_candidates(self, video_info: Dict) -> List[str]:
        video = video_info.get('video', {}) or {}
        cover = video.get('cover', {}) or {}
        return self._unique_urls(cover.get('url_list') or [])

    def _safe_name(self, text: Any, default: str = "unknown", max_len: int = 80) -> str:
        """Build a Windows-safe file/folder name."""
        if text is None:
            return default
        value = str(text).replace('\r', ' ').replace('\n', ' ').strip()
        if not value:
            return default
        value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', value)
        value = value.rstrip('. ').strip()
        if not value:
            return default
        if len(value) > max_len:
            value = value[:max_len].rstrip()
        return value

    def _get_no_watermark_url(self, video_info: Dict) -> Optional[str]:
        """Compatibility wrapper: return first candidate video URL."""
        candidates = self._get_video_url_candidates(video_info)
        return candidates[0] if candidates else None
    
    def _get_best_quality_url(self, url_list: List[str]) -> Optional[str]:
        """Return preferred URL by quality keywords."""
        if not url_list:
            return None
        
        for keyword in ['1080', 'origin', 'high']:
            for url in url_list:
                if keyword in url:
                    return url

        return url_list[0]
    
    def _get_music_url(self, video_info: Dict) -> Optional[str]:
        """Compatibility wrapper: return first candidate music URL."""
        candidates = self._get_music_url_candidates(video_info)
        return candidates[0] if candidates else None
    
    def _get_cover_url(self, video_info: Dict) -> Optional[str]:
        """Compatibility wrapper: return first candidate cover URL."""
        candidates = self._get_cover_url_candidates(video_info)
        return candidates[0] if candidates else None
    
    async def _download_file(self, url: str, save_path: Path, required: bool = True) -> bool:
        """Download a single URL to file path."""
        try:
            if save_path.exists():
                logger.info(f"Skip existing file: {save_path.name}")
                return True

            # CDN 对请求头较敏感（尤其是 Referer），按常见组合依次回退重试，
            # 避免整页下载时个别图片因 403 直接失败。
            header_variants = []
            header_variants.append(dict(self.headers))
            if 'Cookie' in self.headers:
                no_cookie_headers = dict(self.headers)
                no_cookie_headers.pop('Cookie', None)
                header_variants.append(no_cookie_headers)
            header_variants.append({
                'User-Agent': self.headers.get('User-Agent', 'Mozilla/5.0'),
                'Referer': 'https://www.douyin.com/',
                'Accept': 'image/avif,image/webp,image/apng,image/*,video/*,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Accept-Encoding': 'gzip, deflate',
            })
            header_variants.append({})

            last_status = None
            for index, headers in enumerate(header_variants, start=1):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers) as response:
                            if response.status == 200:
                                content = await response.read()
                                if not content:
                                    logger.warning(f"空响应，尝试下一个请求头组合: {save_path.name}")
                                    last_status = 200
                                    continue
                                with open(save_path, 'wb') as f:
                                    f.write(content)
                                if index > 1:
                                    logger.info(
                                        f"使用第 {index} 组请求头成功下载 {save_path.name}"
                                    )
                                return True
                            last_status = response.status
                            logger.debug(
                                "尝试 %s/%s 失败（HTTP %s）: %s",
                                index, len(header_variants), response.status, url[:90],
                            )
                except Exception as exc:
                    logger.debug("请求头组合 %s 请求异常: %s", index, exc)
                    continue

            if required:
                logger.error(f"Download failed, status code: {last_status}")
            else:
                logger.warning(f"Optional file failed, status code: {last_status}")

        except Exception as e:
            if required:
                logger.error(f"Download failed {url}: {e}")
            else:
                logger.warning(f"Optional download failed {url}: {e}")
            return False
        return False
    
    async def download_user_page(self, url: str) -> bool:
        """Download user page content."""
        try:
            # 提取用户 ID
            user_id = self.extract_id_from_url(url, ContentType.USER)
            if not user_id:
                logger.error(f"无法从URL提取用户ID: {url}")
                return False
            
            console.print(f"\n[cyan]Fetching post list for user {user_id}...[/cyan]")
            
            # 根据 mode 配置决定下载内容（post / like / mix）
            mode = self.config.get('mode', ['post'])
            if isinstance(mode, str):
                mode = [mode]
            
            # 累加统计总数
            total_posts = 0
            if 'post' in mode:
                total_posts += self.config.get('number', {}).get('post', 0) or 1
            if 'like' in mode:
                total_posts += self.config.get('number', {}).get('like', 0) or 1
            if 'mix' in mode:
                total_posts += self.config.get('number', {}).get('allmix', 0) or 1
            
            self.stats.total += total_posts
            
            for m in mode:
                if m == 'post':
                    await self._download_user_posts(user_id)
                elif m == 'like':
                    await self._download_user_likes(user_id)
                elif m == 'mix':
                    await self._download_user_mixes(user_id)
            
            return True
            
        except Exception as e:
            logger.error(f"下载用户主页失败: {e}")
            return False

    async def _process_user_post_aweme(self, aweme: Dict, user_id: str, downloaded: int, progress) -> bool:
        """Download one aweme inside the user-post workflow."""
        if not self._check_time_filter(aweme):
            return False

        if self._should_skip_increment('post', aweme, sec_uid=user_id):
            return False

        task_id = progress.add_task(
            f"Downloading post {downloaded + 1}",
            total=100
        )
        success = await self._download_media_files(aweme, progress)

        if success:
            self.stats.success += 1
            progress.update(task_id, completed=100)
            self._record_increment('post', aweme, sec_uid=user_id)
            return True

        self.stats.failed += 1
        progress.update(task_id, description="[red]Download failed[/red]")
        return False
    
    async def _download_user_posts(self, user_id: str):
        """Download posts published by the user."""
        max_count = self.config.get('number', {}).get('post', 0)
        cursor = 0
        downloaded = 0
        seen_aweme_ids: Set[str] = set()
        expected_post_count = 0

        profile = await self._fetch_user_profile(user_id)
        if profile:
            try:
                expected_post_count = int(profile.get('aweme_count') or 0)
            except Exception:
                expected_post_count = 0
            if expected_post_count:
                logger.info(
                    "User profile reports %s public posts for %s",
                    expected_post_count,
                    profile.get('nickname') or user_id,
                )
        
        console.print("\n[green]Start downloading user posts...[/green]")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=console
        ) as progress:
            
            while True:
                # 限速等待
                await self.rate_limiter.acquire()
                
                # 获取作品列表
                posts_data = await self._fetch_user_posts(user_id, cursor)
                if not posts_data:
                    break
                
                aweme_list = posts_data.get('aweme_list', [])
                if not aweme_list:
                    break
                
                # 逐个下载作品
                for aweme in aweme_list:
                    aweme_id = self._get_aweme_id_from_info(aweme)
                    if aweme_id:
                        if aweme_id in seen_aweme_ids:
                            continue
                        seen_aweme_ids.add(aweme_id)

                    if max_count > 0 and downloaded >= max_count:
                        console.print(f"[yellow]Reached download limit: {max_count}[/yellow]")
                        return

                    success = await self._process_user_post_aweme(aweme, user_id, downloaded, progress)
                    if success:
                        downloaded += 1
                
                # 没有更多数据则结束
                if not posts_data.get('has_more'):
                    break
                
                next_cursor = posts_data.get('max_cursor', 0)
                if next_cursor == cursor:
                    logger.warning("User post api returned the same cursor twice; stop paging")
                    break
                cursor = next_cursor

            should_try_browser_recovery = bool(self.config.get('user_posts_browser_recovery', True))
            # 接口被限流/风控时通常拿不到作品数量，此时也应触发浏览器兜底，
            # 否则「一条都没取到」这一最需要兜底的场景反而不会兜底。
            fetched_short = bool(expected_post_count) and expected_post_count > len(seen_aweme_ids)
            fetched_nothing = not seen_aweme_ids
            under_limit = max_count <= 0 or len(seen_aweme_ids) < max_count
            if should_try_browser_recovery and under_limit and (fetched_short or fetched_nothing):
                if fetched_short:
                    console.print(
                        f"[yellow]API fetched {len(seen_aweme_ids)} / {expected_post_count} posts. "
                        "Starting browser-assisted recovery...[/yellow]"
                    )
                else:
                    console.print(
                        "[yellow]API returned no posts (possible rate limit or expired cookie). "
                        "Starting browser-assisted recovery...[/yellow]"
                    )
                remaining = (max_count - downloaded) if max_count > 0 else 0
                recovered_awemes = await self._recover_user_posts_via_browser(
                    user_id,
                    expected_count=expected_post_count,
                    existing_ids=seen_aweme_ids,
                    limit=remaining,
                )
                logger.info(
                    "Browser-assisted recovery returned %s additional post payloads",
                    len(recovered_awemes),
                )
                for aweme in recovered_awemes:
                    aweme_id = self._get_aweme_id_from_info(aweme)
                    if aweme_id:
                        if aweme_id in seen_aweme_ids:
                            continue
                        seen_aweme_ids.add(aweme_id)

                    if max_count > 0 and downloaded >= max_count:
                        console.print(f"[yellow]Reached download limit: {max_count}[/yellow]")
                        return

                    success = await self._process_user_post_aweme(aweme, user_id, downloaded, progress)
                    if success:
                        downloaded += 1
                if not downloaded:
                    console.print(
                        "[red]浏览器兜底也未取到作品，通常是 Cookie 已过期，"
                        "请重新运行 cookie_extractor.py 后再试。[/red]"
                    )
        
        console.print(f"[green]User posts completed, downloaded {downloaded} item(s).[/green]")
    
    async def _fetch_user_posts(self, user_id: str, cursor: int = 0) -> Optional[Dict]:
        """Fetch user posts with multi-strategy fallback."""
        # Strategy 1: direct web API with X-Bogus (paged).
        platform_variants = [
            ("Win32", "Chrome", "123.0.0.0"),
            ("MacIntel", "Chrome", "122.0.0.0"),
        ]

        cookie_map = self._cookies_to_dict(self.cookies)
        ms_token = cookie_map.get('msToken', '') or ''

        for browser_platform, browser_name, browser_version in platform_variants:
            try:
                params_list = [
                    f'sec_user_id={user_id}',
                    f'max_cursor={cursor}',
                    'count=35',
                    'aid=6383',
                    'device_platform=webapp',
                    'channel=channel_pc_web',
                    'pc_client_type=1',
                    'version_code=170400',
                    'version_name=17.4.0',
                    'cookie_enabled=true',
                    'screen_width=1920',
                    'screen_height=1080',
                    'browser_language=zh-CN',
                    f'browser_platform={browser_platform}',
                    f'browser_name={browser_name}',
                    f'browser_version={browser_version}',
                    'browser_online=true',
                    'engine_name=Blink',
                    f'engine_version={browser_version}',
                    'os_name=Windows',
                    'os_version=10',
                    'cpu_core_num=8',
                    'device_memory=8',
                    'platform=PC',
                    'downlink=10',
                    'effective_type=4g',
                    'round_trip_time=50',
                    'locate_query=false',
                    'show_live_replay_strategy=1',
                    'need_time_list=1',
                    'time_list_query=0',
                    'whale_cut_token=',
                    'cut_version=1',
                    'publish_video_strategy_type=2',
                ]
                if ms_token:
                    params_list.append(f'msToken={quote(ms_token, safe="")}')

                params = '&'.join(params_list)
                api_url = self.urls_helper.USER_POST
                try:
                    signed_params = self.utils.getXbogus(params)
                    full_url = f"{api_url}{signed_params}"
                except Exception as e:
                    logger.warning(f"X-Bogus build failed: {e}; fallback to unsigned request")
                    full_url = f"{api_url}{params}"

                logger.info(
                    f"Request user posts: cursor={cursor}, platform={browser_platform}, url={full_url[:120]}..."
                )
                req_headers = dict(self.headers)
                if cookie_map:
                    req_headers.pop('Cookie', None)
                async with aiohttp.ClientSession(cookies=cookie_map or None) as session:
                    async with session.get(full_url, headers=req_headers, timeout=10) as response:
                        self._absorb_response_cookies(response)
                        body = await response.read()
                        if response.status != 200:
                            logger.warning(f"User post api HTTP error: {response.status}")
                            continue
                        if not body:
                            logger.warning("User post api returned empty body")
                            continue
                        text = body.decode('utf-8', errors='ignore')
                        data = json.loads(text)
                        status_code = data.get('status_code')
                        if status_code != 0:
                            logger.warning(
                                f"User post api failed: status_code={status_code}, status_msg={data.get('status_msg')}"
                            )
                            continue
                        aweme_list = data.get('aweme_list')
                        if aweme_list is None:
                            logger.warning("User post api returned null aweme_list, try next strategy")
                            continue
                        if not isinstance(aweme_list, list):
                            logger.warning(f"aweme_list type invalid: {type(aweme_list)}")
                            continue

                        return {
                            'status_code': 0,
                            'aweme_list': aweme_list,
                            'max_cursor': data.get('max_cursor', cursor),
                            'has_more': bool(data.get('has_more', False)),
                        }
            except Exception as e:
                logger.warning(f"Direct user post api failed (platform={browser_platform}): {e}")

        # Strategy 2: fallback to original Douyin class implementation.
        try:
            from apiproxy.douyin.douyin import Douyin
            import apiproxy.douyin.douyin as douyin_module

            silent_output = io.StringIO()
            cookie_str = self._build_cookie_string()
            if cookie_str:
                douyin_headers['Cookie'] = cookie_str
            dy = Douyin(database=False)
            silent_console = Console(
                file=silent_output,
                force_terminal=False,
                no_color=True,
                emoji=False,
                highlight=False,
            )
            if hasattr(dy, 'console'):
                dy.console = silent_console
            if hasattr(douyin_module, 'console'):
                douyin_module.console = silent_console

            with contextlib.redirect_stdout(silent_output), contextlib.redirect_stderr(silent_output):
                result = dy.getUserInfo(user_id, "post", 35, 0, False, "", "")
            if result is None:
                logger.error("Douyin class returned None")
                return None
            if not isinstance(result, list):
                logger.error(f"Douyin class returned invalid type: {type(result)}")
                return None
            if len(result) == 0:
                logger.warning("Douyin class returned empty list; cookie may be expired or request is blocked")
                return None
            logger.info(f"Douyin class fallback returned {len(result)} items")
            return {
                'status_code': 0,
                'aweme_list': result,
                'max_cursor': cursor,
                'has_more': False,
            }
        except Exception as e:
            logger.exception(f"Fetch user posts failed in fallback strategy: {e}")
            return None

    async def _download_user_likes(self, user_id: str):
        """下载用户点赞的作品"""
        max_count = 0
        try:
            max_count = int(self.config.get('number', {}).get('like', 0))
        except Exception:
            max_count = 0
        cursor = 0
        downloaded = 0

        console.print(f"\n[green]✅ 开始下载用户喜欢的作品...[/green]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=console
        ) as progress:

            while True:
                # 限速等待
                await self.rate_limiter.acquire()

                # 获取点赞列表
                likes_data = await self._fetch_user_likes(user_id, cursor)
                if not likes_data:
                    break

                aweme_list = likes_data.get('aweme_list', [])
                if not aweme_list:
                    break

                # 逐个下载作品
                for aweme in aweme_list:
                    if max_count > 0 and downloaded >= max_count:
                        console.print(f"[yellow]已设置下载数量上限: {max_count}[/yellow]")
                        return

                    if not self._check_time_filter(aweme):
                        continue

                    task_id = progress.add_task(
                        f"下载点赞 {downloaded + 1}",
                        total=100
                    )

                    # 增量判断
                    if self._should_skip_increment('like', aweme, sec_uid=user_id):
                        continue

                    success = await self._download_media_files(aweme, progress)

                    if success:
                        downloaded += 1
                        progress.update(task_id, completed=100)
                        self._record_increment('like', aweme, sec_uid=user_id)
                    else:
                        progress.update(task_id, description="[red]下载失败[/red]")

                # 没有更多数据则结束
                if not likes_data.get('has_more'):
                    break
                cursor = likes_data.get('max_cursor', 0)

        console.print(f"[green]✅ 喜欢作品下载完成，共 {downloaded} 个[/green]")

    async def _fetch_user_likes(self, user_id: str, cursor: int = 0) -> Optional[Dict]:
        """获取用户点赞列表"""
        try:
            params_list = [
                f'sec_user_id={user_id}',
                f'max_cursor={cursor}',
                'count=35',
                'aid=6383',
                'device_platform=webapp',
                'channel=channel_pc_web',
                'pc_client_type=1',
                'version_code=170400',
                'version_name=17.4.0',
                'cookie_enabled=true',
                'screen_width=1920',
                'screen_height=1080',
                'browser_language=zh-CN',
                'browser_platform=MacIntel',
                'browser_name=Chrome',
                'browser_version=122.0.0.0',
                'browser_online=true'
            ]
            params = '&'.join(params_list)

            api_url = self.urls_helper.USER_FAVORITE_A

            try:
                signed_params = self.utils.getXbogus(params)
                full_url = f"{api_url}{signed_params}"
            except Exception as e:
                logger.warning(f"获取X-Bogus失败: {e}, 尝试使用默认X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"请求用户喜欢列表: {full_url[:100]}...")

            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    self._absorb_response_cookies(response)
                    if response.status != 200:
                        logger.error(f"请求失败，HTTP 状态码: {response.status}")
                        return None

                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None

                    data = json.loads(text)
                    if data.get('status_code') == 0:
                        return data
                    else:
                        logger.error(f"API返回错误: {data.get('status_msg', '未知错误')}")
                        return None
        except Exception as e:
            logger.error(f"获取用户喜欢列表失败: {e}")
        return None

    async def _download_user_mixes(self, user_id: str):
        """下载用户的全部合集（先取合集列表，再逐个下载）"""
        max_allmix = 0
        try:
            # 兼容旧键名：allmix 优先，其次 mix
            number_cfg = self.config.get('number', {}) or {}
            max_allmix = int(number_cfg.get('allmix', number_cfg.get('mix', 0)) or 0)
        except Exception:
            max_allmix = 0

        cursor = 0
        fetched = 0

        console.print(f"\n[green]✅ 开始获取用户合集列表...[/green]")
        while True:
            await self.rate_limiter.acquire()
            mix_list_data = await self._fetch_user_mix_list(user_id, cursor)
            if not mix_list_data:
                break

            mix_infos = mix_list_data.get('mix_infos') or []
            if not mix_infos:
                break

            for mix in mix_infos:
                if max_allmix > 0 and fetched >= max_allmix:
                    console.print(f"[yellow]已设置合集下载数量上限: {max_allmix}[/yellow]")
                    return
                mix_id = mix.get('mix_id')
                mix_name = mix.get('mix_name', '')
                console.print(f"[cyan]下载合集[/cyan]: {mix_name} ({mix_id})")
                await self._download_mix_by_id(mix_id)
                fetched += 1

            if not mix_list_data.get('has_more'):
                break
            cursor = mix_list_data.get('cursor', 0)

        console.print(f"[green]✅ 获取合集列表完成，共 {fetched} 个[/green]")

    async def _fetch_user_mix_list(self, user_id: str, cursor: int = 0) -> Optional[Dict]:
        """获取用户合集列表"""
        try:
            params_list = [
                f'sec_user_id={user_id}',
                f'cursor={cursor}',
                'count=35',
                'aid=6383',
                'device_platform=webapp',
                'channel=channel_pc_web',
                'pc_client_type=1',
                'version_code=170400',
                'version_name=17.4.0',
                'cookie_enabled=true',
                'screen_width=1920',
                'screen_height=1080',
                'browser_language=zh-CN',
                'browser_platform=MacIntel',
                'browser_name=Chrome',
                'browser_version=122.0.0.0',
                'browser_online=true'
            ]
            params = '&'.join(params_list)

            api_url = self.urls_helper.USER_MIX_LIST
            try:
                signed_params = self.utils.getXbogus(params)
                full_url = f"{api_url}{signed_params}"
            except Exception as e:
                logger.warning(f"获取X-Bogus失败: {e}, 尝试使用默认X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"请求用户合集列表: {full_url[:100]}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    self._absorb_response_cookies(response)
                    if response.status != 200:
                        logger.error(f"请求失败，HTTP 状态码: {response.status}")
                        return None
                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None
                    data = json.loads(text)
                    if data.get('status_code') == 0:
                        return data
                    else:
                        logger.error(f"API返回错误: {data.get('status_msg', '未知错误')}")
                        return None
        except Exception as e:
            logger.error(f"获取用户合集列表失败: {e}")
        return None

    async def download_mix(self, url: str) -> bool:
        """根据合集链接下载整个合集"""
        try:
            mix_id = None
            for pattern in [r'/collection/(\d+)', r'/mix/detail/(\d+)']:
                m = re.search(pattern, url)
                if m:
                    mix_id = m.group(1)
                    break
            if not mix_id:
                logger.error(f"无法从合集链接提取ID: {url}")
                return False
            await self._download_mix_by_id(mix_id)
            return True
        except Exception as e:
            logger.error(f"下载合集失败: {e}")
            return False

    async def _download_mix_by_id(self, mix_id: str):
        """根据合集 ID 下载合集"""
        cursor = 0
        downloaded = 0

        console.print(f"\n[green]✅ 开始下载合集 {mix_id} ...[/green]")

        while True:
            await self.rate_limiter.acquire()
            data = await self._fetch_mix_awemes(mix_id, cursor)
            if not data:
                break

            aweme_list = data.get('aweme_list') or []
            if not aweme_list:
                break

            for aweme in aweme_list:
                success = await self._download_media_files(aweme)
                if success:
                    downloaded += 1

            if not data.get('has_more'):
                break
            cursor = data.get('cursor', 0)

        console.print(f"[green]✅ 合集下载完成，共 {downloaded} 个[/green]")

    async def _fetch_mix_awemes(self, mix_id: str, cursor: int = 0) -> Optional[Dict]:
        """获取合集作品列表"""
        try:
            params_list = [
                f'mix_id={mix_id}',
                f'cursor={cursor}',
                'count=35',
                'aid=6383',
                'device_platform=webapp',
                'channel=channel_pc_web',
                'pc_client_type=1',
                'version_code=170400',
                'version_name=17.4.0',
                'cookie_enabled=true',
                'screen_width=1920',
                'screen_height=1080',
                'browser_language=zh-CN',
                'browser_platform=MacIntel',
                'browser_name=Chrome',
                'browser_version=122.0.0.0',
                'browser_online=true'
            ]
            params = '&'.join(params_list)

            api_url = self.urls_helper.USER_MIX
            try:
                signed_params = self.utils.getXbogus(params)
                full_url = f"{api_url}{signed_params}"
            except Exception as e:
                logger.warning(f"获取X-Bogus失败: {e}, 尝试使用默认X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"请求合集作品列表: {full_url[:100]}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    self._absorb_response_cookies(response)
                    if response.status != 200:
                        logger.error(f"请求失败，HTTP 状态码: {response.status}")
                        return None
                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None
                    data = json.loads(text)
                    # USER_MIX 接口返回非 0 status_code 时视为失败
                    return data
        except Exception as e:
            logger.error(f"获取合集作品列表失败: {e}")
        return None

    async def download_music(self, url: str) -> bool:
        """根据音乐链接下载该音乐下的全部作品"""
        try:
            # 提取 music_id
            music_id = None
            m = re.search(r'/music/(\d+)', url)
            if m:
                music_id = m.group(1)
            if not music_id:
                logger.error(f"无法从音乐链接提取ID: {url}")
                return False

            cursor = 0
            downloaded = 0
            limit_num = 0
            try:
                limit_num = int((self.config.get('number', {}) or {}).get('music', 0))
            except Exception:
                limit_num = 0

            console.print(f"\n[green]✅ 开始下载音乐 {music_id} 的作品...[/green]")

            while True:
                await self.rate_limiter.acquire()
                data = await self._fetch_music_awemes(music_id, cursor)
                if not data:
                    break
                aweme_list = data.get('aweme_list') or []
                if not aweme_list:
                    break

                for aweme in aweme_list:
                    if limit_num > 0 and downloaded >= limit_num:
                        console.print(f"[yellow]已设置音乐下载数量上限: {limit_num}[/yellow]")
                        return True
                    if self._should_skip_increment('music', aweme, music_id=music_id):
                        continue
                    success = await self._download_media_files(aweme)
                    if success:
                        downloaded += 1
                        self._record_increment('music', aweme, music_id=music_id)

                if not data.get('has_more'):
                    break
                cursor = data.get('cursor', 0)

            console.print(f"[green]✅ 音乐作品下载完成，共 {downloaded} 个[/green]")
            return True
        except Exception as e:
            logger.error(f"下载音乐页失败: {e}")
            return False

    async def _fetch_music_awemes(self, music_id: str, cursor: int = 0) -> Optional[Dict]:
        """获取音乐作品列表"""
        try:
            params_list = [
                f'music_id={music_id}',
                f'cursor={cursor}',
                'count=35',
                'aid=6383',
                'device_platform=webapp',
                'channel=channel_pc_web',
                'pc_client_type=1',
                'version_code=170400',
                'version_name=17.4.0',
                'cookie_enabled=true',
                'screen_width=1920',
                'screen_height=1080',
                'browser_language=zh-CN',
                'browser_platform=MacIntel',
                'browser_name=Chrome',
                'browser_version=122.0.0.0',
                'browser_online=true'
            ]
            params = '&'.join(params_list)

            api_url = self.urls_helper.MUSIC
            try:
                signed_params = self.utils.getXbogus(params)
                full_url = f"{api_url}{signed_params}"
            except Exception as e:
                logger.warning(f"获取X-Bogus失败: {e}, 尝试使用默认X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"请求音乐作品列表: {full_url[:100]}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    self._absorb_response_cookies(response)
                    if response.status != 200:
                        logger.error(f"请求失败，HTTP 状态码: {response.status}")
                        return None
                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None
                    data = json.loads(text)
                    return data
        except Exception as e:
            logger.error(f"获取音乐作品列表失败: {e}")
        return None
    
    def _check_time_filter(self, aweme: Dict) -> bool:
        """判断作品发布时间是否落在配置的时间区间内（按自然日，含首尾两天）"""
        start_time = self.config.get('start_time')
        end_time = self.config.get('end_time')
        
        if not start_time and not end_time:
            return True
        
        raw_create_time = aweme.get('create_time')
        if not raw_create_time:
            return True
        
        create_date = None
        if isinstance(raw_create_time, (int, float)):
            try:
                create_date = datetime.fromtimestamp(raw_create_time)
            except Exception:
                create_date = None
        elif isinstance(raw_create_time, str):
            for fmt in ('%Y-%m-%d %H.%M.%S', '%Y-%m-%d_%H-%M-%S', '%Y-%m-%d %H:%M:%S'):
                try:
                    create_date = datetime.strptime(raw_create_time, fmt)
                    break
                except Exception:
                    pass
        
        if create_date is None:
            return True
        
        # 按自然日比较：结束日期当天的作品仍应包含在内
        create_day = create_date.date()
        if start_time:
            try:
                if create_day < datetime.strptime(str(start_time), '%Y-%m-%d').date():
                    return False
            except ValueError:
                logger.warning(f"start_time 格式无效（应为 YYYY-MM-DD）: {start_time}")
        
        if end_time:
            try:
                if create_day > datetime.strptime(str(end_time), '%Y-%m-%d').date():
                    return False
            except ValueError:
                logger.warning(f"end_time 格式无效（应为 YYYY-MM-DD）: {end_time}")
        
        return True
    
    async def run(self):
        """Run downloader."""
        console.print(Panel.fit(
            "[bold cyan]DyinDL v3.2[/bold cyan]\n"
            "[dim]Supports video, image, user page and mix batch download[/dim]",
            border_style="cyan"
        ))

        await self._initialize_cookies_and_headers()

        if self.slice_backfill_on_run:
            console.print("[cyan]Checking previous downloaded videos for missing slices...[/cyan]")
            await asyncio.to_thread(self._slice_backfill_existing_videos)

        urls = self.config.get('link', [])
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            console.print("[red]No URL found to download[/red]")
            return

        console.print("\n[cyan]Link analysis[/cyan]")
        url_types = {}
        for url in urls:
            content_type = self.detect_content_type(url)
            url_types[url] = content_type
            console.print(f"  - {content_type.upper()}: {url[:50]}...")

        console.print(f"\n[green]Start downloading {len(urls)} link(s)...[/green]\n")

        for i, url in enumerate(urls, 1):
            content_type = url_types[url]
            console.print(f"[{i}/{len(urls)}] Processing: {url}")

            if content_type == ContentType.VIDEO or content_type == ContentType.IMAGE:
                await self.download_single_video(url)
            elif content_type == ContentType.USER:
                await self.download_user_page(url)
                modes = self.config.get('mode', ['post'])
                if 'like' in modes:
                    user_id = self.extract_id_from_url(url, ContentType.USER)
                    if user_id:
                        await self._download_user_likes(user_id)
                if 'mix' in modes:
                    user_id = self.extract_id_from_url(url, ContentType.USER)
                    if user_id:
                        await self._download_user_mixes(user_id)
            elif content_type == ContentType.MIX:
                await self.download_mix(url)
            elif content_type == ContentType.MUSIC:
                await self.download_music(url)
            else:
                console.print(f"[yellow]Unsupported content type: {content_type}[/yellow]")

            console.print(f"Progress: {i}/{len(urls)} | Success: {self.stats.success} | Failed: {self.stats.failed}")
            console.print("-" * 60)

        self._show_stats()
    
    def _show_stats(self):
        """Show download summary stats."""
        console.print("\n" + "=" * 60)
        
        table = Table(title="Download Stats", show_header=True, header_style="bold magenta")
        table.add_column("Item", style="cyan", width=12)
        table.add_column("Value", style="green")
        
        stats = self.stats.to_dict()
        table.add_row("Total", str(stats['total']))
        table.add_row("Success", str(stats['success']))
        table.add_row("Failed", str(stats['failed']))
        table.add_row("Skipped", str(stats['skipped']))
        table.add_row("SuccessRate", stats['success_rate'])
        table.add_row("Elapsed", stats['elapsed_time'])
        
        console.print(table)
        console.print("\n[bold green]Download task completed[/bold green]")


def _browser_recovery_helper_main(input_path: str):
    """Hidden helper entrypoint used by another interpreter that has Playwright."""
    global console
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    console = Console(file=sys.stderr)

    with open(input_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    config_path = payload.get('config_path') or 'config_simple.yml'
    user_id = str(payload.get('user_id') or '').strip()
    expected_count = int(payload.get('expected_count') or 0)
    existing_ids = set(payload.get('existing_ids') or [])

    downloader = UnifiedDownloader(config_path)

    async def runner():
        await downloader._initialize_cookies_and_headers()
        result = await downloader._recover_user_posts_via_local_playwright(
            user_id=user_id,
            expected_count=expected_count,
            existing_ids=existing_ids,
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False))

    asyncio.run(runner())


def _browser_fetch_aweme_helper_main(input_path: str):
    """Hidden helper entrypoint for single-aweme browser recovery."""
    global console
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    console = Console(file=sys.stderr)

    with open(input_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)

    config_path = payload.get('config_path') or 'config_simple.yml'
    url = str(payload.get('url') or '').strip()
    aweme_id = str(payload.get('aweme_id') or '').strip()

    downloader = UnifiedDownloader(config_path)

    async def runner():
        await downloader._initialize_cookies_and_headers()
        result = await downloader._fetch_aweme_via_local_playwright(url, aweme_id)
        sys.stdout.write(json.dumps(result, ensure_ascii=False))

    asyncio.run(runner())


async def _refresh_session_command(config_path: str) -> int:
    """刷新登录态并把 Cookie 写回本地，尽量少让用户重新扫码。

    优先用浏览器真实导航（服务端会趁机下发新的 sessionid / sid_guard），
    浏览器取不到时再退回接口请求。返回 0 表示登录态有效。
    """
    downloader = UnifiedDownloader(config_path)
    await downloader._initialize_cookies_and_headers()

    current = downloader._cookies_to_dict(downloader.cookies)
    before = downloader._cookie_expiry_at(current)

    console.print("[cyan]正在刷新登录态…[/cyan]")
    refreshed: Dict[str, str] = {}
    try:
        refreshed = await downloader._fetch_cookies_via_browser()
    except Exception as exc:
        logger.warning(f"浏览器刷新登录态失败: {exc}")

    login_keys = ('sessionid', 'sessionid_ss', 'sid_tt', 'passport_auth_status')
    if refreshed and any(refreshed.get(k) for k in login_keys):
        current.update(refreshed)
        downloader.cookies = current
        downloader.headers['Cookie'] = downloader._build_cookie_string()
        downloader._persist_cookies(current)
        console.print("[green]已通过浏览器刷新并保存登录态[/green]")
    else:
        console.print("[yellow]浏览器中未取到登录态，尝试通过接口请求刷新…[/yellow]")
        urls = downloader.config.get('link') or []
        if isinstance(urls, str):
            urls = [urls]
        touched = False
        for url in list(urls)[:1]:
            try:
                if downloader.detect_content_type(url) == ContentType.USER:
                    sec_uid = downloader.extract_id_from_url(url, ContentType.USER)
                    if sec_uid:
                        await downloader._fetch_user_profile(sec_uid)
                        touched = True
                else:
                    aweme_id = downloader.extract_id_from_url(url)
                    if aweme_id:
                        await downloader._fetch_video_info(aweme_id)
                        touched = True
            except Exception as exc:
                logger.warning(f"接口刷新失败: {exc}")
        if touched:
            current = downloader._cookies_to_dict(downloader.cookies)
            downloader._persist_cookies(current)
            console.print("[green]已通过接口请求刷新 Cookie 并保存[/green]")

    current = downloader._cookies_to_dict(downloader.cookies)
    after = downloader._cookie_expiry_at(current)
    if after and (before is None or after > before):
        gain = (after - (before or 0)) / 86400
        console.print(f"[green]登录态已延长约 {gain:.1f} 天[/green]")

    downloader._report_session_validity(current)
    hint = downloader._cookie_expiry_hint(current)
    if hint:
        console.print(f"[yellow]{hint}[/yellow]")
        console.print(
            "[yellow]如仍需登录，请运行：python cookie_extractor.py[/yellow]"
        )
        return 1
    console.print("[green]登录态有效，无需重新登录。[/green]")
    return 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='DyinDL - Douyin content downloader',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        '-c', '--config',
        default='config.yml',
        help='Config file path (default: config.yml, fallback: config_simple.yml)'
    )
    
    parser.add_argument(
        '-u', '--url',
        nargs='+',
        help='Direct URLs to download'
    )
    parser.add_argument(
        '-p', '--path',
        default=None,
        help='Output path (overrides config)'
    )
    parser.add_argument(
        '--auto-cookie',
        action='store_true',
        help='Auto fetch cookies (requires Playwright)'
    )
    parser.add_argument(
        '--cookie',
        help='Manual cookie string, e.g. "msToken=xxx; ttwid=yyy"'
    )
    parser.add_argument(
        '--slice-backfill',
        dest='slice_backfill_on_run',
        action='store_true',
        help='Before downloading, backfill slicing for old folders that have videos but no slice folder'
    )
    parser.add_argument(
        '--no-slice-backfill',
        dest='slice_backfill_on_run',
        action='store_false',
        help='Disable pre-download backfill slicing check'
    )
    parser.add_argument(
        '--refresh-session',
        action='store_true',
        help='刷新并保存登录态 Cookie（尽量延长免登录时间），不下载任何内容',
    )
    parser.add_argument(
        '--browser-recover-user-posts-helper',
        dest='browser_recover_helper_input',
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        '--browser-fetch-aweme-helper',
        dest='browser_fetch_aweme_helper_input',
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(slice_backfill_on_run=None)
    
    args = parser.parse_args()

    if args.browser_recover_helper_input:
        _browser_recovery_helper_main(args.browser_recover_helper_input)
        return
    if args.browser_fetch_aweme_helper_input:
        _browser_fetch_aweme_helper_main(args.browser_fetch_aweme_helper_input)
        return
    if args.refresh_session:
        raise SystemExit(asyncio.run(_refresh_session_command(args.config)))
    
    # 组装临时配置（命令行参数优先）
    temp_config = {}
    if args.url:
        temp_config['link'] = args.url
    
    # 输出目录覆盖
    if args.path:
        temp_config['path'] = args.path
    
    # Cookie 覆盖
    if args.auto_cookie:
        temp_config['auto_cookie'] = True
        temp_config['cookies'] = 'auto'
    if args.cookie:
        temp_config['cookies'] = args.cookie
        temp_config['auto_cookie'] = False
    if args.slice_backfill_on_run is not None:
        temp_config['slice_backfill_on_run'] = args.slice_backfill_on_run
    
    # 存在配置文件时，读取后与命令行参数合并
    if temp_config:
        # 写出临时配置文件，交给 UnifiedDownloader 读取
        file_config = {}
        if os.path.exists(args.config):
            try:
                with open(args.config, 'r', encoding='utf-8') as f:
                    file_config = yaml.safe_load(f) or {}
            except Exception:
                file_config = {}
        
        # 兼容旧键名
        if 'links' in file_config and 'link' not in file_config:
            file_config['link'] = file_config['links']
        if 'output_dir' in file_config and 'path' not in file_config:
            file_config['path'] = file_config['output_dir']
        if 'cookie' in file_config and 'cookies' not in file_config:
            file_config['cookies'] = file_config['cookie']
        
        merged = {**(file_config or {}), **temp_config}
        with open('temp_config.yml', 'w', encoding='utf-8') as f:
            yaml.dump(merged, f, allow_unicode=True)
        config_path = 'temp_config.yml'
    else:
        config_path = args.config
    
    # 执行下载
    try:
        downloader = UnifiedDownloader(config_path)
        asyncio.run(downloader.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹ 用户中断了下载[/yellow]")
    except Exception as e:
        console.print(f"\n[red]❌ 运行异常: {e}[/red]")
        logger.exception("运行异常")
    finally:
        # 清理临时配置文件
        if args.url and os.path.exists('temp_config.yml'):
            os.remove('temp_config.yml')


if __name__ == '__main__':
    main()





