"""PyInstaller 入口。

打包后：
- 双击 exe 即可运行（自动在 exe 同目录创建配置、数据、日志）
- 可用命令行参数：--config / --init / --webui-only 等
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap() -> None:
    """运行时定位资源目录（兼容 PyInstaller 与源码运行）。"""
    if getattr(sys, "frozen", False):
        # exe 模式：资源在 _MEIPASS，工作目录切到 exe 所在目录
        base = Path(sys.executable).resolve().parent
        os.chdir(base)
        sys.path.insert(0, getattr(sys, "_MEIPASS", str(base)))
        _ensure_streams(base)
    else:
        root = Path(__file__).resolve().parents[1] / "src"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))


def _ensure_streams(base: Path) -> None:
    """windowed 模式没有控制台，sys.stdout/stderr 为 None。

    直接打印会抛 AttributeError，这里重定向到 logs/stdout.log，
    顺便把崩溃信息留下来方便排查。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = base / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        if sys.stdout is None:
            sys.stdout = open(log_dir / "stdout.log", "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(log_dir / "stderr.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        # 实在打不开就换成空设备，保证不因 print 崩溃
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull


_bootstrap()

from feiyu_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
