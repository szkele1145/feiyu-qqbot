"""敏感字段加密存储。

机制：

- 首次运行生成随机主密钥，写入 ``data/secret.key``（仅当前用户可读）。
- 配置中的敏感字段在**保存时立即加密**为 ``ENC:<fernet密文>``，磁盘上不落明文。
- 读取时自动解密；密钥文件丢失则视为字段为空，需要在面板重新填写。
- 可选口令保护：设置 ``FEIYU_SECRET_PASSPHRASE`` 环境变量后，
  主密钥用口令二次派生（密钥文件被盗也无法解密）。

字段加密范围（``SECRET_FIELDS``）：

- qq.appid / qq.secret
- llm.api_key
- media.image_api_key
- webui.token
"""

from __future__ import annotations

import base64
import os
import secrets as pysecrets
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("feiyu.secrets")

ENC_PREFIX = "ENC:"
KEY_FILE_NAME = "secret.key"

# 需要加密存储的字段（点号路径）
SECRET_FIELDS = (
    "qq.secret",
    "llm.api_key",
    "media.image_api_key",
    "webui.token",
    # appid 算半敏感，也一并加密
    "qq.appid",
)

# 面板展示用的掩码
_SECRET_MASK = "******"


def _fernet():
    """惰性导入 cryptography，缺失时给出清晰提示。"""
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415

        return Fernet
    except ImportError as exc:
        raise RuntimeError(
            "缺少 cryptography 库，无法加密存储敏感字段。"
            "请运行: pip install cryptography"
        ) from exc


class SecretVault:
    """敏感字段加密保险箱。"""

    def __init__(self, key_dir: Path):
        self.key_dir = Path(key_dir)
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = None
        self._load_or_create_key()

    # ------------------------------------------------------------ 密钥

    def _key_path(self) -> Path:
        return self.key_dir / KEY_FILE_NAME

    def _load_or_create_key(self) -> None:
        Fernet = _fernet()
        path = self._key_path()
        if path.exists():
            # 文件里存的就是 urlsafe base64 编码的 32 字节主密钥
            key = path.read_bytes().strip()
        else:
            key = base64.urlsafe_b64encode(pysecrets.token_bytes(32))
            path.write_bytes(key)
            self._restrict(path)
            log.info("已生成新的主密钥: %s", path)

        passphrase = os.environ.get("FEIYU_SECRET_PASSPHRASE", "")
        if passphrase:
            # 口令二次派生：PBKDF2-HMAC-SHA256 -> 32 字节 -> urlsafe base64
            import hashlib  # noqa: PLC0415

            salt = b"feiyu-qqbot/v1"
            digest = hashlib.pbkdf2_hmac(
                "sha256", passphrase.encode("utf-8"), salt, 200_000, dklen=32
            )
            key = base64.urlsafe_b64encode(digest)
        self._fernet = Fernet(key)

    @staticmethod
    def _restrict(path: Path) -> None:
        """收紧文件权限（POSIX；Windows 依赖目录 ACL）。"""
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------ 加解密

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        if plaintext.startswith(ENC_PREFIX):
            return plaintext  # 已加密
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return ENC_PREFIX + token.decode("ascii")

    def decrypt(self, stored: str) -> str:
        if not stored:
            return ""
        if not stored.startswith(ENC_PREFIX):
            return stored  # 兼容历史明文
        try:
            return self._fernet.decrypt(stored[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            log.error("解密失败（密钥文件可能已更换）: %s", exc)
            return ""

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return bool(value) and value.startswith(ENC_PREFIX)

    @staticmethod
    def mask(value: str) -> str:
        """面板回显掩码。"""
        if not value:
            return ""
        if SecretVault.is_encrypted(value):
            return _SECRET_MASK
        # 明文也掩码，只露头尾
        if len(value) <= 8:
            return _SECRET_MASK
        return f"{value[:4]}****{value[-4:]}"

    # ------------------------------------------------------------ 配置整体处理

    def encrypt_config(self, cfg_data: dict) -> dict:
        """保存前调用：把敏感字段就地加密。"""
        for dotted in SECRET_FIELDS:
            node = _walk(cfg_data, dotted)
            if node is None:
                continue
            parent, key = node
            val = parent.get(key)
            if isinstance(val, str) and val and not self.is_encrypted(val):
                parent[key] = self.encrypt(val)
        return cfg_data

    def decrypt_config(self, cfg_data: dict) -> dict:
        """加载后调用：把敏感字段就地解密为内存中的明文。"""
        for dotted in SECRET_FIELDS:
            node = _walk(cfg_data, dotted)
            if node is None:
                continue
            parent, key = node
            val = parent.get(key)
            if isinstance(val, str) and val:
                parent[key] = self.decrypt(val)
        return cfg_data

    def masked_config(self, cfg_data: dict) -> dict:
        """面板展示：敏感字段全部掩码。"""
        out = _deepcopy(cfg_data)
        for dotted in SECRET_FIELDS:
            node = _walk(out, dotted)
            if node is None:
                continue
            parent, key = node
            val = parent.get(key)
            if isinstance(val, str) and val:
                parent[key] = self.mask(val)
        return out


def _walk(data: dict, dotted: str):
    """按点号路径定位字段，返回 (父容器, 最后一个键) 或 None。"""
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return None
    return node, parts[-1]


def _deepcopy(data):
    import copy  # noqa: PLC0415

    return copy.deepcopy(data)
