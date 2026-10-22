"""access_token 管理：自动获取、缓存、过期前刷新。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ..logging_setup import get_logger
from .constants import TOKEN_URL

log = get_logger("feiyu.qq.token")


class TokenManager:
    """QQ 官方机器人 access_token 管理器。

    使用新版接口 ``POST https://bots.qq.com/app/getAppAccessToken``。
    token 有效期约 7200 秒，提前 300 秒刷新。
    """

    REFRESH_MARGIN = 300.0

    def __init__(self, appid: str, secret: str, proxy: str = ""):
        self.appid = appid.strip()
        self.secret = secret.strip()
        self.proxy = proxy or None
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 获取

    async def _fetch(self) -> tuple[str, int]:
        if not self.appid or not self.secret:
            raise RuntimeError("QQ appid/secret 未配置")
        async with httpx.AsyncClient(timeout=30, proxy=self.proxy) as client:
            resp = await client.post(
                TOKEN_URL,
                json={"appId": self.appid, "clientSecret": self.secret},
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        token = str(data.get("access_token", ""))
        ttl = int(data.get("expires_in", "7200"))
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        return token, ttl

    async def get(self) -> str:
        """取可用 token，过期自动刷新。"""
        async with self._lock:
            if self._token and time.time() < self._expires_at - self.REFRESH_MARGIN:
                return self._token
            try:
                token, ttl = await self._fetch()
            except Exception:
                if self._token and time.time() < self._expires_at:
                    # 拉新失败但旧 token 还没过有效期，先用旧的
                    log.warning("token 刷新失败，沿用旧 token")
                    return self._token
                raise
            self._token = token
            self._expires_at = time.time() + ttl
            log.info("access_token 已刷新，有效期 %d 秒", ttl)
            return self._token

    async def invalidate(self) -> None:
        """外部判定的 401 后强制刷新。"""
        async with self._lock:
            self._token = ""
            self._expires_at = 0.0

    # ------------------------------------------------------------ 背景刷新

    async def start_auto_refresh(self, interval: float | None = None) -> None:
        """启动后台刷新任务（每 30 分钟检查一次）。"""
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(interval or 1800), name="token-refresh"
        )

    def stop_auto_refresh(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.get()
            except Exception as exc:  # noqa: BLE001
                log.warning("后台 token 刷新失败: %s", exc)
