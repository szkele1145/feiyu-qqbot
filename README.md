# feiyu-qqbot

QQ 开放平台**官方机器人** AI Agent（WebSocket 主动接入），一条会傲娇的「大肥鱼」。

## 特性

- **官方协议接入**：QQ 开放平台 API v2，WebSocket 网关 + 心跳 + 断线重连 + 会话恢复
- **被动回复链**：自动复用 `msg_id + msg_seq`，在官方「群聊 5 分钟 5 条」窗口内多轮回复；主动消息月配额（4 条）仅作兜底
- **纯本地识图**：OCR（多引擎）+ 像素结构分析 → 文字画像注入纯语言模型（DeepSeek 等），**不依赖第三方视觉模型**；可选挂本地 VLM
- **语音库 / 图片库**：本地导入 + Agent 联网搜索入库，工具调用自动发图发语音
- **多套人格**：内置「大肥鱼（傲娇）」「温和深海」「暴躁老哥」「鱼群模式」，YAML 自定义热切换
- **自定义 LLM**：任意 OpenAI 兼容 API（base_url + api_key + model 自由配置）
- **双面板**：exe 内置终端控制台 + 浏览器 WebUI（配置全量可改、人格切换、媒体库管理、识图试跑）
- **敏感字段加密**：appid / secret / api_key 保存即 Fernet 加密落盘，磁盘零明文
- **谷时运行**：总开关控制只在低峰时段工作（如 12:00-14:00、18:00-08:00），非谷时静默
- **预算熔断**：日调用上限 + 单次 token 上限，防烧钱
- **上下文防爆**：滑窗 + 摘要压缩 + 图片描述限额，永不超长
- **15 分钟保活**：定时健康检查，掉线自动重连，token 自动刷新
- **多平台**：Windows 安装程序 / Linux（systemd） / Android（Termux）

## 快速开始

### Windows

1. 下载 `feiyu-qqbot-setup-x.x.x.exe` 安装
2. 编辑安装目录的 `config.yaml`（或设置环境变量 `QQ_APPID` / `QQ_SECRET` / `LLM_API_KEY`）
3. 双击运行，打开 <http://127.0.0.1:8720>

### Linux

```bash
bash install.sh --systemd    # 安装并注册 systemd 用户服务
~/.local/share/feiyu-qqbot/run.sh
```

### Android (Termux)

```bash
pkg install python ffmpeg git
git clone https://github.com/USER/feiyu-qqbot.git
cd feiyu-qqbot && bash termux-install.sh
~/feiyu-qqbot/run.sh
```

## 配置

`config.yaml` 关键字段：

```yaml
qq:
  appid: "你的AppID"        # 加密落盘
  secret: "你的AppSecret"   # 加密落盘
  sandbox: true             # 先沙箱测试，再切 false
  intents:
    - GROUP_AND_C2C_EVENT   # 群聊 + 单聊
    - PUBLIC_GUILD_MESSAGES # 公域频道
    - DIRECT_MESSAGE        # 频道私信

llm:
  base_url: "https://api.deepseek.com/v1"  # 任意 OpenAI 兼容地址
  api_key: "sk-..."          # 加密落盘
  model: "deepseek-chat"
  daily_call_limit: 200      # 每日调用上限
  token_ceiling: 1024        # 单次 token 硬顶
  allowed_hours: [["12:00","14:00"],["18:00","08:00"]]  # 空=不限

runtime:
  offpeak_only: false        # 谷时运行总开关
  offpeak_hours: [["12:00","14:00"],["18:00","08:00"]]

vision:
  enabled: true              # 识图开关
  ocr_engine: auto           # auto/windows/paddle/rapid/none
  use_local_ocr: true

persona:
  current: feiyu             # feiyu / deepsea / laoge / fishschool
```

## 主动消息说明（重要）

QQ 官方 2025-04 起已下线主动推送能力，当前规则：

| 场景 | 被动回复 | 主动消息 |
|---|---|---|
| 群聊 | 收到消息 5 分钟内可回 **5 条** | 每群每月 **4 条** |
| 单聊 | 60 分钟内可回 **5 条** | 每人每月 **4 条** |

本项目的策略：**优先走被动回复链**（复用最近的 `msg_id`，通过 `msg_seq` 递增在窗口内发多条），仅当回复池为空时才消耗主动消息配额。这是完全在官方协议内的做法。

机器人要在群里「主动说话」，前提是最近 5 分钟内有人在群里说过话（有可复用的 msg_id）。

## 识图原理

不依赖第三方视觉模型，三步走：

1. **OCR 层**：Windows 内置 OCR / 本地 ONNX 模型（可选 paddle/rapid），提取图中文字
2. **结构层**：numpy 像素统计——主色调、亮度、对比度、边缘密度、分区、人脸数
3. **组装层**：生成「文字画像」注入 system 上下文，纯语言模型据此理解图片

可选增强：配置 `vision.vlm` 指向本地 VLM（Ollama / LM Studio 的 OpenAI 兼容端点）。

## 面板

- **WebUI**：`http://127.0.0.1:8720`（总览 / 配置 / 人格 / 媒体库 / 识图试跑）
- **终端面板**：`status` / `personas` / `persona <id>` / `media` / `llm` / `keepalive` / `help`

## 开发

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Windows
# .venv/bin/pip install -r requirements-dev.txt     # Linux
.venv/Scripts/python -m pytest tests/ -v

# 打包
python packaging/build.py            # PyInstaller
python packaging/build_installer.py  # + Inno Setup 安装程序
```

## 许可

MIT
