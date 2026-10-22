"""Android 发布包：APK（内置 Termux 一键启动）或 Termux 启动包。

真实 APK 需要 Android SDK + JDK，本机若不具备，则按用户「安卓版就做成一个 apk，
内置 termux」的诉求，生成一个**可直接侧载的最小 APK**：

    dist/feiyu-qqbot-android.apk

该 APK 是一个 WebView 壳，启动后：
  1. 检测是否安装 Termux（com.termux），未装则跳转下载页；
  2. 已装则通过 Intent 把随包脚本交给 Termux 执行，完成 Python 环境准备并启动机器人；
  3. 壳内 WebView 直接指向机器人的 WebUI（127.0.0.1:8720），手机上当控制面板用。

构建优先级：
  A. 有 Android SDK（ANDROID_HOME / %LOCALAPPDATA%\\Android\\Sdk）+ JDK → 用 aapt2/d8/apksigner 真实构建
  B. 否则 → 生成「Termux 一键启动包」（zip），并**如实标注**它不是已签名 APK

用法::

    python packaging/make_android.py
    python packaging/make_android.py --strict   # 强制要求真实 APK，构建不出来就报错
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

APP_ID = "com.feiyu.qqbot"
APP_LABEL = "飞鱼QQ机器人"
VERSION_NAME = "0.1.0"
VERSION_CODE = 1

# ---------------------------------------------------------------- 随包脚本

#: 在 Termux 里执行的安装脚本（APK 内 assets/install-termux.sh）
INSTALL_TERMUX_SH = r"""#!/data/data/com.termux/files/usr/bin/bash
# feiyu-qqbot 安卓一键安装（由 APK 调用）
set -euo pipefail

APP_NAME="feiyu-qqbot"
INSTALL_DIR="$HOME/$APP_NAME"
VERSION="__VERSION__"

info() { echo -e "\\033[36m[信息]\\033[0m $*"; }
ok()   { echo -e "\\033[32m[完成]\\033[0m $*"; }
fail() { echo -e "\\033[31m[错误]\\033[0m $*" >&2; exit 1; }

command -v pkg >/dev/null 2>&1 || fail "请在 Termux 中运行"
command -v python >/dev/null 2>&1 || { info "安装 Python…"; pkg install -y python; }

info "安装系统依赖（ffmpeg 用于语音转码）…"
pkg install -y ffmpeg python-pip 2>/dev/null || true
termux-wake-lock 2>/dev/null || true

mkdir -p "$INSTALL_DIR"/{src,assets/voices,assets/images,logs,data}
info "安装目录: $INSTALL_DIR"

if [ ! -d "$INSTALL_DIR/src/feiyu_bot" ]; then
  fail "缺少程序源码。请把源码放到 $INSTALL_DIR/src/feiyu_bot 后重试，或使用 Linux 包内的源码。"
fi

info "创建虚拟环境…"
python -m venv "$INSTALL_DIR/venv" 2>/dev/null || fail "venv 创建失败，请执行 pkg install python"
VENV_PIP="$INSTALL_DIR/venv/bin/pip"

info "安装 Python 依赖（首次较慢，约 3-8 分钟）…"
"$VENV_PIP" install --upgrade pip -q
"$VENV_PIP" install -q aiohttp websockets httpx pillow pyyaml cryptography numpy

cat > "$INSTALL_DIR/run.sh" <<EOF2
#!/data/data/com.termux/files/usr/bin/bash
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR/src:\$PYTHONPATH"
exec "$INSTALL_DIR/venv/bin/python" -m feiyu_bot "\$@"
EOF2
chmod +x "$INSTALL_DIR/run.sh"

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  info "生成配置模板…"
  ( cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR/src" python -m feiyu_bot --init ) || true
fi

ok "安装完成（$VERSION）"
echo
echo "1) 编辑配置填写 QQ appid/secret 与 LLM Key:"
echo "   nano $INSTALL_DIR/config.yaml"
echo "2) 启动机器人:"
echo "   $INSTALL_DIR/run.sh"
echo "3) 打开手机浏览器访问控制面板:"
echo "   http://127.0.0.1:8720"
"""

#: 前台启动脚本（APK 调用）
START_SH = r"""#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
INSTALL_DIR="$HOME/feiyu-qqbot"
termux-wake-lock 2>/dev/null || true
if [ ! -x "$INSTALL_DIR/run.sh" ]; then
  echo "尚未安装，请先运行安装脚本。"
  exit 1
fi
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/run.sh" "$@"
"""

#: 保活脚本
KEEPALIVE_SH = r"""#!/data/data/com.termux/files/usr/bin/bash
# 15 分钟一次保活（安卓后台容易被杀，配合 wake-lock）
INSTALL_DIR="$HOME/feiyu-qqbot"
termux-wake-lock 2>/dev/null || true
while true; do
  sleep 900
  cd "$INSTALL_DIR" 2>/dev/null || continue
  PYTHONPATH="$INSTALL_DIR/src" "$INSTALL_DIR/venv/bin/python" \
    -m feiyu_bot --keepalive-check >/dev/null 2>&1 || true
done
"""

README_ANDROID = """# feiyu-qqbot Android 包

## 包里是什么

- `install-termux.sh` — 在 Termux 里一键装好 Python 环境
- `start-bot.sh` — 前台启动机器人
- `keepalive.sh` — 15 分钟保活循环
- `README-ANDROID.md` — 本文件

## 使用步骤

1. 安装 **Termux**（F-Droid 版，不要用 Play 商店的旧版）
2. 把本包解压到手机，或把脚本传进 Termux
3. 在 Termux 里执行：

   ```bash
   termux-setup-storage
   cp /sdcard/Download/install-termux.sh ~/
   bash ~/install-termux.sh
   ```

4. 编辑配置：

   ```bash
   nano ~/feiyu-qqbot/config.yaml
   ```

   填写 QQ 开放平台的 `appid` / `secret`、LLM 的 `base_url` / `api_key`。

5. 启动：

   ```bash
   bash ~/feiyu-qqbot/start.sh
   ```

6. 手机浏览器打开 `http://127.0.0.1:8720` 用控制面板。

## 后台常驻

```bash
bash ~/keepalive.sh &
```

或装 Termux:Boot 让它开机自启。

> 安卓会杀后台，务必先 `termux-wake-lock`（脚本里已自动调用）。
"""

# ---------------------------------------------------------------- Android 工程

ANDROID_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="com.feiyu.qqbot"
    android:versionCode="1"
    android:versionName="0.1.0">

    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="33" />
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

    <queries>
        <package android:name="com.termux" />
    </queries>

    <application
        android:label="飞鱼QQ机器人"
        android:allowBackup="true"
        android:usesCleartextTraffic="true"
        android:theme="@android:style/Theme.Material.NoActionBar">
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:configChanges="orientation|screenSize|keyboardHidden">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

MAIN_ACTIVITY_JAVA = """package com.feiyu.qqbot;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.view.KeyEvent;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

/**
 * 飞鱼QQ机器人 —— 安卓壳。
 *
 * 首次启动引导安装 Termux 并执行随包脚本；之后 WebView 指向本机 WebUI：
 * http://127.0.0.1:8720
 */
public class MainActivity extends Activity {

    private WebView web;
    private static final String PANEL_URL = "http://127.0.0.1:8720";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        web.setWebViewClient(new WebViewClient());

        root.addView(web);
        setContentView(root);

        if (!isTermuxInstalled()) {
            promptInstallTermux();
        } else {
            launchTermuxScript("install-termux.sh");
        }
        web.loadUrl(PANEL_URL);
    }

    private boolean isTermuxInstalled() {
        try {
            getPackageManager().getPackageInfo("com.termux", 0);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private void promptInstallTermux() {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW,
                    Uri.parse("https://f-droid.org/packages/com.termux/")));
        } catch (ActivityNotFoundException ignored) {
        }
    }

    /** 通过 RUN_COMMAND intent 让 Termux 执行随包脚本。 */
    private void launchTermuxScript(String script) {
        try {
            Intent i = new Intent();
            i.setClassName("com.termux", "com.termux.app.RunCommandService");
            i.setAction("com.termux.RUN_COMMAND");
            i.putExtra("com.termux.RUN_COMMAND_PATH",
                    "/data/data/com.termux/files/usr/bin/bash");
            i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS",
                    new String[]{"-c", "cp /sdcard/Download/" + script + " ~/ && bash ~/" + script});
            i.putExtra("com.termux.RUN_COMMAND_WORKDIR",
                    "/data/data/com.termux/files/home");
            i.putExtra("com.termux.RUN_COMMAND_BACKGROUND", false);
            startService(i);
        } catch (Exception ignored) {
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
"""

BUILD_GRADLE = """plugins {
    id 'com.android.application'
}

android {
    namespace 'com.feiyu.qqbot'
    compileSdk 34

    defaultConfig {
        applicationId "com.feiyu.qqbot"
        minSdk 21
        targetSdk 33
        versionCode 1
        versionName "0.1.0"
    }

    compileOptions {
        sourceCompatibility JavaVersion.VERSION_1_8
        targetCompatibility JavaVersion.VERSION_1_8
    }
}
"""

SETTINGS_GRADLE = """pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}
rootProject.name = "feiyu-qqbot-android"
include ':app'
"""

ROOT_BUILD_GRADLE = """plugins {
    id 'com.android.application' version '8.1.4' apply false
}
"""


def _write_android_project(base: Path) -> None:
    """把完整 Gradle 工程写到 android/。"""
    app = base / "app" / "src" / "main"
    (app / "java" / "com" / "feiyu" / "qqbot").mkdir(parents=True, exist_ok=True)
    (app / "assets").mkdir(parents=True, exist_ok=True)
    (app / "res" / "values").mkdir(parents=True, exist_ok=True)

    (base / "settings.gradle").write_text(SETTINGS_GRADLE, encoding="utf-8")
    (base / "build.gradle").write_text(ROOT_BUILD_GRADLE, encoding="utf-8")
    (base / "app" / "build.gradle").write_text(BUILD_GRADLE, encoding="utf-8")
    (app / "AndroidManifest.xml").write_text(ANDROID_MANIFEST, encoding="utf-8")
    (app / "java" / "com" / "feiyu" / "qqbot" / "MainActivity.java").write_text(
        MAIN_ACTIVITY_JAVA, encoding="utf-8")
    (app / "assets" / "install-termux.sh").write_text(
        INSTALL_TERMUX_SH.replace("__VERSION__", VERSION_NAME), encoding="utf-8")
    (app / "assets" / "start-bot.sh").write_text(START_SH, encoding="utf-8")
    (app / "res" / "values" / "strings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        f'    <string name="app_name">{APP_LABEL}</string>\n</resources>\n',
        encoding="utf-8")


def _find_android_sdk() -> Path | None:
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        val = os.environ.get(env)
        if val and Path(val).is_dir():
            return Path(val)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        p = Path(local) / "Android" / "Sdk"
        if p.is_dir():
            return p
    return None


def build_termux_pack() -> Path:
    """降级方案：打一个 Termux 一键启动包（zip）。"""
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / "feiyu-qqbot-android-termux.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("install-termux.sh", INSTALL_TERMUX_SH.replace("__VERSION__", VERSION_NAME))
        z.writestr("start-bot.sh", START_SH)
        z.writestr("keepalive.sh", KEEPALIVE_SH)
        z.writestr("README-ANDROID.md", README_ANDROID)
    return target


def build_real_apk(sdk: Path) -> Path | None:
    """用 Android SDK 构建签名 APK。缺工具时返回 None。"""
    bt = sdk / "build-tools"
    if not bt.is_dir():
        return None
    versions = sorted([d for d in bt.iterdir() if d.is_dir()], key=lambda p: p.name)
    if not versions:
        return None
    tools = versions[-1]
    aapt2 = tools / "aapt2.exe" if os.name == "nt" else tools / "aapt2"
    d8 = tools / "d8.bat" if os.name == "nt" else tools / "d8"
    zipalign = tools / "zipalign.exe" if os.name == "nt" else tools / "zipalign"
    apksigner = tools / "apksigner.bat" if os.name == "nt" else tools / "apksigner"
    android_jar = sdk / "platforms"
    jars = sorted(android_jar.glob("android-*/android.jar")) if android_jar.is_dir() else []
    if not all([aapt2.exists(), d8.exists(), zipalign.exists(), apksigner.exists()]) or not jars:
        return None

    work = ROOT / "build" / "android"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    _write_android_project(work / "project")
    proj = work / "project"
    app = proj / "app" / "src" / "main"

    # 1) 资源链接
    linked = work / "app.apk.unaligned"
    cmd = [
        str(aapt2), "link", "-o", str(linked),
        "-I", str(jars[-1]),
        "--manifest", str(app / "AndroidManifest.xml"),
        "-A", str(app / "assets"),
        "--java", str(work / "gen"),
    ]
    res_dir = app / "res"
    if res_dir.is_dir():
        cmd += ["--auto-add-overlay"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("[错误] aapt2 link 失败:\n" + (res.stderr or res.stdout)[:2000])
        return None

    # 2) 编译 Java
    javac = shutil.which("javac")
    if not javac:
        print("[提示] 找不到 javac，跳过真实 APK 构建")
        return None
    classes = work / "classes"
    classes.mkdir()
    java_src = [str(p) for p in (app / "java").rglob("*.java")]
    gen_src = [str(p) for p in (work / "gen").rglob("*.java")] if (work / "gen").is_dir() else []
    rc = subprocess.run([javac, "-nowarn", "-source", "8", "-target", "8",
                         "-classpath", str(jars[-1]), "-d", str(classes)]
                        + java_src + gen_src, capture_output=True, text=True)
    if rc.returncode != 0:
        print("[错误] javac 失败:\n" + (rc.stderr or rc.stdout)[:2000])
        return None

    # 3) d8 -> dex
    dex_out = work / "dex"
    dex_out.mkdir()
    class_files = [str(p) for p in classes.rglob("*.class")]
    rc = subprocess.run([str(d8), "--lib", str(jars[-1]), "--output", str(dex_out)]
                        + class_files, capture_output=True, text=True)
    if rc.returncode != 0:
        print("[错误] d8 失败:\n" + (rc.stderr or rc.stdout)[:2000])
        return None

    # 4) 把 dex 塞进 apk
    aligned = work / "app.apk"
    with zipfile.ZipFile(linked, "a", zipfile.ZIP_DEFLATED) as z:
        for dex in dex_out.glob("*.dex"):
            z.write(dex, dex.name)

    rc = subprocess.run([str(zipalign), "-f", "4", str(linked), str(aligned)],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        print("[错误] zipalign 失败:\n" + (rc.stderr or rc.stdout)[:1200])
        return None

    # 5) 生成 debug keystore 并签名
    keystore = work / "debug.keystore"
    keytool = shutil.which("keytool")
    if keytool and not keystore.exists():
        subprocess.run([keytool, "-genkeypair", "-v", "-keystore", str(keystore),
                        "-storepass", "android", "-keypass", "android",
                        "-alias", "androiddebugkey", "-keyalg", "RSA",
                        "-keysize", "2048", "-validity", "10000",
                        "-dname", "CN=Android Debug,O=Android,C=US"],
                       capture_output=True, text=True)

    signed = work / "app-signed.apk"
    rc = subprocess.run([str(apksigner), "sign", "--ks", str(keystore),
                         "--ks-pass", "pass:android", "--key-pass", "pass:android",
                         "--out", str(signed), str(aligned)],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        print("[错误] apksigner 失败:\n" + (rc.stderr or rc.stdout)[:1200])
        return None

    DIST.mkdir(parents=True, exist_ok=True)
    final = DIST / "feiyu-qqbot-android.apk"
    shutil.copy2(signed, final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 Android 发布物")
    parser.add_argument("--strict", action="store_true", help="必须产出真实 APK")
    args = parser.parse_args(argv)

    # 无论如何先把完整 Gradle 工程落盘（可在 CI 里直接构建）
    _write_android_project(ROOT / "android")
    print(f"[已生成] Android Gradle 工程: {ROOT / 'android'}")

    sdk = _find_android_sdk()
    if sdk:
        print(f"[检测到] Android SDK: {sdk}")
        apk = build_real_apk(sdk)
        if apk:
            size = apk.stat().st_size / 1024
            print(f"[完成] 真实 APK: {apk}  ({size:.0f} KB)")
            return 0
        print("[提示] SDK 存在但构建工具不全，改用 Termux 启动包")
    else:
        print("[提示] 未检测到 Android SDK")

    if args.strict:
        print("[错误] --strict 要求真实 APK，但构建条件不满足", file=sys.stderr)
        return 1

    pack = build_termux_pack()
    size = pack.stat().st_size / 1024
    print(f"[完成] Termux 一键启动包: {pack}  ({size:.0f} KB)")
    print("       注意：这是 zip 启动包，不是已签名 APK。")
    print(f"       完整 Gradle 工程在 {ROOT / 'android'}，可用 Android Studio 或 CI 构建真 APK。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
