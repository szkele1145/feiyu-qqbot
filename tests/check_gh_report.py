"""把 GitHub 上的中文内容写到 UTF-8 文件，避开控制台编码干扰。"""
import json
import urllib.request
from pathlib import Path

TOKEN = Path(
    r"C:\Users\一只屑\.dsh\attachments\v1\files\25"
    r"\25fad3265a510d082ee581b66a2311eacc120fce8c18b8a215c25fe8f5789972\.ghtoken"
).read_text(encoding="utf-8").strip()

opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"https": "http://127.0.0.1:65532", "http": "http://127.0.0.1:65532"})
)
H = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
     "User-Agent": "feiyu-check"}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=H)
    with opener.open(req, timeout=60) as r:
        return r.read()


out = []
repo = json.loads(get("https://api.github.com/repos/szkele1145/feiyu-qqbot").decode("utf-8"))
out.append(f"仓库描述: {repo['description']!r}")
out.append(f"描述含问号: {'?' in (repo['description'] or '')}")

rels = json.loads(get("https://api.github.com/repos/szkele1145/feiyu-qqbot/releases").decode("utf-8"))
for r in rels:
    body = r.get("body") or ""
    out.append(f"\n=== Release {r['tag_name']} ===")
    out.append(f"name: {r.get('name')!r}")
    out.append(f"body 含问号: {body.count('?')}")
    out.append(f"body 前 500 字:\n{body[:500]}")
    out.append(f"assets: {[a['name'] for a in r['assets']]}")

readme = get("https://raw.githubusercontent.com/szkele1145/feiyu-qqbot/main/README.md")
out.append(f"\n=== README ({len(readme)} bytes) ===")
out.append(f"含问号数: {readme.decode('utf-8').count('?')}")
out.append(readme.decode("utf-8")[:400])

Path("tests/_gh_report.txt").write_text("\n".join(out), encoding="utf-8")
print("written")
