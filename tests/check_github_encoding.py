"""检查 GitHub 上 README / Release 正文的编码是否正常。"""
import json
import urllib.request

TOKEN = open(
    r"C:\Users\一只屑\.dsh\attachments\v1\files\25"
    r"\25fad3265a510d082ee581b66a2311eacc120fce8c18b8a215c25fe8f5789972\.ghtoken",
    encoding="utf-8",
).read().strip()

H = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "feiyu-check",
}


def get(url: str):
    req = urllib.request.Request(url, headers=H)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return raw


def show(name: str, raw: bytes, pick: str = None) -> None:
    try:
        text = raw.decode("utf-8")
        ok = "UTF-8 解码正常"
    except UnicodeDecodeError as e:
        text = raw.decode("utf-8", errors="replace")
        ok = f"UTF-8 解码失败: {e}"
    print(f"\n=== {name} ({len(raw)} bytes) — {ok} ===")
    if pick:
        data = json.loads(text)
        for item in data:
            print(f"  name={item.get('name')!r}")
            body = item.get("body") or ""
            print(f"    body前80字: {body[:80]!r}")
    else:
        print(text[:300])


# README（raw）
raw = get("https://raw.githubusercontent.com/szkele1145/feiyu-qqbot/main/README.md")
show("README.md", raw)

# Release
raw = get("https://api.github.com/repos/szkele1145/feiyu-qqbot/releases")
show("releases", raw, pick="releases")
