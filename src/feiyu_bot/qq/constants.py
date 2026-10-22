"""QQ 官方机器人协议常量。

来源（经查证）:
- https://bot.qq.com/wiki/develop/api-v2/
- 官方 SDK 与开源实现的一致行为（intents、file_type、op 均为官方定义）
"""

from __future__ import annotations

# ---------------------------------------------------------------- 网关

# WebSocket opcode
OP_DISPATCH = 0  # 服务端推送事件
OP_HEARTBEAT = 1  # 客户端发送心跳
OP_IDENTIFY = 2  # 客户端鉴权
OP_RESUME = 6  # 客户端恢复连接
OP_RECONNECT = 7  # 服务端要求重连（换地址）
OP_INVALID_SESSION = 9  # 无效会话
OP_HELLO = 10  # 服务端问候（带心跳间隔）
OP_HEARTBEAT_ACK = 11  # 心跳回应
OP_HTTP_CALLBACK_ACK = 12

# intents 位（官方定义）
INTENTS = {
    "GUILDS": 1 << 0,
    "GUILD_MEMBERS": 1 << 1,
    "GUILD_MESSAGES": 1 << 9,  # 私域频道消息
    "GUILD_MESSAGE_REACTIONS": 1 << 10,
    "DIRECT_MESSAGE": 1 << 12,  # 频道私信
    "INTERACTION": 1 << 26,
    "MESSAGE_AUDIT": 1 << 27,
    "FORUMS_EVENT": 1 << 28,
    "AUDIO_ACTION": 1 << 29,
    "PUBLIC_GUILD_MESSAGES": 1 << 30,  # 公域频道消息
    "GROUP_AND_C2C_EVENT": 1 << 25,  # 群聊 + C2C 单聊
}

# 事件名 -> 会话类型
EVENT_KIND = {
    "GROUP_AT_MESSAGE_CREATE": "group",
    "GROUP_MESSAGE_CREATE": "group_all",  # 全量模式（需权限）
    "C2C_MESSAGE_CREATE": "c2c",
    "DIRECT_MESSAGE_CREATE": "guild_dm",
    "AT_MESSAGE_CREATE": "guild_public",
    "MESSAGE_CREATE": "guild_private",
    "MESSAGE_AUDIT_PASS": "audit",
    "MESSAGE_AUDIT_REJECT": "audit",
}

# 网关连接生命周期事件（非聊天消息）
GATEWAY_READY = "READY"
GATEWAY_RESUMED = "RESUMED"

# 可 resume 的 ws close code
RESUMABLE_CLOSE_CODES = {4008, 4009}

# ---------------------------------------------------------------- REST

API_BASE_PROD = "https://api.sgroup.qq.com"
API_BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"

# 获取 access_token（官方新接口）
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# 获取 WS 网关地址
GATEWAY_PATH = "/gateway"

# ---------------------------------------------------------------- 消息

# msg_type 取值（官方）
MSG_TEXT = 0
MSG_MARKDOWN = 2
MSG_ARK = 3
MSG_EMBED = 4
MSG_MEDIA = 7  # 富媒体（图片 / 语音 / 视频 / 文件）
MSG_KEYBOARD = 8  # markdown + button keyboard

# 富媒体 file_type（官方）：1 图片 2 视频 3 语音 4 文件
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_VOICE = 3
MEDIA_FILE = 4

FILE_TYPE_BY_EXT = {
    ".png": 1, ".jpg": 1, ".jpeg": 1, ".gif": 1, ".webp": 1, ".bmp": 1,
    ".mp4": 2, ".mov": 2, ".avi": 2, ".mkv": 2,
    ".mp3": 3, ".wav": 3, ".ogg": 3, ".m4a": 3, ".amr": 3, ".silk": 3, ".aac": 3, ".flac": 3,
    ".zip": 4, ".rar": 4, ".7z": 4, ".pdf": 4, ".txt": 4, ".doc": 4, ".docx": 4,
}

# 被动回复窗口（官方）：
#   群聊 5 分钟内可回 5 条；C2C 60 分钟内可回 5 条
PASSIVE_REPLY_WINDOW_GROUP = 300
PASSIVE_REPLY_WINDOW_C2C = 3600
PASSIVE_REPLY_MAX = 5

# 主动消息配额（官方）：群/单聊每月 4 条
ACTIVE_MONTHLY_LIMIT = 4

# 常见错误码
ERR_ACTIVE_LIMIT = "22003"
ERR_MSG_LIMIT = "22009"  # 消息发送超频
ERR_NOT_IN_WHITELIST = "22009"
ERR_TOKEN_EXPIRED = "40001"  # token 失效（统一鉴权错误）
ERR_MEDIA_UPLOAD_FAIL = "304082"
ERR_MEDIA_CONVERT_FAIL = "304083"


def compute_intents(names: list[str]) -> int:
    """把 intent 名称列表合成位值。默认包含群聊+C2C。"""
    value = 0
    for name in names or ["GROUP_AND_C2C_EVENT"]:
        value |= INTENTS.get(str(name).upper().strip(), 0)
    return value


def infer_file_type(filename: str) -> int:
    """按文件扩展名推断富媒体 file_type，默认按文件处理。"""
    from pathlib import Path  # noqa: PLC0415

    return FILE_TYPE_BY_EXT.get(Path(str(filename)).suffix.lower(), MEDIA_FILE)
