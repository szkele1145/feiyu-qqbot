"""上传 feiyu-qqbot 构建产物到 GitHub Release（替换同名旧资产）。

用法::

    set GITHUB_TOKEN=xxx
    python packaging/upload_release.py

凭证从环境变量 GITHUB_TOKEN 读取，不写进代码。
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "szkele1145/feiyu-qqbot")
ROOT = Path(__file__).resolve().parents[1]
PROXY = os.environ.get("GITHUB_PROXY", "http://127.0.0.1:65532")

#: 要上传的资产：(相对项目根的路径, Release 上的文件名)
ASSETS: list[tuple[str, str]] = [
    ("dist/installer/feiyu-qqbot-setup-0.1.0.exe", "feiyu-qqbot-setup-0.1.0.exe"),
    ("dist/feiyu-qqbot-linux-x86_64.tar.gz", "feiyu-qqbot-linux-x86_64.tar.gz"),
    ("dist/feiyu-qqbot-android.apk", "feiyu-qqbot-android.apk"),
]


def _opener():
    handlers = []
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener(*handlers)


def call(method, url, data=None, headers=None, want_json=True):
    tok = os.environ.get("GITHUB_TOKEN", "")
    h = {
        "Authorization": f"token {tok}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "feiyu-qqbot",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with _opener().open(req, timeout=600) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if (want_json and raw) else None


def main() -> int:
    if not os.environ.get("GITHUB_TOKEN"):
        print("[错误] 请先设置 GITHUB_TOKEN 环境变量")
        return 1

    rel = call("GET", f"https://api.github.com/repos/{REPO}/releases/tags/v0.1.0")
    rid = rel["id"]
    print(f"Release: {rel['html_url']}")

    wanted = {name for _, name in ASSETS}
    for a in rel.get("assets", []):
        if a["name"] in wanted:
            call("DELETE", a["url"], want_json=False)
            print("已删除旧资产:", a["name"])

    for rel_path, name in ASSETS:
        path = ROOT / rel_path
        if not path.exists():
            print(f"[跳过] {name}（不存在: {rel_path}）")
            continue
        size = path.stat().st_size
        with open(path, "rb") as f:
            data = f.read()
        print(f"上传 {name}（{size / 1048576:.1f} MB）")
        res = call(
            "POST",
            f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets?name={name}",
            data, {"Content-Type": "application/octet-stream",
                   "Content-Length": str(size)})
        print("完成:", res["name"], f"{res['size'] / 1048576:.2f} MB")

    final = call("GET", f"https://api.github.com/repos/{REPO}/releases/{rid}")
    print("\n=== 最终资产 ===")
    for a in final["assets"]:
        print(f"  {a['name']:42s} {a['size'] / 1048576:8.2f} MB")
    print("\n下载页:", final["html_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
