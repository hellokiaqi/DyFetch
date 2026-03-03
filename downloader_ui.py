#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import tkinter as tk
from tkinter import messagebox, ttk

HISTORY_FILE = "ui_history.json"


def extract_user_id(url: str) -> str:
    """Extract sec_user_id/user id from a Douyin URL."""
    if not url:
        return ""
    text = url.strip()
    parsed = urlparse(text)

    user_match = re.search(r"/user/([^/?#]+)", parsed.path or "")
    if user_match:
        return user_match.group(1)

    query = parse_qs(parsed.query or "")
    for key in ("sec_user_id", "sec_uid", "user_id"):
        values = query.get(key)
        if values:
            return values[0]
    return ""


def load_history(file_path: Path) -> List[Dict]:
    if not file_path.exists():
        return []
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw
    except Exception:
        pass
    return []


def save_history(file_path: Path, records: List[Dict]) -> None:
    file_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class DownloaderUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Douyin Downloader UI")
        self.root.geometry("1600x900")

        self.base_dir = Path(__file__).resolve().parent
        self.history_path = self.base_dir / HISTORY_FILE
        self.records = load_history(self.history_path)

        self.process: Optional[subprocess.Popen] = None
        self.output_queue: "queue.Queue[str]" = queue.Queue()

        self.url_var = tk.StringVar()
        self.config_var = tk.StringVar(value="config_simple.yml")
        self.path_var = tk.StringVar(value="./my_videos_fix/")
        self.manual_name_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")

        self._build_layout()
        self.refresh_history()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_output)
        self.root.after(160, self._set_initial_pane)

    def _set_initial_pane(self) -> None:
        try:
            width = self.middle_frame.winfo_width() or self.root.winfo_width()
            self.middle_frame.sashpos(0, max(560, int(width * 0.42)))
        except Exception:
            pass

    def _build_layout(self) -> None:
        root_frame = ttk.Frame(self.root, padding=10)
        root_frame.pack(fill=tk.BOTH, expand=True)

        input_frame = ttk.LabelFrame(root_frame, text="Run Downloader", padding=8)
        input_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(input_frame, text="URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(input_frame, textvariable=self.url_var, width=120).grid(
            row=0, column=1, columnspan=7, sticky="ew", padx=(8, 0)
        )

        ttk.Label(input_frame, text="Config").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.config_var, width=45).grid(
            row=1, column=1, sticky="ew", padx=(8, 12), pady=(8, 0)
        )

        ttk.Label(input_frame, text="Output Path").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.path_var, width=45).grid(
            row=1, column=3, columnspan=3, sticky="ew", padx=(8, 12), pady=(8, 0)
        )

        ttk.Button(input_frame, text="Run", command=self.run_download).grid(
            row=1, column=6, pady=(8, 0), padx=(0, 6)
        )
        ttk.Button(input_frame, text="Stop", command=self.stop_download).grid(
            row=1, column=7, pady=(8, 0)
        )

        ttk.Label(input_frame, text="Manual Username").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(input_frame, textvariable=self.manual_name_var, width=45).grid(
            row=2, column=1, columnspan=3, sticky="ew", padx=(8, 12), pady=(8, 0)
        )

        ttk.Label(input_frame, textvariable=self.status_var).grid(
            row=3, column=0, columnspan=8, sticky="w", pady=(8, 0)
        )

        for col in range(8):
            input_frame.columnconfigure(col, weight=1 if col in (1, 3, 4, 5) else 0)

        self.middle_frame = ttk.PanedWindow(root_frame, orient=tk.HORIZONTAL)
        self.middle_frame.pack(fill=tk.BOTH, expand=True)

        history_frame = ttk.LabelFrame(self.middle_frame, text="Saved Records", padding=8)
        self.middle_frame.add(history_frame, weight=2)

        columns = ("user_name", "user_id", "url", "output_path", "last_run", "runs")
        self.tree = ttk.Treeview(history_frame, columns=columns, show="headings", height=18)
        self.tree.heading("user_name", text="Username")
        self.tree.heading("user_id", text="User ID")
        self.tree.heading("url", text="URL")
        self.tree.heading("output_path", text="Output")
        self.tree.heading("last_run", text="Last Run")
        self.tree.heading("runs", text="Runs")
        self.tree.column("user_name", width=180, anchor="w")
        self.tree.column("user_id", width=210, anchor="w")
        self.tree.column("url", width=360, anchor="w")
        self.tree.column("output_path", width=180, anchor="w")
        self.tree.column("last_run", width=140, anchor="w")
        self.tree.column("runs", width=50, anchor="center")

        yscroll = ttk.Scrollbar(history_frame, orient=tk.VERTICAL, command=self.tree.yview)
        xscroll = ttk.Scrollbar(history_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        btn_frame = ttk.Frame(history_frame)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(btn_frame, text="Load Selected", command=self.load_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Save Current", command=self.save_current_record).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Set Name", command=self.set_selected_name).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Delete Selected", command=self.delete_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Refresh", command=self.refresh_history).pack(side=tk.LEFT)

        history_frame.rowconfigure(0, weight=1)
        history_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _event: self.load_selected())

        log_frame = ttk.LabelFrame(self.middle_frame, text="Output", padding=8)
        self.middle_frame.add(log_frame, weight=5)

        self.log_text = tk.Text(log_frame, wrap=tk.NONE, height=30, font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll_y = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll_y.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll_y.set)

        log_scroll_x = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        log_scroll_x.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(xscrollcommand=log_scroll_x.set)

        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    def _find_record_by_id(self, record_id: str) -> Optional[Dict]:
        for record in self.records:
            if record.get("id") == record_id:
                return record
        return None

    def _record_values(self, record: Dict) -> tuple:
        return (
            record.get("user_name", "") or record.get("user_id", ""),
            record.get("user_id", ""),
            record.get("url", ""),
            record.get("output_path", ""),
            record.get("last_run", ""),
            record.get("runs", 0),
        )

    def _update_tree_row(self, record: Dict) -> None:
        record_id = record.get("id", "")
        if not record_id:
            return
        if self.tree.exists(record_id):
            self.tree.item(record_id, values=self._record_values(record))
        else:
            self.tree.insert("", tk.END, iid=record_id, values=self._record_values(record))

    def refresh_history(self) -> None:
        self.records = load_history(self.history_path)
        self.records.sort(key=lambda x: x.get("last_run", ""), reverse=True)
        changed = False

        for item in self.tree.get_children():
            self.tree.delete(item)

        for record in self.records:
            record_id = record.get("id", "")
            if not record_id:
                record_id = f"rec-{int(datetime.now().timestamp() * 1000)}-{len(self.records)}"
                record["id"] = record_id
                changed = True
            self.tree.insert(
                "",
                tk.END,
                iid=record_id,
                values=self._record_values(record),
            )

        if changed:
            save_history(self.history_path, self.records)

    def get_selected_record(self) -> Optional[Dict]:
        selected = self.tree.selection()
        if not selected:
            return None
        record_id = selected[0]
        for record in self.records:
            if record.get("id") == record_id:
                return record
        return None

    def load_selected(self) -> None:
        record = self.get_selected_record()
        if not record:
            return
        self.url_var.set(record.get("url", ""))
        self.config_var.set(record.get("config", "config_simple.yml"))
        self.path_var.set(record.get("output_path", "./my_videos_fix/"))
        self.manual_name_var.set(record.get("user_name", ""))

    def save_current_record(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter a URL first.")
            return
        self.upsert_record(
            url,
            self.config_var.get().strip(),
            self.path_var.get().strip(),
            self.manual_name_var.get().strip(),
        )
        self.refresh_history()
        self.status_var.set("Record saved.")

    def delete_selected(self) -> None:
        record = self.get_selected_record()
        if not record:
            return
        self.records = [item for item in self.records if item.get("id") != record.get("id")]
        save_history(self.history_path, self.records)
        self.refresh_history()
        self.status_var.set("Record deleted.")

    def set_selected_name(self) -> None:
        record = self.get_selected_record()
        if not record:
            return
        manual_name = self.manual_name_var.get().strip()
        if not manual_name:
            messagebox.showwarning("Missing Username", "Please input a username first.")
            return
        record["user_name"] = manual_name
        if not record.get("user_id"):
            record["user_id"] = extract_user_id(record.get("url", ""))
        save_history(self.history_path, self.records)
        self._update_tree_row(record)
        self.status_var.set("Username updated.")

    def upsert_record(self, url: str, config_path: str, output_path: str, user_name: str = "") -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_id = extract_user_id(url)

        existing = None
        for record in self.records:
            if record.get("url") == url:
                existing = record
                break

        if existing:
            existing["config"] = config_path
            existing["output_path"] = output_path
            existing["user_id"] = user_id or existing.get("user_id", "")
            if user_name:
                existing["user_name"] = user_name
            existing["last_run"] = now
            existing["runs"] = int(existing.get("runs", 0)) + 1
            record_id = existing.get("id", "")
            if not record_id:
                record_id = f"rec-{int(datetime.now().timestamp() * 1000)}"
                existing["id"] = record_id
        else:
            record_id = f"rec-{int(datetime.now().timestamp() * 1000)}"
            self.records.append(
                {
                    "id": record_id,
                    "url": url,
                    "user_id": user_id,
                    "user_name": user_name or "",
                    "config": config_path,
                    "output_path": output_path,
                    "last_run": now,
                    "runs": 1,
                }
            )
        save_history(self.history_path, self.records)
        return record_id

    def append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def run_download(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("Running", "A download task is already running.")
            return

        url = self.url_var.get().strip()
        config_path = self.config_var.get().strip() or "config_simple.yml"
        output_path = self.path_var.get().strip() or "./my_videos_fix/"

        if not url:
            messagebox.showwarning("Missing URL", "Please input a Douyin URL.")
            return

        cmd = [
            sys.executable,
            "downloader.py",
            "-c",
            config_path,
            "-u",
            url,
            "--path",
            output_path,
        ]

        self.append_log("\n" + "=" * 72 + "\n")
        self.append_log("Running: " + " ".join(cmd) + "\n")

        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(self.base_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Start failed", str(exc))
            self.status_var.set("Failed to start.")
            return

        self.status_var.set("Running...")
        self.upsert_record(url, config_path, output_path, self.manual_name_var.get().strip())
        self.refresh_history()

        threading.Thread(target=self._read_process_output, daemon=True).start()

    def _read_process_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.output_queue.put(line)
        code = self.process.wait()
        self.output_queue.put(f"\nProcess exited with code {code}\n")
        self.output_queue.put("__PROCESS_DONE__")

    def poll_output(self) -> None:
        processed = 0
        max_items_per_tick = 300
        log_chunks: List[str] = []
        while processed < max_items_per_tick:
            try:
                item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1

            if item == "__PROCESS_DONE__":
                self.status_var.set("Idle")
                self.process = None
            else:
                log_chunks.append(item)

        if log_chunks:
            self.append_log("".join(log_chunks))

        next_delay = 20 if not self.output_queue.empty() else 100
        self.root.after(next_delay, self.poll_output)

    def stop_download(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.process.terminate()
        self.status_var.set("Stopping...")

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Exit", "A task is running. Stop and exit?"):
                return
            self.stop_download()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    DownloaderUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
