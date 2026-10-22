"""表情包识别测试：库内比对、特征提取、梗文归类。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from feiyu_bot.vision.sticker import (  # noqa: E402
    StickerMatcher,
    _ahash,
    _dhash,
    _hamming,
)


def make_sticker(text: str = "", face_color=(250, 220, 180),
                 bg=None, size=240) -> Image.Image:
    """造一张典型的表情包：圆形大脸 + 大眼睛 + 文字。"""
    img = Image.new("RGBA", (size, size), bg if bg else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx, cy, r = size / 2, size * 0.42, size * 0.32
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*face_color, 255))
    # 大眼睛
    er = size * 0.075
    for ex in (cx - r * 0.38, cx + r * 0.38):
        d.ellipse((ex - er, cy - r * 0.3 - er, ex + er, cy - r * 0.3 + er),
                  fill=(255, 255, 255, 255))
        d.ellipse((ex - er * 0.45, cy - r * 0.3 - er * 0.45,
                   ex + er * 0.45, cy - r * 0.3 + er * 0.45),
                  fill=(20, 20, 20, 255))
    # 夸张大嘴
    d.ellipse((cx - r * 0.34, cy + r * 0.18, cx + r * 0.34, cy + r * 0.62),
              fill=(120, 30, 40, 255))
    if text:
        d.text((size * 0.08, size * 0.84), text, fill=(20, 20, 20, 255))
    return img


def test_hashes_similar_for_same_image():
    img = make_sticker()
    assert _hamming(_dhash(img), _dhash(img)) == 0
    assert _hamming(_ahash(img), _ahash(img)) == 0
    print("[哈希] 同一张图差异为 0 OK")


def test_hashes_differ_for_different():
    a = make_sticker(face_color=(250, 220, 180))
    b = make_sticker(face_color=(20, 20, 20), bg=(255, 255, 255, 255))
    d = _hamming(_dhash(a), _dhash(b))
    print(f"[哈希] 不同图 dHash 差异 = {d}")
    assert d > 3


def test_feature_extraction():
    tmp = Path(tempfile.mkdtemp(prefix="stk-"))
    p = tmp / "大笑.png"
    make_sticker("笑死").save(p)
    m = StickerMatcher(None)
    info = m.match(p, ocr_text="笑死我了")
    print("[特征] 提取到:")
    for f in info.features:
        print("   -", f)
    print(f"  判定表情包: {info.is_sticker}")
    print(f"  情绪: {info.mood}")
    print(f"  梗类型: {info.known_meme}")
    print(f"  梗文: {info.meme_text}")
    assert info.features, "应提取到视觉特征"
    assert info.known_meme == "笑死/崩溃", f"梗归类错: {info.known_meme}"


def test_known_sticker_match():
    """同一张图放进库，再匹配应能认出。"""
    from feiyu_bot.config import Config
    from feiyu_bot.media.library import MediaLibrary

    tmp = Path(tempfile.mkdtemp(prefix="stk-lib-"))
    cfg = Config.default()
    cfg.set("app.base_dir", str(tmp))
    cfg.set("image_dir", str(tmp / "assets" / "images"))
    lib = MediaLibrary(cfg)
    lib.init()

    img_dir = tmp / "assets" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    src = img_dir / "经典猫猫头.png"
    make_sticker("喵").save(src)
    lib.import_file(src, "image")

    # 复制一份当"新来的图"
    other = tmp / "new.png"
    make_sticker("喵").save(other)

    m = StickerMatcher(lib)
    n = m.build_index()
    print(f"[比对] 索引建立 {n} 张")
    info = m.match(other)
    print(f"[比对] 匹配={info.matched} 名字={info.matched_name} 相似度={info.similarity:.0%}")
    assert n >= 1, "应建立索引"
    assert info.matched, "应认出库里的表情包"
    assert "经典猫猫头" in info.matched_name


def test_to_text():
    tmp = Path(tempfile.mkdtemp(prefix="stk-"))
    p = tmp / "a.png"
    make_sticker("无语").save(p)
    info = StickerMatcher(None).match(p, ocr_text="无语")
    txt = info.to_text()
    print("[输出] " + txt.replace("\n", " | "))
    assert "表情包" in txt or "情绪" in txt


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}\n")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}\n")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {e}\n")
            failed += 1
    print(f"{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
