"""生成 Android 应用图标（mipmap 各密度）。

复用 icogen 的渲染函数，输出 PNG 到 res/mipmap-{dpi}/ic_launcher.png。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # 项目根
sys.path.insert(0, str(HERE))

from icogen import downsample, png_bytes, render_fish  # noqa: E402

#: 各密度对应的像素尺寸
DENSITIES = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}


def main() -> int:
    res_dir = ROOT / "android" / "app" / "src" / "main" / "res"
    for folder, size in DENSITIES.items():
        target = res_dir / folder
        target.mkdir(parents=True, exist_ok=True)
        # 4x 超采样再降采样，边缘平滑
        big = render_fish(size * 4)
        small = downsample(big, size)
        (target / "ic_launcher.png").write_bytes(png_bytes(small, size))
        print(f"  {folder}/ic_launcher.png  {size}x{size}")
    print(f"[完成] Android 图标已生成到 {res_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
