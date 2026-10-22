"""feiyu-qqbot —— 基于 QQ 开放平台官方机器人（WebSocket 主动接入）的 AI Agent。

模块划分::

    config          配置读写（YAML）
    logging_setup   日志
    llm             OpenAI 兼容的大模型客户端（可自定义 base_url / api_key）
    qq              QQ 官方机器人：网关（WS）、REST API、媒体上传、被动回复池
    vision          识图：多模态模型 + 本地 OCR 兜底
    media           语音库 / 图片库：本地导入 + 联网搜索
    personas        人格提示词包（含「大肥鱼」等多套风格）
    agent           Agent 主循环：记忆、工具调用、@ 人、主动发言
    scheduler       定时任务（15 分钟保活等）
    webui           浏览器面板
    panel           exe 内置控制台面板
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
