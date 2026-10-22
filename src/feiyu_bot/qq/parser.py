"""QQ 事件解析：把 Gateway 事件转成统一的内部消息对象。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from .constants import EVENT_KIND

log = get_logger("feiyu.qq.parser")

# content 中的模板标签
_TAG_RE = re.compile(r"<@!\d+>|<@\d+>|<#\d+>|<emoji:\d+>|</?[^>]+>")


@dataclass
class Attachment:
    """消息附件。"""

    kind: str  # image / file / audio / video
    name: str = ""
    url: str = ""
    file_openid: str = ""
    size: int = 0
    width: int = 0
    height: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedMessage:
    """统一后的入站消息。"""

    kind: str  # group / c2c / guild_dm / guild_public
    msg_id: str = ""
    msg_seq: int = 1
    target: str = ""  # group / user（发消息用）
    openid: str = ""  # 群 openid 或用户 openid
    channel_id: str = ""
    guild_id: str = ""
    user_openid: str = ""
    user_id: str = ""
    username: str = ""
    content: str = ""
    raw_content: str = ""
    timestamp: int = 0
    attachments: list[Attachment] = field(default_factory=list)
    mentions_me: bool = False
    mentioned_openids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """会话标识，用于上下文隔离。"""
        if self.kind == "group":
            return f"group:{self.openid}"
        if self.kind == "c2c":
            return f"c2c:{self.user_openid or self.openid}"
        if self.kind == "guild_dm":
            return f"gdm:{self.channel_id or self.guild_id}"
        return f"guild:{self.guild_id}:{self.channel_id}"

    @property
    def reply_target(self) -> str:
        """发消息时的目标 id。"""
        if self.kind == "group":
            return self.openid
        return self.user_openid or self.openid


def strip_tags(text: str) -> str:
    """去掉 content 里的 QQ 模板标签。"""
    return _TAG_RE.sub("", text).strip()


def parse_event(event_name: str, d: dict[str, Any]) -> ParsedMessage | None:
    """解析事件负载为 ParsedMessage。非消息事件返回 None。"""
    kind = EVENT_KIND.get(event_name, "")
    if not kind or kind == "audit":
        return None

    author = d.get("author") or {}
    user_openid = str(author.get("user_openid", "") or author.get("id", "") or "")
    raw_content = str(d.get("content", "") or "")
    content = strip_tags(raw_content)

    # @ 列表：优先用 mentions 字段
    mentioned: list[str] = []
    mentions = d.get("mentions") or []
    for m in mentions:
        if isinstance(m, dict):
            mid = str(m.get("id", "") or m.get("user_openid", "") or "")
            if mid:
                mentioned.append(mid)

    msg = ParsedMessage(
        kind=kind,
        msg_id=str(d.get("id", "") or d.get("msg_id", "") or ""),
        msg_seq=int(d.get("msg_seq", 1) or 1),
        channel_id=str(d.get("channel_id", "") or ""),
        guild_id=str(d.get("guild_id", "") or ""),
        user_openid=user_openid,
        user_id=str(author.get("id", "") or ""),
        username=str(author.get("username", "") or ""),
        content=content,
        raw_content=raw_content,
        timestamp=int(d.get("timestamp", 0) or 0),
        mentioned_openids=mentioned,
        raw=d,
    )

    if kind == "group":
        msg.target = "group"
        msg.openid = str(d.get("group_openid", "") or d.get("group_id", "") or "")
    elif kind == "c2c":
        msg.target = "user"
        msg.openid = user_openid
    elif kind == "guild_dm":
        msg.target = "user"
        msg.openid = user_openid
    else:  # guild_public
        msg.target = "guild"
        msg.openid = str(d.get("channel_id", "") or "")

    # 是否 @ 了机器人
    bot_id = str(d.get("tts") or "")  # 占位，真实判断由调用方传入
    msg.mentions_me = bool(mentioned) or bool(
        d.get("mention") or d.get("mention_all")
    ) or ("<@!bot>" in raw_content.lower())
    del bot_id

    # 附件
    for att in d.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        content_type = str(att.get("content_type", "") or "")
        kind_ = "file"
        if content_type.startswith("image/"):
            kind_ = "image"
        elif content_type.startswith("audio/"):
            kind_ = "audio"
        elif content_type.startswith("video/"):
            kind_ = "video"
        msg.attachments.append(
            Attachment(
                kind=kind_,
                name=str(att.get("name", "") or ""),
                url=str(att.get("url", "") or att.get("url", "") or ""),
                file_openid=str(att.get("file_openid", "") or ""),
                size=int(att.get("size", 0) or 0),
                width=int(att.get("width", 0) or 0),
                height=int(att.get("height", 0) or 0),
                raw=att,
            )
        )

    # 富媒体消息里图片在 media 字段
    media = d.get("media") or {}
    if isinstance(media, dict):
        for item in media.get("images") or []:
            msg.attachments.append(
                Attachment(
                    kind="image",
                    url=str(item.get("url", "") or ""),
                    file_openid=str(item.get("file_openid", "") or ""),
                    width=int(item.get("width", 0) or 0),
                    height=int(item.get("height", 0) or 0),
                    raw=item,
                )
            )

    return msg
