"""exe 内置面板：终端交互式控制台。

不依赖第三方 TUI 库，纯标准库实现，Windows 控制台直接可用。

功能：

- 实时状态展示（连接、统计、预算、上下文）
- 快捷命令：状态 / 人格 / 刷新 / 测试 LLM / 保活 / 媒体 / 帮助 / 退出
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import threading
from typing import Any

from ..logging_setup import get_logger

log = get_logger("feiyu.panel")

HELP = """
可用命令:
  status            显示运行状态
  personas          列出人格
  persona <id>      切换人格
  media [voice|image]  查看媒体库
  llm               测试 LLM 连通性
  keepalive         立即执行一次保活检查
  refresh           刷新状态
  clear             清空会话上下文
  help              显示帮助
  quit / exit       退出程序
""".strip()


class ConsolePanel:
    """终端控制台面板。"""

    def __init__(self, bot):
        self.bot = bot
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 展示

    def _line(self, text: str = "") -> None:
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def render(self) -> str:
        """渲染状态文本。"""
        s = self.bot.status()
        gw = s.get("gateway", {})
        ag = s.get("agent", {})
        st = ag.get("stats", {})
        bd = ag.get("budget", {})
        rt = ag.get("runtime", {})
        cx = ag.get("context", {})
        rp = ag.get("reply_pool", {})

        conn = "在线" if gw.get("connected") else "离线"
        lines = [
            "┌─ feiyu-qqbot ─────────────────────────────────",
            f"│ 连接      : {conn}   重连次数: {gw.get('reconnect_count', 0)}",
            f"│ 人格      : {ag.get('persona', '-')}",
            f"│ 运行模式  : "
            + ("谷时模式 " + s.get("offpeak_windows", "") if rt.get("offpeak_only") else "全天运行")
            + ("  [当前在谷时]" if rt.get("in_offpeak") else ""),
            "├─ 统计 ───────────────────────────────────────",
            f"│ 收/发    : {st.get('messages_in', 0)} / {st.get('messages_out', 0)}"
            f"   LLM: {st.get('llm_calls', 0)}   工具: {st.get('tool_calls', 0)}",
            f"│ 识图     : {st.get('images_seen', 0)}   发图: {st.get('images_sent', 0)}"
            f"   发语音: {st.get('voices_sent', 0)}   错误: {st.get('errors', 0)}",
            f"│ 运行     : {st.get('uptime_min', 0)} 分钟",
            "├─ 预算 ───────────────────────────────────────",
            f"│ 今日调用 : {bd.get('calls_today', 0)} / {bd.get('daily_limit') or '不限'}"
            f"   服务时段: {'开放' if bd.get('in_window') else '关闭'}",
            f"│ 回复池   : 可用 {rp.get('usable', 0)} / {rp.get('entries', 0)}"
            f"   每条上限 {rp.get('max_uses_per_msg', 5)} 条",
            f"│ 上下文   : {cx.get('sessions', 0)} 会话"
            f"   上限 {cx.get('max_tokens', 0)} tok",
            "└────────────────────────────────────────────────",
        ]
        return "\n".join(lines)

    def show(self) -> None:
        self._line()
        self._line(self.render())
        self._line()

    # ------------------------------------------------------------ 命令

    async def _cmd_llm(self) -> None:
        self._line("测试 LLM 中…")
        ok, detail = await self.bot.llm.ping()
        self._line(("✅ LLM 正常: " if ok else "❌ LLM 失败: ") + detail)

    async def _cmd_keepalive(self) -> None:
        await self.bot._keepalive_once()
        self._line("保活已执行")
        self.show()

    def _cmd_media(self, kind: str) -> None:
        items = self.bot.media.list(kind)
        if not items:
            self._line("（媒体库为空）")
            return
        for i in items[:40]:
            self._line(f"  [{i['kind']}] {i['name']}  {i['size']//1024}KB  {i['duration']}s  {i['source']}")

    # ------------------------------------------------------------ 循环

    async def _run(self) -> None:
        self._running = True
        self._line("内置面板已启动。输入 help 查看命令。")
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, input, "feiyu> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            if not parts:
                continue
            cmd, args = parts[0].lower(), parts[1:]

            if cmd in ("quit", "exit", "q"):
                self._running = False
                break
            if cmd in ("help", "h", "?"):
                self._line(HELP)
            elif cmd in ("status", "s", ""):
                self.show()
            elif cmd == "personas":
                for p in self.bot.personas.list():
                    mark = " *" if p["id"] == self.bot.agent.current_persona_id else ""
                    self._line(f"  {p['id']:<12} {p['name']}{mark}")
            elif cmd == "persona":
                if args and self.bot.agent.set_persona(args[0]):
                    self.bot.cfg.save(vault=self.bot.vault)
                    self._line(f"已切换人格: {args[0]}")
                else:
                    self._line("用法: persona <id>")
            elif cmd == "media":
                self._cmd_media(args[0] if args else "")
            elif cmd == "llm":
                await self._cmd_llm()
            elif cmd == "keepalive":
                await self._cmd_keepalive()
            elif cmd == "refresh":
                self.show()
            elif cmd == "clear":
                self.bot.ctx.clear_all()
                self._line("上下文已清空")
            else:
                self._line(f"未知命令: {cmd}（输入 help 查看）")

        self._line("面板退出。")

    def start_background(self) -> None:
        """后台启动面板（不阻塞主流程）。"""
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run()), daemon=True, name="panel"
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
