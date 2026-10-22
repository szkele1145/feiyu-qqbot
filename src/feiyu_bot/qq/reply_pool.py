"""被动回复池 —— 主动发言的关键。

官方规则（QQ 开放平台 API v2）：

- **被动回复**（携带收到的 msg_id）：群聊 5 分钟内可回 **5 条**，
  C2C 60 分钟内可回 5 条。``msg_seq`` 与 ``msg_id`` 联合使用，
  相同 ``msg_id + msg_seq`` 重复发送会失败。
- **主动消息**（不携带 msg_id）：群聊/单聊每月仅 **4 条**，额度极紧张
  （官方已公告 2025-04-21 起主动推送能力下线）。

因此「想多说几句」的正确姿势是：**复用最近收到的 msg_id 作为被动回复链**。
本模块维护按会话的回复上下文与 msg_seq 计数器，实现这一机制。

这是完全在官方协议内的用法，不涉及任何非官方手段。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger("feiyu.replypool")


@dataclass
class ReplyContext:
    """一条可复用的被动回复上下文。"""

    target: str  # group / user
    openid: str
    msg_id: str
    created_at: float = field(default_factory=time.time)
    seq: int = 1  # 下一个可用的 msg_seq
    max_uses: int = 5  # 官方上限 5 条
    ttl: float = 300.0  # 群聊 5 分钟窗口

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl

    @property
    def exhausted(self) -> bool:
        return self.seq > self.max_uses

    def usable(self) -> bool:
        return bool(self.msg_id) and not self.expired and not self.exhausted

    def next_seq(self) -> int:
        """取下一个 msg_seq（从 1 开始递增）。"""
        value = self.seq
        self.seq += 1
        return value

    def remaining(self) -> int:
        return max(0, self.max_uses - (self.seq - 1))


class ReplyPool:
    """按会话维护的被动回复上下文池（LRU）。"""

    def __init__(self, cfg: dict[str, Any] | None = None):
        cfg = cfg or {}
        self.max_entries = int(cfg.get("max_entries", 200))
        self.max_uses = int(cfg.get("max_uses_per_msg", 5))
        # 群聊 300 秒；C2C 官方 3600 秒，这里按 target 分别处理
        self.ttl_group = float(cfg.get("context_ttl", 300))
        self.ttl_c2c = float(cfg.get("context_ttl_c2c", 3600))
        self._pool: OrderedDict[str, ReplyContext] = OrderedDict()

    @staticmethod
    def _key(target: str, openid: str) -> str:
        return f"{target}:{openid}"

    def _ttl_for(self, target: str) -> float:
        return self.ttl_c2c if target == "user" else self.ttl_group

    # ------------------------------------------------------------ 写入

    def add(self, target: str, openid: str, msg_id: str) -> ReplyContext:
        """收到消息时记入一个新的被动回复上下文。"""
        if not msg_id:
            raise ValueError("msg_id 不能为空")
        key = self._key(target, openid)
        ctx = ReplyContext(
            target=target,
            openid=openid,
            msg_id=msg_id,
            max_uses=self.max_uses,
            ttl=self._ttl_for(target),
        )
        self._pool[key] = ctx
        self._pool.move_to_end(key)
        self._evict()
        return ctx

    def _evict(self) -> None:
        while len(self._pool) > self.max_entries:
            self._pool.popitem(last=False)
        for key in [k for k, v in self._pool.items() if v.expired]:
            self._pool.pop(key, None)

    # ------------------------------------------------------------ 读取

    def get(self, target: str, openid: str) -> ReplyContext | None:
        key = self._key(target, openid)
        ctx = self._pool.get(key)
        if ctx is None:
            return None
        if ctx.expired:
            self._pool.pop(key, None)
            return None
        self._pool.move_to_end(key)
        return ctx

    def acquire(self, target: str, openid: str) -> tuple[str, int] | None:
        """取一个可用的 (msg_id, msg_seq) 并占用配额。

        返回 None 表示该会话没有可复用的被动回复上下文，
        此时只能发主动消息（消耗每月 4 条的极紧张配额）。
        """
        ctx = self.get(target, openid)
        if ctx is None or not ctx.usable():
            return None
        return ctx.msg_id, ctx.next_seq()

    def clear(self, target: str | None = None, openid: str | None = None) -> None:
        if target is None:
            self._pool.clear()
        else:
            self._pool.pop(self._key(target, openid or ""), None)

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict[str, Any]:
        usable = sum(1 for c in self._pool.values() if c.usable())
        return {
            "entries": len(self._pool),
            "usable": usable,
            "max_entries": self.max_entries,
            "max_uses_per_msg": self.max_uses,
            "ttl_group": self.ttl_group,
            "ttl_c2c": self.ttl_c2c,
        }
