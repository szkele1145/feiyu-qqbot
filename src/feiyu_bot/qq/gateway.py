"""QQ 官方机器人 WebSocket 网关客户端。

要点：

- 先取网关地址，再带鉴权连上
- 收到 HELLO(op=10) 后按心跳间隔发心跳(op=1, d=最后一次收到的 s)
- 支持断线重连、session 恢复(op=6)、无效会话重鉴权(op=9)
- 事件通过回调分发
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from ..logging_setup import get_logger
from .constants import (
    GATEWAY_READY,
    GATEWAY_RESUMED,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_HTTP_CALLBACK_ACK,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
)
from .token_manager import TokenManager

log = get_logger("feiyu.qq.gateway")

EventHandler = Callable[[str, dict], Awaitable[None]]


class QQGateway:
    """QQ 官方机器人 WS 网关。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        token_manager: TokenManager,
        on_event: EventHandler,
    ):
        self.cfg = cfg
        self.tokens = token_manager
        self.on_event = on_event
        self.intents = int(cfg.get("intents_value", 0))
        self.shard = [0, 1]
        self.sandbox = bool(cfg.get("sandbox", False))

        self.session_id = ""
        self.resume_url = ""
        self.last_seq: int | None = None
        self._ws: Any = None
        self._heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._reconnect_count = 0
        # 连接健康状态（供保活任务探测）
        self.last_recv_at = 0.0
        self.connected_at = 0.0

    # ------------------------------------------------------------ 网关地址

    async def _fetch_gateway_url(self) -> str:
        """获取 WS 网关地址。"""
        import httpx  # noqa: PLC0415

        token = await self.tokens.get()
        base = (
            "https://sandbox.api.sgroup.qq.com"
            if self.sandbox
            else "https://api.sgroup.qq.com"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/gateway",
                headers={
                    "Authorization": f"QQBot {token}",
                    "X-Union-Appid": str(self.cfg.get("appid", "")),
                },
            )
            resp.raise_for_status()
            data = resp.json()
        url = str(data.get("url", ""))
        if not url:
            raise RuntimeError(f"获取网关地址失败: {data}")
        return url

    # ------------------------------------------------------------ 主循环

    async def run(self) -> None:
        """持续运行，自动重连。"""
        self._running = True
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("网关连接异常: %s", exc)
            if not self._running:
                break
            self._reconnect_count += 1
            delay = min(5 * self._reconnect_count, 60) + random.random() * 3
            log.info("%.1f 秒后重连（第 %d 次）", delay, self._reconnect_count)
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _connect_once(self) -> None:
        url = self.resume_url or await self._fetch_gateway_url()
        token = await self.tokens.get()
        log.info("连接网关: %s", url.split("?")[0])

        async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
            self._ws = ws
            self.connected_at = time.time()
            self.last_recv_at = time.time()
            self._reconnect_count = 0

            # 有 session 就恢复，否则重新鉴权
            if self.session_id and self.last_seq is not None:
                await self._send_identify(resume=True)
            else:
                await self._send_identify(resume=False)

            async for raw in ws:
                self.last_recv_at = time.time()
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_payload(payload)

    async def _send_identify(self, resume: bool) -> None:
        token = await self.tokens.get()
        if resume and self.session_id:
            payload = {
                "op": OP_RESUME,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self.session_id,
                    "seq": self.last_seq,
                },
            }
            log.info("尝试恢复会话 %s", self.session_id[:12])
        else:
            payload = {
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self.intents,
                    "shard": self.shard,
                    "properties": {},
                },
            }
            log.info("发送鉴权 (intents=%d)", self.intents)
        await self._ws.send(json.dumps(payload))

    # ------------------------------------------------------------ 消息处理

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        d = payload.get("d")
        s = payload.get("s")

        if s is not None:
            self.last_seq = int(s)

        if op == OP_DISPATCH:
            t = payload.get("t", "")
            await self._dispatch(t, d or {})
        elif op == OP_HELLO:
            interval = int((d or {}).get("heartbeat_interval", 30_000)) / 1000
            log.info("收到 HELLO，心跳间隔 %.1f 秒", interval)
            self._start_heartbeat(interval)
        elif op == OP_HEARTBEAT:
            # 服务端要求立即心跳
            await self._send_heartbeat()
        elif op == OP_HEARTBEAT_ACK:
            log.debug("心跳已确认")
        elif op == OP_RECONNECT:
            log.info("服务端要求重新连接")
            self.resume_url = ""
            self.session_id = ""
            await self.stop()
        elif op == OP_INVALID_SESSION:
            # d 为 true 表示可恢复，false/dict 表示需重新鉴权
            if d is True:
                log.info("会话可恢复，等待重连")
            else:
                log.warning("会话无效，重新鉴权")
                self.session_id = ""
                self.last_seq = None
            await self.stop()
        elif op == OP_HTTP_CALLBACK_ACK:
            log.debug("HTTP 回调 ACK")

    async def _dispatch(self, t: str, d: dict[str, Any]) -> None:
        """分发事件。"""
        if t == GATEWAY_READY:
            self.session_id = str(d.get("session_id", "") or "")
            self.resume_url = str(d.get("resume_gateway_url", "") or "")
            user = d.get("user") or {}
            log.info(
                "已连接 | 机器人=%s(session=%s)",
                user.get("username", "?"), self.session_id[:12],
            )
            return
        if t == GATEWAY_RESUMED:
            log.info("会话已恢复")
            return

        log.debug("事件 %s", t)
        try:
            await self.on_event(t, d)
        except Exception as exc:  # noqa: BLE001
            log.exception("事件处理失败 %s: %s", t, exc)

    # ------------------------------------------------------------ 心跳

    def _start_heartbeat(self, interval: float) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(interval), name="qq-heartbeat"
        )

    async def _heartbeat_loop(self, interval: float) -> None:
        try:
            while self._running:
                await asyncio.sleep(interval)
                try:
                    await self._send_heartbeat()
                except ConnectionClosed:
                    log.warning("心跳时连接已断开")
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("心跳发送失败: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _send_heartbeat(self) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.last_seq}))

    # ------------------------------------------------------------ 状态

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self.connected_at > 0

    def health(self) -> dict[str, Any]:
        """连接健康信息，供保活任务与面板使用。"""
        now = time.time()
        return {
            "connected": self.is_connected,
            "session_id": self.session_id[:12],
            "last_seq": self.last_seq,
            "seconds_since_recv": round(now - self.last_recv_at, 1) if self.last_recv_at else None,
            "reconnect_count": self._reconnect_count,
        }
