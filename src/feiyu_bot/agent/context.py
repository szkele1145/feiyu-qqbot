"""上下文管理：防超长。

策略（三层，从硬到软）：

1. **硬截断** —— 估算 token 数超过上限时，从最旧的非系统消息开始丢弃。
2. **滚动摘要** —— 把被丢弃的部分压缩成一段摘要，保留语义连续性。
3. **图片描述限量** —— 单张图的文字画像按字符上限裁剪，避免一图吃满窗口。

估算方式：中文按 ~1.6 字符/token，英文按 ~4 字符/token 的混合启发式。
不需要 tiktoken 依赖，误差可接受。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..llm.client import ChatMessage
from ..logging_setup import get_logger

log = get_logger("feiyu.context")

# 中文字符（含 CJK 统一表意文字、全角标点）
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """估算文本 token 数。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    # 中文约 1.5 字符/token，其余约 4 字符/token
    return int(cjk / 1.5 + other / 4) + 1


def estimate_message_tokens(msg: ChatMessage) -> int:
    """估算单条消息的 token 数（含工具调用）。"""
    total = 4  # 每条消息的角色/分隔开销
    content = msg.content
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    total += estimate_tokens(str(part.get("text", "")))
                else:
                    # 图片块只算占位成本
                    total += 80
    for tc in msg.tool_calls:
        total += estimate_tokens(tc.name) + estimate_tokens(tc.arguments)
    if msg.tool_call_id:
        total += 4
    return total


@dataclass
class Conversation:
    """单个会话（群或私聊）的上下文。"""

    key: str
    system: str = ""
    messages: list[ChatMessage] = field(default_factory=list)
    summary: str = ""
    updated_at: float = 0.0
    # 已压缩次数，便于排查
    compress_count: int = 0

    def add(self, msg: ChatMessage) -> None:
        self.messages.append(msg)

    def total_tokens(self) -> int:
        return (
            estimate_tokens(self.system)
            + estimate_tokens(self.summary)
            + sum(estimate_message_tokens(m) for m in self.messages)
        )


class ContextManager:
    """按会话维护上下文，超长时压缩。"""

    def __init__(self, cfg: dict[str, Any]):
        self.max_history = int(cfg.get("max_history", 20))
        self.max_tokens = int(cfg.get("max_tokens", 6000))
        self.keep_recent = int(cfg.get("keep_recent", 8))
        self.summarize_ratio = float(cfg.get("summarize_ratio", 0.75))
        self.summary_max_tokens = int(cfg.get("summary_max_tokens", 400))
        self.max_image_desc_chars = int(cfg.get("max_image_desc_chars", 1200))
        self.conversations: dict[str, Conversation] = {}

    # ------------------------------------------------------------ 会话

    def get(self, key: str, system: str = "") -> Conversation:
        conv = self.conversations.get(key)
        if conv is None:
            conv = Conversation(key=key, system=system)
            self.conversations[key] = conv
        elif system:
            conv.system = system
        return conv

    def drop(self, key: str) -> None:
        self.conversations.pop(key, None)

    def clear_all(self) -> None:
        self.conversations.clear()

    # ------------------------------------------------------------ 组装

    def build(self, conv: Conversation) -> list[ChatMessage]:
        """产出送模型的最终消息列表（含 system、摘要、近期消息）。"""
        out: list[ChatMessage] = []
        if conv.system:
            out.append(ChatMessage(role="system", content=conv.system))
        if conv.summary:
            out.append(
                ChatMessage(
                    role="system",
                    content=f"【此前对话摘要】\n{conv.summary}",
                )
            )
        out.extend(conv.messages)
        return out

    def trim_image_text(self, text: str) -> str:
        """裁剪图片描述长度。"""
        limit = self.max_image_desc_chars
        if len(text) <= limit:
            return text
        return text[:limit] + "…（图片描述已截断）"

    # ------------------------------------------------------------ 压缩

    async def compress_if_needed(
        self,
        conv: Conversation,
        summarizer=None,
    ) -> bool:
        """超长时压缩。返回是否发生了压缩。

        summarizer: 可选的异步回调 ``async (text) -> str``，
        用于调用模型做摘要；没有它则退化为纯截断（不额外烧 token）。
        """
        # 1) 条数超限 —— 先按条数裁
        if len(conv.messages) > self.max_history:
            overflow = len(conv.messages) - self.max_history
            conv.messages = conv.messages[overflow:]

        # 2) token 超限
        threshold = int(self.max_tokens * self.summarize_ratio)
        if conv.total_tokens() <= threshold:
            return False

        # 保护最近的 keep_recent 条
        keep = max(1, min(self.keep_recent, len(conv.messages) - 1))
        old = conv.messages[:-keep] if keep < len(conv.messages) else []
        recent = conv.messages[-keep:] if keep else list(conv.messages)

        if not old:
            # 只剩少量消息，仍然超长 —— 硬裁掉最旧
            if conv.total_tokens() > self.max_tokens:
                while len(recent) > 1 and conv.total_tokens() > self.max_tokens:
                    recent.pop(0)
            conv.messages = recent
            conv.compress_count += 1
            return True

        # 3) 把旧消息压成摘要
        old_text = "\n".join(
            f"{m.role}: {_flatten(m.content)}" for m in old
        )
        if summarizer is not None:
            try:
                summary = await summarizer(old_text)
            except Exception as exc:  # noqa: BLE001
                log.warning("摘要生成失败，退化为截断: %s", exc)
                summary = ""
        else:
            summary = ""

        if not summary:
            # 退化：只留旧消息里的用户意图摘要（纯截断，零 token 成本）
            summary = _cheap_summary(old)

        conv.summary = _merge_summary(conv.summary, summary, self.summary_max_tokens)
        conv.messages = recent
        conv.compress_count += 1
        log.info(
            "会话 %s 上下文已压缩: 丢弃 %d 条旧消息, 摘要 %d 字, 剩余 %d 条",
            conv.key, len(old), len(conv.summary), len(recent),
        )
        return True

    def stats(self) -> dict[str, Any]:
        return {
            "sessions": len(self.conversations),
            "max_history": self.max_history,
            "max_tokens": self.max_tokens,
            "keep_recent": self.keep_recent,
            "detail": {
                key: {
                    "messages": len(conv.messages),
                    "tokens": conv.total_tokens(),
                    "summary_chars": len(conv.summary),
                    "compress_count": conv.compress_count,
                }
                for key, conv in self.conversations.items()
            },
        }


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
                else:
                    parts.append("[图片]")
        return " ".join(parts)
    return str(content)


def _cheap_summary(messages: list[ChatMessage]) -> str:
    """零成本的粗摘要：抽取用户发言要点。"""
    points: list[str] = []
    for m in messages:
        if m.role != "user":
            continue
        text = _flatten(m.content).strip().replace("\n", " ")
        if not text:
            continue
        points.append(text[:60])
    if not points:
        return "（此前有若干轮对话）"
    return "用户此前提到：" + "；".join(points[-8:])


def _merge_summary(old: str, new: str, limit: int) -> str:
    """合并新旧摘要并裁到 token 上限内。"""
    merged = f"{old}\n{new}".strip() if old else new.strip()
    max_chars = max(100, limit * 2)
    if len(merged) > max_chars:
        # 保留结尾（最近的信息更重要）
        merged = "…（前略）\n" + merged[-max_chars:]
    return merged
