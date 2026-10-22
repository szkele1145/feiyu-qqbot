"""LLM 客户端：OpenAI Chat Completions 兼容协议。

- 自定义 base_url / api_key / model
- 工具调用（function calling）
- 失败重试 + 指数退避
- 可选代理，代理失效自动降级直连（反之亦然）
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ..logging_setup import get_logger

log = get_logger("feiyu.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串

    def parsed_args(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class ChatMessage:
    role: str = "user"
    content: Any = ""  # str 或 OpenAI 多模态 parts 列表
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    def to_api(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            msg["name"] = self.name
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg


class LLMError(RuntimeError):
    pass


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(self, cfg: dict[str, Any]):
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.api_key = str(cfg.get("api_key", ""))
        self.model = str(cfg.get("model", ""))
        self.vision_model = str(cfg.get("vision_model", "") or cfg.get("model", ""))
        self.temperature = float(cfg.get("temperature", 0.9))
        self.top_p = float(cfg.get("top_p", 0.95))
        self.max_tokens = int(cfg.get("max_tokens", 2048))
        self.timeout = float(cfg.get("timeout", 120))
        self.max_retries = int(cfg.get("max_retries", 3))
        self.proxy = str(cfg.get("proxy", "")).strip()

        self._no_proxy_until = 0.0  # 代理被判坏后直连的截止时间
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ 会话管理

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            proxy = self.proxy if self._proxy_ok() else None
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                proxy=proxy,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _proxy_ok(self) -> bool:
        return bool(self.proxy) and time.time() >= self._no_proxy_until

    def _mark_proxy_bad(self) -> None:
        """代理坏掉后 10 分钟内直连。"""
        if self.proxy:
            log.warning("代理 %s 请求失败，10 分钟内改为直连", self.proxy)
            self._no_proxy_until = time.time() + 600
            if self._client is not None:
                self._client_failed = True

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------ 请求

    def _payload(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if stream:
            body["stream"] = True
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _parse_message(data: dict[str, Any]) -> ChatMessage:
        choice = (data.get("choices") or [{}])[0]
        raw = choice.get("message") or {}
        tcs = [
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments", ""),
            )
            for tc in (raw.get("tool_calls") or [])
        ]
        return ChatMessage(
            role=raw.get("role", "assistant"),
            content=raw.get("content") or "",
            tool_calls=tcs,
        )

    async def _post(self, body: dict[str, Any], *, stream: bool = False) -> httpx.Response:
        """带代理降级的 POST。"""
        url = f"{self.base_url}/chat/completions"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            client = await self._http()
            try:
                if stream:
                    req = client.build_request("POST", url, json=body)
                    return await client.send(req, stream=True)
                resp = await client.post(url, json=body)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                    await asyncio.sleep(min(2**attempt + random.random(), 20))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                if self.proxy and self._proxy_ok() and self._looks_like_proxy_issue(exc):
                    self._mark_proxy_bad()
                    continue  # 立刻用直连重试
                await asyncio.sleep(min(2**attempt + random.random(), 20))
        raise LLMError(f"LLM 请求失败（重试 {self.max_retries} 次后）: {last_exc}")

    @staticmethod
    def _looks_like_proxy_issue(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            k in text
            for k in ("proxy", "tunnel", "connect", "timeout", "eof", "reset")
        )

    # ------------------------------------------------------------ 对外 API

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatMessage:
        body = self._payload(
            messages, model=model, tools=tools,
            temperature=temperature, max_tokens=max_tokens,
        )
        resp = await self._post(body)
        return self._parse_message(resp.json())

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式返回文本增量。"""
        body = self._payload(
            messages, model=model, temperature=temperature,
            max_tokens=max_tokens, stream=True,
        )
        resp = await self._post(body, stream=True)
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                piece = delta.get("content")
                if piece:
                    yield piece
        finally:
            await resp.aclose()

    async def ping(self) -> tuple[bool, str]:
        """连通性测试：发一条 1 token 的问候。"""
        try:
            msg = await self.chat(
                [ChatMessage(role="user", content="ping")],
                max_tokens=8, temperature=0.0,
            )
            return True, str(msg.content)[:120]
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:300]

    async def list_models(self) -> tuple[bool, list[str] | str]:
        """从服务端拉取可用模型列表（OpenAI 兼容 /models）。

        返回 (成功与否, 模型名列表 或 错误信息)。
        自建/本地服务可能没实现这个端点，此时退回配置文件里的 model。
        """
        if not self.base_url:
            return False, "未配置 base_url"
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"请求失败：{exc}"

        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return False, "返回格式不是 OpenAI 兼容的 /models"

        names: list[str] = []
        for it in items:
            if isinstance(it, dict):
                mid = it.get("id") or it.get("name")
                if mid:
                    names.append(str(mid))
            elif isinstance(it, str):
                names.append(it)
        if not names:
            return False, "服务端返回空模型列表"
        return True, sorted(names)
