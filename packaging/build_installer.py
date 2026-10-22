"""一键构建安装程序（Windows）。

流程::

    1. PyInstaller 打包 onedir
    2. 用 Inno Setup（ISCC）编译安装程序

用法::

    python packaging/build_installer.py            # 完整流程
    python packaging/build_installer.py --skip-exe # 只做 Inno Setup
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_iscc() -> str | None:
    """定位 Inno Setup 编译器。"""
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        str(Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe"),
        str(Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 5" / "ISCC.exe"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return shutil.which("ISCC")


def build_exe() -> bool:
    # 用 importlib 显式加载，避免与标准库 packaging 撞名
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "feiyu_build", ROOT / "packaging" / "build.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.build(onefile=False, clean=True)
    return rc == 0


def build_installer() -> bool:
    iscc = find_iscc()
    if not iscc:
        print("未找到 Inno Setup（ISCC.exe）。")
        print("请安装：https://jrsoftware.org/isdl.php")
        print("  winget install JRSoftware.InnoSetup")
        return False
    out_dir = ROOT / "dist" / "installer"
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run([iscc, str(ROOT / "packaging" / "installer.iss")]).returncode
    if rc == 0:
        print("\n安装程序已生成:")
        for f in sorted(out_dir.glob("*.exe")):
            print(f"  {f}  ({f.stat().st_size / 1024 / 1024:.1f} MB)")
    return rc == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-exe", action="store_true", help="跳过 PyInstaller")
    args = parser.parse_args()

    if not args.skip_exe:
        if not build_exe():
            print("PyInstaller 打包失败", file=sys.stderr)
            return 1
    ok = build_installer()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
