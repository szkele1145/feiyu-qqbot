"""识图管线测试：场景判断、人脸检测兜底。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from feiyu_bot.vision.pipeline import (  # noqa: E402
    ImageReport,
    _count_faces,
    _count_faces_pil,
)


def make_skin_face() -> Image.Image:
    """画一张带肤色椭圆（模拟人脸）的图。"""
    img = Image.new("RGB", (240, 240), (30, 60, 120))
    d = ImageDraw.Draw(img)
    d.ellipse((70, 50, 170, 190), fill=(235, 190, 160))   # 脸
    d.ellipse((95, 95, 110, 110), fill=(40, 30, 30))      # 眼
    d.ellipse((135, 95, 150, 110), fill=(40, 30, 30))
    d.ellipse((112, 140, 130, 155), fill=(190, 120, 110))  # 嘴
    return img


def make_blank() -> Image.Image:
    return Image.new("RGB", (240, 240), (20, 20, 20))


def make_screenshot() -> Image.Image:
    """模拟手机截图：白底 + 大量文字条 + 顶部状态栏。"""
    img = Image.new("RGB", (360, 640), (245, 245, 245))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 360, 40), fill=(60, 60, 60))
    for i in range(14):
        y = 70 + i * 38
        d.rectangle((20, y, 330, y + 12), fill=(90, 90, 90))
        d.rectangle((20, y + 18, 240, y + 26), fill=(160, 160, 160))
    return img


def test_face_detection_pil():
    n = _count_faces_pil(make_skin_face())
    print(f"[人脸检测-纯PIL] 有人脸图 -> {n}")
    assert n >= 1, "应该至少检测到 1 张人脸"

    n2 = _count_faces_pil(make_blank())
    print(f"[人脸检测-纯PIL] 空白图 -> {n2}")
    assert n2 == 0, "空白图不应检测到人脸"


def test_count_faces_dispatch():
    n = _count_faces(make_skin_face())
    print(f"[人脸检测-统一入口] -> {n}")
    assert n >= 1


def test_scene_guess_photo():
    r = ImageReport()
    r.ok = True
    r.width, r.height = 800, 600
    r.brightness = 0.45
    r.contrast = 0.6
    r.colorfulness = 0.5
    r.edge_density = 0.3
    r.face_count = 2
    r.ocr_text = ""
    g = r.scene_guess()
    print(f"[场景判断] 2人照片 -> {g}")
    assert "2 个人" in g


def test_scene_guess_screenshot():
    r = ImageReport()
    r.ok = True
    r.width, r.height = 360, 640
    r.brightness = 0.8
    r.contrast = 0.3
    r.colorfulness = 0.08
    r.edge_density = 0.25
    r.is_screenshot_like = True
    r.ocr_text = "聊天记录\n今天天气不错\n下午三点开会\n记得带伞\n记得带伞"
    g = r.scene_guess()
    print(f"[场景判断] 截图 -> {g}")
    assert "截图" in g


def test_scene_guess_sticker():
    r = ImageReport()
    r.ok = True
    r.width, r.height = 300, 300
    r.brightness = 0.6
    r.contrast = 0.5
    r.colorfulness = 0.55
    r.edge_density = 0.15
    r.ocr_text = ""
    g = r.scene_guess()
    print(f"[场景判断] 表情包 -> {g}")
    assert "表情包" in g


def test_scene_guess_document():
    r = ImageReport()
    r.ok = True
    r.width, r.height = 800, 1000
    r.brightness = 0.9
    r.contrast = 0.2
    r.colorfulness = 0.05
    r.edge_density = 0.22
    r.ocr_text = "第一章 总则\n第一条 为规范管理\n第二条 适用范围\n第三条 责任主体" * 3
    g = r.scene_guess()
    print(f"[场景判断] 文档 -> {g}")
    assert "文档" in g or "文字" in g


def test_report_text():
    r = ImageReport()
    r.ok = True
    r.width, r.height = 100, 100
    r.face_count = 1
    r.palette = [("蓝色", 0.4), ("白色", 0.3)]
    r.brightness = 0.5
    r.contrast = 0.5
    txt = r.to_prompt_text()
    assert "场景判断" in txt
    print("[报告] 含场景判断 OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
