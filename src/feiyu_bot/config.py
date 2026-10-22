"""配置系统：YAML 持久化，支持默认值合并与敏感字段掩码。"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------- 默认配置

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "name": "feiyu-qqbot",
        "log_level": "INFO",
        "log_dir": "logs",
        "data_dir": "data",
        # 根目录：exe 模式下为 exe 所在目录，否则为当前工作目录
        "base_dir": "",
    },
    "runtime": {
        # ---- 谷时运行总开关 ----
        # 打开后，bot 只在低峰时段工作；非谷时段收到消息也不回复、不主动发言。
        "offpeak_only": False,
        # 低峰时段定义，格式 [[\"HH:MM\",\"HH:MM\"], ...]，支持跨午夜
        "offpeak_hours": [["12:00", "14:00"], ["18:00", "08:00"]],
        # 非谷时段的行为：
        #   silent = 静默丢弃（不回复，省钱省配额）
        #   notice = 回复一句"当前非服务时段"
        "offpeak_behavior": "silent",
        # 非谷时段是否仍允许主动发言（一般关）
        "offpeak_allow_proactive": False,
    },
    "context": {
        # 送入模型的历史消息条数上限
        "max_history": 20,
        # 估算后的上下文 token 上限，超了就压缩
        "max_tokens": 6000,
        # 保留最近 N 条原文不动，更早的做摘要
        "keep_recent": 8,
        # 触发摘要的阈值比例（相对 max_tokens）
        "summarize_ratio": 0.75,
        # 摘要后保留的压缩文本 token 上限
        "summary_max_tokens": 400,
        # 单张图片描述注入的字符上限（防止一张图吃满上下文）
        "max_image_desc_chars": 1200,
        # 单轮工具调用最大次数，防止死循环烧 token
        "max_tool_rounds": 4,
    },
    "qq": {
        "appid": "",
        "secret": "",
        # 沙箱环境：真机测试通过后再切 False
        "sandbox": True,
        "intents": [
            "GROUP_AND_C2C_EVENT",
            "PUBLIC_GUILD_MESSAGES",
            "GUILD_MESSAGES",
            "DIRECT_MESSAGE",
        ],
        # 群 / 用户白名单，空表示不限制
        "group_whitelist": [],
        "user_whitelist": [],
        "blacklist": [],
        # 是否允许主动发言（受限配额）
        "allow_proactive": True,
        # 主动发言时是否 @ 触发来源用户
        "proactive_at_source": True,
        # 主动发言最小间隔（秒），防止刷屏
        "proactive_min_interval": 300,
        # 被动回复链的最大消息条数
        "max_reply_chunks": 5,
    },
    "llm": {
        # 自定义 API 地址与 Key（兼容 OpenAI Chat Completions 协议）
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        # 识图用模型，留空则复用上面的 model
        "vision_model": "",
        "temperature": 0.9,
        "top_p": 0.95,
        "max_tokens": 2048,
        "timeout": 120,
        "max_history": 20,
        # 请求失败重试次数
        "max_retries": 3,
        "proxy": "",
        # ---- 省 token 策略 ----
        # 单日最大调用次数（0 = 不限）
        "daily_call_limit": 200,
        # 单次请求的 token 上限（硬顶，防止一次烧太多）
        "token_ceiling": 1024,
        # 只有在允许的时间窗内才调用模型。
        # 格式：[["12:00","14:00"],["18:00","08:00"]]，空列表 = 不限制
        "allowed_hours": [],
        # 窗口外不重试，直接返回"繁忙"降级
        "out_of_hours_fallback": "模型当前不在服务时段，稍后再问吧～",
    },
    "persona": {
        # 当前人格 id，对应 personas/<id>.yaml
        "current": "feiyu",
        "pack_dir": "personas",
        # 人格包里额外追加的指令
        "extra_instructions": "",
    },
    "vision": {
        "enabled": True,
        # 本地 OCR 兜底：auto / windows / paddle / rapid / none
        "ocr_engine": "auto",
        # 单张图片最大体积（MB），超过则先压缩
        "max_image_mb": 10,
        # 传给视觉模型的细节档位：auto / low / high
        "detail": "auto",
        # 识图提示词（可自定义）
        "prompt": (
            "请用中文详细描述这张图片的内容，包括主体、场景、可见文字、"
            "颜色、风格以及任何值得注意的细节。如果是一张梗图或表情包，"
            "请同时描述它的梗意。"
        ),
        # 是否同时附带本地 OCR 文本
        "use_local_ocr": True,
        # 下载图片超时（秒）
        "download_timeout": 30,
    },
    "media": {
        "voice_dir": "assets/voices",
        "image_dir": "assets/images",
        # 允许 Agent 自行联网搜索素材
        "allow_online_search": True,
        # 单条语音最大时长（秒）
        "max_voice_seconds": 60,
        # 语音转码为 QQ 所需格式时使用的 ffmpeg
        "ffmpeg": "ffmpeg",
        # 素材搜索使用的图片 API（可选，未配置则走公共源）
        "image_api": "",
        "image_api_key": "",
    },
    "webui": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8720,
        # 面板访问口令，留空则不校验
        "token": "",
        # 启动时自动打开浏览器
        "open_browser": True,
    },
    "panel": {
        # exe 内置控制台面板
        "enabled": True,
        # tray = 常驻托盘（Windows），console = 直接在终端交互
        "mode": "console",
    },
    "scheduler": {
        # 保活定时任务：15 分钟一次
        "keepalive_seconds": 900,
        # 掉线后自动重连（网关层本身也会重连，这里是进程级看门狗）
        "watchdog": True,
        # 看门狗检测到进程异常时的最大重启次数（-1 = 无限）
        "max_restarts": -1,
    },
}

# 需要在面板中掩码显示的字段
SECRET_KEYS = {"secret", "api_key", "image_api_key", "token"}

# 允许出现在配置/面板中的环境变量占位符。
# 项目内不得出现任何本机绝对路径，运行时路径一律由配置解析。
ENV_PLACEHOLDER = "${ENV}"

# ---------------------------------------------------------------- 中文化

# 配置字段的中文标签（面板展示用，键为点号路径）
FIELD_LABELS: dict[str, str] = {
    # 应用
    "app.name": "程序名称",
    "app.log_level": "日志级别",
    "app.log_dir": "日志目录",
    "app.data_dir": "数据目录",
    "app.base_dir": "根目录",
    # 运行时段
    "runtime.offpeak_only": "谷时运行（只在低峰时段工作）",
    "runtime.offpeak_hours": "低峰时段",
    "runtime.offpeak_behavior": "非谷时行为",
    "runtime.offpeak_allow_proactive": "非谷时允许主动发言",
    # 上下文
    "context.max_history": "历史消息条数上限",
    "context.max_tokens": "上下文 token 上限",
    "context.keep_recent": "保留最近原文条数",
    "context.summarize_ratio": "触发摘要阈值比例",
    "context.summary_max_tokens": "摘要 token 上限",
    "context.max_image_desc_chars": "图片描述字符上限",
    "context.max_tool_rounds": "单轮工具调用上限",
    # QQ
    "qq.appid": "QQ AppID",
    "qq.secret": "QQ AppSecret",
    "qq.sandbox": "沙箱环境（先测试再关闭）",
    "qq.intents": "订阅事件（intents）",
    "qq.group_whitelist": "群白名单",
    "qq.user_whitelist": "用户白名单",
    "qq.blacklist": "黑名单",
    "qq.allow_proactive": "允许主动发言",
    "qq.proactive_at_source": "主动发言时 @ 来源用户",
    "qq.proactive_min_interval": "主动发言最小间隔（秒）",
    "qq.max_reply_chunks": "单条回复最大分片数",
    # LLM
    "llm.base_url": "API 地址（base_url）",
    "llm.api_key": "API Key",
    "llm.model": "模型名称",
    "llm.vision_model": "识图模型（留空复用主模型）",
    "llm.temperature": "温度",
    "llm.top_p": "Top P",
    "llm.max_tokens": "单次最大 token",
    "llm.timeout": "请求超时（秒）",
    "llm.max_history": "历史条数",
    "llm.max_retries": "失败重试次数",
    "llm.proxy": "代理地址",
    "llm.daily_call_limit": "每日调用上限（0=不限）",
    "llm.token_ceiling": "单次 token 硬顶",
    "llm.allowed_hours": "允许调用时段",
    "llm.out_of_hours_fallback": "非服务时段话术",
    # 人格
    "persona.current": "当前人格",
    "persona.pack_dir": "人格包目录",
    "persona.extra_instructions": "附加指令",
    # 识图
    "vision.enabled": "启用识图",
    "vision.ocr_engine": "OCR 引擎",
    "vision.max_image_mb": "单图最大体积（MB）",
    "vision.detail": "细节档位",
    "vision.prompt": "识图提示词",
    "vision.use_local_ocr": "附带本地 OCR 文本",
    "vision.download_timeout": "图片下载超时（秒）",
    # 媒体
    "media.voice_dir": "语音库目录",
    "media.image_dir": "图片库目录",
    "media.allow_online_search": "允许联网搜素材",
    "media.max_voice_seconds": "单条语音最长（秒）",
    "media.ffmpeg": "ffmpeg 路径",
    "media.image_api": "图片搜索 API",
    "media.image_api_key": "图片搜索 API Key",
    # WebUI
    "webui.enabled": "启用 WebUI 面板",
    "webui.host": "监听地址",
    "webui.port": "监听端口",
    "webui.token": "访问口令",
    "webui.open_browser": "启动时打开浏览器",
    # 桌面面板
    "panel.enabled": "启用桌面面板",
    "panel.mode": "桌面面板模式",
    # 定时任务
    "scheduler.keepalive_seconds": "保活间隔（秒）",
    "scheduler.watchdog": "启用看门狗",
    "scheduler.max_restarts": "最大重启次数",
}

# 配置分节的中文标题
SECTION_TITLES: dict[str, str] = {
    "app": "应用",
    "runtime": "运行时段",
    "context": "上下文管理",
    "qq": "QQ 机器人",
    "llm": "AI 模型",
    "persona": "人格设置",
    "vision": "识图设置",
    "media": "媒体库",
    "webui": "WebUI 面板",
    "panel": "桌面面板",
    "scheduler": "定时任务",
}

# 枚举值的中文解释
VALUE_HINTS: dict[str, dict[str, str]] = {
    "runtime.offpeak_behavior": {
        "silent": "静默丢弃（推荐）",
        "notice": "回复一句提示",
    },
    "vision.ocr_engine": {
        "auto": "自动选择",
        "windows": "Windows 内置 OCR",
        "paddle": "PaddleOCR（需另装）",
        "rapid": "RapidOCR（需另装）",
        "none": "关闭 OCR",
    },
    "panel.mode": {
        "console": "终端控制台",
        "tray": "系统托盘",
    },
    "app.log_level": {
        "DEBUG": "调试",
        "INFO": "普通",
        "WARNING": "警告",
        "ERROR": "错误",
    },
}


def field_label(dotted: str) -> str:
    """取字段的中文标签（未登记则回退到键名）。"""
    return FIELD_LABELS.get(dotted, dotted.split(".")[-1])


def section_title(key: str) -> str:
    """取分节的中文标题。"""
    return SECTION_TITLES.get(key, key)


def value_hint(dotted: str, value: Any) -> str:
    """取枚举值的中文解释。"""
    return VALUE_HINTS.get(dotted, {}).get(str(value), "")


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典，override 优先。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def mask_secrets(obj: Any) -> Any:
    """递归把敏感字段替换成掩码，用于面板展示。"""
    if isinstance(obj, dict):
        return {
            k: ("***" if k in SECRET_KEYS and v else mask_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_secrets(v) for v in obj]
    return obj


class Config:
    """配置对象。属性访问 + 按节访问。"""

    def __init__(self, data: dict[str, Any], path: Path | None = None):
        self._data = data
        self._path = path

    # ---------------------------------------------------------- 加载 / 保存

    @classmethod
    def load(cls, path: str | os.PathLike, vault=None) -> "Config":
        p = Path(path).expanduser().resolve()
        raw: dict = {}
        if p.exists():
            text = p.read_text(encoding="utf-8")
            try:
                raw = yaml.safe_load(text) or {}
            except yaml.YAMLError as exc:
                raise ValueError(f"配置文件解析失败: {p}\n{exc}") from exc
        data = _deep_merge(DEFAULT_CONFIG, raw)

        # 未显式传 vault 时自动创建一个：
        # 否则读到的是 ENC:... 密文，下游把密文当密钥用会直接报错。
        if vault is None:
            try:
                from .secrets import SecretVault  # noqa: PLC0415

                data_dir = p.parent / "data"
                vault = SecretVault(data_dir)
            except Exception:  # noqa: BLE001
                vault = None
        if vault is not None:
            try:
                vault.decrypt_config(data)  # 磁盘密文 -> 内存明文
            except Exception:  # noqa: BLE001
                pass
        return cls(data, p)

    def save(self, path: str | os.PathLike | None = None, vault=None) -> Path:
        """保存配置。path 为空则保存回原路径。

        vault: SecretVault 实例。提供时敏感字段以密文落盘（填入明文保存后
        立即被加密覆盖，磁盘上不出现明文密钥）。
        """
        target = Path(path).expanduser().resolve() if path else self._path
        if target is None:
            raise ValueError("未指定配置文件保存路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = self._data
        if vault is not None:
            data = vault.encrypt_config(copy.deepcopy(self._data))
        target.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self._path = target
        return target

    @classmethod
    def default(cls) -> "Config":
        """纯默认配置（不读文件）。"""
        return cls(copy.deepcopy(DEFAULT_CONFIG), None)

    def masked(self, vault=None) -> dict[str, Any]:
        """面板展示：掩码敏感字段。"""
        import copy as _copy  # noqa: PLC0415

        data = _copy.deepcopy(self._data)
        if vault is not None:
            return vault.masked_config(data)
        return mask_secrets(data)

    # ---------------------------------------------------------- 访问器

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        """按 ``a.b.c`` 点号路径读取。"""
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        """按 ``a.b.c`` 点号路径写入，自动创建中间层。"""
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def expand_env(self) -> None:
        """把配置里的 ``${VAR}`` 占位符替换为环境变量值。

        这样密钥可以只存在于环境变量或本地 .env，仓库中不会出现真实凭据。
        """
        def _expand(node: Any) -> Any:
            if isinstance(node, dict):
                return {k: _expand(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_expand(v) for v in node]
            if isinstance(node, str) and "${" in node:
                return os.path.expandvars(node)
            return node

        self._data = _expand(self._data)

    def section(self, name: str) -> dict[str, Any]:
        return self._data.get(name, {})

    # ---------------------------------------------------------- 路径工具

    @property
    def base_dir(self) -> Path:
        """资源根目录：exe 模式取 exe 所在目录，否则取当前工作目录。"""
        configured = self.get("app.base_dir", "")
        if configured:
            return Path(configured).expanduser().resolve()
        return Path.cwd()

    def path(self, dotted: str) -> Path:
        """取一个配置项对应的绝对路径（相对 base_dir 解析）。"""
        value = str(self.get(dotted, ""))
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def log_dir(self) -> Path:
        return self.path("app.log_dir")

    @property
    def data_dir(self) -> Path:
        return self.path("app.data_dir")

    def ensure_dirs(self) -> None:
        """创建运行期需要的目录。"""
        for d in (self.log_dir, self.data_dir,
                  self.path("media.voice_dir"), self.path("media.image_dir")):
            d.mkdir(parents=True, exist_ok=True)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """加载配置；path 为空时按 ``config.yaml -> config.yml`` 顺序查找。"""
    if path:
        return Config.load(path)
    for candidate in ("config.yaml", "config.yml"):
        if Path(candidate).exists():
            return Config.load(candidate)
    return Config.load(Path("config.yaml"))
