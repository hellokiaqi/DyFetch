#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
閹舵牠鐓舵稉瀣祰閸?- 缂佺喍绔存晶鐐插繁閻?
閺€顖涘瘮鐟欏棝顣堕妴浣告禈閺傚洢鈧胶鏁ら幋铚傚瘜妞ょ偣鈧礁鎮庨梿鍡欑搼婢舵氨顫掗崘鍛啇閻ㄥ嫭澹掗柌蹇庣瑓鏉?
"""

import asyncio
import json
import logging
import os
import pickle
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse, quote
import argparse
import yaml

# 缁楊兛绗侀弬鐟扮氨
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
    print(f"鐠囧嘲鐣ㄧ憗鍛箑鐟曚胶娈戞笟婵婄: pip install aiohttp requests rich pyyaml")
    sys.exit(1)

# 濞ｈ濮炴い鍦窗鐠侯垰绶?
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 鐎电厧鍙嗘い鍦窗濡€虫健
from apiproxy.douyin import douyin_headers
from apiproxy.douyin.urls import Urls
from apiproxy.douyin.result import Result
from apiproxy.common.utils import Utils
from apiproxy.douyin.auth.cookie_manager import AutoCookieManager
from apiproxy.douyin.database import DataBase

# 闁板秶鐤嗛弮銉ョ箶
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
    """閸愬懎顔愮猾璇茬€烽弸姘"""
    VIDEO = "video"
    IMAGE = "image" 
    USER = "user"
    MIX = "mix"
    MUSIC = "music"
    LIVE = "live"


class DownloadStats:
    """娑撳娴囩紒鐔活吀"""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.start_time = time.time()
    
    @property
    def success_rate(self):
        return (self.success / self.total * 100) if self.total > 0 else 0
    
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
    """闁喓宸奸梽鎰煑閸?"""
    def __init__(self, max_per_second: float = 2):
        self.max_per_second = max_per_second
        self.min_interval = 1.0 / max_per_second
        self.last_request = 0
    
    async def acquire(self):
        """閼惧嘲褰囩拋绋垮讲"""
        current = time.time()
        time_since_last = current - self.last_request
        if time_since_last < self.min_interval:
            await asyncio.sleep(self.min_interval - time_since_last)
        self.last_request = time.time()


class RetryManager:
    """闁插秷鐦粻锛勬倞閸?"""
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self.retry_delays = [1, 2, 5]  # 闁插秷鐦鎯扮箿
    
    async def execute_with_retry(self, func, *args, **kwargs):
        """閹笛嗩攽閸戣姤鏆熼獮鎯板殰閸斻劑鍣哥拠?"""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                    logger.warning(f"缁?{attempt + 1} 濞嗏€崇毦鐠囨洖銇戠拹? {e}, {delay}缁夋帒鎮楅柌宥堢槸...")
                    await asyncio.sleep(delay)
        raise last_error


class UnifiedDownloader:
    """缂佺喍绔存稉瀣祰閸?"""
    
    def __init__(self, config_path: str = "config.yml"):
        self.config = self._load_config(config_path)
        self.urls_helper = Urls()
        self.result_helper = Result()
        self.utils = Utils()
        
        # 缂佸嫪娆㈤崚婵嗩潗閸?
        self.stats = DownloadStats()
        self.rate_limiter = RateLimiter(max_per_second=2)
        self.retry_manager = RetryManager(max_retries=self.config.get('retry_times', 3))
        
        # Cookie娑撳氦顕Ч鍌氥仈閿涘牆娆㈡潻鐔峰灥婵瀵查敍灞炬暜閹镐浇鍤滈崝銊ㄥ箯閸欐牭绱?
        self.cookies = self.config.get('cookies') if 'cookies' in self.config else self.config.get('cookie')
        self.auto_cookie = bool(self.config.get('auto_cookie')) or (isinstance(self.config.get('cookie'), str) and self.config.get('cookie') == 'auto') or (isinstance(self.config.get('cookies'), str) and self.config.get('cookies') == 'auto')
        self.headers = {**douyin_headers}
        # 闁灝鍘ら張宥呭缁旑垯濞囬悽鈺瀝otli鐎佃壈鍤iohttp閺冪姵纭剁憴锝呭竾閿涘牊婀€瑰顥奲rotli鎼存挻妞傛导姘毉閻滄壆鈹栭崫宥呯安閿?
        self.headers['accept-encoding'] = 'gzip, deflate'
        # 婢х偤鍣烘稉瀣祰娑撳孩鏆熼幑顔肩氨
        self.increase_cfg: Dict[str, Any] = self.config.get('increase', {}) or {}
        self.enable_database: bool = bool(self.config.get('database', True))
        self.db: Optional[DataBase] = DataBase() if self.enable_database else None
        
        # 娣囨繂鐡ㄧ捄顖氱窞
        self.save_path = Path(self.config.get('path', './Downloaded'))
        self.save_path.mkdir(parents=True, exist_ok=True)
        
    def _load_config(self, config_path: str) -> Dict:
        """閸旂姾娴囬柊宥囩枂閺傚洣娆?"""
        if not os.path.exists(config_path):
            # 閸忕厧顔愰柊宥囩枂閺傚洣娆㈤崨钘夋倳閿涙矮绱崗?config.yml閿涘苯鍙惧▎?config_simple.yml
            alt_path = 'config_simple.yml'
            if os.path.exists(alt_path):
                config_path = alt_path
            else:
                # 鏉╂柨娲栨稉鈧稉顏嗏敄闁板秶鐤嗛敍宀€鏁遍崨鎴掓姢鐞涘苯寮弫鏉垮枀鐎?
                return {}
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 缁犫偓閸栨牠鍘ょ純顔煎悑鐎圭櫢绱發inks/link, output_dir/path, cookie/cookies
        if 'links' in config and 'link' not in config:
            config['link'] = config['links']
        if 'output_dir' in config and 'path' not in config:
            config['path'] = config['output_dir']
        if 'cookie' in config and 'cookies' not in config:
            config['cookies'] = config['cookie']
        if isinstance(config.get('cookies'), str) and config.get('cookies') == 'auto':
            config['auto_cookie'] = True
        
        # 閸忎浇顔忛弮?link閿涘牓鈧俺绻冮崨鎴掓姢鐞涘奔绱堕崗銉礆
        # 婵″倹鐏夋稉銈堚偓鍛村厴濞屸剝婀侀敍灞芥倵缂侇厺绱伴崷銊ㄧ箥鐞涘本妞傞幓鎰仛
        
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

        if cookie_map:
            self.cookies = cookie_map
            cookie_str = '; '.join([f'{k}={v}' for k, v in cookie_map.items() if k and v is not None])
            self.headers['Cookie'] = cookie_str
            from apiproxy.douyin import douyin_headers
            douyin_headers['Cookie'] = cookie_str
            return

        if self.auto_cookie:
            try:
                console.print("[cyan]Auto fetching cookies...[/cyan]")
                async with AutoCookieManager(cookie_file='cookies.pkl', headless=False) as cm:
                    cookies_list = await cm.get_cookies()
                    if cookies_list:
                        cookie_map = self._cookies_to_dict(cookies_list)
                        self.cookies = cookie_map
                        cookie_str = '; '.join([f'{k}={v}' for k, v in cookie_map.items() if k and v is not None])
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
        """濡偓濞村┚RL閸愬懎顔愮猾璇茬€?"""
        if '/user/' in url:
            return ContentType.USER
        elif '/video/' in url or 'v.douyin.com' in url:
            return ContentType.VIDEO
        elif '/note/' in url:
            return ContentType.IMAGE
        elif '/collection/' in url or '/mix/' in url:
            return ContentType.MIX
        elif '/music/' in url:
            return ContentType.MUSIC
        elif 'live.douyin.com' in url:
            return ContentType.LIVE
        else:
            return ContentType.VIDEO  # 姒涙顓昏ぐ鎾茬稊鐟欏棝顣?
    
    async def resolve_short_url(self, url: str) -> str:
        """鐟欙絾鐎介惌顓㈡懠閹?"""
        if 'v.douyin.com' in url:
            try:
                # 娴ｈ法鏁ら崥灞绢劄鐠囬攱鐪伴懢宄板絿闁插秴鐣鹃崥?
                response = requests.get(url, headers=self.headers, allow_redirects=True, timeout=10)
                final_url = response.url
                logger.info(f"鐟欙絾鐎介惌顓㈡懠閹? {url} -> {final_url}")
                return final_url
            except Exception as e:
                logger.warning(f"鐟欙絾鐎介惌顓㈡懠閹恒儱銇戠拹? {e}")
        return url
    
    def extract_id_from_url(self, url: str, content_type: ContentType = None) -> Optional[str]:
        """娴犲抖RL閹绘劕褰嘔D
        
        Args:
            url: 鐟曚浇袙閺嬫劗娈慤RL
            content_type: 閸愬懎顔愮猾璇茬€烽敍鍫濆讲闁绱濋悽銊ょ艾閹稿洤顕遍幓鎰絿閿?
        """
        # 婵″倹鐏夊鑼叀閺勵垳鏁ら幋鐑姐€夐棃顫礉閻╁瓨甯撮幓鎰絿閻劍鍩汭D
        if content_type == ContentType.USER or '/user/' in url:
            user_patterns = [
                r'/user/([\w.-]+)',
                r'sec_uid=([\w.-]+)'
            ]
            
            for pattern in user_patterns:
                match = re.search(pattern, url)
                if match:
                    user_id = match.group(1)
                    logger.info(f"閹绘劕褰囬崚鎵暏閹寸īD: {user_id}")
                    return user_id
        
        # 鐟欏棝顣禝D濡€崇础閿涘牅绱崗鍫礆
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
                logger.info(f"閹绘劕褰囬崚鎷岊潒妫版厲D: {video_id}")
                return video_id
        
        # 閸忔湹绮Ο鈥崇础
        other_patterns = [
            r'/collection/(\d+)',
            r'/music/(\d+)'
        ]
        
        for pattern in other_patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        # 鐏忔繆鐦禒宥禦L娑擃厽褰侀崣鏍ㄦ殶鐎涙“D
        number_match = re.search(r'(\d{15,20})', url)
        if number_match:
            video_id = number_match.group(1)
            logger.info(f"娴犲抖RL閹绘劕褰囬崚鐗堟殶鐎涙“D: {video_id}")
            return video_id
        
        logger.error(f"閺冪姵纭舵禒宥禦L閹绘劕褰嘔D: {url}")
        return None

    def _get_aweme_id_from_info(self, info: Dict) -> Optional[str]:
        """娴?aweme 娣団剝浼呮稉顓熷絹閸?aweme_id"""
        try:
            if 'aweme_id' in info:
                return str(info.get('aweme_id'))
            # aweme_detail 缂佹挻鐎?
            return str(info.get('aweme', {}).get('aweme_id') or info.get('aweme_id'))
        except Exception:
            return None

    def _get_sec_uid_from_info(self, info: Dict) -> Optional[str]:
        """娴?aweme 娣団剝浼呮稉顓熷絹閸欐牔缍旈懓?sec_uid"""
        try:
            return info.get('author', {}).get('sec_uid')
        except Exception:
            return None

    def _should_skip_increment(self, context: str, info: Dict, mix_id: Optional[str] = None, music_id: Optional[str] = None, sec_uid: Optional[str] = None) -> bool:
        """閺嶈宓佹晶鐐哄櫤闁板秶鐤嗘稉搴㈡殶閹诡喖绨辩拋鏉跨秿閸掋倖鏌囬弰顖氭儊鐠哄疇绻冩稉瀣祰"""
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
        """娑撳娴囬幋鎰閸氬骸鍟撻崗銉︽殶閹诡喖绨辩拋鏉跨秿"""
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
        """娑撳娴囬崡鏇氶嚋鐟欏棝顣?閸ョ偓鏋?"""
        try:
            # 鐟欙絾鐎介惌顓㈡懠閹?
            url = await self.resolve_short_url(url)
            
            # 閹绘劕褰嘔D
            video_id = self.extract_id_from_url(url, ContentType.VIDEO)
            if not video_id:
                logger.error(f"閺冪姵纭舵禒宥禦L閹绘劕褰嘔D: {url}")
                return False
            
            # 婵″倹鐏夊▽鈩冩箒閹绘劕褰囬崚鎷岊潒妫版厲D閿涘苯鐨剧拠鏇氱稊娑撻缚顫嬫０鎱朌閻╁瓨甯存担璺ㄦ暏
            if not video_id and '/user/' not in url:
                # 閸欘垵鍏橀惌顓㈡懠閹恒儳娲块幒銉ュ瘶閸氼偂绨＄憴鍡涱暥ID
                video_id = url.split('/')[-2] if url.endswith('/') else url.split('/')[-1]
                logger.info(f"鐏忔繆鐦禒搴ｇ叚闁剧偓甯寸捄顖氱窞閹绘劕褰嘔D: {video_id}")
            
            if not video_id:
                logger.error(f"閺冪姵纭舵禒宥禦L閹绘劕褰囩憴鍡涱暥ID: {url}")
                return False
            
            # 闂勬劙鈧?
            await self.rate_limiter.acquire()
            
            # 閼惧嘲褰囩憴鍡涱暥娣団剝浼?
            if progress:
                progress.update(task_id=progress.task_ids[-1], description="閼惧嘲褰囩憴鍡涱暥娣団剝浼?..")
            
            video_info = await self.retry_manager.execute_with_retry(
                self._fetch_video_info, video_id
            )
            
            if not video_info:
                logger.error(f"閺冪姵纭堕懢宄板絿鐟欏棝顣舵穱鈩冧紖: {video_id}")
                self.stats.failed += 1
                return False
            
            # 娑撳娴囩憴鍡涱暥閺傚洣娆?
            if progress:
                progress.update(task_id=progress.task_ids[-1], description="娑撳娴囩憴鍡涱暥閺傚洣娆?..")
            
            success = await self._download_media_files(video_info, progress)
            
            if success:
                self.stats.success += 1
                logger.info(f"閴?娑撳娴囬幋鎰: {url}")
            else:
                self.stats.failed += 1
                logger.error(f"閴?娑撳娴囨径杈Е: {url}")
            
            return success
            
        except Exception as e:
            logger.error(f"娑撳娴囩憴鍡涱暥瀵倸鐖?{url}: {e}")
            self.stats.failed += 1
            return False
        finally:
            self.stats.total += 1
    
    async def _fetch_video_info(self, video_id: str) -> Optional[Dict]:
        """閼惧嘲褰囩憴鍡涱暥娣団剝浼?"""
        try:
            # 閻╁瓨甯存担璺ㄦ暏 DouYinCommand.py 娑擃厽鍨氶崝鐔烘畱 Douyin 缁?
            from apiproxy.douyin.douyin import Douyin
            
            # 閸掓稑缂?Douyin 鐎圭偘绶?
            dy = Douyin(database=False)
            
            # 鐠佸墽鐤嗛幋鎴滄粦閻?cookies 閸?douyin_headers
            if hasattr(self, 'cookies') and self.cookies:
                cookie_str = self._build_cookie_string()
                if cookie_str:
                    from apiproxy.douyin import douyin_headers
                    douyin_headers['Cookie'] = cookie_str
                    logger.info(f"鐠佸墽鐤?Cookie 閸?Douyin 缁? {cookie_str[:100]}...")
            
            try:
                # 娴ｈ法鏁ら悳鐗堟箒閻ㄥ嫭鍨氶崝鐔风杽閻?
                result = dy.getAwemeInfo(video_id)
                if result:
                    logger.info(f"Douyin 缁粯鍨氶崝鐔诲箯閸欐牞顫嬫０鎴滀繆閹? {result.get('desc', '')[:30]}")
                    return result
                else:
                    logger.error("Douyin class returned empty result")
                    
            except Exception as e:
                logger.error(f"Douyin 缁槒骞忛崣鏍潒妫版垳淇婇幁顖氥亼鐠? {e}")
                
        except Exception as e:
            logger.error(f"鐎电厧鍙嗛幋鏍﹀▏閻?Douyin 缁銇戠拹? {e}")
            import traceback
            traceback.print_exc()
        
        # 婵″倹鐏?Douyin 缁銇戠拹銉礉鐏忔繆鐦径鍥╂暏閹恒儱褰涢敍鍧昬sdouyin閿涘本妫ら棁鈧琗-Bogus閿?
        try:
            fallback_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
            logger.info(f"鐏忔繆鐦径鍥╂暏閹恒儱褰涢懢宄板絿鐟欏棝顣舵穱鈩冧紖: {fallback_url}")
            
            # 鐠佸墽鐤嗛弴鎾偓姘辨暏閻ㄥ嫯顕Ч鍌氥仈
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
                    logger.info(f"婢跺洨鏁ら幒銉ュ經閸濆秴绨查悩鑸碘偓? {response.status}")
                    if response.status != 200:
                        logger.error(f"婢跺洨鏁ら幒銉ュ經鐠囬攱鐪版径杈Е閿涘瞼濮搁幀浣虹垳: {response.status}")
                        return None
                    
                    text = await response.text()
                    logger.info(f"婢跺洨鏁ら幒銉ュ經閸濆秴绨查崘鍛啇闂€鍨: {len(text)}")
                    
                    if not text:
                        logger.error("婢跺洨鏁ら幒銉ュ經閸濆秴绨叉稉铏光敄")
                        return None
                    
                    try:
                        data = json.loads(text)
                        logger.info(f"婢跺洨鏁ら幒銉ュ經鏉╂柨娲栭弫鐗堝祦: {data}")
                        
                        item_list = (data or {}).get('item_list') or []
                        if item_list:
                            aweme_detail = item_list[0]
                            logger.info("婢跺洨鏁ら幒銉ュ經閹存劕濮涢懢宄板絿鐟欏棝顣舵穱鈩冧紖")
                            return aweme_detail
                        else:
                            logger.error("婢跺洨鏁ら幒銉ュ經鏉╂柨娲栭惃鍕殶閹诡喕鑵戝▽鈩冩箒 item_list")
                            
                    except json.JSONDecodeError as e:
                        logger.error(f"婢跺洨鏁ら幒銉ュ經JSON鐟欙絾鐎芥径杈Е: {e}")
                        logger.error(f"閸樼喎顫愰崫宥呯安閸愬懎顔? {text}")
                        return None
                        
        except Exception as e:
            logger.error(f"婢跺洨鏁ら幒銉ュ經閼惧嘲褰囩憴鍡涱暥娣団剝浼呮径杈Е: {e}")
        
        return None
    
    def _build_detail_params(self, aweme_id: str) -> str:
        """閺嬪嫬缂撶拠锔藉剰API閸欏倹鏆?"""
        # 娴ｈ法鏁ゆ稉搴ｅ箛閺?douyinapi.py 閻╃鎮撻惃鍕棘閺佺増鐗稿?
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

            folder_name = f"{create_time}_{desc}" if desc else create_time
            folder_name = self._safe_name(folder_name, default=create_time, max_len=120)
            save_dir = self.save_path / author_name / folder_name
            save_dir.mkdir(parents=True, exist_ok=True)

            success = True

            if is_image:
                images = video_info.get('images', [])
                for i, image_info in enumerate(images):
                    clip_urls = self._get_image_clip_url_candidates(image_info)
                    if clip_urls:
                        clip_path = save_dir / f"clip_{i+1}.mp4"
                        if await self._download_first_success(clip_urls, clip_path, required=False):
                            logger.info(f"Downloaded clip {i+1}/{len(images)}: {clip_path.name}")
                            continue
                        logger.warning(f"Clip {i+1}/{len(images)} failed, fallback to image")

                    image_urls = self._get_image_url_candidates(image_info)
                    image_path = save_dir / f"image_{i+1}.jpg"
                    if image_urls and await self._download_first_success(image_urls, image_path, required=True):
                        logger.info(f"Downloaded image {i+1}/{len(images)}: {image_path.name}")
                    else:
                        logger.error(f"Failed to download image {i+1}/{len(images)}")
                        success = False
            else:
                video_urls = self._get_video_url_candidates(video_info)
                file_path = save_dir / f"{folder_name}.mp4"
                if video_urls and await self._download_first_success(video_urls, file_path, required=True):
                    logger.info(f"Downloaded video: {file_path.name}")
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

            header_variants = []
            header_variants.append(dict(self.headers))
            if 'Cookie' in self.headers:
                no_cookie_headers = dict(self.headers)
                no_cookie_headers.pop('Cookie', None)
                header_variants.append(no_cookie_headers)

            for headers in header_variants:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            content = await response.read()
                            with open(save_path, 'wb') as f:
                                f.write(content)
                            return True
                        if required:
                            logger.error(f"Download failed, status code: {response.status}")
                        else:
                            logger.warning(f"Optional file failed, status code: {response.status}")

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
            # 閹绘劕褰囬悽銊﹀煕ID
            user_id = self.extract_id_from_url(url, ContentType.USER)
            if not user_id:
                logger.error(f"閺冪姵纭舵禒宥禦L閹绘劕褰囬悽銊﹀煕ID: {url}")
                return False
            
            console.print(f"\n[cyan]Fetching post list for user {user_id}...[/cyan]")
            
            # 閺嶈宓侀柊宥囩枂娑撳娴囨稉宥呮倱缁鐎烽惃鍕敶鐎?
            mode = self.config.get('mode', ['post'])
            if isinstance(mode, str):
                mode = [mode]
            
            # 婢х偛濮為幀璁虫崲閸斺剝鏆熺紒鐔活吀
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
            logger.error(f"娑撳娴囬悽銊﹀煕娑撳銆夋径杈Е: {e}")
            return False
    
    async def _download_user_posts(self, user_id: str):
        """Download posts published by the user."""
        max_count = self.config.get('number', {}).get('post', 0)
        cursor = 0
        downloaded = 0
        
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
                # 闂勬劙鈧?
                await self.rate_limiter.acquire()
                
                # 閼惧嘲褰囨担婊冩惂閸掓銆?
                posts_data = await self._fetch_user_posts(user_id, cursor)
                if not posts_data:
                    break
                
                aweme_list = posts_data.get('aweme_list', [])
                if not aweme_list:
                    break
                
                # 娑撳娴囨担婊冩惂
                for aweme in aweme_list:
                    if max_count > 0 and downloaded >= max_count:
                        console.print(f"[yellow]Reached download limit: {max_count}[/yellow]")
                        return
                    
                    # 閺冨爼妫挎潻鍥ㄦ姢
                    if not self._check_time_filter(aweme):
                        continue
                    
                    # 閸掓稑缂撴稉瀣祰娴犺濮?
                    task_id = progress.add_task(
                        f"Downloading post {downloaded + 1}",
                        total=100
                    )
                    
                    # 婢х偤鍣洪崚銈嗘焽
                    if self._should_skip_increment('post', aweme, sec_uid=user_id):
                        continue
                    
                    # 娑撳娴?
                    success = await self._download_media_files(aweme, progress)
                    
                    if success:
                        downloaded += 1
                        self.stats.success += 1  # 婢х偛濮為幋鎰鐠佲剝鏆?
                        progress.update(task_id, completed=100)
                        self._record_increment('post', aweme, sec_uid=user_id)
                    else:
                        self.stats.failed += 1  # 婢х偛濮炴径杈Е鐠佲剝鏆?
                        progress.update(task_id, description="[red]Download failed[/red]")
                
                # 濡偓閺屻儲妲搁崥锔芥箒閺囨潙顦?
                if not posts_data.get('has_more'):
                    break
                
                cursor = posts_data.get('max_cursor', 0)
        
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
            cookie_str = self._build_cookie_string()
            if cookie_str:
                douyin_headers['Cookie'] = cookie_str
            dy = Douyin(database=False)
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
            logger.error(f"Fetch user posts failed in fallback strategy: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def _download_user_likes(self, user_id: str):
        """娑撳娴囬悽銊﹀煕閸犳粍顐介惃鍕稊閸?"""
        max_count = 0
        try:
            max_count = int(self.config.get('number', {}).get('like', 0))
        except Exception:
            max_count = 0
        cursor = 0
        downloaded = 0

        console.print(f"\n[green]瀵偓婵绗呮潪鐣屾暏閹村嘲鏋╁▎銏㈡畱娴ｆ粌鎼?..[/green]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeRemainingColumn(),
            console=console
        ) as progress:

            while True:
                # 闂勬劙鈧?
                await self.rate_limiter.acquire()

                # 閼惧嘲褰囬崰婊勵偨閸掓銆?
                likes_data = await self._fetch_user_likes(user_id, cursor)
                if not likes_data:
                    break

                aweme_list = likes_data.get('aweme_list', [])
                if not aweme_list:
                    break

                # 娑撳娴囨担婊冩惂
                for aweme in aweme_list:
                    if max_count > 0 and downloaded >= max_count:
                        console.print(f"[yellow]瀹歌尪鎻崚棰佺瑓鏉炶姤鏆熼柌蹇涙閸? {max_count}[/yellow]")
                        return

                    if not self._check_time_filter(aweme):
                        continue

                    task_id = progress.add_task(
                        f"娑撳娴囬崰婊勵偨 {downloaded + 1}",
                        total=100
                    )

                    # 婢х偤鍣洪崚銈嗘焽
                    if self._should_skip_increment('like', aweme, sec_uid=user_id):
                        continue

                    success = await self._download_media_files(aweme, progress)

                    if success:
                        downloaded += 1
                        progress.update(task_id, completed=100)
                        self._record_increment('like', aweme, sec_uid=user_id)
                    else:
                        progress.update(task_id, description="[red]娑撳娴囨径杈Е[/red]")

                # 缂堝銆?
                if not likes_data.get('has_more'):
                    break
                cursor = likes_data.get('max_cursor', 0)

        console.print(f"[green]閴?閸犳粍顐芥担婊冩惂娑撳娴囩€瑰本鍨氶敍灞藉彙娑撳娴?{downloaded} 娑撶實/green]")

    async def _fetch_user_likes(self, user_id: str, cursor: int = 0) -> Optional[Dict]:
        """閼惧嘲褰囬悽銊﹀煕閸犳粍顐介惃鍕稊閸濅礁鍨悰?"""
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
                logger.warning(f"閼惧嘲褰嘪-Bogus婢惰精瑙? {e}, 鐏忔繆鐦稉宥呯敨X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"鐠囬攱鐪伴悽銊﹀煕閸犳粍顐介崚妤勩€? {full_url[:100]}...")

            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    if response.status != 200:
                        logger.error(f"鐠囬攱鐪版径杈Е閿涘瞼濮搁幀浣虹垳: {response.status}")
                        return None

                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None

                    data = json.loads(text)
                    if data.get('status_code') == 0:
                        return data
                    else:
                        logger.error(f"API鏉╂柨娲栭柨娆掝嚖: {data.get('status_msg', '閺堫亞鐓￠柨娆掝嚖')}")
                        return None
        except Exception as e:
            logger.error(f"閼惧嘲褰囬悽銊﹀煕閸犳粍顐介崚妤勩€冩径杈Е: {e}")
        return None

    async def _download_user_mixes(self, user_id: str):
        """娑撳娴囬悽銊﹀煕閻ㄥ嫭澧嶉張澶婃値闂嗗棴绱欓幐澶愬帳缂冾喖褰查梽鎰煑閺佷即鍣洪敍?"""
        max_allmix = 0
        try:
            # 閸忕厧顔愰弮褔鏁崥?allmix 閹?mix
            number_cfg = self.config.get('number', {}) or {}
            max_allmix = int(number_cfg.get('allmix', number_cfg.get('mix', 0)) or 0)
        except Exception:
            max_allmix = 0

        cursor = 0
        fetched = 0

        console.print(f"\n[green]瀵偓婵骞忛崣鏍暏閹村嘲鎮庨梿鍡楀灙鐞?..[/green]")
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
                    console.print(f"[yellow]瀹歌尪鎻崚鏉挎値闂嗗棙鏆熼柌蹇涙閸? {max_allmix}[/yellow]")
                    return
                mix_id = mix.get('mix_id')
                mix_name = mix.get('mix_name', '')
                console.print(f"[cyan]娑撳娴囬崥鍫ユ肠[/cyan]: {mix_name} ({mix_id})")
                await self._download_mix_by_id(mix_id)
                fetched += 1

            if not mix_list_data.get('has_more'):
                break
            cursor = mix_list_data.get('cursor', 0)

        console.print(f"[green]閴?閻劍鍩涢崥鍫ユ肠娑撳娴囩€瑰本鍨氶敍灞藉彙婢跺嫮鎮?{fetched} 娑撶實/green]")

    async def _fetch_user_mix_list(self, user_id: str, cursor: int = 0) -> Optional[Dict]:
        """閼惧嘲褰囬悽銊﹀煕閸氬牓娉﹂崚妤勩€?"""
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
                logger.warning(f"閼惧嘲褰嘪-Bogus婢惰精瑙? {e}, 鐏忔繆鐦稉宥呯敨X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"鐠囬攱鐪伴悽銊﹀煕閸氬牓娉﹂崚妤勩€? {full_url[:100]}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    if response.status != 200:
                        logger.error(f"鐠囬攱鐪版径杈Е閿涘瞼濮搁幀浣虹垳: {response.status}")
                        return None
                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None
                    data = json.loads(text)
                    if data.get('status_code') == 0:
                        return data
                    else:
                        logger.error(f"API鏉╂柨娲栭柨娆掝嚖: {data.get('status_msg', '閺堫亞鐓￠柨娆掝嚖')}")
                        return None
        except Exception as e:
            logger.error(f"閼惧嘲褰囬悽銊﹀煕閸氬牓娉﹂崚妤勩€冩径杈Е: {e}")
        return None

    async def download_mix(self, url: str) -> bool:
        """閺嶈宓侀崥鍫ユ肠闁剧偓甯存稉瀣祰閸氬牓娉﹂崘鍛閺堝缍旈崫?"""
        try:
            mix_id = None
            for pattern in [r'/collection/(\d+)', r'/mix/detail/(\d+)']:
                m = re.search(pattern, url)
                if m:
                    mix_id = m.group(1)
                    break
            if not mix_id:
                logger.error(f"閺冪姵纭舵禒搴℃値闂嗗棝鎽奸幒銉﹀絹閸欐湏D: {url}")
                return False
            await self._download_mix_by_id(mix_id)
            return True
        except Exception as e:
            logger.error(f"娑撳娴囬崥鍫ユ肠婢惰精瑙? {e}")
            return False

    async def _download_mix_by_id(self, mix_id: str):
        """閹稿鎮庨梿鍜篋娑撳娴囬崗銊╁劥娴ｆ粌鎼?"""
        cursor = 0
        downloaded = 0

        console.print(f"\n[green]瀵偓婵绗呮潪钘夋値闂?{mix_id} ...[/green]")

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

        console.print(f"[green]閴?閸氬牓娉︽稉瀣祰鐎瑰本鍨氶敍灞藉彙娑撳娴?{downloaded} 娑撶實/green]")

    async def _fetch_mix_awemes(self, mix_id: str, cursor: int = 0) -> Optional[Dict]:
        """閼惧嘲褰囬崥鍫ユ肠娑撳缍旈崫浣稿灙鐞?"""
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
                logger.warning(f"閼惧嘲褰嘪-Bogus婢惰精瑙? {e}, 鐏忔繆鐦稉宥呯敨X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"鐠囬攱鐪伴崥鍫ユ肠娴ｆ粌鎼ч崚妤勩€? {full_url[:100]}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    if response.status != 200:
                        logger.error(f"鐠囬攱鐪版径杈Е閿涘瞼濮搁幀浣虹垳: {response.status}")
                        return None
                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None
                    data = json.loads(text)
                    # USER_MIX 鏉╂柨娲栧▽鈩冩箒缂佺喍绔撮惃?status_code閿涘矁绻栭柌宀€娲块幒銉ㄧ箲閸?
                    return data
        except Exception as e:
            logger.error(f"閼惧嘲褰囬崥鍫ユ肠娴ｆ粌鎼ф径杈Е: {e}")
        return None

    async def download_music(self, url: str) -> bool:
        """閺嶈宓侀棅鍏呯妞ょ敻鎽奸幒銉ょ瑓鏉炰粙鐓舵稊鎰瑓閻ㄥ嫭澧嶉張澶夌稊閸濅緤绱欓弨顖涘瘮婢х偤鍣洪敍?"""
        try:
            # 閹绘劕褰?music_id
            music_id = None
            m = re.search(r'/music/(\d+)', url)
            if m:
                music_id = m.group(1)
            if not music_id:
                logger.error(f"閺冪姵纭舵禒搴ㄧ叾娑旀劙鎽奸幒銉﹀絹閸欐湏D: {url}")
                return False

            cursor = 0
            downloaded = 0
            limit_num = 0
            try:
                limit_num = int((self.config.get('number', {}) or {}).get('music', 0))
            except Exception:
                limit_num = 0

            console.print(f"\n[green]瀵偓婵绗呮潪浠嬬叾娑?{music_id} 娑撳娈戞担婊冩惂...[/green]")

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
                        console.print(f"[yellow]瀹歌尪鎻崚浼寸叾娑旀劒绗呮潪鑺ユ殶闁插繘妾洪崚? {limit_num}[/yellow]")
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

            console.print(f"[green]閴?闂婂厖绠版担婊冩惂娑撳娴囩€瑰本鍨氶敍灞藉彙娑撳娴?{downloaded} 娑撶實/green]")
            return True
        except Exception as e:
            logger.error(f"娑撳娴囬棅鍏呯妞ら潧銇戠拹? {e}")
            return False

    async def _fetch_music_awemes(self, music_id: str, cursor: int = 0) -> Optional[Dict]:
        """閼惧嘲褰囬棅鍏呯娑撳缍旈崫浣稿灙鐞?"""
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
                logger.warning(f"閼惧嘲褰嘪-Bogus婢惰精瑙? {e}, 鐏忔繆鐦稉宥呯敨X-Bogus")
                full_url = f"{api_url}{params}"

            logger.info(f"鐠囬攱鐪伴棅鍏呯娴ｆ粌鎼ч崚妤勩€? {full_url[:100]}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(full_url, headers=self.headers, timeout=10) as response:
                    if response.status != 200:
                        logger.error(f"鐠囬攱鐪版径杈Е閿涘瞼濮搁幀浣虹垳: {response.status}")
                        return None
                    text = await response.text()
                    if not text:
                        logger.error("Empty response body")
                        return None
                    data = json.loads(text)
                    return data
        except Exception as e:
            logger.error(f"閼惧嘲褰囬棅鍏呯娴ｆ粌鎼ф径杈Е: {e}")
        return None
    
    def _check_time_filter(self, aweme: Dict) -> bool:
        """濡偓閺屻儲妞傞梻纾嬬箖濠?"""
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
        
        if start_time:
            start_date = datetime.strptime(start_time, '%Y-%m-%d')
            if create_date < start_date:
                return False
        
        if end_time:
            end_date = datetime.strptime(end_time, '%Y-%m-%d')
            if create_date > end_date:
                return False
        
        return True
    
    async def run(self):
        """Run downloader."""
        console.print(Panel.fit(
            "[bold cyan]Douyin Downloader v3.0[/bold cyan]\n"
            "[dim]Supports video, image, user page and mix batch download[/dim]",
            border_style="cyan"
        ))

        await self._initialize_cookies_and_headers()

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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Douyin downloader - unified enhanced edition',
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
    
    args = parser.parse_args()
    
    # 缂佸嫬鎮庨柊宥囩枂閺夈儲绨敍姘喘閸忓牆鎳℃禒銈堫攽
    temp_config = {}
    if args.url:
        temp_config['link'] = args.url
    
    # 鐟曞棛娲婃穱婵嗙摠鐠侯垰绶?
    if args.path:
        temp_config['path'] = args.path
    
    # Cookie闁板秶鐤?
    if args.auto_cookie:
        temp_config['auto_cookie'] = True
        temp_config['cookies'] = 'auto'
    if args.cookie:
        temp_config['cookies'] = args.cookie
        temp_config['auto_cookie'] = False
    
    # 婵″倹鐏夌€涙ê婀稉瀛樻闁板秶鐤嗛敍灞藉灟閻㈢喐鍨氭稉鈧稉顏冨閺冭埖鏋冩禒鏈电返閻滅増婀侀弸鍕偓鐘插毐閺侀濞囬悽?
    if temp_config:
        # 閸氬牆鑻熼弬鍥︽闁板秶鐤嗛敍鍫濐洤鐎涙ê婀敍?
        file_config = {}
        if os.path.exists(args.config):
            try:
                with open(args.config, 'r', encoding='utf-8') as f:
                    file_config = yaml.safe_load(f) or {}
            except Exception:
                file_config = {}
        
        # 閸忕厧顔愮粻鈧崠鏍暛閸?
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
    
    # 鏉╂劘顢戞稉瀣祰閸?
    try:
        downloader = UnifiedDownloader(config_path)
        asyncio.run(downloader.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]閳跨媴绗?閻劍鍩涙稉顓熸焽娑撳娴嘯/yellow]")
    except Exception as e:
        console.print(f"\n[red]閴?缁嬪绨鍌氱埗: {e}[/red]")
        logger.exception("缁嬪绨鍌氱埗")
    finally:
        # 濞撳懐鎮婃稉瀛樻闁板秶鐤?
        if args.url and os.path.exists('temp_config.yml'):
            os.remove('temp_config.yml')


if __name__ == '__main__':
    main()





