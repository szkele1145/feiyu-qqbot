"""验证 Windows exe：双击能弹出 GUI 桌面面板。"""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "feiyu-qqbot" / "feiyu-qqbot.exe"


def window_titles() -> list[str]:
    titles = []
    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowTextW = ctypes.windll.user32.GetWindowTextW
    GetWindowTextLengthW = ctypes.windll.user32.GetWindowTextLengthW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))

    def cb(hwnd, _lparam):
        if IsWindowVisible(hwnd):
            n = GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    EnumWindows(WNDENUMPROC(cb), None)
    return titles


def main() -> int:
    if not EXE.exists():
        print(f"[错误] 找不到 {EXE}")
        return 1

    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent))
    found = None
    try:
        for _ in range(25):
            time.sleep(1)
            for t in window_titles():
                if "鱼" in t or "feiyu" in t.lower() or "QQ" in t:
                    found = t
                    break
            if found:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if found:
        print(f"OK: exe 成功弹出桌面面板 -> 「{found}」")
        return 0

    print("FAIL: 25 秒内未检测到窗口")
    print("可见窗口:", window_titles()[:15])
    return 1


if __name__ == "__main__":
    sys.exit(main())
