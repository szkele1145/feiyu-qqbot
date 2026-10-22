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
    else:
        root = Path(__file__).resolve().parents[1] / "src"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))


_bootstrap()

from feiyu_bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
