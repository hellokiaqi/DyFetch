#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音下载器 · 桌面控制台

零额外依赖的 Tkinter 图形界面，驱动 ``downloader.py``：

* 批量队列：一行一个链接，按顺序下载并逐项显示状态
* 运行日志：分级着色、按级别过滤、关键字搜索与高亮、导出
* 下载设置：输出目录、切帧、数量上限、模式、并发等，写入独立的运行配置
* 任务记录：双击载入、右键菜单、搜索、命名、删除
* 浅色 / 深色主题切换，窗口尺寸与上次输入自动记忆

运行方式::

    python ui.py
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "DyinDL"
APP_SUBTITLE = "抖音下载器 · 队列 / 批量下载 / 抽帧"
APP_VERSION = "3.2"

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = "ui_history.json"
STATE_FILE = "ui_state.json"
STARTUP_LOG = "ui_startup.log"
RUN_CONFIG = "ui_run_config.yml"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
PROGRESS_RE = re.compile(
    r"Progress:\s*(\d+)\s*/\s*(\d+)\s*\|\s*Success:\s*(\d+)\s*\|\s*Failed:\s*(\d+)"
)
LINK_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]\s*Processing:\s*(\S+)")
SESSION_RE = re.compile(r"登录态有效期至\s*(\d{4}-\d{2}-\d{2})（剩余\s*(\d+)\s*天）")
SESSION_EXPIRED_RE = re.compile(r"登录态已于\s*(\d{4}-\d{2}-\d{2})\s*过期")
STAT_ROW_RE = re.compile(r"^\D*?(Total|Success|Failed|Skipped|SuccessRate|Elapsed)\D+(\S+)\s*$")
URL_RE = re.compile(r"https?://[^\s，,；;]+")

MAX_LOG_LINES = 6000

# ---------------------------------------------------------------- 主题调色板

THEMES: Dict[str, Dict[str, str]] = {
    "light": {
        "bg": "#F1F3F8",
        "card": "#FFFFFF",
        "card_alt": "#F8FAFD",
        "border": "#E3E6EF",
        "border_hi": "#CBD2E1",
        "text": "#151823",
        "text_sub": "#5A6172",
        "text_mute": "#8A93A6",
        "accent": "#FE2C55",
        "accent_hi": "#FF5178",
        "accent_dk": "#D81E44",
        "accent_soft": "#FFE8EC",
        "on_accent": "#FFFFFF",
        "chip": "#F4F6FB",
        "chip_hi": "#E9EDF7",
        "chip_press": "#DDE3F0",
        "ok": "#0FA968",
        "warn": "#E08A00",
        "err": "#E03B32",
        "log_bg": "#12141C",
        "log_fg": "#D8DEE9",
        "log_dim": "#6E7787",
        "log_info": "#7FA6D9",
        "log_ok": "#4ED07F",
        "log_warn": "#E5B454",
        "log_err": "#FF6B66",
        "log_cmd": "#6FD3DA",
        "log_sel": "#2A3550",
        "scroll": "#CBD2E1",
        "scroll_trough": "#FFFFFF",
    },
    "dark": {
        "bg": "#14161C",
        "card": "#1B1E27",
        "card_alt": "#222633",
        "border": "#2A2F3C",
        "border_hi": "#3B4250",
        "text": "#E7EBF3",
        "text_sub": "#AAB2C2",
        "text_mute": "#79808F",
        "accent": "#FE2C55",
        "accent_hi": "#FF5178",
        "accent_dk": "#C8163C",
        "accent_soft": "#3A1B24",
        "on_accent": "#FFFFFF",
        "chip": "#252A36",
        "chip_hi": "#2F3543",
        "chip_press": "#3A4152",
        "ok": "#3DD68C",
        "warn": "#F5B84D",
        "err": "#FF6B66",
        "log_bg": "#0E1015",
        "log_fg": "#D8DEE9",
        "log_dim": "#6E7787",
        "log_info": "#87AEDF",
        "log_ok": "#4ED07F",
        "log_warn": "#E5B454",
        "log_err": "#FF6B66",
        "log_cmd": "#6FD3DA",
        "log_sel": "#2A3550",
        "scroll": "#39404E",
        "scroll_trough": "#1B1E27",
    },
}

COLOR: Dict[str, str] = dict(THEMES["light"])


def apply_theme(name: str) -> None:
    """把指定主题的色值写入全局调色板（控件重建后生效）。"""
    COLOR.clear()
    COLOR.update(THEMES.get(name) or THEMES["light"])


class F:
    """字体集合（在 Tk 根窗口创建后才可使用）。"""

    family = "Microsoft YaHei UI"
    mono = "Consolas"

    @classmethod
    def init(cls) -> None:
        from tkinter import font as tkfont

        available = set(tkfont.families())
        for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "SimHei"):
            if candidate in available:
                cls.family = candidate
                break
        for candidate in ("Consolas", "Cascadia Mono", "Courier New"):
            if candidate in available:
                cls.mono = candidate
                break

    @classmethod
    def ui(cls, size: int = 10, weight: str = "normal"):
        return (cls.family, size, weight)

    @classmethod
    def code(cls, size: int = 10):
        return (cls.mono, size)


# ---------------------------------------------------------------- 工具函数


def enable_dpi_awareness() -> None:
    """让窗口在高分屏上保持清晰（失败时静默忽略）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def log_startup(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with (BASE_DIR / STARTUP_LOG).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_user_id(url: str) -> str:
    """从抖音链接中解析 sec_user_id / user id。"""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    match = re.search(r"/user/([^/?#]+)", parsed.path or "")
    if match:
        return match.group(1)
    for key in ("sec_user_id", "sec_uid", "user_id"):
        values = parse_qs(parsed.query or "").get(key)
        if values:
            return values[0]
    return ""


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_history(path: Path) -> List[Dict]:
    raw = read_json(path, [])
    return raw if isinstance(raw, list) else []


def save_history(path: Path, records: List[Dict]) -> None:
    write_json(path, records)


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def shorten(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def rounded_points(x1: float, y1: float, x2: float, y2: float, r: float):
    """圆角矩形的平滑多边形顶点。"""
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def url_label(url: str) -> str:
    """把长链接压缩成语义化短标签。"""
    url = str(url or "").strip()
    if not url:
        return "-"
    aweme = re.search(r"/(?:video|note)/(\d+)", url) or re.search(
        r"[?&](?:modal_id|vid)=(\d+)", url
    )
    if aweme:
        kind = "图文" if "/note/" in url else "视频"
        return f"{kind} {shorten(aweme.group(1), 12)}"
    user = re.search(r"/user/([^/?#]+)", url)
    if user:
        return "主页 " + shorten(user.group(1), 12)
    mix = re.search(r"/(?:collection|mix)/([^/?#]+)", url)
    if mix:
        return "合集 " + shorten(mix.group(1), 12)
    if "v.douyin.com" in url:
        return "短链 " + shorten(url.rstrip("/").split("/")[-1] or url, 10)
    return shorten(url.replace("https://", "").replace("http://", ""), 22)


# ---------------------------------------------------------------- 自定义控件


class PillButton(tk.Canvas):
    """圆角按钮，支持悬停 / 按下 / 禁用三种状态，配色取自当前主题。"""

    KIND_KEYS = {
        "primary": ("accent", "accent_hi", "accent_dk", "on_accent"),
        "secondary": ("chip", "chip_hi", "chip_press", "text_sub"),
        "ghost": ("card", "chip", "chip_hi", "text_sub"),
        "danger": ("accent_soft", "chip_hi", "chip_press", "err"),
    }

    def __init__(
        self,
        master,
        text: str,
        command=None,
        kind: str = "primary",
        width: int = 108,
        height: int = 34,
        radius: int = 9,
        font=None,
        bg: Optional[str] = None,
    ) -> None:
        parent_bg = bg or master.cget("bg")
        super().__init__(
            master, width=width, height=height, highlightthickness=0, bd=0, bg=parent_bg
        )
        self._text = text
        self._command = command
        self._kind = kind
        self._radius = radius
        self._font = font or F.ui(10, "bold")
        self._enabled = True
        self._hovering = False
        self._pressing = False
        self._bw, self._bh = width, height
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Configure>", self._on_configure)

    def _palette(self):
        bg_key, hover_key, press_key, fg_key = self.KIND_KEYS.get(
            self._kind, self.KIND_KEYS["primary"]
        )
        if not self._enabled:
            return {"bg": COLOR["chip"], "hover": COLOR["chip"], "press": COLOR["chip"],
                    "fg": COLOR["text_mute"]}
        return {
            "bg": COLOR.get(bg_key, COLOR["accent"]),
            "hover": COLOR.get(hover_key, COLOR["accent_hi"]),
            "press": COLOR.get(press_key, COLOR["accent_dk"]),
            "fg": COLOR.get(fg_key, COLOR["on_accent"]),
        }

    def _draw(self) -> None:
        spec = self._palette()
        fill = spec["press"] if self._pressing else spec["hover"] if self._hovering else spec["bg"]
        self.delete("all")
        pts = rounded_points(1, 1, self._bw - 1, self._bh - 1, self._radius)
        self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        self.create_text(
            self._bw / 2, self._bh / 2 + 1, text=self._text, fill=spec["fg"], font=self._font
        )
        self.configure(cursor="hand2" if self._enabled else "arrow")

    def _on_configure(self, event) -> None:
        if (event.width, event.height) != (self._bw, self._bh):
            self._bw, self._bh = event.width, event.height
            self._draw()

    def _on_enter(self, _e=None) -> None:
        if self._enabled:
            self._hovering = True
            self._draw()

    def _on_leave(self, _e=None) -> None:
        self._hovering = False
        self._pressing = False
        self._draw()

    def _on_press(self, _e=None) -> None:
        if self._enabled:
            self._pressing = True
            self._draw()

    def _on_release(self, _e=None) -> None:
        if not self._enabled:
            return
        was_pressing = self._pressing
        self._pressing = False
        self._draw()
        if was_pressing and self._command:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled != enabled:
            self._enabled = enabled
            self._hovering = self._pressing = False
            self._draw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._draw()


class CheckToggle(tk.Canvas):
    """自绘复选框：圆角方框 + 勾选标记，避免 ttk 主题差异。"""

    BOX = 16

    def __init__(self, master, text: str, variable: tk.BooleanVar, command=None):
        self._var = variable
        self._text = text
        self._command = command
        self._hovering = False
        super().__init__(
            master, height=26, highlightthickness=0, bd=0, bg=master.cget("bg"),
            cursor="hand2",
        )
        self._width = self.BOX + 8 + self._measure(text)
        self.configure(width=self._width)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Configure>", lambda _e: self._render())
        self._render()

    def _measure(self, text: str) -> int:
        from tkinter import font as tkfont

        return tkfont.Font(font=F.ui(9)).measure(text)

    def _toggle(self, _event=None) -> None:
        self._var.set(not self._var.get())
        self._render()
        if self._command:
            self._command()

    def _on_enter(self, _e=None) -> None:
        self._hovering = True
        self._render()

    def _on_leave(self, _e=None) -> None:
        self._hovering = False
        self._render()

    def _render(self) -> None:
        self.delete("all")
        checked = bool(self._var.get())
        top = (26 - self.BOX) / 2
        if checked:
            fill, outline = COLOR["accent"], COLOR["accent"]
        else:
            fill = COLOR["card"] if not self._hovering else COLOR["chip"]
            outline = COLOR["border_hi"] if not self._hovering else COLOR["accent"]
        self.create_polygon(
            rounded_points(1, top + 1, 1 + self.BOX, top + 1 + self.BOX, 4),
            smooth=True, fill=fill, outline=outline,
        )
        if checked:
            self.create_line(
                4.5, top + 8, 7.5, top + 11.5, 12.5, top + 5,
                fill=COLOR["on_accent"], width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND,
            )
        self.create_text(
            self.BOX + 8, 13, text=self._text, anchor="w",
            fill=COLOR["text"] if self._hovering else COLOR["text_sub"], font=F.ui(9),
        )


class StatusPill(tk.Canvas):
    """状态徽章：彩色圆点 + 文案。"""

    def __init__(self, master, text: str = "就绪", color: str = "#0FA968", width: int = 96):
        super().__init__(
            master, width=width, height=28, highlightthickness=0, bd=0,
            bg=master.cget("bg"),
        )
        self._text = text
        self._color = color
        self._width = width
        self._render()

    def _render(self) -> None:
        self.delete("all")
        pts = rounded_points(1, 1, self._width - 1, 27, 13)
        self.create_polygon(pts, smooth=True, fill=COLOR["chip"])
        self.create_oval(12, 10, 20, 18, fill=self._color, outline="")
        self.create_text(
            27, 14, text=self._text, anchor="w", fill=COLOR["text_sub"], font=F.ui(9)
        )

    def update_state(self, text: str, color: str) -> None:
        self._text, self._color = text, color
        needed = 27 + max(30, len(text) * 13)
        if needed > self._width:
            self._width = needed
            self.configure(width=needed)
        self._render()


class Card(tk.Frame):
    """带可选标题的卡片容器。"""

    def __init__(self, master, title: str = "", subtitle: str = "", padding=(14, 12), **kw):
        super().__init__(
            master, bg=COLOR["card"], highlightthickness=1,
            highlightbackground=COLOR["border"], highlightcolor=COLOR["border"], **kw
        )
        self.header = None
        row = 0
        if title:
            self.header = tk.Frame(self, bg=COLOR["card"])
            self.header.grid(row=0, column=0, sticky="ew", padx=padding[0], pady=(padding[1], 0))
            tk.Label(
                self.header, text=title, bg=COLOR["card"], fg=COLOR["text"], font=F.ui(10, "bold")
            ).pack(side=tk.LEFT)
            if subtitle:
                tk.Label(
                    self.header, text=subtitle, bg=COLOR["card"], fg=COLOR["text_mute"],
                    font=F.ui(9),
                ).pack(side=tk.LEFT, padx=(8, 0))
            row = 1
        self.body = tk.Frame(self, bg=COLOR["card"])
        self.body.grid(
            row=row, column=0, sticky="nsew",
            padx=padding[0], pady=(0 if title else padding[1], padding[1]),
        )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(row, weight=1)


class LogView(tk.Frame):
    """分级着色 + 过滤 + 搜索高亮的日志面板。"""

    TAGS = {
        "info": "log_fg",
        "dim": "log_dim",
        "ok": "log_ok",
        "warn": "log_warn",
        "err": "log_err",
        "cmd": "log_cmd",
    }

    def __init__(self, master):
        super().__init__(master, bg=COLOR["log_bg"])
        self.buffer: List[Tuple[str, str]] = []  # (text, tag)
        self.level = "all"
        self.keyword = ""
        self.text = tk.Text(
            self, wrap=tk.NONE, bg=COLOR["log_bg"], fg=COLOR["log_fg"],
            insertbackground=COLOR["log_fg"], relief=tk.FLAT, bd=0,
            font=F.code(10), padx=12, pady=10, spacing1=1, spacing3=2,
            selectbackground=COLOR["log_sel"], selectforeground="#FFFFFF",
        )
        yscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.text.yview,
                                style="Log.Vertical.TScrollbar")
        xscroll = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.text.xview,
                                style="Log.Horizontal.TScrollbar")
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        for name, colour in self.TAGS.items():
            self.text.tag_configure(name, foreground=COLOR[colour])
        self.text.tag_configure("match", background="#6b4d00", foreground="#FFFFFF")
        self.text.configure(state=tk.DISABLED)

    # -- 分级 ---------------------------------------------------------
    def classify(self, line: str) -> str:
        low = line.lower()
        if "error" in low or "失败" in line or "traceback" in low or "exception" in low:
            return "err"
        if "warning" in low or "警告" in line or "重试" in line or "⚠" in line:
            return "warn"
        if "completed" in low or "成功" in line or "完成" in line:
            return "ok"
        if line.startswith("=" * 10) or line.lstrip().startswith("$"):
            return "cmd"
        if re.match(r"^\s*(total|success|failed|skipped|successrate|elapsed)\b", low):
            return "dim"
        return "info"

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        lines = chunk.splitlines(keepends=True)
        for raw_line in lines:
            line = strip_ansi(raw_line)
            tag = self.classify(line)
            self.buffer.append((line, tag))
            if self._accepts(line, tag):
                self._insert(line, tag)
        if len(self.buffer) > MAX_LOG_LINES:
            del self.buffer[: len(self.buffer) - MAX_LOG_LINES]
            self._render()

    def hint(self, text: str) -> None:
        for line in text.splitlines(keepends=True):
            self.buffer.append((line, "dim"))
            if self._accepts(line, "dim"):
                self._insert(line, "dim")

    def _accepts(self, line: str, tag: str) -> bool:
        if self.level == "warn" and tag not in ("warn", "err"):
            return False
        if self.level == "err" and tag != "err":
            return False
        if self.keyword and self.keyword not in line.lower():
            return False
        return True

    def _insert(self, line: str, tag: str) -> None:
        self.text.configure(state=tk.NORMAL)
        start = self.text.index("end-1c")
        self.text.insert(tk.END, line, tag)
        if self.keyword:
            idx = start
            while True:
                pos = self.text.search(self.keyword, idx, tk.END, nocase=True)
                if not pos:
                    break
                end = f"{pos}+{len(self.keyword)}c"
                self.text.tag_add("match", pos, end)
                idx = end
        self.text.configure(state=tk.DISABLED)

    def _render(self) -> None:
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        for line, tag in self.buffer:
            if self._accepts(line, tag):
                self._insert(line, tag)

    def set_level(self, level: str) -> None:
        self.level = level
        self._render()

    def set_keyword(self, keyword: str) -> None:
        self.keyword = keyword.strip().lower()
        self._render()

    def clear(self) -> None:
        self.buffer.clear()
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)

    def content(self) -> str:
        return self.text.get("1.0", tk.END)

    def line_count(self) -> int:
        return len(self.buffer)

    def scroll_to_end(self) -> None:
        self.text.see(tk.END)


# ---------------------------------------------------------------- 设置对话框


class DownloaderUI:
    """界面主体：负责布局、状态与子进程生命周期。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.base_dir = BASE_DIR
        self.history_path = self.base_dir / HISTORY_FILE
        self.state_path = self.base_dir / STATE_FILE

        self.records: List[Dict] = load_history(self.history_path)
        self.state: Dict = read_json(self.state_path, {})
        apply_theme(str(self.state.get("theme") or "light"))

        # 队列项: {url, status(pending/running/done/failed), detail}
        self.queue_items: List[Dict] = [
            {"url": u, "status": "pending", "detail": "等待"}
            for u in (self.state.get("queue") or [])
            if isinstance(u, str) and u.strip()
        ]
        self.overrides: Dict = dict(self.state.get("overrides") or {})

        self.process: Optional[subprocess.Popen] = None
        self.output_queue: "queue.Queue[str]" = queue.Queue()
        self.reader_thread: Optional[threading.Thread] = None
        self.started_at: Optional[float] = None
        self._last_elapsed = 0.0
        self._suspended = False
        self._run_sequence: List[str] = []
        self._current_index = 0
        self._success_at_item_start = 0
        self.stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}

        self.url_var = tk.StringVar()
        self.config_var = tk.StringVar(value=self.state.get("config", "config_simple.yml"))
        self.path_var = tk.StringVar(value=self.state.get("path", "./my_videos_fix/"))
        self.backfill_var = tk.BooleanVar(value=bool(self.state.get("backfill", False)))
        self.autoscroll_var = tk.BooleanVar(value=bool(self.state.get("autoscroll", True)))
        self.notify_var = tk.BooleanVar(value=bool(self.state.get("notify", True)))
        self.filter_var = tk.StringVar()
        self.log_filter_var = tk.StringVar(value="全部")
        self.log_search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self.session_date = str(self.state.get("session_date") or "")
        self.session_ok = bool(self.state.get("session_ok", True))

        # 设置页控件变量（只创建一次，主题重建时复用）
        self.ov_slice = tk.BooleanVar()
        self.ov_fps = tk.StringVar()
        self.ov_num_post = tk.StringVar()
        self.ov_num_like = tk.StringVar()
        self.ov_thread = tk.StringVar()
        self.ov_retry = tk.StringVar()
        self.ov_browser = tk.BooleanVar()
        self.ov_headless = tk.BooleanVar()
        self.ov_mode = {key: tk.BooleanVar() for key in ("post", "like", "mix")}
        self._sync_settings_vars()

        self._configure_root()
        self._build_ui()
        self._restore_geometry()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Control-Return>", lambda _e: self.run_download())
        self.root.bind("<Escape>", lambda _e: self.stop_download())
        self.root.after(120, self.poll_output)
        self.root.after(250, self._bring_to_front)

    # -- 窗口 ---------------------------------------------------------
    def _configure_root(self) -> None:
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.configure(bg=COLOR["bg"])
        self.root.minsize(1060, 700)

    def _restore_geometry(self) -> None:
        geometry = self.state.get("geometry")
        if isinstance(geometry, str) and re.match(r"^\d+x\d+\+-?\d+\+-?\d+$", geometry):
            try:
                w, h = (int(v) for v in geometry.split("+")[0].split("x"))
                if w >= 1060 and h >= 700:
                    self.root.geometry(geometry)
                    return
            except Exception:
                pass
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w = min(1460, max(1060, sw - 160))
        h = min(920, max(700, sh - 170))
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2)}")

    def _bring_to_front(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    # -- 主题 ---------------------------------------------------------
    def switch_theme(self) -> None:
        """在浅色 / 深色之间切换：保存状态后整窗重建。"""
        if self.process and self.process.poll() is None:
            messagebox.showinfo("任务运行中", "请等当前任务结束后再切换主题。", parent=self.root)
            return
        new_theme = "dark" if self.state.get("theme") == "light" else "light"
        self.state["theme"] = new_theme
        apply_theme(new_theme)
        self._rebuild_ui()

    def _rebuild_ui(self) -> None:
        """主题切换后重建界面，并尽量保留日志 / 统计 / 当前页签。"""
        self._suspended = True
        log_buffer = list(self.log.buffer) if getattr(self, "log", None) else []
        try:
            active_tab = self.notebook.index(self.notebook.select())
        except Exception:
            active_tab = 0
        try:
            progress_value = float(self.progress.cget("value"))
        except Exception:
            progress_value = 0.0
        try:
            for child in self.root.winfo_children():
                child.destroy()
        except Exception:
            pass
        self.root.configure(bg=COLOR["bg"])
        self._build_ui()
        if log_buffer:
            self.log.buffer = log_buffer
            self.log._render()
            self._update_log_counter()
        try:
            self.progress.configure(value=progress_value)
        except Exception:
            pass
        self._update_stats_label(self._last_elapsed)
        try:
            self.notebook.select(active_tab)
        except Exception:
            pass
        self._set_running(bool(self.process and self.process.poll() is None))
        self._suspended = False

    # -- 样式 ---------------------------------------------------------
    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Treeview", background=COLOR["card"], fieldbackground=COLOR["card"],
            foreground=COLOR["text"], rowheight=30, borderwidth=0, font=F.ui(9),
        )
        style.configure(
            "Treeview.Heading", background=COLOR["card_alt"], foreground=COLOR["text_sub"],
            font=F.ui(9, "bold"), relief="flat", borderwidth=0, padding=(6, 7),
        )
        style.map("Treeview.Heading", background=[("active", COLOR["chip"])])
        style.map(
            "Treeview",
            background=[("selected", COLOR["accent_soft"])],
            foreground=[("selected", COLOR["text"])],
        )
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        style.configure(
            "TCombobox", fieldbackground=COLOR["card"], background=COLOR["card"],
            foreground=COLOR["text"], arrowcolor=COLOR["text_sub"],
            bordercolor=COLOR["border_hi"], lightcolor=COLOR["border_hi"],
            darkcolor=COLOR["border_hi"], padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLOR["card"])],
            bordercolor=[("focus", COLOR["accent"])],
            selectbackground=[("readonly", COLOR["card"])],
            selectforeground=[("readonly", COLOR["text"])],
        )
        for key, value in (
            ("*TCombobox*Listbox.background", COLOR["card"]),
            ("*TCombobox*Listbox.foreground", COLOR["text"]),
            ("*TCombobox*Listbox.selectBackground", COLOR["accent_soft"]),
            ("*TCombobox*Listbox.selectForeground", COLOR["text"]),
        ):
            self.root.option_add(key, value)
        self.root.option_add("*TCombobox*Listbox.font", F.ui(10))

        style.configure(
            "TEntry", fieldbackground=COLOR["card"], foreground=COLOR["text"],
            bordercolor=COLOR["border_hi"], lightcolor=COLOR["border_hi"],
            darkcolor=COLOR["border_hi"], insertcolor=COLOR["text"], padding=6,
        )
        style.map("TEntry", bordercolor=[("focus", COLOR["accent"])])

        style.configure(
            "TScrollbar", background=COLOR["scroll"], troughcolor=COLOR["scroll_trough"],
            bordercolor=COLOR["scroll_trough"], arrowcolor=COLOR["text_mute"], width=11,
        )
        style.configure(
            "Log.Vertical.TScrollbar", background="#2C3446", troughcolor=COLOR["log_bg"],
            bordercolor=COLOR["log_bg"], arrowcolor="#5D6B85", width=11,
        )
        style.configure(
            "Log.Horizontal.TScrollbar", background="#2C3446", troughcolor=COLOR["log_bg"],
            bordercolor=COLOR["log_bg"], arrowcolor="#5D6B85",
        )
        style.configure(
            "Accent.Horizontal.TProgressbar", troughcolor=COLOR["chip"],
            background=COLOR["accent"], bordercolor=COLOR["chip"],
            lightcolor=COLOR["accent"], darkcolor=COLOR["accent"], thickness=6,
        )
        style.configure(
            "Warn.Horizontal.TProgressbar", troughcolor=COLOR["chip"],
            background=COLOR["warn"], bordercolor=COLOR["chip"],
            lightcolor=COLOR["warn"], darkcolor=COLOR["warn"], thickness=6,
        )

        style.configure("TNotebook", background=COLOR["bg"], borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure(
            "TNotebook.Tab", background=COLOR["bg"], foreground=COLOR["text_mute"],
            padding=(16, 7), borderwidth=0, font=F.ui(9),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLOR["card"])],
            foreground=[("selected", COLOR["text"])],
        )

    # -- 布局 ---------------------------------------------------------
    def _build_ui(self) -> None:
        self._configure_styles()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        self._build_header()

        body = tk.Frame(self.root, bg=COLOR["bg"])
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(12, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        self._build_input_card(body)
        self._build_content(body)
        self._build_footer()

        self.refresh_queue()
        self.refresh_history()
        if not self.log.line_count():
            self.log.hint(
                f"就绪 · {APP_NAME} v{APP_VERSION}\n"
                "支持：视频 / 图文 / 用户主页 / 合集 / 音乐 / 短链接\n"
                "粘贴链接后回车加入队列，点击「开始下载」依次执行。\n\n"
            )
        self._update_log_counter()
        self.root.after(200, lambda: self._set_sash(self.state.get("sash", 0.46)))

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLOR["card"], height=68)
        header.grid(row=0, column=0, sticky="ew")
        header.pack_propagate(False)
        header.columnconfigure(1, weight=1)

        logo = tk.Canvas(header, width=38, height=38, bg=COLOR["card"], highlightthickness=0)
        logo.grid(row=0, column=0, padx=(18, 12), sticky="w")
        logo.create_polygon(rounded_points(0, 0, 38, 38, 11), smooth=True, fill=COLOR["accent"])
        logo.create_polygon([15, 11, 15, 27, 28, 19], fill=COLOR["on_accent"], outline="")

        titles = tk.Frame(header, bg=COLOR["card"])
        titles.grid(row=0, column=1, sticky="w")
        tk.Label(
            titles, text=APP_NAME, bg=COLOR["card"], fg=COLOR["text"], font=F.ui(14, "bold")
        ).pack(anchor="w")
        tk.Label(
            titles, text=APP_SUBTITLE, bg=COLOR["card"], fg=COLOR["text_mute"], font=F.ui(9)
        ).pack(anchor="w")

        right = tk.Frame(header, bg=COLOR["card"])
        right.grid(row=0, column=2, padx=(0, 18), sticky="e")
        self.session_label = tk.Label(
            right, text="", bg=COLOR["card"], fg=COLOR["text_mute"], font=F.ui(8)
        )
        self.session_label.pack(side=tk.LEFT, padx=(0, 12))
        theme_text = "浅色模式" if self.state.get("theme") == "dark" else "深色模式"
        PillButton(
            right, theme_text, command=self.switch_theme, kind="ghost", width=84, height=28,
            font=F.ui(9),
        ).pack(side=tk.LEFT, padx=(0, 10))
        self.status_pill = StatusPill(right, "就绪", COLOR["ok"])
        self.status_pill.pack(side=tk.LEFT)
        self._render_session_label()

        tk.Frame(self.root, bg=COLOR["border"], height=1).grid(row=0, column=0, sticky="sew")

    def _build_input_card(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        body = card.body
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, weight=1)

        tk.Label(
            body, text="下载链接", bg=COLOR["card"], fg=COLOR["text_sub"], font=F.ui(9)
        ).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        self.url_entry = ttk.Entry(body, textvariable=self.url_var, font=F.ui(10))
        self.url_entry.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        self.url_entry.bind("<Return>", lambda _e: self.add_to_queue())

        actions = tk.Frame(body, bg=COLOR["card"])
        actions.grid(row=0, column=2, sticky="e", padx=(12, 0), pady=(0, 8))
        PillButton(actions, "＋ 加入队列", command=self.add_to_queue, kind="secondary",
                   width=98, height=32, font=F.ui(9)).pack(side=tk.LEFT)
        self.run_btn = PillButton(actions, "▶  开始下载", command=self.run_download, width=108,
                                  height=32)
        self.run_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_btn = PillButton(actions, "■  停止", command=self.stop_download,
                                   kind="secondary", width=76, height=32, font=F.ui(9))
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        tk.Label(
            body, text="配置文件", bg=COLOR["card"], fg=COLOR["text_sub"], font=F.ui(9)
        ).grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.config_box = ttk.Combobox(
            body, textvariable=self.config_var, values=self._config_candidates(),
            state="readonly", font=F.ui(9), width=22,
        )
        self.config_box.grid(row=1, column=1, sticky="w")

        path_row = tk.Frame(body, bg=COLOR["card"])
        path_row.grid(row=1, column=2, sticky="ew", padx=(18, 0))
        path_row.columnconfigure(1, weight=1)
        tk.Label(
            path_row, text="保存目录", bg=COLOR["card"], fg=COLOR["text_sub"], font=F.ui(9)
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(path_row, textvariable=self.path_var, font=F.ui(9)).grid(
            row=0, column=1, sticky="ew"
        )
        PillButton(
            path_row, "浏览…", command=self.browse_output, kind="ghost", width=62, height=28,
            font=F.ui(9),
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        PillButton(
            path_row, "打开", command=self.open_output_dir, kind="ghost", width=50, height=28,
            font=F.ui(9),
        ).grid(row=0, column=3, sticky="w", padx=(6, 0))

        options = tk.Frame(body, bg=COLOR["card"])
        options.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        CheckToggle(options, "补齐切帧", self.backfill_var).pack(side=tk.LEFT)
        CheckToggle(options, "日志自动滚动", self.autoscroll_var).pack(side=tk.LEFT, padx=(16, 0))
        CheckToggle(options, "完成后提醒", self.notify_var).pack(side=tk.LEFT, padx=(16, 0))

        self.summary_label = tk.Label(
            options, text="", bg=COLOR["card"], fg=COLOR["text_mute"], font=F.ui(8)
        )
        self.summary_label.pack(side=tk.LEFT, padx=(16, 0))

        PillButton(
            options, "⚙ 设置", command=self._show_settings_tab, kind="ghost", width=70,
            height=28, font=F.ui(9),
        ).pack(side=tk.RIGHT)
        self._refresh_summary()

    def _show_settings_tab(self) -> None:
        try:
            self._sync_settings_vars()
            self.notebook.select(2)
        except Exception:
            pass

    def _render_session_label(self) -> None:
        """把登录有效期渲染成一行小字（按当前日期实时计算剩余天数）。"""
        label = getattr(self, "session_label", None)
        if label is None:
            return
        date_text = getattr(self, "session_date", "") or ""
        if not date_text:
            label.configure(text="", fg=COLOR["text_mute"])
            return
        try:
            days = (datetime.strptime(date_text, "%Y-%m-%d").date()
                    - datetime.now().date()).days
        except Exception:
            days = None
        if self.session_ok and days is not None and days >= 0:
            text, colour = f"登录有效期至 {date_text}（剩 {days} 天）", COLOR["text_mute"]
        elif days is not None and days < 0:
            text, colour = f"登录已过期 {abs(days)} 天，建议续期", COLOR["warn"]
        else:
            text, colour = "登录已过期，建议续期", COLOR["warn"]
        label.configure(text=text, fg=colour)

    def _update_session(self, date_text: str, ok: bool) -> None:
        self.session_date = date_text
        self.session_ok = ok
        self._render_session_label()

    def refresh_session(self) -> None:
        """调用 downloader.py --refresh-session 续期登录态。"""
        if self._suspended:
            return
        if self.process and self.process.poll() is None:
            self.set_status("已有任务正在运行，请稍后再续期", "warn")
            return
        config = self.config_var.get().strip() or "config_simple.yml"
        if not (self.base_dir / config).exists() and not Path(config).exists():
            messagebox.showerror("配置不存在", f"找不到配置文件：{config}", parent=self.root)
            return
        cmd = [sys.executable, str(self.base_dir / "downloader.py"),
               "-c", config, "--refresh-session"]
        self.append_log("\n" + "─" * 68 + "\n")
        self.append_log(f"$ {' '.join(cmd)}\n")
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        env.pop("COLUMNS", None)
        try:
            self.process = subprocess.Popen(
                cmd, cwd=str(self.base_dir), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self.started_at = time.monotonic()
        self.stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
        self._set_running(True)
        self.set_status("正在刷新登录态…", "ok")
        threading.Thread(target=self._read_output, daemon=True).start()
        self._tick_elapsed()

    def _refresh_summary(self) -> None:
        mode_names = {"post": "作品", "like": "点赞", "mix": "合集"}
        modes = "、".join(mode_names.get(m, m) for m in (self.overrides.get("mode") or ["post"]))
        fps = self.overrides.get("slice_fps", 10)
        slice_on = bool(self.overrides.get("slice_videos", True))
        limit = self.overrides.get("number_post", 0)
        parts = [
            f"模式 {modes}",
            "切帧 " + (f"{fps}fps" if slice_on else "关"),
            "上限 " + ("不限" if not limit else str(limit)),
        ]
        if getattr(self, "summary_label", None) is not None:
            self.summary_label.configure(text=" · ".join(parts))

    def _build_content(self, parent) -> None:
        panes = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        panes.grid(row=1, column=0, sticky="nsew")
        panes.rowconfigure(0, weight=1)
        self.panes = panes

        left = tk.Frame(panes, bg=COLOR["bg"])
        panes.add(left, weight=2)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(left)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        queue_page = tk.Frame(self.notebook, bg=COLOR["card"])
        records_page = tk.Frame(self.notebook, bg=COLOR["card"])
        settings_page = tk.Frame(self.notebook, bg=COLOR["card"])
        self.notebook.add(queue_page, text="下载队列")
        self.notebook.add(records_page, text="任务记录")
        self.notebook.add(settings_page, text="下载设置")
        self._build_queue_tab(queue_page)
        self._build_records_tab(records_page)
        self._build_settings_tab(settings_page)

        log_card = Card(panes, title="运行日志")
        panes.add(log_card, weight=5)
        self._build_log_tab(log_card.body)

    def _build_queue_tab(self, page) -> None:
        page.rowconfigure(1, weight=1)
        page.columnconfigure(0, weight=1)

        toolbar = tk.Frame(page, bg=COLOR["card"])
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 8))
        self.queue_hint = tk.Label(
            toolbar, text="队列为空", bg=COLOR["card"], fg=COLOR["text_mute"], font=F.ui(9)
        )
        self.queue_hint.pack(side=tk.LEFT)
        PillButton(toolbar, "移除选中", command=self.remove_selected_queue,
                   kind="ghost", width=78, height=28, font=F.ui(9)).pack(side=tk.RIGHT)
        PillButton(toolbar, "清空", command=self.clear_queue, kind="ghost", width=56,
                   height=28, font=F.ui(9)).pack(side=tk.RIGHT, padx=(0, 6))

        table_wrap = tk.Frame(page, bg=COLOR["card"])
        table_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 6))
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        self.queue_tree = ttk.Treeview(
            table_wrap, columns=("index", "label", "status"), show="headings",
            selectmode="extended", height=8,
        )
        for key, (label, width, anchor) in {
            "index": ("#", 34, "center"),
            "label": ("链接", 196, "w"),
            "status": ("状态", 80, "w"),
        }.items():
            self.queue_tree.heading(key, text=label, anchor=anchor)
            self.queue_tree.column(key, width=width, minwidth=30, anchor=anchor,
                                   stretch=(key == "label"))
        self.queue_tree.tag_configure("pending", foreground=COLOR["text_sub"])
        self.queue_tree.tag_configure("running", foreground=COLOR["accent"])
        self.queue_tree.tag_configure("done", foreground=COLOR["ok"])
        self.queue_tree.tag_configure("failed", foreground=COLOR["err"])

        yscroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=yscroll.set)
        self.queue_tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self.queue_tree.bind("<Delete>", lambda _e: self.remove_selected_queue())

        self.queue_empty = tk.Label(
            page, text="队列为空\n在上方输入链接后按 Enter 加入", bg=COLOR["card"],
            fg=COLOR["text_mute"], font=F.ui(9), justify=tk.CENTER,
        )

    def _build_records_tab(self, page) -> None:
        page.rowconfigure(1, weight=1)
        page.columnconfigure(0, weight=1)

        toolbar = tk.Frame(page, bg=COLOR["card"])
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 8))
        search_wrap = tk.Frame(toolbar, bg=COLOR["card"])
        search_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.filter_entry = ttk.Entry(search_wrap, textvariable=self.filter_var, font=F.ui(9))
        self.filter_entry.pack(fill=tk.X, padx=(0, 8))
        self._install_placeholder(self.filter_entry, self.filter_var, "搜索用户 / 链接 …")
        self.filter_var.trace_add("write", lambda *_: self.refresh_history())

        table_wrap = tk.Frame(page, bg=COLOR["card"])
        table_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 6))
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        columns = ("user_name", "link", "output_path", "last_run", "runs")
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="browse")
        for key, (label, width, anchor) in {
            "user_name": ("用户", 84, "w"),
            "link": ("链接", 148, "w"),
            "output_path": ("输出目录", 96, "w"),
            "last_run": ("最近运行", 82, "w"),
            "runs": ("次数", 40, "center"),
        }.items():
            self.tree.heading(key, text=label, anchor=anchor)
            self.tree.column(key, width=width, minwidth=34, anchor=anchor,
                             stretch=(key == "link"))
        self.tree.tag_configure("odd", background=COLOR["card_alt"])
        self.tree.tag_configure("even", background=COLOR["card"])

        yscroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Double-1>", lambda _e: self.load_selected())
        self.tree.bind("<Return>", lambda _e: self.load_selected())
        self.tree.bind("<Button-3>", self._show_record_menu)

        self.record_menu = tk.Menu(self.root, tearoff=0, font=F.ui(9))
        self.record_menu.add_command(label="载入到输入框", command=self.load_selected)
        self.record_menu.add_command(label="加入下载队列", command=self.enqueue_selected)
        self.record_menu.add_command(label="设置用户名…", command=self.set_selected_name)
        self.record_menu.add_separator()
        self.record_menu.add_command(label="打开输出目录", command=self.open_selected_dir)
        self.record_menu.add_separator()
        self.record_menu.add_command(label="删除记录", command=self.delete_selected)

        actions = tk.Frame(page, bg=COLOR["card"])
        actions.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        for label, command, width in (
            ("载入", self.load_selected, 54),
            ("加入队列", self.enqueue_selected, 74),
            ("命名", self.set_selected_name, 54),
            ("删除", self.delete_selected, 54),
            ("刷新", self.refresh_history, 54),
        ):
            PillButton(actions, label, command=command, kind="ghost", width=width, height=28,
                       font=F.ui(9)).pack(side=tk.LEFT, padx=(0, 6))

    def _build_log_tab(self, body) -> None:
        toolbar = tk.Frame(body, bg=COLOR["card"])
        toolbar.pack(fill=tk.X, pady=(0, 8))

        self.log_level_box = ttk.Combobox(
            toolbar, textvariable=self.log_filter_var, values=["全部", "仅警告", "仅错误"],
            state="readonly", font=F.ui(9), width=8,
        )
        self.log_level_box.pack(side=tk.LEFT)
        self.log_level_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_log_filters())

        self.log_search = ttk.Entry(toolbar, textvariable=self.log_search_var, font=F.ui(9))
        self.log_search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        self._install_placeholder(self.log_search, self.log_search_var, "在日志中搜索 …")
        self.log_search_var.trace_add("write", lambda *_: self._apply_log_filters())

        PillButton(toolbar, "清空", command=self.clear_log, kind="ghost", width=52,
                   height=28, font=F.ui(9)).pack(side=tk.LEFT)
        PillButton(toolbar, "导出", command=self.save_log, kind="ghost", width=52,
                   height=28, font=F.ui(9)).pack(side=tk.LEFT, padx=(6, 0))
        self.log_counter = tk.Label(
            toolbar, text="0 行", bg=COLOR["card"], fg=COLOR["text_mute"], font=F.ui(8)
        )
        self.log_counter.pack(side=tk.RIGHT, padx=(8, 0))

        self.log = LogView(body)
        self.log.pack(fill=tk.BOTH, expand=True)

    def _build_footer(self) -> None:
        footer = tk.Frame(self.root, bg=COLOR["bg"])
        footer.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 14))
        footer.columnconfigure(1, weight=1)

        left = tk.Frame(footer, bg=COLOR["bg"])
        left.grid(row=0, column=0, sticky="w")
        tk.Label(
            left, textvariable=self.status_var, bg=COLOR["bg"], fg=COLOR["text_sub"],
            font=F.ui(9),
        ).pack(side=tk.LEFT)

        self.progress = ttk.Progressbar(
            footer, style="Accent.Horizontal.TProgressbar", mode="determinate",
            maximum=100, value=0,
        )
        self.progress.grid(row=0, column=1, sticky="ew", padx=16)

        self.stats_label = tk.Label(
            footer, text="成功 0 · 失败 0 · 跳过 0 · 用时 00:00",
            bg=COLOR["bg"], fg=COLOR["text_mute"], font=F.ui(9),
        )
        self.stats_label.grid(row=0, column=2, sticky="e")

    def _set_sash(self, ratio: float, attempt: int = 0) -> None:
        try:
            self.root.update_idletasks()
            width = self.panes.winfo_width()
            if width < 400:
                if attempt < 25:
                    self.root.after(120, lambda: self._set_sash(ratio, attempt + 1))
                return
            self.panes.sashpos(0, int(width * ratio))
            if self.panes.sashpos(0) <= 1 and attempt < 25:
                self.root.after(120, lambda: self._set_sash(ratio, attempt + 1))
        except Exception:
            if attempt < 25:
                self.root.after(120, lambda: self._set_sash(ratio, attempt + 1))

    # ---------------------------------------------------------------- 占位提示

    def _install_placeholder(self, entry, variable: tk.StringVar, hint: str) -> None:
        state = {"placeholder": False}

        def show() -> None:
            if not variable.get():
                state["placeholder"] = True
                variable.set(hint)
                entry.configure(foreground=COLOR["text_mute"])

        def hide(_event=None) -> None:
            if state["placeholder"]:
                state["placeholder"] = False
                variable.set("")
                entry.configure(foreground=COLOR["text"])

        entry.bind("<FocusIn>", hide, add="+")
        entry.bind("<FocusOut>", lambda _e: show(), add="+")
        show()
        if entry is getattr(self, "filter_entry", None):
            self._filter_placeholder = state
        else:
            self._log_placeholder = state

    def _placeholder_value(self, attr: str, variable: tk.StringVar) -> str:
        state = getattr(self, attr, None)
        if state and state.get("placeholder"):
            return ""
        return variable.get().strip()

    # ---------------------------------------------------------------- 配置

    def _config_candidates(self) -> List[str]:
        names = sorted(p.name for p in self.base_dir.glob("*.yml"))
        names += sorted(p.name for p in self.base_dir.glob("*.yaml"))
        default = ["config_simple.yml", "config.yml"]
        merged = [d for d in default if d in names or (self.base_dir / d).exists()]
        merged += [n for n in names if n not in merged and n != RUN_CONFIG]
        if not merged:
            merged = default
        if self.config_var.get() not in merged:
            merged.insert(0, self.config_var.get())
        return merged

    # ---------------------------------------------------------------- 设置页

    def _build_settings_tab(self, page) -> None:
        page.columnconfigure(0, weight=1)

        wrapper = tk.Frame(page, bg=COLOR["card"])
        wrapper.grid(row=0, column=0, sticky="nsew", padx=14, pady=12)
        wrapper.columnconfigure(1, weight=1)
        wrapper.columnconfigure(3, weight=1)

        def label(text, row, column, pady=(0, 10)):
            tk.Label(
                wrapper, text=text, bg=COLOR["card"], fg=COLOR["text_sub"], font=F.ui(9)
            ).grid(row=row, column=column, sticky="w", padx=(0, 8), pady=pady)

        label("每秒抽帧", 0, 0)
        ttk.Entry(wrapper, textvariable=self.ov_fps, width=8, font=F.ui(9)).grid(
            row=0, column=1, sticky="w", pady=(0, 10)
        )
        label("并发线程", 0, 2)
        ttk.Entry(wrapper, textvariable=self.ov_thread, width=8, font=F.ui(9)).grid(
            row=0, column=3, sticky="w", pady=(0, 10)
        )

        label("作品上限", 1, 0)
        ttk.Entry(wrapper, textvariable=self.ov_num_post, width=8, font=F.ui(9)).grid(
            row=1, column=1, sticky="w", pady=(0, 10)
        )
        label("点赞上限", 1, 2)
        ttk.Entry(wrapper, textvariable=self.ov_num_like, width=8, font=F.ui(9)).grid(
            row=1, column=3, sticky="w", pady=(0, 10)
        )

        label("重试次数", 2, 0)
        ttk.Entry(wrapper, textvariable=self.ov_retry, width=8, font=F.ui(9)).grid(
            row=2, column=1, sticky="w", pady=(0, 10)
        )
        CheckToggle(wrapper, "启用切帧", self.ov_slice).grid(
            row=2, column=2, columnspan=2, sticky="w", pady=(0, 10)
        )

        modes = tk.Frame(wrapper, bg=COLOR["card"])
        modes.grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 8))
        tk.Label(
            modes, text="主页下载内容", bg=COLOR["card"], fg=COLOR["text_sub"], font=F.ui(9)
        ).pack(side=tk.LEFT, padx=(0, 10))
        for key, text in (("post", "作品"), ("like", "点赞"), ("mix", "合集")):
            CheckToggle(modes, text, self.ov_mode[key]).pack(side=tk.LEFT, padx=(0, 12))

        extra = tk.Frame(wrapper, bg=COLOR["card"])
        extra.grid(row=4, column=0, columnspan=4, sticky="w")
        CheckToggle(extra, "接口失败时用浏览器兜底", self.ov_browser).pack(side=tk.LEFT)
        CheckToggle(extra, "兜底浏览器无头运行", self.ov_headless).pack(side=tk.LEFT, padx=(16, 0))

        tk.Label(
            wrapper,
            text="上限填 0 表示不限制。保存后写入 ui_run_config.yml，"
                 "不会改动你的原始配置文件。\n"
                 "「续期登录」会读取浏览器里的最新登录态并回写 Cookie，尽量免去重新扫码。",
            bg=COLOR["card"], fg=COLOR["text_mute"], font=F.ui(8), wraplength=420,
            justify=tk.LEFT,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 0))

        actions = tk.Frame(wrapper, bg=COLOR["card"])
        actions.grid(row=6, column=0, columnspan=4, sticky="w", pady=(14, 0))
        PillButton(actions, "保存设置", command=self._apply_settings, width=88, height=30,
                   font=F.ui(9)).pack(side=tk.LEFT)
        PillButton(actions, "恢复默认", command=self._reset_settings, kind="ghost", width=84,
                   height=30, font=F.ui(9)).pack(side=tk.LEFT, padx=(8, 0))
        PillButton(actions, "续期登录", command=self.refresh_session, kind="secondary",
                   width=88, height=30, font=F.ui(9)).pack(side=tk.LEFT, padx=(8, 0))

    def _reset_settings(self) -> None:
        self.ov_slice.set(True)
        self.ov_fps.set("10")
        self.ov_num_post.set("0")
        self.ov_num_like.set("0")
        self.ov_thread.set("5")
        self.ov_retry.set("3")
        self.ov_browser.set(True)
        self.ov_headless.set(True)
        for key in self.ov_mode:
            self.ov_mode[key].set(key == "post")

    @staticmethod
    def _as_int(var: tk.StringVar, name: str, low: int, high: int) -> int:
        try:
            value = int(str(var.get()).strip())
        except Exception:
            raise ValueError(f"{name} 需要填写整数")
        if not low <= value <= high:
            raise ValueError(f"{name} 需在 {low} ~ {high} 之间")
        return value

    def _apply_settings(self) -> None:
        try:
            overrides = {
                "slice_videos": bool(self.ov_slice.get()),
                "slice_fps": self._as_int(self.ov_fps, "每秒抽帧", 1, 60),
                "number_post": self._as_int(self.ov_num_post, "作品上限", 0, 9999),
                "number_like": self._as_int(self.ov_num_like, "点赞上限", 0, 9999),
                "thread": self._as_int(self.ov_thread, "并发线程", 1, 32),
                "retry_times": self._as_int(self.ov_retry, "重试次数", 0, 10),
                "browser_recovery": bool(self.ov_browser.get()),
                "headless": bool(self.ov_headless.get()),
                "mode": [k for k in ("post", "like", "mix") if self.ov_mode[k].get()] or ["post"],
            }
        except ValueError as exc:
            messagebox.showwarning("参数有误", str(exc), parent=self.root)
            return
        self.overrides = overrides
        self._refresh_summary()
        self.set_status("设置已保存，下次运行生效", "ok")

    def _sync_settings_vars(self) -> None:
        """把当前 overrides 同步到设置页控件。"""
        self.ov_slice.set(bool(self.overrides.get("slice_videos", True)))
        self.ov_fps.set(str(self.overrides.get("slice_fps", 10)))
        self.ov_num_post.set(str(self.overrides.get("number_post", 0)))
        self.ov_num_like.set(str(self.overrides.get("number_like", 0)))
        self.ov_thread.set(str(self.overrides.get("thread", 5)))
        self.ov_retry.set(str(self.overrides.get("retry_times", 3)))
        self.ov_browser.set(bool(self.overrides.get("browser_recovery", True)))
        self.ov_headless.set(bool(self.overrides.get("headless", True)))
        modes = self.overrides.get("mode") or ["post"]
        for key in self.ov_mode:
            self.ov_mode[key].set(key in modes)

    def _write_run_config(self, base_config: str) -> Optional[Path]:
        """把界面设置合并进独立运行配置，避免改动用户原始配置文件。"""
        try:
            import yaml
        except Exception:
            return None
        base_path = self.base_dir / base_config
        data: Dict = {}
        if base_path.exists():
            try:
                data = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}

        overrides = self.overrides or {}
        data["slice_videos"] = bool(overrides.get("slice_videos", data.get("slice_videos", True)))
        data["slice_fps"] = int(overrides.get("slice_fps", data.get("slice_fps", 10)) or 10)
        number = data.get("number")
        if not isinstance(number, dict):
            number = {}
        number["post"] = int(overrides.get("number_post", number.get("post", 0)) or 0)
        number["like"] = int(overrides.get("number_like", number.get("like", 0)) or 0)
        data["number"] = number
        data["mode"] = list(overrides.get("mode", data.get("mode", ["post"])) or ["post"])
        data["thread"] = int(overrides.get("thread", data.get("thread", 5)) or 5)
        data["retry_times"] = int(overrides.get("retry_times", data.get("retry_times", 3)) or 3)
        data["user_posts_browser_recovery"] = bool(
            overrides.get("browser_recovery", data.get("user_posts_browser_recovery", True))
        )
        data["user_posts_browser_headless"] = bool(
            overrides.get("headless", data.get("user_posts_browser_headless", True))
        )
        data["path"] = self.path_var.get().strip() or data.get("path", "./Downloaded/")

        target = self.base_dir / RUN_CONFIG
        try:
            target.write_text(
                "# 由 ui.py 自动生成，请勿手工编辑；修改请使用界面中的「设置」\n"
                + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            return target
        except Exception:
            return None

    # ---------------------------------------------------------------- 队列

    def add_to_queue(self) -> None:
        raw = self.url_var.get().strip()
        if not raw:
            self.set_status("请先输入链接", "warn")
            self.url_entry.focus_set()
            return
        urls = URL_RE.findall(raw) or ([raw] if raw.startswith("http") else [])
        if not urls:
            self.set_status("没有识别到有效链接", "warn")
            return
        existing = {item["url"] for item in self.queue_items}
        added = 0
        for url in urls:
            if url in existing:
                continue
            self.queue_items.append({"url": url, "status": "pending", "detail": "等待"})
            existing.add(url)
            added += 1
        self.url_var.set("")
        self.refresh_queue()
        self.set_status(f"已加入 {added} 个链接" if added else "链接已在队列中",
                        "ok" if added else "warn")

    def remove_selected_queue(self) -> None:
        if self.process and self.process.poll() is None:
            self.set_status("运行中不能修改队列", "warn")
            return
        selection = set(self.queue_tree.selection())
        if not selection:
            return
        self.queue_items = [
            item for index, item in enumerate(self.queue_items, start=1)
            if str(index) not in selection
        ]
        self.refresh_queue()

    def clear_queue(self) -> None:
        if self.process and self.process.poll() is None:
            self.set_status("运行中不能清空队列", "warn")
            return
        self.queue_items = []
        self.refresh_queue()

    def refresh_queue(self) -> None:
        if not hasattr(self, "queue_tree"):
            return
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        labels = {"pending": "等待", "running": "下载中", "done": "完成", "failed": "失败"}
        for index, item in enumerate(self.queue_items, start=1):
            status = item.get("status", "pending")
            self.queue_tree.insert(
                "", tk.END, iid=str(index),
                values=(index, url_label(item["url"]),
                        item.get("detail") or labels.get(status, status)),
                tags=(status,),
            )
        pending = sum(1 for i in self.queue_items if i["status"] in ("pending", "failed"))
        if getattr(self, "queue_hint", None) is not None:
            self.queue_hint.configure(
                text=f"待下载 {pending} 个 · 共 {len(self.queue_items)} 个"
                if self.queue_items else "队列为空"
            )
        if getattr(self, "queue_empty", None) is not None:
            if self.queue_items:
                self.queue_empty.grid_forget()
            else:
                self.queue_empty.grid(row=2, column=0, pady=(0, 10))

    def _set_item_status(self, index: int, status: str, detail: str = "") -> None:
        if 1 <= index <= len(self.queue_items):
            self.queue_items[index - 1]["status"] = status
            self.queue_items[index - 1]["detail"] = detail
            self.refresh_queue()

    # ---------------------------------------------------------------- 记录

    def _filter_keyword(self) -> str:
        return self._placeholder_value("_filter_placeholder", self.filter_var).lower()

    def _visible_records(self) -> List[Dict]:
        keyword = self._filter_keyword()
        records = self.records
        if keyword:
            records = [
                r for r in records
                if keyword in str(r.get("user_name", "")).lower()
                or keyword in str(r.get("url", "")).lower()
                or keyword in str(r.get("output_path", "")).lower()
                or keyword in str(r.get("user_id", "")).lower()
            ]
        return sorted(records, key=lambda r: str(r.get("last_run", "")), reverse=True)

    def refresh_history(self) -> None:
        if not hasattr(self, "tree"):
            return
        self.records = load_history(self.history_path)
        changed = False
        for index, record in enumerate(self.records):
            if not record.get("id"):
                record["id"] = f"rec-{int(time.time() * 1000)}-{index}"
                changed = True
        if changed:
            save_history(self.history_path, self.records)

        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, record in enumerate(self._visible_records()):
            self.tree.insert(
                "", tk.END, iid=record["id"], values=self._record_values(record),
                tags=("odd" if index % 2 else "even",),
            )

    @staticmethod
    def _display_name(record: Dict) -> str:
        name = str(record.get("user_name") or "").strip()
        if name:
            return shorten(name, 10)
        user_id = str(record.get("user_id") or "").strip()
        return shorten(user_id, 10) if user_id else "未命名"

    @staticmethod
    def _display_path(path: str) -> str:
        text = str(path or "").strip().replace("/", "\\")
        if not text:
            return "-"
        tail = text.rstrip("\\").split("\\")[-1]
        return shorten(tail or text, 16)

    @staticmethod
    def _display_time(stamp: str) -> str:
        text = str(stamp or "").strip()
        return text[:10] if len(text) >= 10 else (text or "-")

    def _record_values(self, record: Dict) -> tuple:
        return (
            self._display_name(record),
            url_label(record.get("url", "")),
            self._display_path(record.get("output_path", "")),
            self._display_time(record.get("last_run", "")),
            record.get("runs", 0),
        )

    def get_selected_record(self) -> Optional[Dict]:
        selection = self.tree.selection()
        if not selection:
            return None
        for record in self.records:
            if record.get("id") == selection[0]:
                return record
        return None

    def _show_record_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        try:
            self.record_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.record_menu.grab_release()

    def load_selected(self) -> None:
        record = self.get_selected_record()
        if not record:
            self.set_status("请先选择一条任务记录", "warn")
            return
        self.url_var.set(record.get("url", ""))
        self.config_var.set(record.get("config", "config_simple.yml"))
        self.path_var.set(record.get("output_path", "./my_videos_fix/"))
        self.set_status(f"已载入：{record.get('user_name') or '未命名'}", "ok")

    def enqueue_selected(self) -> None:
        record = self.get_selected_record()
        if not record:
            self.set_status("请先选择一条任务记录", "warn")
            return
        self.url_var.set(record.get("url", ""))
        self.add_to_queue()

    def save_current_record(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("缺少链接", "请先填写抖音链接。", parent=self.root)
            return
        self.upsert_record(url, self.config_var.get().strip(), self.path_var.get().strip())
        self.refresh_history()
        self.set_status("当前设置已保存到任务记录", "ok")

    def set_selected_name(self) -> None:
        record = self.get_selected_record()
        if not record:
            self.set_status("请先选择一条任务记录", "warn")
            return
        name = simpledialog.askstring(
            "设置用户名", "请输入用户名：", initialvalue=record.get("user_name", ""),
            parent=self.root,
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("缺少用户名", "请输入用户名。", parent=self.root)
            return
        record["user_name"] = name
        if not record.get("user_id"):
            record["user_id"] = extract_user_id(record.get("url", ""))
        save_history(self.history_path, self.records)
        self.refresh_history()
        self.set_status("用户名已更新", "ok")

    def delete_selected(self) -> None:
        record = self.get_selected_record()
        if not record:
            self.set_status("请先选择一条任务记录", "warn")
            return
        label = record.get("user_name") or record.get("user_id") or "该记录"
        if not messagebox.askyesno("删除记录", f"确定删除「{label}」吗？", parent=self.root):
            return
        self.records = [r for r in self.records if r.get("id") != record.get("id")]
        save_history(self.history_path, self.records)
        self.refresh_history()
        self.set_status("记录已删除", "ok")

    def upsert_record(self, url: str, config: str, output: str) -> str:
        now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        user_id = extract_user_id(url)
        existing = next((r for r in self.records if r.get("url") == url), None)
        if existing:
            existing.update(config=config, output_path=output, last_run=now,
                            runs=int(existing.get("runs", 0)) + 1)
            existing["user_id"] = user_id or existing.get("user_id", "")
            if not existing.get("id"):
                existing["id"] = f"rec-{int(time.time() * 1000)}"
            record_id = existing["id"]
        else:
            record_id = f"rec-{int(time.time() * 1000)}"
            self.records.append({
                "id": record_id, "url": url, "user_id": user_id, "user_name": "",
                "config": config, "output_path": output, "last_run": now, "runs": 1,
            })
        save_history(self.history_path, self.records)
        return record_id

    # ---------------------------------------------------------------- 操作

    def browse_output(self) -> None:
        initial = self.path_var.get().strip() or str(self.base_dir)
        initial_path = Path(initial)
        if not initial_path.is_absolute():
            initial_path = self.base_dir / initial
        chosen = filedialog.askdirectory(
            title="选择保存目录",
            initialdir=str(initial_path if initial_path.exists() else self.base_dir),
            parent=self.root,
        )
        if chosen:
            self.path_var.set(chosen)

    def open_output_dir(self) -> None:
        self._open_path(Path(self.path_var.get().strip() or "."))

    def open_selected_dir(self) -> None:
        record = self.get_selected_record()
        if record:
            self._open_path(Path(record.get("output_path") or "."))

    def _open_path(self, path: Path) -> None:
        target = path if path.is_absolute() else (self.base_dir / path)
        if not target.exists():
            self.set_status(f"目录不存在：{target}", "warn")
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def clear_log(self) -> None:
        self.log.clear()
        self._update_log_counter()

    def _apply_log_filters(self) -> None:
        level = {"全部": "all", "仅警告": "warn", "仅错误": "err"}.get(
            self.log_filter_var.get(), "all"
        )
        self.log.set_level(level)
        keyword = self._placeholder_value("_log_placeholder", self.log_search_var)
        self.log.set_keyword(keyword)
        self._update_log_counter()
        if self.autoscroll_var.get():
            self.log.scroll_to_end()

    def save_log(self) -> None:
        content = self.log.content().strip()
        if not content:
            self.set_status("当前没有日志内容", "warn")
            return
        chosen = filedialog.asksaveasfilename(
            title="保存日志", defaultextension=".log",
            initialfile=f"download-{datetime.now():%Y%m%d-%H%M%S}.log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt")],
            parent=self.root,
        )
        if not chosen:
            return
        try:
            Path(chosen).write_text(content, encoding="utf-8")
            self.set_status("日志已保存", "ok")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)

    # ---------------------------------------------------------------- 执行

    def _pending_urls(self) -> List[str]:
        return [item["url"] for item in self.queue_items
                if item["status"] in ("pending", "failed")]

    def run_download(self) -> None:
        if self._suspended:
            return
        if self.process and self.process.poll() is None:
            self.set_status("已有任务正在运行", "warn")
            return

        urls = self._pending_urls()
        if not urls:
            single = self.url_var.get().strip()
            if single:
                urls = URL_RE.findall(single) or [single]
        if not urls:
            if self.queue_items:
                messagebox.showinfo("没有待下载项", "队列中的链接都已处理完成。", parent=self.root)
            else:
                messagebox.showwarning("缺少链接", "请输入或加入至少一个链接。", parent=self.root)
                self.url_entry.focus_set()
            return

        config = self.config_var.get().strip() or "config_simple.yml"
        if not (self.base_dir / config).exists() and not Path(config).exists():
            messagebox.showerror("配置不存在", f"找不到配置文件：{config}", parent=self.root)
            return

        run_config = self._write_run_config(config) or Path(config)
        output = self.path_var.get().strip() or "./my_videos_fix/"

        cmd = [
            sys.executable, str(self.base_dir / "downloader.py"),
            "-c", str(run_config), "-u", *urls, "--path", output,
            "--slice-backfill" if self.backfill_var.get() else "--no-slice-backfill",
        ]

        self._run_sequence = urls
        self._current_index = 0
        for item in self.queue_items:
            if item["url"] in urls:
                item["status"] = "pending"
                item["detail"] = "等待"
        self.refresh_queue()

        self.append_log("\n" + "─" * 68 + "\n")
        self.append_log(f"$ {' '.join(cmd)}\n")

        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        env.pop("COLUMNS", None)
        try:
            self.process = subprocess.Popen(
                cmd, cwd=str(self.base_dir), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            self.set_status("启动失败", "err")
            return

        self.started_at = time.monotonic()
        self.stats = {"total": len(urls), "success": 0, "failed": 0, "skipped": 0}
        self._success_at_item_start = 0
        self.progress.configure(mode="determinate", value=0,
                                style="Accent.Horizontal.TProgressbar")
        self._set_running(True)
        self.set_status(f"正在运行（{len(urls)} 个链接）…", "ok")

        self.upsert_record(urls[0], config, output)
        self.refresh_history()

        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()
        self._tick_elapsed()

    def _set_running(self, running: bool) -> None:
        if getattr(self, "run_btn", None) is None:
            return
        self.run_btn.set_enabled(not running)
        self.stop_btn.set_enabled(running)
        self.run_btn.set_text("运行中…" if running else "▶  开始下载")

    def _read_output(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            self.output_queue.put(line)
        code = process.wait()
        self.output_queue.put(f"\n—— 进程结束，退出码 {code} ——\n")
        self.output_queue.put("__PROCESS_DONE__")

    def poll_output(self) -> None:
        if self._suspended:
            self.root.after(150, self.poll_output)
            return
        chunks: List[str] = []
        done = False
        for _ in range(400):
            try:
                item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if item == "__PROCESS_DONE__":
                done = True
                continue
            chunks.append(item)
            self._consume_metrics(item)

        if chunks:
            self.append_log("".join(chunks))
            if self.autoscroll_var.get():
                self.log.scroll_to_end()

        if done:
            self.process = None
            self._set_running(False)
            self._finish_run()

        delay = 20 if not self.output_queue.empty() else 90
        self.root.after(delay, self.poll_output)

    def _consume_metrics(self, chunk: str) -> None:
        for line in chunk.splitlines():
            match = PROGRESS_RE.search(line)
            if match:
                done, total, ok, failed = (int(g) for g in match.groups())
                self.stats.update(total=total, success=ok, failed=failed)
                self.progress.configure(value=(done / total * 100) if total else 0)
                if self._current_index:
                    status = "done" if ok > self._success_at_item_start else "failed"
                    self._set_item_status(
                        self._current_index, status, "完成" if status == "done" else "失败"
                    )
                self._success_at_item_start = ok
                continue
            link = LINK_RE.search(line)
            if link:
                index = int(link.group(1))
                self._current_index = index
                self._success_at_item_start = self.stats.get("success", 0)
                self._set_item_status(index, "running", "下载中")
                self.set_status(
                    f"处理中 {link.group(1)}/{link.group(2)}：{url_label(link.group(3))}", "ok"
                )
                continue
            stat = STAT_ROW_RE.match(line.replace("│", " "))
            if stat:
                key, value = stat.group(1), stat.group(2)
                if key in ("Total", "Success", "Failed", "Skipped") and value.isdigit():
                    self.stats[key.lower()] = int(value)
                continue
            if "Download task completed" in line:
                self.progress.configure(value=100)
                continue
            session = SESSION_RE.search(line)
            if session:
                self._update_session(session.group(1), True)
                continue
            expired = SESSION_EXPIRED_RE.search(line)
            if expired:
                self._update_session(expired.group(1), False)
                continue

    def _finish_run(self) -> None:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        failed = self.stats.get("failed", 0)
        success = self.stats.get("success", 0)
        for item in self.queue_items:
            if item["status"] == "running":
                item["status"] = "done"
                item["detail"] = "完成"
        self.refresh_queue()

        if failed or success:
            self.progress.configure(
                value=100,
                style="Warn.Horizontal.TProgressbar" if failed
                else "Accent.Horizontal.TProgressbar",
            )
        if failed:
            self.set_status(f"任务结束：成功 {success} · 失败 {failed}", "warn")
        elif success:
            self.set_status(f"任务完成：成功 {success} 项，用时 {human_duration(elapsed)}", "ok")
        else:
            self.set_status("任务已结束", "idle")
        self._update_stats_label(elapsed)
        if self.notify_var.get():
            self._notify(failed)
        self._current_index = 0

    def _notify(self, failed: int) -> None:
        """完成后提醒：窗口置前 + 系统提示音。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.bell()
        except Exception:
            pass
        if failed:
            self.set_status(self.status_var.get() + "（请检查日志中的错误）", "warn")

    def _tick_elapsed(self) -> None:
        if self.process and self.process.poll() is None:
            elapsed = time.monotonic() - self.started_at if self.started_at else 0
            self._update_stats_label(elapsed)
            self.root.after(500, self._tick_elapsed)
            return
        if self.started_at:
            self._update_stats_label(time.monotonic() - self.started_at)

    def _update_stats_label(self, elapsed: float) -> None:
        self._last_elapsed = elapsed
        if getattr(self, "stats_label", None) is None:
            return
        self.stats_label.configure(
            text=(
                f"成功 {self.stats.get('success', 0)} · "
                f"失败 {self.stats.get('failed', 0)} · "
                f"跳过 {self.stats.get('skipped', 0)} · "
                f"用时 {human_duration(elapsed)}"
            )
        )

    def stop_download(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.set_status("正在停止…", "warn")
        try:
            self.process.terminate()
        except Exception:
            return
        self.root.after(4000, self._force_kill)

    def _force_kill(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.kill()
                self.append_log("\n—— 已强制结束进程 ——\n")
            except Exception:
                pass

    def append_log(self, text: str) -> None:
        self.log.append(text)
        self._update_log_counter()

    def _update_log_counter(self) -> None:
        if getattr(self, "log_counter", None) is None or not hasattr(self, "log"):
            return
        shown = len(self.log.content().splitlines()) - 1
        self.log_counter.configure(text=f"{max(0, shown)} 行")

    def set_status(self, message: str, tone: str = "idle") -> None:
        self.status_var.set(message)
        if getattr(self, "status_pill", None) is None:
            return
        palette = {"ok": COLOR["ok"], "warn": COLOR["warn"], "err": COLOR["err"],
                   "idle": COLOR["text_mute"]}
        badge = {"ok": ("运行中" if self.process else "就绪"), "warn": "注意",
                 "err": "出错", "idle": "就绪"}
        self.status_pill.update_state(badge.get(tone, "就绪"), palette.get(tone, COLOR["ok"]))

    # ---------------------------------------------------------------- 退出

    def _collect_state(self) -> Dict:
        try:
            sash = self.panes.sashpos(0) / max(1, self.panes.winfo_width())
        except Exception:
            sash = self.state.get("sash", 0.46)
        return {
            "geometry": self.root.geometry(),
            "config": self.config_var.get(),
            "path": self.path_var.get(),
            "backfill": self.backfill_var.get(),
            "autoscroll": self.autoscroll_var.get(),
            "notify": self.notify_var.get(),
            "theme": self.state.get("theme", "light"),
            "session_date": getattr(self, "session_date", ""),
            "session_ok": getattr(self, "session_ok", True),
            "sash": round(float(sash), 3),
            "overrides": self.overrides,
            "queue": [i["url"] for i in self.queue_items
                      if i["status"] in ("pending", "failed")],
        }

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("退出", "任务仍在运行，确定停止并退出吗？", parent=self.root):
                return
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        write_json(self.state_path, self._collect_state())
        self.root.destroy()


# ---------------------------------------------------------------- 入口


def main() -> None:
    enable_dpi_awareness()
    log_startup(f"启动 {APP_NAME} v{APP_VERSION} …")
    selftest = "--selftest" in sys.argv[1:]

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        log_startup(f"无法创建窗口：{exc}")
        raise SystemExit(1)

    F.init()
    log_startup("窗口已创建")
    try:
        app = DownloaderUI(root)
        root.update_idletasks()
        root.update()
    except Exception:
        log_startup("界面初始化失败")
        import traceback

        traceback.print_exc()
        raise SystemExit(1)

    log_startup("界面已就绪")
    if selftest:
        root.after(1500, root.destroy)
        root.mainloop()
        log_startup("自检完成")
        print("SELFTEST OK", flush=True)
        return

    root.mainloop()


if __name__ == "__main__":
    main()
