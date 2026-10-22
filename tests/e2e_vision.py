"""端到端验证：上传图片 -> 识图 -> 表情包识别。"""
import base64
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, payload=None, timeout=30):
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
            try:
                return r.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {}


def make_sticker_png() -> bytes:
    """造一张表情包 PNG（圆形大脸 + 透明底）。"""
    import io

    from PIL import Image, ImageDraw

    size = 240
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy, r = size / 2, size * 0.42, size * 0.32
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(250, 220, 180, 255))
    er = size * 0.075
    for ex in (cx - r * 0.38, cx + r * 0.38):
        d.ellipse((ex - er, cy - r * 0.3 - er, ex + er, cy - r * 0.3 + er),
                  fill=(255, 255, 255, 255))
        d.ellipse((ex - er * 0.45, cy - r * 0.3 - er * 0.45,
                   ex + er * 0.45, cy - r * 0.3 + er * 0.45), fill=(20, 20, 20, 255))
    d.ellipse((cx - r * 0.34, cy + r * 0.18, cx + r * 0.34, cy + r * 0.62),
              fill=(120, 30, 40, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main() -> int:
    from feiyu_bot.config import Config
    from feiyu_bot.media.library import MediaLibrary
    from feiyu_bot.webui.server import WebUIController
    from feiyu_bot.vision.pipeline import VisionPipeline

    tmpdir = Path(tempfile.mkdtemp(prefix="feiyu-vision-e2e-"))
    cfg = Config.default()
    cfg.set("app.base_dir", str(tmpdir))
    cfg.set("webui.port", PORT)
    cfg.save(tmpdir / "config.yaml")
    cfg = Config.load(tmpdir / "config.yaml")
    media = MediaLibrary(cfg)
    media.init()

    class FakeBot:
        def __init__(self):
            self.cfg = cfg
            self.loop = None
            self.running = False
            self.media = media
            self.vision = VisionPipeline(cfg.section("vision"))
            self.vision.media = media
            self.ctx = type("C", (), {"clear_all": lambda self: None})()

        def status(self):
            return {"gateway": {"connected": False}, "agent": {}}

    bot = FakeBot()
    ctl = WebUIController(bot)
    ctl.serve_background("127.0.0.1", PORT, "")
    time.sleep(1.2)

    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"  {'OK  ' if cond else 'FAIL'} {name} {detail}")

    print("[识图端到端]")
    b64 = base64.b64encode(make_sticker_png()).decode()
    st, d = call("POST", "/api/vision_upload", {"name": "测试表情包.png", "data": b64})

    check("POST /api/vision_upload", st == 200 and d.get("ok"), f"-> {d.get('error', '')}")
    if d.get("ok"):
        print(f"    场景: {d.get('scene')}")
        print(f"    人脸: {d.get('face_count')}")
        st_info = d.get("sticker") or {}
        print(f"    表情包: 判定={st_info.get('is_sticker')} "
              f"情绪={st_info.get('mood')} 特征={st_info.get('features')}")
        check("返回场景判断", bool(d.get("scene")))
        check("返回表情包结果", bool(st_info.get("features")),
              f"-> {st_info.get('features')}")
        check("报告含表情包识别", "表情包识别" in (d.get("report") or ""))
        print("    报告片段:")
        for ln in (d.get("report") or "").splitlines()[:8]:
            print("      ", ln)

    # 库内比对：先入库再传同一张
    src = tmpdir / "assets" / "images" / "猫猫头.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(make_sticker_png())
    media.import_file(src, "image")
    st, d2 = call("POST", "/api/vision_upload", {"name": "猫猫头.png", "data": b64})
    st_info2 = (d2 or {}).get("sticker") or {}
    check("认出库内表情包", bool(st_info2.get("matched")),
          f"-> {st_info2.get('matched_name')} {st_info2.get('similarity')}")

    ctl.stop()
    ok = sum(1 for _, c in results if c)
    print(f"\n结果: {ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
