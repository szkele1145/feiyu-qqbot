"""QQ 官方机器人 REST 客户端。

职责：

- 统一带鉴权、重试、限速的 HTTP 调用
- 发送文本 / Markdown / 富媒体（图片、语音）
- 上传富媒体拿到 file_info
- 被动回复（带 msg_id）与主动消息（带 event_id + msg_seq）
- @ 他人
"""

from __future__ import annotations

import asyncio
import base64
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..logging_setup import get_logger
from .constants import (
    API_BASE_PROD,
    API_BASE_SANDBOX,
    ERR_ACTIVE_LIMIT,
    ERR_MSG_LIMIT,
    ERR_TOKEN_EXPIRED,
    MEDIA_IMAGE,
    MEDIA_VOICE,
    MSG_MARKDOWN,
    MSG_MEDIA,
    MSG_TEXT,
    infer_file_type,
)
from .token_manager import TokenManager

log = get_logger("feiyu.qq.api")


class QQApiError(RuntimeError):
    """QQ 接口错误，携带 code 便于分支处理。"""

    def __init__(self, code: str, message: str, payload: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.payload = payload

    @property
    def is_active_limit(self) -> bool:
        """主动消息配额用尽（22003 / 22004 / 22005）。"""
        return self.code in (ERR_ACTIVE_LIMIT, "22004", "22005")

    @property
    def is_token_error(self) -> bool:
        return self.code in (ERR_TOKEN_EXPIRED, "11244", "11245")

    @property
    def is_permission_error(self) -> bool:
        """权限/白名单类错误。"""
        return self.code in ("22009", "11253", "403")

    @property
    def is_rate_limited(self) -> bool:
        """超频（22009）。"""
        return self.code == ERR_MSG_LIMIT


@dataclass
class MediaInfo:
    """上传后的富媒体信息。"""

    file_uuid: str = ""
    file_info: str = ""
    ttl: int = 0
    url: str = ""


@dataclass
class SendResult:
    ok: bool = False
    message_id: str = ""
    timestamp: int = 0
    error: str = ""
    code: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class QQApiClient:
    """QQ 官方机器人 REST 客户端。"""

    def __init__(self, cfg: dict[str, Any], token_manager: TokenManager):
        self.cfg = cfg
        self.tokens = token_manager
        sandbox = bool(cfg.get("sandbox", False))
        self.base = API_BASE_SANDBOX if sandbox else API_BASE_PROD
        self.proxy = str(cfg.get("proxy", "") or "") or None
        self._client: httpx.AsyncClient | None = None
        # 简易限速：全局最小请求间隔
        self._min_interval = float(cfg.get("min_request_interval", 0.35))
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 底层

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base,
                timeout=httpx.Timeout(60),
                proxy=self.proxy,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            delta = time.monotonic() - self._last_request
            if delta < self._min_interval:
                await asyncio.sleep(self._min_interval - delta)
            self._last_request = time.monotonic()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        """带鉴权与重试的请求，返回 JSON。"""
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            await self._throttle()
            try:
                token = await self.tokens.get()
                client = await self._http()
                resp = await client.request(
                    method,
                    path,
                    json=json_body,
                    params=params,
                    headers={
                        "Authorization": f"QQBot {token}",
                        "X-Union-Appid": str(self.cfg.get("appid", "")),
                    },
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(min(2**attempt + random.random(), 10))
                continue

            if resp.status_code in (200, 201, 202):
                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    return {"raw": resp.text}

            # 解析错误体
            try:
                body = resp.json()
            except ValueError:
                body = {"message": resp.text[:300]}

            code = str(body.get("code", resp.status_code))
            message = str(body.get("message", resp.reason_phrase))

            if code == ERR_TOKEN_EXPIRED or resp.status_code == 401:
                log.warning("token 失效，强制刷新后重试")
                await self.tokens.invalidate()
                continue
            if resp.status_code == 429:
                await asyncio.sleep(min(2**attempt + random.random(), 15))
                last_exc = QQApiError(code, message, body)
                continue

            raise QQApiError(code, message, body)

        raise QQApiError("NETWORK", f"请求失败: {last_exc}")

    # ------------------------------------------------------------ 富媒体上传

    async def upload_media(
        self,
        target: str,
        openid: str,
        file_path: Path | str | None = None,
        *,
        data: bytes | None = None,
        url: str = "",
        file_type: int | None = None,
        send_immediately: bool = False,
    ) -> MediaInfo:
        """上传富媒体。

        target: "group" | "user" | "c2c"
        openid: 群 openid 或用户 openid
        file_type: 见 constants.FILE_TYPE；为 None 时按扩展名推断
        """
        if data is None and file_path is not None:
            data = Path(file_path).read_bytes()
        if file_type is None:
            name = str(file_path) if file_path else "image.jpg"
            file_type = infer_file_type(name)

        path = (
            f"/v2/groups/{openid}/files"
            if target == "group"
            else f"/v2/users/{openid}/files"
        )
        body: dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": send_immediately,
        }
        if url:
            body["url"] = url
        elif data:
            body["file_data"] = base64.b64encode(data).decode("ascii")
        else:
            raise ValueError("必须提供 file_path、data 或 url 之一")

        result = await self.request("POST", path, json_body=body)
        return MediaInfo(
            file_uuid=str(result.get("file_uuid", "")),
            file_info=str(result.get("file_info", "")),
            ttl=int(result.get("ttl", 0) or 0),
            url=str(result.get("url", "")),
        )

    # ------------------------------------------------------------ 发消息

    async def send_text(
        self,
        target: str,
        openid: str,
        text: str,
        *,
        msg_id: str = "",
        event_id: str = "",
        msg_seq: int = 1,
        at_user: str = "",
    ) -> SendResult:
        """发纯文本。msg_id 存在即为被动回复。"""
        content = text
        if at_user:
            content = f"<@!{at_user}> {text}"
        return await self._send(
            target, openid,
            {"content": content, "msg_type": MSG_TEXT},
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    async def send_markdown(
        self,
        target: str,
        openid: str,
        markdown: str,
        *,
        msg_id: str = "",
        event_id: str = "",
        msg_seq: int = 1,
        keyboard: dict | None = None,
    ) -> SendResult:
        body: dict[str, Any] = {"markdown": {"content": markdown}, "msg_type": MSG_MARKDOWN}
        if keyboard:
            body["keyboard"] = keyboard
        return await self._send(
            target, openid, body,
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    async def send_image(
        self,
        target: str,
        openid: str,
        *,
        file_path: Path | str | None = None,
        data: bytes | None = None,
        url: str = "",
        msg_id: str = "",
        event_id: str = "",
        msg_seq: int = 1,
        text: str = "",
    ) -> SendResult:
        """发图片。先上传拿 file_info，再以 msg_type=7 发送。"""
        # 有公网 url 时可直接发，省一次上传
        if url:
            body: dict[str, Any] = {
                "msg_type": MSG_MEDIA,
                "content": " ",  # msg_type=7 时 content 必填（可为空格）
                "media": {"url": url},
            }
            if text:
                body["content"] = text
            return await self._send(
                target, openid, body,
                msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
            )

        info = await self.upload_media(
            target, openid, file_path, data=data, file_type=None
        )
        body = {
            "msg_type": MSG_MEDIA,
            "content": " ",
            "media": {"file_info": info.file_info},
        }
        if text:
            body["content"] = text
        return await self._send(
            target, openid, body,
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    async def send_voice(
        self,
        target: str,
        openid: str,
        *,
        file_path: Path | str | None = None,
        data: bytes | None = None,
        msg_id: str = "",
        event_id: str = "",
        msg_seq: int = 1,
    ) -> SendResult:
        """发语音。file_type=3（官方语音类型，支持 silk/amr/mp3 等由平台转码）。"""
        info = await self.upload_media(
            target, openid, file_path, data=data, file_type=MEDIA_VOICE
        )
        body = {
            "msg_type": MSG_MEDIA,
            # 官方要求 msg_type=7 时 content 必须非空
            "content": " ",
            "media": {"file_info": info.file_info},
        }
        return await self._send(
            target, openid, body,
            msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
        )

    async def _send(
        self,
        target: str,
        openid: str,
        body: dict[str, Any],
        *,
        msg_id: str = "",
        event_id: str = "",
        msg_seq: int = 1,
    ) -> SendResult:
        """统一发送入口。

        被动回复：携带 msg_id（body 内）+ msg_seq（每条递增）。
        主动消息：无 msg_id，改用 query event_id（可选）+ msg_seq 递增。
        """
        body = dict(body)
        body["msg_seq"] = msg_seq
        if msg_id:
            body["msg_id"] = msg_id

        path = (
            f"/v2/groups/{openid}/messages"
            if target == "group"
            else f"/v2/users/{openid}/messages"
        )
        params = {"event_id": event_id} if (event_id and not msg_id) else None

        try:
            result = await self.request("POST", path, json_body=body, params=params)
        except QQApiError as exc:
            if exc.is_active_limit:
                log.warning("主动消息配额用尽（%s），本条放弃", exc.code)
            return SendResult(ok=False, error=exc.message, code=exc.code)

        return SendResult(
            ok=True,
            message_id=str(result.get("id", "")),
            timestamp=int(result.get("timestamp", 0) or 0),
            raw=result,
        )

    # ------------------------------------------------------------ 频道（guild）

    async def send_channel_message(
        self,
        channel_id: str,
        content: str,
        *,
        msg_id: str = "",
        image_url: str = "",
    ) -> SendResult:
        """频道消息（旧版 guild 接口）。"""
        body: dict[str, Any] = {"content": content}
        if image_url:
            body["image"] = image_url
        if msg_id:
            body["msg_id"] = msg_id
        try:
            result = await self.request(
                "POST", f"/channels/{channel_id}/messages", json_body=body
            )
        except QQApiError as exc:
            return SendResult(ok=False, error=exc.message, code=exc.code)
        return SendResult(
            ok=True, message_id=str(result.get("id", "")), raw=result
        )


def _infer_file_type(file_path: Path | str | None) -> int:
    """按扩展名推断 file_type（已废弃，保留兼容）。"""
    if file_path is None:
        return MEDIA_IMAGE
    return infer_file_type(str(file_path))
