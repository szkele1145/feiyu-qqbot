"""表情包识别：感知哈希比对 + 视觉特征提取 + 梗文识别。

三层能力（全部纯本地，不依赖外部模型）：

1. **已知表情包比对**：对媒体库里的每张图算感知哈希（dHash+aHash），
   新图与库中某张图哈希接近时，直接报出「这张图库里有，叫 XXX」，
   并给出相似度百分比。
2. **视觉特征描述**：提取能描述表情包性格的特征——
   主体是否居中、是否有大眼/大嘴、配色是否夸张、是否圆脸、
   背景是否透明、文字占比多少，组合成一句人能读的描述。
3. **梗文/文字识别**：OCR 出的文字直接作为表情包的「梗」，
   并对常见网络梗做特征词匹配（如"笑死""awsl""我裂开了"等）。

用法::

    from feiyu_bot.vision.sticker import StickerMatcher
    matcher = StickerMatcher(media_library)
    info = matcher.match(image_path)
    print(info.to_text())
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("feiyu.sticker")

#: 常见网络梗特征词 -> 归类
MEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "笑死/崩溃": ("笑死", "哈哈", "哈哈哈", "awsl", "笑不活了", "绷不住", "蚌埠住了"),
    "无语/无语了": ("无语", "服了", "离谱", "栓Q", "yysy", "我不理解"),
    "震惊/疑问": ("？？", "???", "什么鬼", "离谱", "震惊", "啊这", "我靠"),
    "疲惫/摆烂": ("摆烂", "累了", "不想动", "躺平", "麻了", "我裂开", "我碎了"),
    "生气/暴躁": ("气死", "滚", "烦", "怒", "别烦我", "烦死了"),
    "可怜/委屈": ("呜呜", "可怜", "委屈", "555", "QAQ", "抱抱"),
    "点赞/认同": ("好", "牛", "厉害", "赞", "同意", "确实", "有道理"),
    "拒绝/否定": ("不", "拒绝", "不要", "别", "算了吧", "我不"),
    "疑问/求解": ("怎么办", "怎么办", "?", "？", "求", "帮", "如何"),
}

#: 表情包常见情绪词（文件名里带这些也算线索）
EMOJI_HINTS: tuple[str, ...] = (
    "笑", "哭", "怒", "惊", "怕", "爱", "亲", "死", "困", "饿",
    "开心", "难过", "无语", "尴尬", "得意", "疑惑", "冷漠", "生气",
)


@dataclass
class StickerInfo:
    """表情包识别结果。"""

    matched: bool = False
    matched_name: str = ""
    similarity: float = 0.0
    features: list[str] = field(default_factory=list)
    mood: str = ""
    meme_text: str = ""
    known_meme: str = ""
    is_sticker: bool = False
    detail: str = ""

    def to_text(self) -> str:
        """转成给语言模型看的文字描述。"""
        lines: list[str] = []
        if self.matched:
            lines.append(
                f"这张图在表情库里见过：{self.matched_name}"
                f"（相似度 {self.similarity:.0%}）"
            )
        if self.is_sticker:
            lines.append("判定为表情包")
        if self.mood:
            lines.append(f"情绪：{self.mood}")
        if self.known_meme:
            lines.append(f"网络梗类型：{self.known_meme}")
        if self.meme_text:
            lines.append(f"图上文字：{self.meme_text}")
        if self.features:
            lines.append("视觉特征：" + "、".join(self.features))
        if self.detail:
            lines.append(self.detail)
        return "\n".join(lines)


# ---------------------------------------------------------------- 感知哈希


def _prepare_for_hash(img):
    """算哈希前先做规范化处理。

    表情包多是带透明通道的 PNG：直接转 L 模式时透明区会被算成黑色，
    导致同一张图在不同打开方式下哈希不一致。这里统一合成到白底。
    """
    from PIL import Image

    try:
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            return Image.alpha_composite(bg, rgba).convert("RGB")
    except Exception:  # noqa: BLE001
        pass
    return img.convert("RGB")


def _dhash(img, size: int = 8) -> int:
    """差分哈希：比较相邻像素的明暗关系。"""
    from PIL import Image

    gray = _prepare_for_hash(img).convert("L").resize(
        (size + 1, size), Image.Resampling.LANCZOS)
    px = list(gray.getdata())
    bits = 0
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[offset + col] > px[offset + col + 1] else 0)
    return bits


def _ahash(img, size: int = 8) -> int:
    """均值哈希：与平均亮度比较。"""
    from PIL import Image

    small = _prepare_for_hash(img).convert("L").resize(
        (size, size), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for v in px:
        bits = (bits << 1) | (1 if v > avg else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class StickerMatcher:
    """表情包匹配器。持有媒体库里的图片哈希索引。"""

    def __init__(self, media: Any = None):
        self.media = media
        self._index: list[tuple[str, int, int]] = []  # (name, dhash, ahash)
        self._loaded = False

    # -------------------------------------------------- 索引

    def build_index(self, force: bool = False) -> int:
        """扫描媒体库里的图片，建立哈希索引。"""
        if self._loaded and not force:
            return len(self._index)
        self._index.clear()
        if self.media is None:
            self._loaded = True
            return 0
        try:
            items = self.media.list("image")
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            name = str(it.get("name") or it.get("path") or "")
            path = self._resolve_path(it)
            if not path:
                continue
            try:
                from PIL import Image

                with Image.open(path) as img:
                    img.load()
                    self._index.append((name, _dhash(img), _ahash(img)))
            except Exception:  # noqa: BLE001
                continue
        self._loaded = True
        log.info("表情包索引: %d 张", len(self._index))
        return len(self._index)

    def _resolve_path(self, item: dict[str, Any]) -> Path | None:
        """把 summary 里的文件名还原成真实路径。"""
        if self.media is None:
            return None
        name = str(item.get("name") or item.get("path") or "")
        if not name:
            return None
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            cand = Path(str(self.media.image_dir)) / f"{name}{ext}"
            if cand.is_file():
                return cand
        try:
            raw = self.media.get(str(item.get("id")))
            if raw is not None and getattr(raw, "path", None):
                p = Path(raw.path)
                if p.is_file():
                    return p
        except Exception:  # noqa: BLE001
            pass
        return None

    # -------------------------------------------------- 匹配

    def match(self, image_path: str | Path, ocr_text: str = "") -> StickerInfo:
        """识别一张图。"""
        info = StickerInfo()
        path = Path(image_path)
        if not path.is_file():
            info.detail = "文件不存在"
            return info

        try:
            from PIL import Image

            with Image.open(path) as img:
                img.load()
                rgb = img.convert("RGB")
                info = self._analyze(rgb, path)
        except Exception as exc:  # noqa: BLE001
            info.detail = f"分析失败：{exc}"
            return info

        if ocr_text:
            info.meme_text = ocr_text.strip()
        self._classify_meme(info)
        self._match_known(info, rgb=path)
        return info

    def _analyze(self, img, path: Path) -> StickerInfo:
        """提取视觉特征。"""
        from PIL import Image

        info = StickerInfo()
        w, h = img.size
        if w <= 0 or h <= 0:
            return info

        aspect = w / h
        if 0.75 <= aspect <= 1.35:
            info.features.append("接近正方形")
        elif aspect > 1.8:
            info.features.append("横幅")
        elif aspect < 0.55:
            info.features.append("竖图")

        # 透明背景检测（表情包常见 PNG 透明底）
        if path.suffix.lower() == ".png":
            try:
                if img.mode in ("RGBA", "LA") or (
                    img.mode == "P" and "transparency" in img.info
                ):
                    rgba = img.convert("RGBA")
                    alpha = rgba.getchannel("A")
                    hist = alpha.histogram()
                    transparent = hist[0]
                    total = w * h
                    if transparent / total > 0.35:
                        info.features.append("背景透明（抠图）")
                    elif transparent / total > 0.10:
                        info.features.append("边缘有透明区域")
            except Exception:  # noqa: BLE001
                pass

        # 主体居中度：看中心区域的边缘密度 vs 整图
        try:
            from ..vision.pipeline import _analyze_structure  # type: ignore  # noqa: PLC0415

            full = _analyze_structure(img)
            fd = float(full.get("edge_density") or 0.0)
            if fd > 0.005:
                cw, ch = int(w * 0.5), int(h * 0.5)
                center = img.crop((int(w * 0.25), int(h * 0.25),
                                   int(w * 0.25) + cw, int(h * 0.25) + ch))
                cd = float(_analyze_structure(center).get("edge_density") or 0.0)
                if cd / max(fd, 1e-6) > 1.35:
                    info.features.append("主体集中在画面中央")
                elif cd / max(fd, 1e-6) < 0.5:
                    info.features.append("主体偏向画面边缘")
        except Exception:  # noqa: BLE001
            pass

        # 夸张配色：饱和度高
        try:
            from ..vision.pipeline import _analyze_structure  # type: ignore  # noqa: PLC0415

            color = float(_analyze_structure(img).get("colorfulness") or 0.0)
            if color > 0.20:
                info.features.append("配色夸张鲜艳")
            elif color < 0.05:
                info.features.append("配色偏素")
        except Exception:  # noqa: BLE001
            pass

        # 圆形主体检测（很多表情包是圆脸）
        if self._has_round_subject(img):
            info.features.append("有圆形/椭圆主体（像大头贴或圆脸）")

        # 表情包判定
        info.is_sticker = self._looks_like_sticker(info, aspect)
        info.mood = self._guess_mood(img, path)
        if info.mood:
            info.features.append(f"情绪偏{info.mood}")
        return info

    def _has_round_subject(self, img) -> bool:
        """粗略检测圆形主体：中心区域亮度/饱和度与边缘差异大。"""
        try:
            from PIL import Image, ImageFilter

            w, h = img.size
            if w < 40 or h < 40:
                return False
            gray = img.convert("L").resize((64, 64), Image.Resampling.LANCZOS)
            px = list(gray.getdata())
            # 中心 40% 区域的平均亮度 vs 四周
            center_vals = []
            edge_vals = []
            for y in range(64):
                for x in range(64):
                    v = px[y * 64 + x]
                    if 12 <= x < 52 and 12 <= y < 52:
                        center_vals.append(v)
                    elif x < 10 or x > 53 or y < 10 or y > 53:
                        edge_vals.append(v)
            if not center_vals or not edge_vals:
                return False
            c = sum(center_vals) / len(center_vals)
            e = sum(edge_vals) / len(edge_vals)
            return abs(c - e) > 38
        except Exception:  # noqa: BLE001
            return False

    def _looks_like_sticker(self, info: StickerInfo, aspect: float) -> bool:
        """综合特征判断是否表情包。"""
        joined = " ".join(info.features)
        strong = 0
        if "背景透明（抠图）" in joined:
            strong += 2
        if "主体集中在画面中央" in joined:
            strong += 1
        if "配色夸张鲜艳" in joined:
            strong += 1
        if "有圆形/椭圆主体（像大头贴或圆脸）" in joined:
            strong += 1
        if 0.8 <= aspect <= 1.25:
            strong += 1
        return strong >= 3

    def _guess_mood(self, img, path: Path) -> str:
        """从文件名线索 + 图像特征猜情绪。"""
        stem = path.stem.lower()
        for hint in EMOJI_HINTS:
            if hint.lower() in stem:
                return hint
        # 纯色图片多为"冷漠/无语"
        try:
            from ..vision.pipeline import _analyze_structure  # type: ignore  # noqa: PLC0415

            if float(_analyze_structure(img).get("colorfulness") or 0.0) < 0.03:
                return "冷漠"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _classify_meme(self, info: StickerInfo) -> None:
        """按文字判断梗的类型。"""
        text = (info.meme_text or "").lower()
        if not text:
            return
        for label, keywords in MEME_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    info.known_meme = label
                    return

    def _match_known(self, info: StickerInfo, rgb=None) -> None:
        """与库里的图片比对哈希。"""
        self.build_index()
        if not self._index or rgb is None:
            return
        try:
            from PIL import Image

            with Image.open(rgb) as img:
                img.load()
                d = _dhash(img)
                a = _ahash(img)
        except Exception:  # noqa: BLE001
            return

        best_name = ""
        best_score = 0.0
        for name, hd, ha in self._index:
            dist = (_hamming(d, hd) + _hamming(a, ha)) / 2.0
            score = 1.0 - dist / 64.0
            if score > best_score:
                best_score = score
                best_name = name
        # 阈值：dHash 差异 <= 6（约 90% 相似）才算同一张
        if best_score >= 0.88:
            info.matched = True
            info.matched_name = best_name
            info.similarity = best_score
            info.is_sticker = True


def summarize_for_llm(info: StickerInfo, ocr_text: str = "") -> str:
    """把识别结果拼成一段可注入提示词的文字。"""
    parts: list[str] = []
    text = info.to_text()
    if text:
        parts.append(f"【表情包识别】\n{text}")
    return "\n".join(parts)
