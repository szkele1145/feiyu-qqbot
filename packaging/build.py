"""PyInstaller 打包配置。

用法::

    python packaging/build.py                 # 当前平台打包
    python packaging/build.py --onefile       # 单文件（启动慢）
    python packaging/build.py --clean

Windows 产物：dist/feiyu-qqbot.exe
Linux 产物：dist/feiyu-qqbot
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ENTRY = ROOT / "packaging" / "entry.py"


def _hidden_imports() -> list[str]:
    """动态导入的模块，需要显式声明。"""
    return [
        "feiyu_bot",
        "feiyu_bot.agent.core",
        "feiyu_bot.agent.context",
        "feiyu_bot.panel.gui",
        "feiyu_bot.panel.console",
        "feiyu_bot.config",
        "feiyu_bot.llm.client",
        "feiyu_bot.llm.budget",
        "feiyu_bot.logging_setup",
        "feiyu_bot.media.library",
        "feiyu_bot.personas.manager",
        "feiyu_bot.qq.api",
        "feiyu_bot.qq.constants",
        "feiyu_bot.qq.gateway",
        "feiyu_bot.qq.parser",
        "feiyu_bot.qq.reply_pool",
        "feiyu_bot.qq.token_manager",
        "feiyu_bot.runtime_guard",
        "feiyu_bot.secrets",
        "feiyu_bot.vision.pipeline",
        "feiyu_bot.webui.server",
        "feiyu_bot.panel.console",
        "yaml",
        "httpx",
        "aiohttp",
        "websockets",
        "PIL.Image",
        "PIL.ImageDraw",
        "cryptography.fernet",
        "cryptography.hazmat.primitives",
    ]


def _datas() -> list[tuple[str, str]]:
    """需要打包的数据文件：人格包、静态资源。"""
    out: list[tuple[str, str]] = []
    packs = SRC / "feiyu_bot" / "personas" / "packs"
    if packs.exists():
        out.append((str(packs), "feiyu_bot/personas/packs"))
    static = SRC / "feiyu_bot" / "webui" / "static"
    if static.exists():
        out.append((str(static), "feiyu_bot/webui/static"))
    return out


def build(onefile: bool = False, clean: bool = False, name: str = "feiyu-qqbot") -> int:
    if clean and (ROOT / "build").exists():
        shutil.rmtree(ROOT / "build", ignore_errors=True)
    if clean and (ROOT / "dist").exists():
        shutil.rmtree(ROOT / "dist", ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        str(ENTRY),
        "--name", name,
        "--noconfirm",
        "--clean",
        "--log-level", "WARN",
        "--paths", str(SRC),
        "--hidden-import", "feiyu_bot.personas.manager",
    ]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    for mod in _hidden_imports():
        cmd += ["--hidden-import", mod]
    for src, dest in _datas():
        cmd += ["--add-data", f"{src}{os.pathsep}{dest}"]

    # 减小体积（tkinter 是 GUI 面板依赖，不能排除）
    cmd += [
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
        "--exclude-module", "IPython",
        "--exclude-module", "notebook",
    ]

    # 保留控制台窗口：启动后在这里显示运行日志（用户要求命令行形态）
    cmd.append("--console")

    # 应用图标
    icon = ROOT / "packaging" / "app.ico"
    if icon.exists():
        cmd += ["--icon", str(icon)]
        cmd += ["--add-data", f"{icon}{os.pathsep}."]

    print("执行:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        print("\n打包完成，产物在 dist/ 目录")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="feiyu-qqbot 打包")
    parser.add_argument("--onefile", action="store_true", help="单文件模式")
    parser.add_argument("--clean", action="store_true", help="清理后重新构建")
    parser.add_argument("--name", default="feiyu-qqbot", help="产物名")
    args = parser.parse_args()
    return build(args.onefile, args.clean, args.name)


if __name__ == "__main__":
    raise SystemExit(main())
