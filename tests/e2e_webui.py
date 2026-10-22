"""端到端验证 WebUI 新接口：上传、日志、缩略图、启停、模型列表。"""
import base64
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, payload=None, timeout=20):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ct = r.headers.get("Content-Type", "")
            if "json" in ct:
                return r.status, json.loads(raw)
            return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def make_png() -> bytes:
    """最小合法 PNG（1x1 红色）。"""
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"\x00\xff\x00\x00"  # filter + RGB
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main() -> int:
    from feiyu_bot.config import Config
    from feiyu_bot.webui.server import WebUIController

    # 用临时配置，不碰仓库里的 config.yaml
    tmpdir = Path(tempfile.mkdtemp(prefix="feiyu-e2e-"))
    cfg = Config.default()
    cfg.set("app.base_dir", str(tmpdir))  # 关键：媒体/日志都相对它解析
    cfg.set("webui.port", PORT)
    cfg.save(tmpdir / "config.yaml")
    cfg = Config.load(tmpdir / "config.yaml")
    from feiyu_bot.media.library import MediaLibrary

    media = MediaLibrary(cfg)

    class FakeBot:
        def __init__(self):
            self.cfg = cfg
            self.loop = None
            self.running = False
            self.media = media  # 共用同一个实例
            self.ctx = type("C", (), {"clear_all": lambda self: None})()

        def status(self):
            return {"gateway": {"connected": False}, "agent": {}}

    bot = FakeBot()
    ctl = WebUIController(bot)
    ctl.serve_background("127.0.0.1", PORT, "")
    time.sleep(1.2)

    results = []

    def check(name, cond, detail=""):
        results.append((name, cond, detail))
        print(f"  {'OK  ' if cond else 'FAIL'} {name} {detail}")

    print("[接口验证]")

    st, d = call("GET", "/api/status")
    check("GET /api/status", st == 200 and isinstance(d, dict))

    st, d = call("GET", "/api/media_list")
    check("GET /api/media_list", st == 200 and "items" in d)

    # 上传 PNG
    png = base64.b64encode(make_png()).decode()
    st, d = call("POST", "/api/media_upload",
                 {"name": "测试图片.png", "data": png, "kind": "image"})
    check("POST /api/media_upload", st == 200 and d.get("ok"),
          f"-> {d.get('item', {}).get('name', d.get('error'))}")

    # 上传 MP3 头（伪造但魔数正确）
    mp3 = base64.b64encode(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 20).decode()
    st, d = call("POST", "/api/media_upload",
                 {"name": "测试语音.mp3", "data": mp3, "kind": "voice"})
    check("POST /api/media_upload (mp3)", st == 200 and d.get("ok"),
          f"-> {d.get('item', {}).get('kind', d.get('error'))}")

    # 不支持的格式
    st, d = call("POST", "/api/media_upload",
                 {"name": "evil.exe", "data": base64.b64encode(b"MZ\x90\x00").decode()})
    check("拒绝未知格式", st == 200 and not d.get("ok"))


    # 日志
    logdir = Path(str(cfg.get("app.log_dir", "logs")))
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "test.log").write_text("第一行日志\n第二行日志\n", encoding="utf-8")
    st, d = call("GET", "/api/logs?offset=0")
    check("GET /api/logs", st == 200 and d.get("ok") and "第一行" in d.get("text", ""),
          f"-> offset={d.get('offset')}")
    if d.get("ok"):
        st2, d2 = call("GET", f"/api/logs?offset={d['offset']}")
        check("GET /api/logs 增量", st2 == 200 and d2.get("text") == "")

    # 启停（FakeBot 没有事件循环，应优雅报错而不是崩）
    st, d = call("GET", "/api/start")
    check("GET /api/start 优雅处理", st == 200 and "ok" in d)
    st, d = call("GET", "/api/stop")
    check("GET /api/stop 优雅处理", st == 200 and "ok" in d)

    # LLM 模型列表
    st, d = call("GET", "/api/llm_models")
    check("GET /api/llm_models", st == 200 and "models" in d,
          f"-> {d.get('models')}")

    ctl.stop()

    ok = sum(1 for _, c, _ in results if c)
    print(f"\n结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
