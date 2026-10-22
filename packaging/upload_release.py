"""上传 feiyu-qqbot Windows 安装包到 GitHub Release（替换旧资产）。"""
import json
import os
import urllib.request

TOK = "GITHUB_TOKEN_PLACEHOLDER"
REPO = "szkele1145/feiyu-qqbot"
RID = 420347916
ASSET_NAME = "feiyu-qqbot-setup-0.1.0.exe"
EXE = r"F:\vibe coding工程\feiyu-qqbot\dist\installer\feiyu-qqbot-setup-0.1.0.exe"

op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"https": "http://127.0.0.1:65532"}))
H = {"Authorization": f"token {TOK}", "Accept": "application/vnd.github+json",
     "User-Agent": "dsh"}


def call(method, url, data=None, hdr=None, want_json=True):
    h = dict(H)
    h.update(hdr or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with op.open(req, timeout=300) as resp:
        raw = resp.read()
    if not want_json or not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def main():
    rel = call("GET", f"https://api.github.com/repos/{REPO}/releases/{RID}")
    print("release:", rel["tag_name"])
    print("现有资产:", [a["name"] for a in rel["assets"]])

    for a in rel["assets"]:
        if a["name"] == ASSET_NAME:
            call("DELETE", a["url"], want_json=False)
            print("已删除旧资产:", a["id"])

    size = os.path.getsize(EXE)
    print(f"上传 {EXE} ({size / 1024 / 1024:.1f} MB)")
    with open(EXE, "rb") as f:
        data = f.read()

    url = (f"https://uploads.github.com/repos/{REPO}/releases/{RID}"
           f"/assets?name={ASSET_NAME}")
    res = call("POST", url, data,
               {"Content-Type": "application/octet-stream",
                "Content-Length": str(size)})
    print("上传完成:", res["name"], res["size"], res["state"])

    rel2 = call("GET", f"https://api.github.com/repos/{REPO}/releases/{RID}")
    print("\n=== Release 最终资产 ===")
    for a in rel2["assets"]:
        print(f"  {a['name']:42s} {a['size'] / 1024 / 1024:8.2f} MB  {a['state']}")
    print("\n下载页:", rel2["html_url"])


if __name__ == "__main__":
    main()
