"""验证打包后的 exe 双击会弹出 GUI 窗口。

做法：后台启动 exe（无参数），3 秒后检查窗口标题是否存在，然后杀掉进程。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "feiyu-qqbot" / "feiyu-qqbot.exe"


def find_window_titles(pid: int) -> list[str]:
    """用 ctypes 枚举该进程的所有窗口标题。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    titles: list[str] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def callback(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return titles


def main() -> int:
    if not EXE.exists():
        print(f"找不到 exe: {EXE}")
        return 1

    print(f"启动 {EXE.name}（无参数 = GUI 模式）…")
    proc = subprocess.Popen(
        [str(EXE)], cwd=str(EXE.parent), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    try:
        titles: list[str] = []
        for _ in range(25):
            time.sleep(1)
            if proc.poll() is not None:
                print(f"进程已退出，返回码 {proc.returncode}")
                out = proc.stdout.read() if proc.stdout else ""
                print(out[-800:])
                return 1
            titles = find_window_titles(proc.pid)
            if any("大肥鱼" in t for t in titles):
                break
        print(f"检测到窗口: {titles}")
        if not any("大肥鱼" in t for t in titles):
            print("未找到 GUI 主窗口")
            return 1
        print("OK: 双击 exe 成功弹出桌面 GUI")
        return 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
