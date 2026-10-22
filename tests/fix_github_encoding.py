"""检查 + 修复 GitHub 上仓库描述 / Release 正文的中文乱码。

乱码根因：PowerShell 5.1 的 Invoke-RestMethod 发送字符串 body 时按
ISO-8859-1 编码，中文变成 ????。本脚本全程用 requests + UTF-8 字节。
"""

import json
import sys
import urllib.request
from pathlib import Path

TOKEN = Path(
    r"C:\Users\一只屑\.dsh\attachments\v1\files\25"
    r"\25fad3265a510d082ee581b66a2311eacc120fce8c18b8a215c25fe8f5789972\.ghtoken"
).read_text(encoding="utf-8").strip()

API = "https://api.github.com"
REPO = "szkele1145/feiyu-qqbot"
PROXY = "http://127.0.0.1:65532"

opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
)


def call(method: str, path: str, body: dict | None = None, raw: bytes | None = None):
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    )
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "feiyu-fix",
        },
    )
    with opener.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def has_mojibake(text: str) -> bool:
    """检测典型乱码特征。"""
    if not text:
        return False
    bad = ["????", "â€", "ä¸", "å¤", "çš", "ç‰ˆ", "锟斤", "烫烫"]
    return any(b in text for b in bad) or (
        len(text) > 20 and text.count("?") > 5
    )


def main() -> int:
    # ---- 仓库描述 ----
    repo = call("GET", f"/repos/{REPO}")
    print(f"当前描述: {repo['description']!r}")
    if has_mojibake(repo["description"] or ""):
        new = "QQ 开放平台官方机器人 AI Agent（WebSocket 接入）—— 大肥鱼人格 / 纯本地识图 / 双面板 / 加密存储"
        call("PATCH", f"/repos/{REPO}", raw=json.dumps({"description": new}, ensure_ascii=False).encode("utf-8"))
        print(f"已修复描述 -> {new!r}")
    else:
        print("描述正常")

    # ---- Release 正文 ----
    rels = call("GET", f"/repos/{REPO}/releases")
    for rel in rels:
        body = rel.get("body") or ""
        name = rel.get("name") or ""
        print(f"\nRelease {rel['tag_name']}: name={name!r} body前60={body[:60]!r}")
        if has_mojibake(body) or has_mojibake(name):
            new_body = """## 大肥鱼 QQ 机器人 v0.1.0

QQ 开放平台官方机器人 AI Agent（WebSocket 主动接入）。

### 下载
| 平台 | 文件 | 说明 |
|---|---|---|
| Windows | `feiyu-qqbot-setup-0.1.0.exe` | 安装程序（双击即装，桌面 GUI + WebUI） |
| Linux | `feiyu-qqbot-linux-x86_64.tar.gz` | 解压后 `bash install.sh` 一键安装 |
| Android | `feiyu-qqbot-android-termux.tar.gz` | Termux 一键装 |

### 核心特性
- 官方协议：WS 网关 + 心跳 + 断线重连 + 会话恢复
- 被动回复池：复用 msg_id+msg_seq，群聊 5 分钟 5 条窗口内多轮回复
- 桌面 GUI：双击 exe 弹窗，启动页含 WebUI 开关、谷时运行开关
- 纯本地识图：OCR + 结构分析 → 文字画像注入纯语言模型
- 4 套人格：大肥鱼（傲娇）/ 温和深海 / 暴躁老哥 / 鱼群模式
- 双面板全中文：桌面窗口 + WebUI（NapCat 风格动效）
- 敏感字段 Fernet 加密落盘，谷时运行，预算熔断，15 分钟保活

### 快速开始
1. 到 QQ 开放平台 (https://q.qq.com) 申请机器人，拿 AppID / AppSecret
2. Windows：安装后运行，GUI 里填入配置（保存即加密）
3. Linux：`tar xzf feiyu-qqbot-linux-*.tar.gz && bash install.sh --systemd`
4. WebUI: http://127.0.0.1:8720

> 沙箱环境默认开启，测试通过后在设置里关闭。"""
            call("PATCH", f"/repos/{REPO}/releases/{rel['id']}",
                 raw=json.dumps({"name": "feiyu-qqbot v0.1.0", "body": new_body},
                                ensure_ascii=False).encode("utf-8"))
            print(f"已修复 Release {rel['tag_name']} 正文")
        else:
            print("正文正常")

    # ---- 验证 ----
    repo2 = call("GET", f"/repos/{REPO}")
    print(f"\n验证描述: {repo2['description']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
