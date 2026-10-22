"""Bot 主入口：组装各层、提供发送器、调度定时任务。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .agent.context import ContextManager
from .agent.core import Agent
from .config import Config
from .llm import LLMClient
from .llm.budget import BudgetGuard
from .logging_setup import get_logger, setup_logging
from .media import MediaLibrary
from .personas import PersonaManager
from .qq import (
    QQApiClient,
    QQGateway,
    ReplyPool,
    TokenManager,
    compute_intents,
    parse_event,
)
from .runtime_guard import RuntimeGuard
from .secrets import SecretVault
from .vision import VisionPipeline

log = get_logger("feiyu.bot")


class Bot:
    """feiyu-qqbot 主对象。"""

    def __init__(self, cfg: Config, vault: SecretVault | None = None):
        self.cfg = cfg
        self.vault = vault
        cfg.ensure_dirs()

        # ---- QQ ----
        qq_cfg = dict(cfg.section("qq"))
        intent_names = qq_cfg.get("intents", [])
        qq_cfg["intents_value"] = compute_intents(intent_names)
        self.tokens = TokenManager(
            str(qq_cfg.get("appid", "")), str(qq_cfg.get("secret", "")),
            proxy=str(cfg.get("llm.proxy", "")),
        )
        self.api = QQApiClient(qq_cfg, self.tokens)
        self.gateway = QQGateway(qq_cfg, self.tokens, self._on_event)

        # ---- LLM ----
        self.llm = LLMClient(cfg.section("llm"))
        self.budget = BudgetGuard(
            cfg.section("llm"), state_path=cfg.data_dir / "budget.json"
        )

        # ---- 人格 / 媒体 / 识图 ----
        self.personas = PersonaManager(cfg.path("persona.pack_dir"))
        media_cfg = dict(cfg.section("media"))
        media_cfg["voice_dir"] = str(cfg.path("media.voice_dir"))
        media_cfg["image_dir"] = str(cfg.path("media.image_dir"))
        self.media = MediaLibrary(media_cfg)
        self.vision = VisionPipeline(cfg.section("vision"))

        # ---- Agent ----
        self.ctx = ContextManager(cfg.section("context"))
        self.reply_pool = ReplyPool(cfg.section("qq"))
        self.runtime = RuntimeGuard(cfg.section("runtime"))
        self.agent = Agent(
            cfg=cfg,
            llm=self.llm,
            personas=self.personas,
            media=self.media,
            vision=self.vision,
            ctx_mgr=self.ctx,
            reply_pool=self.reply_pool,
            runtime_guard=self.runtime,
            budget=self.budget,
            sender=self,
        )

        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._last_activity = time.time()
        # 事件循环引用（WebUI 线程与主循环通信用）
        self.loop: asyncio.AbstractEventLoop | None = None
        self.webui = None  # WebUIController，由 main 注入

    # ------------------------------------------------------------ 发送器

    async def send_text(
        self,
        openid: str,
        text: str,
        *,
        target: str = "user",
        msg_id: str = "",
        msg_seq: int = 1,
        at_user: str = "",
    ):
        """供 Agent 调用的文本发送。"""
        return await self.api.send_text(
            target, openid, text,
            msg_id=msg_id, msg_seq=msg_seq, at_user=at_user,
        )

    async def send_image(
        self,
        openid: str,
        *,
        file_path=None,
        target: str = "user",
        msg_id: str = "",
        msg_seq: int = 1,
    ):
        return await self.api.send_image(
            target, openid, file_path=file_path, msg_id=msg_id, msg_seq=msg_seq
        )

    async def send_voice(
        self,
        openid: str,
        *,
        file_path=None,
        target: str = "user",
        msg_id: str = "",
        msg_seq: int = 1,
    ):
        return await self.api.send_voice(
            target, openid, file_path=file_path, msg_id=msg_id, msg_seq=msg_seq
        )

    # ------------------------------------------------------------ 事件

    async def _on_event(self, event_name: str, payload: dict[str, Any]) -> None:
        msg = parse_event(event_name, payload)
        if msg is None:
            return
        self._last_activity = time.time()

        # 记录被动回复上下文（用于之后的主动发言复用 msg_id）
        if msg.msg_id:
            self.reply_pool.add(msg.target, msg.reply_target, msg.msg_id)
        # 白名单过滤
        if not self._allowed(msg):
            log.debug("消息被白名单过滤: %s", msg.session_key)
            return

        await self.agent.handle_message(msg)

    def _allowed(self, msg) -> bool:
        qq_cfg = self.cfg.section("qq")
        blacklist = set(qq_cfg.get("blacklist", []) or [])
        if msg.user_openid in blacklist or msg.openid in blacklist:
            return False
        group_wl = set(qq_cfg.get("group_whitelist", []) or [])
        user_wl = set(qq_cfg.get("user_whitelist", []) or [])
        if msg.kind == "group":
            if group_wl and msg.openid not in group_wl:
                return False
        elif user_wl and msg.user_openid not in user_wl:
            return False
        return True

    # ------------------------------------------------------------ 定时任务

    async def _keepalive_loop(self) -> None:
        """保活：每 15 分钟检查一次连接健康，必要时重连。"""
        interval = float(self.cfg.get("scheduler.keepalive_seconds", 900))
        log.info("保活任务已启动，间隔 %d 秒", int(interval))
        while not self._stopping:
            await asyncio.sleep(interval)
            try:
                await self._keepalive_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("保活检查异常: %s", exc)

    async def _keepalive_once(self) -> None:
        """执行一次保活检查。"""
        health = self.gateway.health()
        idle = health.get("seconds_since_recv")

        # 网关未连接 → 强制重连
        if not health.get("connected"):
            log.warning("保活：网关未连接，尝试重连")
            await self.gateway.stop()
            self.gateway._running = True  # 允许 run() 继续
            return

        # 长时间无数据 → 重连
        if idle is not None and idle > interval_threshold(self.cfg):
            log.warning("保活：%.0f 秒无数据，重连网关", idle)
            await self.gateway.stop()
            self.gateway._running = True
            return

        # token 提前刷新
        try:
            await self.tokens.get()
        except Exception as exc:  # noqa: BLE001
            log.warning("保活：token 刷新失败: %s", exc)

        log.info("保活检查通过: %s", health)

    # ------------------------------------------------------------ 运行

    async def run(self) -> None:
        """启动全部组件。"""
        self.loop = asyncio.get_running_loop()
        self.media.init()
        await self.tokens.start_auto_refresh()

        log.info("=" * 60)
        log.info("feiyu-qqbot 启动 | 人格=%s | 沙箱=%s",
                 self.agent.current_persona_id, self.cfg.get("qq.sandbox"))
        log.info("谷时模式=%s (%s) | 当前在谷时=%s",
                 self.runtime.offpeak_only, self.runtime.describe_windows(),
                 self.runtime.in_offpeak())
        log.info("=" * 60)

        self._tasks.append(asyncio.create_task(self.gateway.run(), name="gateway"))
        self._tasks.append(
            asyncio.create_task(self._keepalive_loop(), name="keepalive")
        )

        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopping = True
        await self.gateway.stop()
        self.tokens.stop_auto_refresh()
        for task in self._tasks:
            task.cancel()
        await self.api.close()
        await self.llm.close()

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        return {
            "gateway": self.gateway.health(),
            "agent": self.agent.status(),
            "personas": self.personas.list(),
            "media": {
                "voices": len(self.media.list("voice")),
                "images": len(self.media.list("image")),
            },
            "offpeak_windows": self.runtime.describe_windows(),
        }


def interval_threshold(cfg: Config) -> float:
    """多久没收到数据算掉线。取心跳间隔的 3 倍。"""
    keepalive = float(cfg.get("scheduler.keepalive_seconds", 900))
    return max(120.0, keepalive * 2 / 3)


def create_bot(config_path: str | Path | None = None) -> Bot:
    """工厂：从配置文件创建 Bot。"""
    from .config import load_config  # noqa: PLC0415

    cfg = load_config(config_path)
    setup_logging(
        str(cfg.get("app.log_level", "INFO")),
        cfg.log_dir,
    )
    vault = SecretVault(cfg.data_dir)
    cfg.expand_env()
    return Bot(cfg, vault)
