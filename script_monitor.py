"""
Python Script Monitor - Windows Desktop Tool
使用 CustomTkinter 构建，支持脚本启停管理、实时日志、资源监控
依赖安装: pip install customtkinter psutil
"""

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import threading
import psutil
import os
import sys
import time
import json
import queue
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ─────────────────────────── 全局主题设置 ───────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CONFIG_FILE = "scripts_config.json"

# ─────────────────────────── 脚本进程管理 ───────────────────────────
class ScriptProcess:
    """管理单个 Python 脚本的生命周期"""

    def __init__(self, name: str, path: str, args: str = "",
                 auto_restart: bool = False, interpreter: str = "",
                 feishu_webhook: str = ""):
        self.name = name
        self.path = path
        self.args = args
        self.auto_restart = auto_restart
        # interpreter: 留空则使用监控器自身的 Python；
        # 填写路径可指定 venv/uv 环境的 python.exe
        self.interpreter = interpreter.strip()
        self.feishu_webhook = feishu_webhook.strip()
        self.process: subprocess.Popen | None = None
        self.status = "stopped"   # stopped | running | paused | error
        self.log_queue: queue.Queue = queue.Queue()
        self.log_history: list[str] = []   # 存储本脚本的日志历史
        self.start_time: float | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── 启动 ──
    def start(self):
        if self.status == "running":
            return
        self._stop_event.clear()
        try:
            # 优先使用用户指定解释器，否则用监控器自身的 Python
            if self.interpreter and os.path.isfile(self.interpreter):
                python_exe = self.interpreter
            else:
                python_exe = sys.executable

            cmd = [python_exe, "-u", self.path]  # -u 禁用输出缓冲
            if self.args.strip():
                cmd += self.args.split()

            # Windows 下强制子进程使用 UTF-8 输出，解决中文乱码
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self.status = "running"
            self.start_time = time.time()
            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()
            self._log(f"[SYSTEM] 脚本已启动 PID={self.process.pid}")
        except Exception as e:
            self.status = "error"
            self._log(f"[ERROR] 启动失败: {e}")

    # ── 停止 ──
    def stop(self):
        self._stop_event.set()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception:
                pass
        self.status = "stopped"
        self.start_time = None
        self._log("[SYSTEM] 脚本已停止")

    # ── 暂停 / 恢复（Windows: suspend via psutil）──
    def pause(self):
        if self.process and self.process.poll() is None:
            try:
                p = psutil.Process(self.process.pid)
                p.suspend()
                self.status = "paused"
                self._log("[SYSTEM] 脚本已暂停")
            except Exception as e:
                self._log(f"[ERROR] 暂停失败: {e}")

    def resume(self):
        if self.process and self.process.poll() is None:
            try:
                p = psutil.Process(self.process.pid)
                p.resume()
                self.status = "running"
                self._log("[SYSTEM] 脚本已恢复运行")
            except Exception as e:
                self._log(f"[ERROR] 恢复失败: {e}")

    # ── 资源信息 ──
    def get_resource_info(self) -> dict:
        if self.process and self.process.poll() is None:
            try:
                p = psutil.Process(self.process.pid)
                mem = p.memory_info()
                cpu = p.cpu_percent(interval=0.1)
                return {
                    "cpu": f"{cpu:.1f}%",
                    "mem_rss": f"{mem.rss / 1024 / 1024:.1f} MB",
                    "mem_vms": f"{mem.vms / 1024 / 1024:.1f} MB",
                    "threads": p.num_threads(),
                    "pid": self.process.pid,
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return {"cpu": "—", "mem_rss": "—", "mem_vms": "—", "threads": "—", "pid": "—"}

    def get_uptime(self) -> str:
        if self.start_time and self.status in ("running", "paused"):
            delta = timedelta(seconds=int(time.time() - self.start_time))
            return str(delta)
        return "—"

    # ── 飞书 Webhook 通知 ──
    def _send_feishu_notification(self, exit_code: int):
        if not self.feishu_webhook:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "⚠️ 脚本异常退出"},
                    "template": "red"
                },
                "elements": [
                    {
                        "tag": "div",
                        "fields": [
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**脚本名称**\n{self.name}"}},
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**退出码**\n{exit_code}"}},
                        ]
                    },
                    {
                        "tag": "div",
                        "fields": [
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**脚本路径**\n`{self.path}`"}},
                            {"is_short": True, "text": {"tag": "lark_md", "content": f"**发生时间**\n{now}"}},
                        ]
                    },
                    {"tag": "hr"},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": "Python 脚本监控器自动通知"}]}
                ]
            }
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.feishu_webhook,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    # ── 私有：读取输出 ──
    def _read_output(self):
        try:
            for line in self.process.stdout:
                if self._stop_event.is_set():
                    break
                self._log(line.rstrip())
            # 进程自然退出（非用户手动停止）
            if not self._stop_event.is_set():
                exit_code = self.process.wait()
                self.status = "stopped"
                self.start_time = None
                self._log(f"[SYSTEM] 进程退出，退出码={exit_code}")
                # 异常退出时发送飞书通知
                if exit_code != 0:
                    self._send_feishu_notification(exit_code)
        except Exception as e:
            self._log(f"[ERROR] 读取输出异常: {e}")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_history.append(line)
        # 限制日志历史长度，防止内存无限增长
        if len(self.log_history) > 5000:
            self.log_history = self.log_history[-3000:]
        self.log_queue.put(line)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path,
            "args": self.args, "auto_restart": self.auto_restart,
            "interpreter": self.interpreter,
            "feishu_webhook": self.feishu_webhook,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScriptProcess":
        return cls(d["name"], d["path"], d.get("args", ""),
                   d.get("auto_restart", False), d.get("interpreter", ""),
                   d.get("feishu_webhook", ""))


# ─────────────────────────── 添加/编辑脚本弹窗 ───────────────────────────
class AddScriptDialog(ctk.CTkToplevel):
    def __init__(self, parent, on_confirm, existing: ScriptProcess | None = None):
        super().__init__(parent)
        self.on_confirm = on_confirm
        self.result = None

        title = "编辑脚本" if existing else "添加脚本"
        self.title(title)
        self.geometry("520x440")
        self.resizable(False, False)
        self.grab_set()
        self.lift()
        self.focus_force()

        # ── 居中 ──
        self.update_idletasks()
        px, py = parent.winfo_x(), parent.winfo_y()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"+{px + pw//2 - 260}+{py + ph//2 - 190}")

        pad = {"padx": 16, "pady": 6}
        self.columnconfigure(1, weight=1)

        # 脚本名称
        ctk.CTkLabel(self, text="脚本名称", anchor="w", width=80).grid(row=0, column=0, sticky="w", **pad)
        self.name_var = ctk.StringVar(value=existing.name if existing else "")
        self.name_entry = ctk.CTkEntry(self, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)

        # 脚本路径
        ctk.CTkLabel(self, text="脚本路径", anchor="w", width=80).grid(row=1, column=0, sticky="w", **pad)
        self.path_var = ctk.StringVar(value=existing.path if existing else "")
        self.path_entry = ctk.CTkEntry(self, textvariable=self.path_var)
        self.path_entry.grid(row=1, column=1, sticky="ew", **pad)
        ctk.CTkButton(self, text="浏览", width=55, corner_radius=6,
                      fg_color="#4a5568", hover_color="#5a6578",
                      command=self._browse_script).grid(
            row=1, column=2, padx=(0, 16), pady=6)

        # Python 解释器（可选）
        ctk.CTkLabel(self, text="Python 解释器\n(留空用默认)", anchor="w", width=80,
                     font=ctk.CTkFont(size=11)).grid(row=2, column=0, sticky="w", **pad)
        self.interp_var = ctk.StringVar(value=existing.interpreter if existing else "")
        self.interp_entry = ctk.CTkEntry(self, textvariable=self.interp_var,
                                          placeholder_text="留空=使用监控器的Python；或填写 venv/uv 的 python.exe 路径")
        self.interp_entry.grid(row=2, column=1, sticky="ew", **pad)
        ctk.CTkButton(self, text="浏览", width=55, corner_radius=6,
                      fg_color="#4a5568", hover_color="#5a6578",
                      command=self._browse_interp).grid(
            row=2, column=2, padx=(0, 16), pady=6)

        # 启动参数
        ctk.CTkLabel(self, text="启动参数", anchor="w", width=80).grid(row=3, column=0, sticky="w", **pad)
        self.args_var = ctk.StringVar(value=existing.args if existing else "")
        ctk.CTkEntry(self, textvariable=self.args_var,
                     placeholder_text="例: --port 8080 --debug").grid(
            row=3, column=1, columnspan=2, sticky="ew", **pad)

        # 自动重启
        self.restart_var = ctk.BooleanVar(value=existing.auto_restart if existing else False)
        ctk.CTkCheckBox(self, text="进程退出后自动重启", variable=self.restart_var).grid(
            row=4, column=0, columnspan=3, sticky="w", padx=16, pady=8
        )

        # 飞书 Webhook
        ctk.CTkLabel(self, text="飞书 Webhook", anchor="w", width=80,
                     font=ctk.CTkFont(size=11)).grid(row=5, column=0, sticky="w", **pad)
        self.webhook_var = ctk.StringVar(value=existing.feishu_webhook if existing else "")
        ctk.CTkEntry(self, textvariable=self.webhook_var,
                     placeholder_text="异常退出时通知，留空则不通知").grid(
            row=5, column=1, columnspan=2, sticky="ew", **pad)

        # 按钮行
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=6, column=0, columnspan=3, pady=(8, 16))
        ctk.CTkButton(btn_frame, text="取消", fg_color="#4a5568", hover_color="#5a6578",
                      corner_radius=8, width=100, command=self.destroy).pack(side="left", padx=8)
        ctk.CTkButton(btn_frame, text="确认", fg_color="#3498db", hover_color="#2980b9",
                      corner_radius=8, width=100, command=self._confirm).pack(side="left", padx=8)

    def _browse_script(self):
        path = filedialog.askopenfilename(
            title="选择 Python 脚本",
            filetypes=[("Python 脚本", "*.py"), ("所有文件", "*.*")]
        )
        if path:
            self.path_var.set(path)
            if not self.name_var.get():
                self.name_var.set(Path(path).stem)

    def _browse_interp(self):
        path = filedialog.askopenfilename(
            title="选择 Python 解释器",
            filetypes=[("Python 可执行文件", "python.exe python3.exe python3"),
                       ("所有文件", "*.*")]
        )
        if path:
            self.interp_var.set(path)

    def _confirm(self):
        name = self.name_var.get().strip()
        path = self.path_var.get().strip()
        interp = self.interp_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入脚本名称", parent=self)
            return
        if not path:
            messagebox.showwarning("提示", "请选择脚本路径", parent=self)
            return
        if not os.path.exists(path):
            messagebox.showwarning("提示", f"脚本路径不存在:\n{path}", parent=self)
            return
        if interp and not os.path.isfile(interp):
            messagebox.showwarning("提示", f"解释器路径不存在:\n{interp}\n留空则使用默认Python", parent=self)
            return
        self.on_confirm(name, path, self.args_var.get(), self.restart_var.get(), interp,
                        self.webhook_var.get().strip())
        self.destroy()


# ─────────────────────────── 脚本列表项 ───────────────────────────
STATUS_COLOR = {
    "running": "#00d68f",
    "stopped": "#6b7b8d",
    "paused":  "#ffaa00",
    "error":   "#ff6b6b",
}
STATUS_LABEL = {
    "running": "运行中",
    "stopped": "已停止",
    "paused":  "已暂停",
    "error":   "错误",
}


class ScriptListItem(ctk.CTkFrame):
    def __init__(self, parent, script: ScriptProcess, on_select, **kwargs):
        super().__init__(parent, corner_radius=10, height=60, **kwargs)
        self.script = script
        self.on_select = on_select
        self.selected = False
        self._build()
        self.bind("<Button-1>", self._click)
        self.configure(cursor="hand2")

    def _build(self):
        self.grid_columnconfigure(1, weight=1)

        # 状态指示点
        self.dot = ctk.CTkLabel(self, text="●", font=("Arial", 16),
                                 text_color=STATUS_COLOR.get(self.script.status, "#6b7b8d"), width=28)
        self.dot.grid(row=0, column=0, rowspan=2, padx=(12, 4), sticky="ns")
        self.dot.bind("<Button-1>", self._click)

        # 脚本名
        self.name_lbl = ctk.CTkLabel(self, text=self.script.name, anchor="w",
                                      font=ctk.CTkFont(size=13, weight="bold"))
        self.name_lbl.grid(row=0, column=1, sticky="sw", padx=6, pady=(10, 0))
        self.name_lbl.bind("<Button-1>", self._click)

        # 状态文字
        self.status_lbl = ctk.CTkLabel(self, text=STATUS_LABEL.get(self.script.status, "—"),
                                        anchor="w", font=ctk.CTkFont(size=11),
                                        text_color=STATUS_COLOR.get(self.script.status, "#6b7b8d"))
        self.status_lbl.grid(row=1, column=1, sticky="nw", padx=6, pady=(0, 10))
        self.status_lbl.bind("<Button-1>", self._click)

    def refresh(self):
        color = STATUS_COLOR.get(self.script.status, "#6b7b8d")
        self.dot.configure(text_color=color)
        self.status_lbl.configure(text=STATUS_LABEL.get(self.script.status, "—"), text_color=color)
        self.name_lbl.configure(text=self.script.name)

    def set_selected(self, selected: bool):
        self.selected = selected
        self.configure(fg_color=("#1e4d8f", "#163a6e") if selected else ("gray18", "gray14"))

    def _click(self, _event=None):
        self.on_select(self.script)


# ─────────────────────────── 主应用窗口 ───────────────────────────
class ScriptMonitorApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Python 脚本监控器")
        self.geometry("1100x700")
        self.minsize(900, 580)

        self.scripts: list[ScriptProcess] = []
        self.list_items: list[ScriptListItem] = []
        self.selected_script: ScriptProcess | None = None
        self._current_log_script: ScriptProcess | None = None  # 当前日志区域显示的脚本
        self._search_highlight_tag = "highlight"
        self._log_lock = threading.Lock()

        self._build_layout()
        self._load_config()
        self._start_refresh_loop()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ═══════════════════════════ 布局构建 ═══════════════════════════

    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── 左侧面板 ──
        left = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color=("gray14", "gray9"))
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(1, weight=1)   # 滚动区占满剩余空间
        left.grid_rowconfigure(0, minsize=50) # 顶部标题栏最小高度
        left.grid_rowconfigure(2, minsize=60) # 底部按钮区最小高度，防止被压缩
        left.grid_columnconfigure(0, weight=1)
        left.grid_propagate(False)
        self._build_left(left)

        # 分隔线
        sep = ctk.CTkFrame(self, width=1, fg_color=("gray25", "gray20"), corner_radius=0)
        sep.grid(row=0, column=0, sticky="nse")

        # ── 右侧面板 ──
        right = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray16", "gray11"))
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self._build_right(right)

    # ── 左侧 ──
    def _build_left(self, parent):
        # 顶部标题栏
        top_bar = ctk.CTkFrame(parent, height=50, fg_color="transparent")
        top_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        top_bar.grid_columnconfigure(0, weight=1)
        top_bar.grid_propagate(False)

        ctk.CTkLabel(top_bar, text="脚本列表", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=("#d0d8e8", "#e8ecf0")).grid(
            row=0, column=0, sticky="w", padx=4, pady=10
        )
        ctk.CTkButton(top_bar, text="＋ 添加", width=76, height=30,
                      corner_radius=8, font=ctk.CTkFont(size=12),
                      fg_color="#3498db", hover_color="#2980b9",
                      command=self._open_add_dialog).grid(row=0, column=1, padx=(4, 0), pady=10)

        # 脚本列表滚动区（占满中间弹性空间）
        self.list_scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        self.list_scroll.grid(row=1, column=0, sticky="nsew", padx=4, pady=0)
        self.list_scroll.grid_columnconfigure(0, weight=1)

        # 底部按钮 —— 固定在底部，不被压缩
        bottom_bar = ctk.CTkFrame(parent, height=60, fg_color=("gray12", "gray7"), corner_radius=0)
        bottom_bar.grid(row=2, column=0, sticky="sew", padx=0, pady=0)
        bottom_bar.grid_propagate(False)
        bottom_bar.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(bottom_bar, text="▶ 启动全部", height=36, fg_color="#00b871",
                      hover_color="#009b5f", corner_radius=8,
                      font=ctk.CTkFont(size=12),
                      command=self._start_all).grid(row=0, column=0, padx=(10, 4), pady=12, sticky="ew")
        ctk.CTkButton(bottom_bar, text="■ 停止全部", height=36, fg_color="#e74c3c",
                      hover_color="#c0392b", corner_radius=8,
                      font=ctk.CTkFont(size=12),
                      command=self._stop_all).grid(row=0, column=1, padx=(4, 10), pady=12, sticky="ew")

    # ── 右侧 ──
    def _build_right(self, parent):
        # ─ 顶部工具栏 ─
        toolbar = ctk.CTkFrame(parent, height=64, fg_color=("gray17", "gray12"), corner_radius=0)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(0, weight=1)
        toolbar.grid_propagate(False)

        self.script_title = ctk.CTkLabel(toolbar, text="未选择脚本",
                                          font=ctk.CTkFont(size=16, weight="bold"), anchor="w",
                                          text_color=("#d0d8e8", "#e8ecf0"))
        self.script_title.grid(row=0, column=0, padx=16, pady=14, sticky="w")

        btn_area = ctk.CTkFrame(toolbar, fg_color="transparent")
        btn_area.grid(row=0, column=1, padx=12, pady=10, sticky="e")

        self.btn_start = ctk.CTkButton(btn_area, text="▶ 启动", width=76, height=34,
                                        fg_color="#00b871", hover_color="#009b5f",
                                        corner_radius=8, font=ctk.CTkFont(size=12),
                                        command=self._start_selected)
        self.btn_start.pack(side="left", padx=3)

        self.btn_stop = ctk.CTkButton(btn_area, text="■ 停止", width=76, height=34,
                                       fg_color="#e74c3c", hover_color="#c0392b",
                                       corner_radius=8, font=ctk.CTkFont(size=12),
                                       command=self._stop_selected)
        self.btn_stop.pack(side="left", padx=3)

        self.btn_pause = ctk.CTkButton(btn_area, text="⏸ 暂停", width=76, height=34,
                                        fg_color="#e67e22", hover_color="#d35400",
                                        corner_radius=8, font=ctk.CTkFont(size=12),
                                        command=self._pause_selected)
        self.btn_pause.pack(side="left", padx=3)

        self.btn_config = ctk.CTkButton(btn_area, text="⚙ 配置", width=76, height=34,
                                         fg_color="#3498db", hover_color="#2980b9",
                                         corner_radius=8, font=ctk.CTkFont(size=12),
                                         command=self._config_selected)
        self.btn_config.pack(side="left", padx=3)

        self.btn_delete = ctk.CTkButton(btn_area, text="🗑 删除", width=76, height=34,
                                         fg_color="#8e44ad", hover_color="#7d3c98",
                                         corner_radius=8, font=ctk.CTkFont(size=12),
                                         command=self._delete_selected)
        self.btn_delete.pack(side="left", padx=3)

        # ─ 日志区域 ─
        log_frame = ctk.CTkFrame(parent, fg_color="transparent")
        log_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(8, 4))
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)

        # 日志工具栏
        log_toolbar = ctk.CTkFrame(log_frame, height=38, fg_color=("gray20", "gray15"), corner_radius=8)
        log_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        log_toolbar.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(log_toolbar, text="实时日志", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#b0b8c8", "#c8cdd5")).grid(
            row=0, column=0, padx=10, pady=6, sticky="w"
        )

        # 搜索框
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", self._on_search_change)
        self.search_entry = ctk.CTkEntry(log_toolbar, textvariable=self.search_var,
                                          placeholder_text="搜索关键词...", width=200, height=28,
                                          corner_radius=6)
        self.search_entry.grid(row=0, column=2, padx=8, pady=5, sticky="e")

        # 日志操作按钮
        btn_log_frame = ctk.CTkFrame(log_toolbar, fg_color="transparent")
        btn_log_frame.grid(row=0, column=3, padx=8, pady=5)
        ctk.CTkButton(btn_log_frame, text="清除", width=56, height=28,
                      corner_radius=6, font=ctk.CTkFont(size=11),
                      fg_color="#4a5568", hover_color="#5a6578",
                      command=self._clear_log).pack(side="left", padx=2)
        ctk.CTkButton(btn_log_frame, text="复制", width=56, height=28,
                      corner_radius=6, font=ctk.CTkFont(size=11),
                      fg_color="#4a5568", hover_color="#5a6578",
                      command=self._copy_log).pack(side="left", padx=2)
        self.auto_scroll_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(btn_log_frame, text="自动滚动", variable=self.auto_scroll_var,
                         width=80, height=28).pack(side="left", padx=6)

        # 日志文本框
        # Windows 下 Consolas 不含 emoji 字形，改用支持 emoji 的字体栈
        # tkinter 单 Text 只能一种主字体，emoji 通过独立 tag + Segoe UI Emoji 渲染
        import platform
        if platform.system() == "Windows":
            LOG_FONT = ("Consolas", 13)
            EMOJI_FONT = ("Segoe UI Emoji", 13)
        elif platform.system() == "Darwin":
            LOG_FONT = ("Menlo", 13)
            EMOJI_FONT = ("Apple Color Emoji", 13)
        else:
            LOG_FONT = ("DejaVu Sans Mono", 13)
            EMOJI_FONT = ("Noto Color Emoji", 13)

        self._emoji_font = EMOJI_FONT

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            font=LOG_FONT,
            bg="#0f1923",
            fg="#d4dae3",
            insertbackground="white",
            selectbackground="#1e4d8f",
            relief="flat",
            padx=10,
            pady=8,
            spacing1=2,
            spacing3=2,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.tag_configure(self._search_highlight_tag, background="#f39c12", foreground="black")
        # emoji tag：用系统 emoji 字体渲染含 emoji 的片段
        self.log_text.tag_configure("emoji", font=self._emoji_font)

        scrollbar = ctk.CTkScrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        # ─ 底部资源栏 ─
        self._build_resource_bar(parent)

    def _build_resource_bar(self, parent):
        res_bar = ctk.CTkFrame(parent, fg_color=("gray17", "gray12"), corner_radius=0)
        res_bar.grid(row=2, column=0, sticky="ew", padx=0, pady=0)
        for c in range(6):
            res_bar.grid_columnconfigure(c, weight=1)

        def _metric(label_text, col):
            f = ctk.CTkFrame(res_bar, fg_color=("gray22", "gray16"), corner_radius=8)
            f.grid(row=0, column=col, padx=5, pady=8, sticky="ew")
            ctk.CTkLabel(
                f, text=label_text,
                font=ctk.CTkFont(size=10), text_color="#7f8c9a"
            ).pack(anchor="w", padx=10, pady=(8, 0))
            val = ctk.CTkLabel(
                f, text="—",
                font=ctk.CTkFont(size=14, weight="bold")
            )
            val.pack(anchor="w", padx=10, pady=(0, 8))
            return val

        self.res_cpu    = _metric("CPU 占用",  0)
        self.res_mem    = _metric("内存 RSS",  1)
        self.res_vms    = _metric("虚拟内存",  2)
        self.res_thread = _metric("线程数",    3)
        self.res_pid    = _metric("PID",       4)
        self.res_uptime = _metric("运行时长",  5)

    # ═══════════════════════════ 脚本列表操作 ═══════════════════════════

    def _rebuild_list(self):
        for w in self.list_scroll.winfo_children():
            w.destroy()
        self.list_items.clear()
        for s in self.scripts:
            item = ScriptListItem(self.list_scroll, s, self._select_script,
                                  fg_color=("gray20", "gray17"))
            item.grid(sticky="ew", padx=4, pady=3)
            item.grid_columnconfigure(1, weight=1)
            self.list_items.append(item)
        # 恢复选中
        if self.selected_script:
            self._highlight_selected()

    def _select_script(self, script: ScriptProcess):
        self.selected_script = script
        self._highlight_selected()
        self.script_title.configure(text=f"  {script.name}")
        # 切换脚本时重新加载该脚本的日志历史
        if self._current_log_script is not script:
            self._swap_log_content(script)
        self._refresh_log_from_queue()

    def _swap_log_content(self, script: ScriptProcess):
        """切换日志区域内容到指定脚本的历史日志"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in script.log_history:
            self._insert_log_line(line)
        if self.auto_scroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self._current_log_script = script
        kw = self.search_var.get().strip()
        if kw:
            self._on_search_change()

    def _highlight_selected(self):
        for item in self.list_items:
            item.set_selected(item.script is self.selected_script)

    def _open_add_dialog(self):
        def on_confirm(name, path, args, restart, interp="", webhook=""):
            sp = ScriptProcess(name, path, args, restart, interp, webhook)
            self.scripts.append(sp)
            self._rebuild_list()
            self._save_config()

        AddScriptDialog(self, on_confirm)

    def _config_selected(self):
        if not self.selected_script:
            return

        def on_confirm(name, path, args, restart, interp="", webhook=""):
            s = self.selected_script
            was_running = s.status == "running"
            if was_running:
                s.stop()
            s.name = name
            s.path = path
            s.args = args
            s.auto_restart = restart
            s.interpreter = interp
            s.feishu_webhook = webhook
            self._rebuild_list()
            self._save_config()
            self.script_title.configure(text=f"  {name}")
            if was_running:
                s.start()

        AddScriptDialog(self, on_confirm, existing=self.selected_script)

    def _delete_selected(self):
        if not self.selected_script:
            return
        s = self.selected_script
        if messagebox.askyesno("确认删除", f"确定要删除脚本「{s.name}」吗？", parent=self):
            s.stop()
            self.scripts.remove(s)
            self.selected_script = None
            self._current_log_script = None
            self.script_title.configure(text="未选择脚本")
            self._clear_log()
            self._rebuild_list()
            self._save_config()

    # ═══════════════════════════ 启停控制 ═══════════════════════════

    def _start_selected(self):
        if self.selected_script:
            self.selected_script.start()

    def _stop_selected(self):
        if self.selected_script:
            self.selected_script.stop()

    def _pause_selected(self):
        if not self.selected_script:
            return
        s = self.selected_script
        if s.status == "running":
            s.pause()
        elif s.status == "paused":
            s.resume()
            self.btn_pause.configure(text="⏸ 暂停")
            return
        self.btn_pause.configure(text="▶ 恢复" if s.status == "paused" else "⏸ 暂停")

    def _start_all(self):
        for s in self.scripts:
            if s.status == "stopped":
                s.start()

    def _stop_all(self):
        for s in self.scripts:
            s.stop()

    # ═══════════════════════════ 日志操作 ═══════════════════════════

    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self._insert_log_line(text)
        kw = self.search_var.get().strip()
        if kw:
            self._highlight_keyword_in_last_line(kw)
        if self.auto_scroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _highlight_keyword_in_last_line(self, keyword: str):
        self.log_text.tag_remove(self._search_highlight_tag, "1.0", "end")
        if not keyword:
            return
        content = self.log_text.get("1.0", "end")
        for m in re.finditer(re.escape(keyword), content, re.IGNORECASE):
            start = f"1.0+{m.start()}c"
            end = f"1.0+{m.end()}c"
            self.log_text.tag_add(self._search_highlight_tag, start, end)

    def _on_search_change(self, *_):
        kw = self.search_var.get().strip()
        self.log_text.tag_remove(self._search_highlight_tag, "1.0", "end")
        if not kw:
            return
        content = self.log_text.get("1.0", "end")
        first_match = None
        for m in re.finditer(re.escape(kw), content, re.IGNORECASE):
            start = f"1.0+{m.start()}c"
            end = f"1.0+{m.end()}c"
            self.log_text.tag_add(self._search_highlight_tag, start, end)
            if first_match is None:
                first_match = start
        if first_match:
            self.log_text.see(first_match)

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _copy_log(self):
        content = self.log_text.get("1.0", "end").strip()
        if content:
            self.clipboard_clear()
            self.clipboard_append(content)

    # ── emoji 检测 ──
    _EMOJI_RE = re.compile(
        "["
        "\U0001F300-\U0001FAFF"   # 杂项符号/表情
        "\U00002700-\U000027BF"   # Dingbats
        "\U0001F000-\U0001F02F"   # 麻将/多米诺
        "\U0001F0A0-\U0001F0FF"   # 扑克牌
        "\U00002600-\U000026FF"   # 杂项符号
        "\U0001FA00-\U0001FA6F"   # 国际象棋等
        "\U00000023-\U00000023"   # #
        "\U0000200D"              # ZWJ
        "\U0000FE0F"              # variation selector
        "]+",
        flags=re.UNICODE,
    )

    def _insert_log_line(self, line: str):
        """向日志文本框插入一行，自动对 emoji 片段应用 emoji tag"""
        text = line + "\n"
        pos = 0
        for m in self._EMOJI_RE.finditer(text):
            if m.start() > pos:
                self.log_text.insert("end", text[pos:m.start()])
            self.log_text.insert("end", m.group(), "emoji")
            pos = m.end()
        if pos < len(text):
            self.log_text.insert("end", text[pos:])

    def _refresh_log_from_queue(self):
        """一次性把队列里的日志刷入文本框（在选中时调用）"""
        if not self.selected_script:
            return
        q = self.selected_script.log_queue
        lines = []
        try:
            while True:
                lines.append(q.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_text.configure(state="normal")
            for line in lines:
                self._insert_log_line(line)
            if self.auto_scroll_var.get():
                self.log_text.see("end")
            self.log_text.configure(state="disabled")
            self._current_log_script = self.selected_script
            kw = self.search_var.get().strip()
            if kw:
                self._on_search_change()

    # ═══════════════════════════ 定时刷新 ═══════════════════════════

    def _start_refresh_loop(self):
        self._refresh()

    def _refresh(self):
        # 刷新列表项状态
        for item in self.list_items:
            item.refresh()

        # 刷新暂停按钮文字
        if self.selected_script:
            s = self.selected_script
            self.btn_pause.configure(text="▶ 恢复" if s.status == "paused" else "⏸ 暂停")
            # 刷新资源
            info = s.get_resource_info()
            self.res_cpu.configure(text=info["cpu"])
            self.res_mem.configure(text=info["mem_rss"])
            self.res_vms.configure(text=info["mem_vms"])
            self.res_thread.configure(text=str(info["threads"]))
            self.res_pid.configure(text=str(info["pid"]))
            self.res_uptime.configure(text=s.get_uptime())

            # 刷新日志
            self._refresh_log_from_queue()
        else:
            for lbl in (self.res_cpu, self.res_mem, self.res_vms,
                        self.res_thread, self.res_pid, self.res_uptime):
                lbl.configure(text="—")

        # 处理自动重启
        for s in self.scripts:
            if s.auto_restart and s.status == "stopped" and s.start_time is None:
                # start_time 为 None 且 stopped 说明是自然退出，触发自动重启
                # 但首次 stopped（从未启动）不重启，靠 _has_ever_run 标记
                if hasattr(s, "_ever_started") and s._ever_started:
                    s._log("[SYSTEM] 自动重启中...")
                    s.start()

        self.after(500, self._refresh)

    # ═══════════════════════════ 配置持久化 ═══════════════════════════

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump([s.to_dict() for s in self.scripts], f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for d in data:
                self.scripts.append(ScriptProcess.from_dict(d))
            self._rebuild_list()
        except Exception:
            pass

    def _on_close(self):
        # 停止所有脚本再退出
        for s in self.scripts:
            s.stop()
        self._save_config()
        self.destroy()


# ─────────────────────────── 入口 ───────────────────────────
def patch_ever_started():
    """Monkey-patch ScriptProcess.start 以记录是否曾经启动过"""
    _orig_start = ScriptProcess.start

    def _patched_start(self):
        self._ever_started = True
        _orig_start(self)

    ScriptProcess.start = _patched_start


if __name__ == "__main__":
    patch_ever_started()
    app = ScriptMonitorApp()
    app.mainloop()