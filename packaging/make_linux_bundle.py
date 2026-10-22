"""Linux 发布包：源码 + 一键安装脚本，打成 tar.gz。

用户要求的形态是「Linux 版本一个压缩包，内置一个 .sh 自动安装」，
所以这里不复用 PyInstaller（跨平台交叉编译不现实），改为源码分发：

    feiyu-qqbot-linux-x86_64/
        src/feiyu_bot/          程序源码
        requirements.txt        运行依赖
        install.sh              一键安装（压缩包根目录，已加可执行位）
        start.sh                启动脚本
        run-webui.sh            只起 WebUI（可选）
        README.md
        LICENSE

用法::

    python packaging/make_linux_bundle.py
    python packaging/make_linux_bundle.py --out dist
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "feiyu-qqbot-linux-x86_64"

#: 打进压缩包的顶层文件（相对项目根）
INCLUDE_FILES = [
    "requirements.txt",
    "README.md",
    "install.sh",
    "LICENSE",
]

#: 打进压缩包的目录
INCLUDE_DIRS = [
    "src/feiyu_bot",
]


def _chmod_executable(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """以 0755 权限写入一个可执行文件。"""
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = 0o755
    info.mtime = int(__import__("time").time())
    tar.addfile(info, io.BytesIO(data))


def _add_file(tar: tarfile.TarFile, src: Path, arcname: str, executable: bool = False) -> None:
    data = src.read_bytes()
    if executable or os.access(src, os.X_OK):
        _chmod_executable(tar, arcname, data)
        return
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = int(src.stat().st_mtime)
    tar.addfile(info, io.BytesIO(data))


def _add_dir(tar: tarfile.TarFile, src: Path, prefix: str) -> int:
    """递归加入目录，跳过 __pycache__。返回加入的文件数。"""
    count = 0
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        rel = path.relative_to(src)
        arcname = f"{prefix}/{rel.as_posix()}"
        _add_file(tar, path, arcname)
        count += 1
    return count


START_SH = """#!/usr/bin/env bash
# feiyu-qqbot 启动脚本（Linux / macOS）
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/feiyu-qqbot}"
if [ -x "$INSTALL_DIR/run.sh" ]; then
  exec "$INSTALL_DIR/run.sh" "$@"
fi
# 未安装时直接就地跑
export PYTHONPATH="$APP_DIR/src:${PYTHONPATH:-}"
exec python3 -m feiyu_bot "$@"
"""

RUN_WEBUI_SH = """#!/usr/bin/env bash
# 只启动 WebUI 面板（共享面板用）
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/feiyu-qqbot}"
if [ -x "$INSTALL_DIR/run.sh" ]; then
  exec "$INSTALL_DIR/run.sh" --webui-only "$@"
fi
export PYTHONPATH="$APP_DIR/src:${PYTHONPATH:-}"
exec python3 -m feiyu_bot --webui-only "$@"
"""

BUNDLE_README = """# feiyu-qqbot Linux 包

这是 `feiyu-qqbot`（QQ 开放平台官方机器人 AI Agent）的 Linux 发行包。
压缩包根目录已经带好一键安装脚本。

## 一键安装

```bash
tar -xzf feiyu-qqbot-linux-x86_64.tar.gz
cd feiyu-qqbot-linux-x86_64
bash install.sh
```

安装脚本会：

1. 检测包管理器并安装系统依赖（python3 / venv / ffmpeg）
2. 在 `~/.local/share/feiyu-qqbot/` 建虚拟环境并安装 Python 依赖
3. 把程序复制过去，生成 `run.sh` 启动脚本
4. 首次运行时生成配置模板

装完编辑配置：

```bash
nano ~/.local/share/feiyu-qqbot/data/config.yaml
```

填写 QQ 机器人的 `appid` / `secret` 和 LLM 的 `api_key`，然后启动：

```bash
~/.local/share/feiyu-qqbot/run.sh
```

## 装成开机自启服务

```bash
bash install.sh --systemd
systemctl --user start feiyu-qqbot
journalctl --user -u feiyu-qqbot -f
```

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--systemd` | 同时注册 systemd 用户服务 |
| `--no-venv` | 直接用系统 Python，不建虚拟环境 |

## 面板

- 桌面面板：Windows / macOS 上启动 exe 或 `python -m feiyu_bot` 会弹出图形面板
- 网页面板：默认 `http://127.0.0.1:8720`（可在设置里关闭或改端口）

## 目录

```
feiyu-qqbot-linux-x86_64/
├── install.sh          一键安装脚本
├── start.sh            启动脚本
├── run-webui.sh        只起 WebUI
├── requirements.txt
├── src/feiyu_bot/      程序源码
└── README.md
```
"""


def build(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{NAME}.tar.gz"

    # 打包前检查必需文件都在
    for rel in INCLUDE_FILES:
        src = ROOT / rel
        if not src.exists() and rel != "LICENSE":
            print(f"[警告] 缺少 {rel}，跳过")
    for rel in INCLUDE_DIRS:
        if not (ROOT / rel).is_dir():
            raise SystemExit(f"[错误] 缺少目录 {rel}")

    with tarfile.open(target, "w:gz") as tar:
        total = 0
        for rel in INCLUDE_FILES:
            src = ROOT / rel
            if not src.exists():
                continue
            _add_file(tar, src, f"{NAME}/{rel}", executable=(rel == "install.sh"))
            total += 1

        for rel in INCLUDE_DIRS:
            total += _add_dir(tar, ROOT / rel, f"{NAME}/{rel}")

        # 内置启动脚本（动态生成，权限 0755）
        scripts = {
            "start.sh": START_SH,
            "run-webui.sh": RUN_WEBUI_SH,
        }
        for fname, content in scripts.items():
            _chmod_executable(tar, f"{NAME}/{fname}", content.encode("utf-8"))
            total += 1

        # 包内说明
        _add_file_bytes = BUNDLE_README.encode("utf-8")
        info = tarfile.TarInfo(f"{NAME}/README-BUNDLE.md")
        info.size = len(_add_file_bytes)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(_add_file_bytes))
        total += 1

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"[完成] {target}  ({size_mb:.2f} MB, {total} 个文件)")
    print("       解压后执行: bash install.sh")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 Linux 发行包")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="输出目录")
    args = parser.parse_args(argv)
    build(Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
