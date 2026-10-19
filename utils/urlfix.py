"""链接清洗与提取工具。

用户粘贴的内容常常不是干净链接，例如：

    7.43 abc:/ 复制此链接，打开抖音搜索，直接观看视频！
    https://www.douyin.com/user/MS4wLjABAAAA...，看看这个
    ｗｗｗ.douyin.com／video／123
    https://www.douyin.com/video/123?&amp;from=share

这里把这些写法统一成可用的链接，必要时补全协议、去掉多余参数。
"""

from __future__ import annotations

import html
import re
import unicodedata
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse

# 抖音常见域名：主站、分享页、短链
_URL_HOSTS = (
    "douyin.com",
    "iesdouyin.com",
    "douyin.ie",
)

# 只接受 URL 里合法的 ASCII 字符，遇到中文、全角标点或空白就截断，
# 否则「链接后面跟着中文说明」会把说明也吞进链接。
_URL_CANDIDATE = re.compile(
    r"(?:https?://|//)?"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"(?:douyin\.com|iesdouyin\.com|douyin\.ie)"
    r"(?::\d+)?"
    r"/[A-Za-z0-9\-._~:/?#@!$&()*+,;=%]*",
    re.IGNORECASE,
)

_AWEME_ID_ONLY = re.compile(r"^\d{15,25}$")
_SEC_UID_ONLY = re.compile(r"^MS4wLjAB[A-Za-z0-9_\-]+$")

# 链接末尾常见的干扰字符
_TRAILING_JUNK = "，。、；：！？）》」』】,.;:!?)]}\\>\"'"

_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


def _clean_text(text: str) -> str:
    """统一全角字符、去零宽字符、还原 HTML 实体。"""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = _ZERO_WIDTH.sub("", normalized)
    normalized = normalized.replace("\u00a0", " ")
    return html.unescape(normalized).strip()


def _normalize_url(url: str) -> Optional[str]:
    """补协议、规范主机名与路径、去掉尾部干扰字符。"""
    candidate = url.strip().strip("<>\"'“”‘’()（）【】[]")
    candidate = candidate.rstrip(_TRAILING_JUNK)
    if not candidate:
        return None

    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", candidate):
        candidate = "https://" + candidate.lstrip("/")

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if not host:
        return None
    # 裸域名补 www，保证与用户从浏览器复制的形式一致
    if host in ("douyin.com", "iesdouyin.com"):
        host = "www." + host

    path = re.sub(r"/{2,}", "/", parsed.path or "/") or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    # 查询参数去重并保序，避免 a=1&a=2 这类重复参数
    seen = set()
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in seen:
            continue
        seen.add(key)
        pairs.append((key, value))
    query = urlencode(pairs, doseq=True)

    rebuilt = f"https://{host}{path}"
    if query:
        rebuilt += "?" + query
    return rebuilt.rstrip(_TRAILING_JUNK)


def repair_url(text: str) -> Optional[str]:
    """从一段文本里取出一个可用的抖音链接；无法识别时返回 None。"""
    cleaned = _clean_text(text)
    if not cleaned:
        return None

    # 直接就是链接（可能缺协议头）
    direct = _normalize_url(cleaned)
    if direct:
        host = (urlparse(direct).netloc or "").lower()
        if host.endswith(_URL_HOSTS):
            return direct

    # 从分享文案里挑出第一个抖音链接
    match = _URL_CANDIDATE.search(cleaned)
    if match:
        candidate = _normalize_url(match.group(0))
        if candidate:
            host = (urlparse(candidate).netloc or "").lower()
            if host.endswith(_URL_HOSTS):
                return candidate

    # 只给了作品 ID 或 sec_user_id 时，拼成对应链接
    if _AWEME_ID_ONLY.match(cleaned):
        return f"https://www.douyin.com/video/{cleaned}"
    if _SEC_UID_ONLY.match(cleaned):
        return f"https://www.douyin.com/user/{cleaned}"
    return None


def extract_urls(text: str) -> List[str]:
    """从任意文本中提取所有抖音链接，去重并保持出现顺序。"""
    cleaned = _clean_text(text)
    if not cleaned:
        return []

    found: List[str] = []
    for match in _URL_CANDIDATE.finditer(cleaned):
        normalized = _normalize_url(match.group(0))
        if not normalized:
            continue
        host = (urlparse(normalized).netloc or "").lower()
        if not host.endswith(_URL_HOSTS):
            continue
        if normalized not in found:
            found.append(normalized)

    if found:
        return found

    # 没有链接时，尝试把整段文本当作单个链接 / 单个 ID
    single = repair_url(cleaned)
    return [single] if single else []
