"""校验 Linux 发行包结构。"""
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

root = Path(__file__).resolve().parents[1]
tgz = root / "dist" / "feiyu-qqbot-linux-x86_64.tar.gz"

with tarfile.open(tgz, "r:gz") as tar:
    members = tar.getmembers()

names = [m.name for m in members]
print(f"总条目: {len(names)}")

required = [
    "feiyu-qqbot-linux-x86_64/install.sh",
    "feiyu-qqbot-linux-x86_64/start.sh",
    "feiyu-qqbot-linux-x86_64/requirements.txt",
    "feiyu-qqbot-linux-x86_64/src/feiyu_bot/__main__.py",
    "feiyu-qqbot-linux-x86_64/src/feiyu_bot/config.py",
]

ok = True
for r in required:
    hit = r in names
    print(f"  {'OK ' if hit else 'MISS'} {r}")
    ok = ok and hit

ex = [m for m in members if m.name.endswith("install.sh")]
for m in ex:
    print(f"  install.sh mode = {oct(m.mode)}  {'OK' if m.mode & 0o111 else 'NOT EXECUTABLE'}")
    ok = ok and bool(m.mode & 0o111)

print("\n前 12 个条目:")
for n in names[:12]:
    print("   ", n)

print("\nBUNDLE", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
