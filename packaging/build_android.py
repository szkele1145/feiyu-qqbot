"""构建 Android APK。

优先用本机已有的 Android SDK + gradle 发行版直接构建；
没有则提示用 CI 构建。

用法::

    python packaging/build_android.py
    python packaging/build_android.py --sdk-dir Z:/asdk
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANDROID_DIR = ROOT / "android"
DIST = ROOT / "dist"

#: 常见 SDK 位置（按优先级）
SDK_CANDIDATES = [
    os.environ.get("ANDROID_HOME", ""),
    os.environ.get("ANDROID_SDK_ROOT", ""),
    r"Z:\asdk",
    r"C:\andsdk",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"),
]

#: gradle 发行版查找位置
GRADLE_DISTS = Path(os.environ.get("USERPROFILE", "")) / ".gradle" / "wrapper" / "dists"


def find_sdk() -> Path | None:
    for cand in SDK_CANDIDATES:
        if cand and Path(cand).is_dir():
            p = Path(cand)
            if (p / "platforms").is_dir() or (p / "build-tools").is_dir():
                return p
    return None


def find_gradle() -> Path | None:
    """找本机已下载的 gradle 发行版（避免 wrapper 联网下载失败）。"""
    if not GRADLE_DISTS.is_dir():
        return None
    for dist in sorted(GRADLE_DISTS.glob("gradle-*-bin"), reverse=True):
        for bat in dist.rglob("gradle.bat"):
            return bat
    return None


def find_jdk() -> Path | None:
    """找 JDK（含 javac），Gradle 编译 Java 需要。"""
    for env in ("JAVA_HOME",):
        val = os.environ.get(env, "")
        if val and (Path(val) / "bin" / "javac.exe").exists():
            return Path(val)
    # 常见安装位置
    for base in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Eclipse Adoptium",
                 Path("C:/Program Files/Eclipse Adoptium")):
        if base.is_dir():
            for d in sorted(base.iterdir(), reverse=True):
                if d.is_dir() and (d / "bin" / "javac.exe").exists():
                    return d
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Android APK")
    parser.add_argument("--sdk-dir", default=None, help="Android SDK 目录")
    parser.add_argument("--work-dir", default=None, help="构建工作目录（需纯 ASCII 路径）")
    args = parser.parse_args(argv)

    if not ANDROID_DIR.is_dir():
        print(f"[错误] 找不到 {ANDROID_DIR}")
        return 1

    sdk = Path(args.sdk_dir) if args.sdk_dir else find_sdk()
    if not sdk:
        print("[错误] 未找到 Android SDK，请用 --sdk-dir 指定")
        return 1
    print(f"SDK: {sdk}")

    jdk = find_jdk()
    if not jdk:
        print("[错误] 未找到 JDK（需要 javac）。请安装 Temurin JDK 或用 CI 构建。")
        return 1
    print(f"JDK: {jdk}")

    gradle = find_gradle()
    if not gradle:
        print("[错误] 未找到本机 gradle 发行版；请让 gradlew 先下载一次，或用 CI 构建。")
        return 1
    print(f"Gradle: {gradle}")

    # 构建目录必须是无中文的路径（Gradle/AGP 限制）
    work = Path(args.work_dir) if args.work_dir else Path(r"Z:\_imdeploy_apk")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    for item in ANDROID_DIR.iterdir():
        if item.name in (".gradle", "build", "local.properties"):
            continue
        dst = work / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)

    (work / "local.properties").write_text(
        f"sdk.dir={str(sdk).replace(chr(92), chr(92) * 2)}", encoding="ascii")

    env = dict(os.environ)
    env["JAVA_HOME"] = str(jdk)
    env["Path"] = f"{jdk / 'bin'};{env.get('Path', '')}"

    cmd = [str(gradle), "assembleRelease", "--no-daemon"]
    print("执行:", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(work), env=env)
    if r.returncode != 0:
        print("[错误] Gradle 构建失败")
        return 1

    apk = work / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    if not apk.exists():
        print("[错误] 未找到 APK 产物")
        return 1

    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / "feiyu-qqbot-android.apk"
    shutil.copy2(apk, target)
    print(f"[完成] {target}  ({target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
