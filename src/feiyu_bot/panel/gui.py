"""桌面 GUI 面板：双击 exe 弹出的图形化设置窗口。

用 tkinter（标准库）实现，零第三方 GUI 依赖。

四个页签：

- **启动**：启动/停止机器人、启用 WebUI 开关（共享面板用）、打开 WebUI、谷时运行
- **设置**：关键配置（中文标签），保存即加密落盘
- **人格**：人格包切换
- **日志**：实时日志
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from ..config import field_label, load_config, section_title
from ..logging_setup import get_logger, setup_logging
from ..secrets import SecretVault

log = get_logger("feiyu.gui")

# ---------------------------------------------------------------- 配色

BG = "#0d1117"
PANEL = "#161b22"
PANEL2 = "#1c2129"
BORDER = "#30363d"
TEXT = "#e6edf3"
DIM = "#8b949e"
ACCENT = "#58a6ff"
OK = "#3fb950"
WARN = "#d29922"
ERR = "#f85149"


class _QueueHandler(logging.Handler):
    """把日志推入队列，供 GUI 线程读取。"""

    def __init__(self) -> None:
        super().__init__()
        self.queue: queue.Queue[str] = queue.Queue(maxsize=2000)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.getMessage()
            self.queue.put_nowait(f"{record.levelname:<7} {record.name:<22} {msg}")
        except Exception:  # noqa: BLE001
            pass


class ControlPanelGUI:
    """桌面控制面板。"""

    def __init__(self, bot=None, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else Path("config.yaml")
        self.bot = bot
        self._owns_bot = bot is None

        self.running = False
        self.bot_thread: threading.Thread | None = None
        self.bot_loop: asyncio.AbstractEventLoop | None = None
        self._log_handler = _QueueHandler()
        self._vars: dict[str, Any] = {}
        self._started_at = 0.0
        self._masked_values: dict[str, str] = {}

        self.root = None
        self._build()

    # ------------------------------------------------------------ 引导

    def _ensure_bot(self) -> None:
        """确保 bot 可用（GUI 启动时按需创建）。"""
        if self.bot is not None:
            return
        cfg = load_config(self.config_path)
        setup_logging(str(cfg.get("app.log_level", "INFO")), cfg.log_dir)
        from ..bot import create_bot  # noqa: PLC0415

        vault = SecretVault(cfg.data_dir)
        self.bot = create_bot(self.config_path)
        self.bot.vault = vault
        cfg.expand_env()

    # ------------------------------------------------------------ 构建界面

    def _build(self) -> None:
        import tkinter as tk  # noqa: PLC0415
        from tkinter import ttk  # noqa: PLC0415

        self.tk = tk
        self.ttk = ttk

        self.root = tk.Tk()
        self.root.title("大肥鱼 QQ 机器人")
        self.root.geometry("560x640")
        self.root.minsize(520, 560)
        self.root.configure(bg=BG)

        self._style(ttk)

        # ---- 顶部状态条 ----
        header = tk.Frame(self.root, bg=PANEL, highlightthickness=1,
                          highlightbackground=BORDER)
        header.pack(fill="x", side="top")
        tk.Frame(header, bg=BORDER, height=1).pack(fill="x", side="bottom")

        self.status_dot = tk.Canvas(header, width=14, height=14, bg=PANEL,
                                    highlightthickness=0)
        self.status_dot.pack(side="left", padx=(16, 6), pady=14)
        self._draw_dot(DIM)

        self.lbl_status = tk.Label(header, text="未启动", bg=PANEL, fg=TEXT,
                                   font=("Microsoft YaHei UI", 11, "bold"))
        self.lbl_status.pack(side="left")

        self.lbl_persona = tk.Label(header, text="人格: -", bg=PANEL, fg=DIM,
                                    font=("Microsoft YaHei UI", 10))
        self.lbl_persona.pack(side="left", padx=14)

        self.lbl_uptime = tk.Label(header, text="", bg=PANEL, fg=DIM,
                                   font=("Microsoft YaHei UI", 10))
        self.lbl_uptime.pack(side="right", padx=16)

        # ---- 标签页 ----
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_start = ttk.Frame(self.nb)
        self.tab_settings = ttk.Frame(self.nb)
        self.tab_persona = ttk.Frame(self.nb)
        self.tab_logs = ttk.Frame(self.nb)

        self.nb.add(self.tab_start, text="  启动  ")
        self.nb.add(self.tab_settings, text="  设置  ")
        self.nb.add(self.tab_persona, text="  人格  ")
        self.nb.add(self.tab_logs, text="  日志  ")

        self._build_start_tab()
        self._build_settings_tab()
        self._build_persona_tab()
        self._build_logs_tab()

        # 绑定日志
        logging.getLogger().addHandler(self._log_handler)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

    def _style(self, ttk) -> None:
        """深色主题样式。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure(".", background=BG, foreground=TEXT,
                        fieldbackground=PANEL2, bordercolor=BORDER,
                        focuscolor=ACCENT, font=("Microsoft YaHei UI", 10))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=DIM,
                        padding=(18, 9), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", BG), ("active", PANEL2)],
                  foreground=[("selected", ACCENT), ("active", TEXT)])
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Hint.TLabel", background=BG, foreground=DIM,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("TButton", background=PANEL2, foreground=TEXT,
                        padding=(14, 7), relief="flat", borderwidth=1)
        style.map("TButton",
                  background=[("active", BORDER), ("pressed", BG)],
                  foreground=[("active", ACCENT)])
        style.configure("Accent.TButton", background=ACCENT, foreground="#06101f",
                        padding=(16, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Accent.TButton",
                  background=[("active", "#79b8ff"), ("pressed", "#3d7fd0")],
                  foreground=[("active", "#06101f")])
        style.configure("Danger.TButton", background=ERR, foreground="#fff",
                        padding=(16, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT,
                        bordercolor=BORDER, insertcolor=TEXT)
        style.configure("TRadiobutton", background=BG, foreground=TEXT)
        style.configure("TText", fieldbackground=PANEL2, foreground=TEXT,
                        bordercolor=BORDER)

    def _draw_dot(self, color: str) -> None:
        c = self.status_dot
        c.delete("all")
        c.create_oval(2, 2, 12, 12, fill=color, outline="")

    # ------------------------------------------------------------ 启动页

    def _build_start_tab(self) -> None:
        import tkinter as tk  # noqa: PLC0415

        pad = tk.Frame(self.tab_start, bg=BG)
        pad.pack(fill="both", expand=True, padx=16, pady=14)

        self.btn_toggle = self.ttk.Button(pad, text="▶  启动机器人",
                                          style="Accent.TButton",
                                          command=self._toggle_bot)
        self.btn_toggle.pack(fill="x", ipady=4)

        # WebUI 开关（共享面板用）
        card = tk.Frame(pad, bg=PANEL, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x", pady=(14, 0))
        tk.Frame(card, bg=BORDER, height=1).pack(fill="x")

        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=12)

        self.var_webui = tk.BooleanVar(value=True)
        cb = self.ttk.Checkbutton(inner, text="启用 WebUI 面板（浏览器共享面板）",
                                  variable=self.var_webui, command=self._on_webui_toggle)
        cb.pack(anchor="w")
        tk.Label(inner, text="关闭后仅使用本桌面窗口，浏览器面板不再启动",
                 bg=PANEL, fg=DIM, font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=(20, 0))

        self.btn_open_webui = self.ttk.Button(inner, text="打开 WebUI 面板",
                                              command=self._open_webui)
        self.btn_open_webui.pack(anchor="w", padx=(20, 0), pady=(8, 0))

        # 谷时运行
        card2 = tk.Frame(pad, bg=PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        card2.pack(fill="x", pady=(10, 0))
        tk.Frame(card2, bg=BORDER, height=1).pack(fill="x")

        inner2 = tk.Frame(card2, bg=PANEL)
        inner2.pack(fill="x", padx=14, pady=12)

        self.var_offpeak = tk.BooleanVar(value=False)
        self.ttk.Checkbutton(inner2, text="谷时运行（只在低峰时段工作）",
                             variable=self.var_offpeak,
                             command=self._on_offpeak_toggle).pack(anchor="w")
        self.lbl_offpeak_hint = tk.Label(inner2, text="低峰时段: -",
                                         bg=PANEL, fg=DIM,
                                         font=("Microsoft YaHei UI", 9))
        self.lbl_offpeak_hint.pack(anchor="w", padx=(20, 0))

        # 快捷操作
        row = tk.Frame(pad, bg=BG)
        row.pack(fill="x", pady=(12, 0))
        self.ttk.Button(row, text="测试 LLM", command=self._test_llm).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.ttk.Button(row, text="立即保活", command=self._keepalive).pack(side="left", fill="x", expand=True, padx=(4, 0))

    # ------------------------------------------------------------ 设置页

    def _build_settings_tab(self) -> None:
        import tkinter as tk  # noqa: PLC0415

        wrap = tk.Frame(self.tab_settings, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        self.form = tk.Frame(wrap, bg=BG)
        self.form.pack(fill="both", expand=True)

        btn_row = tk.Frame(wrap, bg=BG)
        btn_row.pack(fill="x", pady=(10, 0))
        self.ttk.Button(btn_row, text="保存设置", style="Accent.TButton",
                        command=self._save_settings).pack(side="left", fill="x", expand=True)
        self.ttk.Button(btn_row, text="刷新", command=self._load_settings).pack(side="left", fill="x", expand=True, padx=(8, 0))

    def _load_settings(self) -> None:
        """载入配置到表单（敏感字段掩码回显）。"""
        import tkinter as tk  # noqa: PLC0415

        for child in self.form.winfo_children():
            child.destroy()
        self._vars.clear()
        self._masked_values.clear()

        try:
            cfg = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            tk.Label(self.form, text=f"配置读取失败: {exc}", bg=BG,
                     fg=ERR).pack(anchor="w")
            return

        vault = SecretVault(cfg.data_dir)
        data = cfg.masked(vault)

        # 分组展示
        groups = [
            ("QQ 机器人", ["qq.appid", "qq.secret", "qq.sandbox", "qq.intents",
                          "qq.group_whitelist", "qq.user_whitelist"]),
            ("AI 模型", ["llm.base_url", "llm.api_key", "llm.model",
                        "llm.temperature", "llm.max_tokens", "llm.daily_call_limit",
                        "llm.token_ceiling", "llm.allowed_hours", "llm.proxy"]),
            ("运行与识图", ["runtime.offpeak_only", "runtime.offpeak_hours",
                           "runtime.offpeak_behavior", "vision.enabled",
                           "vision.ocr_engine", "vision.use_local_ocr"]),
            ("面板与定时", ["webui.enabled", "webui.host", "webui.port",
                           "webui.token", "scheduler.keepalive_seconds",
                           "panel.enabled"]),
        ]

        for title, keys in groups:
            tk.Label(self.form, text=title, bg=BG, fg=ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(10, 4))

            for key in keys:
                value = cfg.get(key)
                self._add_field(key, value)

    def _add_field(self, dotted: str, value: Any) -> None:
        import tkinter as tk  # noqa: PLC0415

        row = tk.Frame(self.form, bg=BG)
        row.pack(fill="x", pady=3)

        label = tk.Label(row, text=field_label(dotted), bg=BG, fg=TEXT,
                         font=("Microsoft YaHei UI", 10), width=22, anchor="w")
        label.pack(side="left")

        if isinstance(value, bool):
            var = tk.BooleanVar(value=value)
            self.ttk.Checkbutton(row, variable=var).pack(side="left")
            self._vars[dotted] = var
            return

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            var = tk.StringVar(value=str(value))
            entry = self.ttk.Entry(row, textvariable=var, width=42)
            entry.pack(side="left", fill="x", expand=True)
            self._vars[dotted] = (var, "number")
            return

        if isinstance(value, list):
            var = tk.StringVar(value=", ".join(str(v) for v in value))
            entry = self.ttk.Entry(row, textvariable=var, width=42)
            entry.pack(side="left", fill="x", expand=True)
            self._vars[dotted] = (var, "list")
            return

        var = tk.StringVar(value="" if value is None else str(value))
        secret = any(k in dotted for k in ("secret", "api_key", "token"))
        entry = self.ttk.Entry(row, textvariable=var, width=42,
                               show="•" if secret else "")
        entry.pack(side="left", fill="x", expand=True)
        self._vars[dotted] = (var, "secret" if secret else "text")
        if secret and value:
            self._masked_values[dotted] = str(value)

    def _save_settings(self) -> None:
        """保存设置（敏感字段自动加密）。"""
        from ..bot import create_bot  # noqa: PLC0415

        cfg = load_config(self.config_path)
        vault = SecretVault(cfg.data_dir)

        changed: list[str] = []
        for dotted, holder in self._vars.items():
            if isinstance(holder, tk.BooleanVar):
                new = holder.get()
            else:
                var, kind = holder
                raw = var.get()
                # 掩码未改动 -> 跳过
                if kind == "secret" and dotted in self._masked_values:
                    if raw in ("", "******", "••••"):
                        continue
                if kind == "number":
                    try:
                        new = int(raw) if float(raw).is_integer() else float(raw)
                    except ValueError:
                        new = raw
                elif kind == "list":
                    new = [x.strip() for x in raw.split(",") if x.strip()]
                else:
                    new = raw
            if cfg.get(dotted) != new:
                cfg.set(dotted, new)
                changed.append(dotted)

        if not changed:
            self._toast("没有改动")
            return

        cfg.save(vault=vault)

        # 热更新运行中的 bot
        if self.bot is not None:
            for dotted in changed:
                self._apply_runtime(dotted)

        self._toast(f"已保存 {len(changed)} 项（敏感字段已加密）")

    def _apply_runtime(self, dotted: str) -> None:
        """把改动应用到运行中的 bot。"""
        try:
            if dotted.startswith("runtime."):
                self.bot.runtime.offpeak_only = bool(self.bot.cfg.get("runtime.offpeak_only"))
                self.bot.runtime.offpeak_hours = self.bot.cfg.get("runtime.offpeak_hours")
                self.bot.runtime.behavior = str(self.bot.cfg.get("runtime.offpeak_behavior", "silent"))
                self._update_offpeak_hint()
            elif dotted == "persona.current":
                self.bot.agent.set_persona(str(self.bot.cfg.get("persona.current")))
            elif dotted.startswith("vision."):
                self.bot.vision.enabled = bool(self.bot.cfg.get("vision.enabled", True))
                self.bot.vision.ocr_engine = str(self.bot.cfg.get("vision.ocr_engine", "auto"))
                self.bot.vision.use_local_ocr = bool(self.bot.cfg.get("vision.use_local_ocr", True))
        except Exception as exc:  # noqa: BLE001
            log.warning("应用运行时改动失败 %s: %s", dotted, exc)

    # ------------------------------------------------------------ 人格页

    def _build_persona_tab(self) -> None:
        import tkinter as tk  # noqa: PLC0415

        wrap = tk.Frame(self.tab_persona, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        self.persona_frame = tk.Frame(wrap, bg=BG)
        self.persona_frame.pack(fill="both", expand=True)

        self.ttk.Button(wrap, text="刷新人格列表", command=self._load_personas).pack(anchor="e", pady=(8, 0))

    def _load_personas(self) -> None:
        import tkinter as tk  # noqa: PLC0415

        for child in self.persona_frame.winfo_children():
            child.destroy()

        self._ensure_bot()
        personas = self.bot.personas.list()
        current = self.bot.agent.current_persona_id

        tk.Label(self.persona_frame, text="选择机器人的人格风格", bg=BG, fg=DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(0, 8))

        self.persona_var = tk.StringVar(value=current)
        for p in personas:
            row = tk.Frame(self.persona_frame, bg=BG)
            row.pack(fill="x", pady=2)
            self.ttk.Radiobutton(
                row, text=p["name"], value=p["id"], variable=self.persona_var,
                command=self._set_persona,
            ).pack(side="left")
            tk.Label(row, text=p.get("description", ""), bg=BG, fg=DIM,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left", padx=(10, 0))

    def _set_persona(self) -> None:
        pid = self.persona_var.get()
        self._ensure_bot()
        if self.bot.agent.set_persona(pid):
            cfg = load_config(self.config_path)
            vault = SecretVault(cfg.data_dir)
            cfg.set("persona.current", pid)
            cfg.save(vault=vault)
            self._toast(f"已切换到「{pid}」")

    # ------------------------------------------------------------ 日志页

    def _build_logs_tab(self) -> None:
        import tkinter as tk  # noqa: PLC0415

        wrap = tk.Frame(self.tab_logs, bg=BG)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        self.log_text = tk.Text(wrap, bg=PANEL2, fg=DIM, relief="flat",
                                font=("Consolas", 9), wrap="none", state="disabled")
        scroll = self.ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _poll(self) -> None:
        """定时刷新：日志 + 状态。"""
        try:
            self._drain_logs()
            self._refresh_status()
        except Exception:  # noqa: BLE001
            pass
        self.root.after(1000, self._poll)

    def _drain_logs(self) -> None:
        import tkinter as tk  # noqa: PLC0415

        lines = []
        while True:
            try:
                lines.append(self._log_handler.queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "\n".join(lines) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_status(self) -> None:
        if self.bot is None or not self.running:
            self.lbl_status.config(text="未启动", fg=DIM)
            self._draw_dot(DIM)
            self.lbl_uptime.config(text="")
            return
        health = self.bot.gateway.health()
        connected = bool(health.get("connected"))
        self.lbl_status.config(text="运行中 · 已连接" if connected else "运行中 · 连接中",
                               fg=OK if connected else WARN)
        self._draw_dot(OK if connected else WARN)
        self.lbl_persona.config(text=f"人格: {self.bot.agent.current_persona_id}")
        if self._started_at:
            mins = int((time.time() - self._started_at) / 60)
            self.lbl_uptime.config(text=f"运行 {mins} 分钟")
        self._update_offpeak_hint()

    def _update_offpeak_hint(self) -> None:
        if self.bot is None:
            return
        rt = self.bot.runtime
        state = "当前在低峰时段" if rt.in_offpeak() else "当前不在低峰时段"
        self.lbl_offpeak_hint.config(
            text=f"低峰时段: {rt.describe_windows()} · {state}"
        )

    # ------------------------------------------------------------ 操作

    def _toggle_bot(self) -> None:
        if self.running:
            self._stop_bot()
        else:
            self._start_bot()

    def _start_bot(self) -> None:
        self._ensure_bot()
        self.running = True
        self._started_at = time.time()

        def runner():
            asyncio.run(self._bot_main())

        self.bot_thread = threading.Thread(target=runner, daemon=True, name="bot")
        self.bot_thread.start()
        self.btn_toggle.config(text="■  停止机器人", style="Danger.TButton")
        self._toast("机器人已启动")

    async def _bot_main(self) -> None:
        self.bot_loop = asyncio.get_running_loop()
        try:
            await self.bot.run()
        except Exception as exc:  # noqa: BLE001
            log.exception("机器人异常退出: %s", exc)
        finally:
            self.running = False

    def _stop_bot(self) -> None:
        self.running = False
        if self.bot_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.bot.stop(), self.bot_loop)
            except Exception:  # noqa: BLE001
                pass
        self.btn_toggle.config(text="▶  启动机器人", style="Accent.TButton")
        self._toast("机器人已停止")

    def _on_webui_toggle(self) -> None:
        """WebUI 开关 —— 写回配置。"""
        enabled = self.var_webui.get()
        cfg = load_config(self.config_path)
        vault = SecretVault(cfg.data_dir)
        cfg.set("webui.enabled", enabled)
        cfg.save(vault=vault)
        if self.bot is not None:
            self.bot.cfg.set("webui.enabled", enabled)
        self._toast("WebUI 面板已" + ("启用" if enabled else "关闭") + "（重启后完全生效）")

    def _on_offpeak_toggle(self) -> None:
        enabled = self.var_offpeak.get()
        cfg = load_config(self.config_path)
        vault = SecretVault(cfg.data_dir)
        cfg.set("runtime.offpeak_only", enabled)
        cfg.save(vault=vault)
        if self.bot is not None:
            self.bot.runtime.offpeak_only = enabled
        self._update_offpeak_hint()
        self._toast("谷时运行已" + ("开启" if enabled else "关闭"))

    def _open_webui(self) -> None:
        self._ensure_bot()
        cfg = self.bot.cfg
        host = str(cfg.get("webui.host", "127.0.0.1"))
        port = int(cfg.get("webui.port", 8720))
        token = str(cfg.get("webui.token", "") or "")
        url = f"http://{host}:{port}" + (f"?token={token}" if token else "")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    def _test_llm(self) -> None:
        import threading  # noqa: PLC0415

        def worker():
            self._ensure_bot()
            ok, detail = asyncio.run(self.bot.llm.ping())
            self.root.after(0, lambda: self._toast(
                ("LLM 正常: " if ok else "LLM 失败: ") + detail[:120]))

        threading.Thread(target=worker, daemon=True).start()

    def _keepalive(self) -> None:
        import threading  # noqa: PLC0415

        def worker():
            self._ensure_bot()
            asyncio.run(self.bot._keepalive_once())
            self.root.after(0, lambda: self._toast("保活检查完成"))

        threading.Thread(target=worker, daemon=True).start()

    def _toast(self, message: str) -> None:
        import tkinter as tk  # noqa: PLC0415

        toast = tk.Label(self.root, text=message, bg=PANEL2, fg=TEXT,
                         font=("Microsoft YaHei UI", 10), padx=16, pady=10,
                         highlightthickness=1, highlightbackground=BORDER)
        toast.place(relx=0.5, rely=1.0, anchor="s", y=-20)
        self.root.after(2600, toast.destroy)

    def _on_close(self) -> None:
        try:
            logging.getLogger().removeHandler(self._log_handler)
        except Exception:  # noqa: BLE001
            pass
        if self.running:
            self._stop_bot()
        self.root.destroy()

    def run(self) -> None:
        """显示窗口并进入事件循环。"""
        self._load_settings()
        self._load_personas()
        self.root.mainloop()


def run_gui(bot=None, config_path: str | Path | None = None) -> int:
    """桌面 GUI 入口。"""
    gui = ControlPanelGUI(bot=bot, config_path=config_path)
    gui.run()
    return 0
