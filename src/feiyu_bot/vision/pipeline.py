"""纯本地识图管线：OCR（多引擎）+ 图像结构化分析 → 结构化文本描述。

设计原则：**不依赖任何第三方视觉模型**。纯语言模型（如 DeepSeek）也能识图——
把图片转成「文字画像」后注入上下文即可。

管线分三层：

1. **OCR 层**：提取图中文字（截图/表情包/梗图的关键）。
2. **结构层**：像素统计分析——主色调、构图分区、亮度布局、明暗对比等，
   模仿人类「先扫一眼」的粗描。
3. **语义组装层**：把 1+2 的结果组装成自然语言描述，注入纯语言模型。

本地 VLM 为可选项（配置 ``vision.vlm`` 时启用）；没有则纯文本管线照跑。
"""

from __future__ import annotations

import asyncio
import base64
import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("feiyu.vision")


# ---------------------------------------------------------------- 数据结构


@dataclass
class ImageReport:
    """一张图的本地分析结果。"""

    ok: bool = False
    error: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    format: str = ""
    ocr_text: str = ""
    ocr_engine: str = ""
    palette: list[tuple[str, float]] = field(default_factory=list)
    brightness: float = 0.0
    contrast: float = 0.0
    colorfulness: float = 0.0
    edge_density: float = 0.0
    face_count: int = 0
    grid_regions: list[tuple[int, int, int, int]] = field(default_factory=list)
    is_screenshot_like: bool = False
    vlm_description: str = ""
    #: 表情包识别结果（vision/sticker.py 的 StickerInfo）
    sticker: Any = None

    def scene_guess(self) -> str:
        """综合各项指标推断这张图大概率是什么。

        纯本地、无模型，靠版式/色彩/边缘/文字/人脸等信号做启发式判定。
        目标：给语言模型一句人话结论，而不是一堆百分比。
        """
        if not self.ok:
            return ""

        has_text = bool((self.ocr_text or "").strip())
        text_len = len((self.ocr_text or "").strip())
        faces = self.face_count or 0
        grid = len(self.grid_regions or [])
        edge = self.edge_density or 0.0
        color = self.colorfulness or 0.0
        bright = self.brightness or 0.0
        contrast = self.contrast or 0.0
        aspect = (self.width / self.height) if self.height else 1.0

        # 1) 聊天/网页截图：等宽文字块 + 大量文字 + 竖长版式
        if self.is_screenshot_like or (has_text and text_len > 60 and 0.3 < aspect < 0.75):
            return "大概率是一张手机或电脑截图（含较多界面文字）"

        # 2) 文档/表格：文字多、色彩低、边缘锐利
        if has_text and text_len > 40 and color < 0.2:
            if grid >= 2:
                return "大概率是文档或表格截图（规整分区 + 成片文字）"
            return "大概率是纯文字内容的截图或扫描件"

        # 3) 表情包：方形/近方形、色彩饱和、文字少、细节中等
        if 0.8 < aspect < 1.25 and color > 0.35 and text_len < 20 and 0.05 < edge < 0.35:
            return "大概率是一张表情包 / 梗图（近方形、色彩饱和、文字很少）"

        # 4) 有人脸：照片
        if faces == 1:
            return "这是一张含 1 个人物的照片"
        if faces > 1:
            return f"这是一张含 {faces} 个人物的照片"

        # 5) 九宫格/多图拼接
        if grid >= 4:
            return f"大概率是多图拼接或九宫格图（检测到 {grid} 个规则分区）"

        # 6) 图表：低饱和 + 规则分区 + 中等文字
        if grid >= 2 and color < 0.35 and has_text:
            return "大概率是一张图表或数据可视化图（规整分区 + 标注文字）"

        # 7) 风景/实拍：高细节、无文字、无脸
        if edge > 0.2 and text_len < 10 and color > 0.3:
            if bright > 0.6:
                return "大概率是一张明亮的实景照片（户外/风光类）"
            if bright < 0.3:
                return "大概率是一张偏暗的实景照片（夜景或室内）"
            return "大概率是一张实景照片"

        # 8) 线稿/极简
        if color < 0.12 and edge < 0.1:
            return "大概率是线稿、图标或极简设计图"

        # 9) 纯色/空白
        if edge < 0.02 and contrast < 0.1:
            return "画面几乎是纯色或空白，没有实质内容"

        if has_text:
            return "画面以文字为主，属于图文类内容"
        return "一张普通图片（无明显文字与人脸）"

    def to_prompt_text(self) -> str:
        """组装成注入纯语言模型的文本画像。"""
        if not self.ok:
            return f"[图片分析失败: {self.error}]"

        lines: list[str] = ["【图片本地分析报告】"]

        # ---- 先给结论：这张图是什么（比罗列参数有用得多）----
        verdict = self.scene_guess()
        if verdict:
            lines.append(f"场景判断: {verdict}")

        # ---- 表情包专项（库内比对 + 特征 + 梗文）----
        if self.sticker is not None:
            try:
                st = self.sticker.to_text()
                if st:
                    lines.append("表情包识别:")
                    lines.extend("  " + ln for ln in st.splitlines())
            except Exception:  # noqa: BLE001
                pass

        # 基础属性
        aspect = self.width / self.height if self.height else 0
        orient = (
            "竖图" if aspect < 0.92 else "横图" if aspect > 1.08 else "接近方形"
        )
        lines.append(
            f"尺寸: {self.width}x{self.height}px（{orient}，{self.size_bytes // 1024}KB，{self.format}）"
        )

        # 色彩
        if self.palette:
            top = "、".join(
                f"{name}({pct:.0%})" for name, pct in self.palette[:5]
            )
            lines.append(f"主要颜色: {top}")
        lines.append(
            f"明暗: 平均亮度 {self.brightness:.0%}"
            f"（{'偏亮' if self.brightness > 0.6 else '偏暗' if self.brightness < 0.35 else '适中'}），"
            f"对比度 {self.contrast:.0%}"
        )
        if self.colorfulness > 0.35:
            lines.append("画面色彩饱和、元素丰富")
        elif self.colorfulness < 0.12:
            lines.append("画面接近单色/灰度，可能是极简图或线稿")

        # 结构
        if self.edge_density > 0.18:
            lines.append("边缘/细节密集（信息量较大，可能是实拍照片或复杂截图）")
        elif self.edge_density < 0.04:
            lines.append("画面非常简洁，大面积留白")
        if self.face_count:
            lines.append(f"检测到人脸: {self.face_count} 张")
        if self.grid_regions:
            lines.append(
                f"检测到 {len(self.grid_regions)} 个规则图文分区（可能是聊天截图、表情包九宫格或多图拼接）"
            )
        if self.is_screenshot_like:
            lines.append("版式接近手机截图（顶部状态栏/底栏或等宽文字块）")

        # OCR
        if self.ocr_text.strip():
            ocr = self.ocr_text.strip()
            if len(ocr) > 600:
                ocr = ocr[:600] + "…（截断）"
            lines.append(f"图中文字（OCR/{self.ocr_engine}）:\n{ocr}")
        else:
            lines.append("图中未识别到文字（可能是纯图片/照片/表情包）")

        if self.vlm_description:
            lines.append(f"本地视觉模型描述: {self.vlm_description}")

        return "\n".join(lines)


# ---------------------------------------------------------------- 图像工具


def _load_pil():
    try:
        from PIL import Image  # noqa: PLC0415

        return Image
    except ImportError:  # pragma: no cover
        log.error("Pillow 未安装，无法进行本地图像分析")
        return None


def _dominant_colors(img, top: int = 6) -> list[tuple[str, float]]:
    """量化取主色，返回 (颜色名, 占比)。"""
    try:
        small = img.convert("RGB").resize((64, 64))
        quantized = small.quantize(colors=top, method=2)
        palette = quantized.getpalette() or []
        counts = sorted(quantized.getcolors() or [], key=lambda c: -c[0])
        total = sum(c for c, _ in counts) or 1

        result: list[tuple[str, float]] = []
        for count, idx in counts:
            base = idx * 3
            rgb = tuple(palette[base: base + 3]) if len(palette) >= base + 3 else (0, 0, 0)
            result.append((_color_name(rgb), count / total))
        return result
    except Exception as exc:  # noqa: BLE001
        log.debug("主色提取失败: %s", exc)
        return []


def _color_name(rgb: tuple[int, int, int]) -> str:
    """把 RGB 归类成中文颜色名。"""
    r, g, b = rgb
    maxc, minc = max(rgb), min(rgb)
    if maxc - minc < 24:
        if maxc > 210:
            return "白色"
        if maxc > 140:
            return "浅灰"
        if maxc > 70:
            return "灰色"
        return "黑色"
    # 色相判断
    import colorsys  # noqa: PLC0415

    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    deg = h * 360
    if s < 0.2:
        return "灰白"
    if v < 0.25:
        return "深色"
    if deg < 15 or deg >= 345:
        return "红色"
    if deg < 40:
        return "橙色"
    if deg < 70:
        return "黄色"
    if deg < 165:
        return "绿色"
    if deg < 200:
        return "青色"
    if deg < 255:
        return "蓝色"
    if deg < 290:
        return "紫色"
    if deg < 345:
        return "粉紫色"
    return "红色"


def _analyze_structure(img) -> dict[str, Any]:
    """像素级结构统计：亮度、对比度、饱和度、边缘密度、分区。"""
    stats: dict[str, Any] = {
        "brightness": 0.0,
        "contrast": 0.0,
        "colorfulness": 0.0,
        "edge_density": 0.0,
        "grid_regions": [],
        "is_screenshot_like": False,
    }
    try:
        import numpy as np  # noqa: PLC0415
    except ImportError:
        # 没有 numpy 时退化为纯 Pillow 统计
        try:
            gray = img.convert("L").resize((128, 128))
            pixels = list(gray.getdata())
            if pixels:
                mean = sum(pixels) / len(pixels) / 255
                var = sum((p - mean * 255) ** 2 for p in pixels) / len(pixels)
                stats["brightness"] = mean
                stats["contrast"] = (var ** 0.5) / 128
        except Exception as exc:  # noqa: BLE001
            log.debug("结构统计失败: %s", exc)
        return stats

    try:
        rgb = np.asarray(img.convert("RGB").resize((160, 160)), dtype=float)
        gray = np.asarray(img.convert("L").resize((160, 160)), dtype=float)

        stats["brightness"] = float(gray.mean() / 255)
        stats["contrast"] = float(gray.std() / 128)
        rg = rgb[:, :, 0] - rgb[:, :, 1]
        yb = 0.5 * (rgb[:, :, 0] + rgb[:, :, 1]) - rgb[:, :, 2]
        stats["colorfulness"] = float(
            (np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))
            / 160
        )

        # 边缘密度（Sobel 近似）
        gx = np.abs(np.diff(gray, axis=1)).mean() if gray.shape[1] > 1 else 0
        gy = np.abs(np.diff(gray, axis=0)).mean() if gray.shape[0] > 1 else 0
        stats["edge_density"] = float((gx + gy) / 2 / 64)

        # 规则分区检测：水平方向上的强边缘（分隔线）
        row_edges = np.abs(np.diff(gray, axis=0)).mean(axis=1)
        col_edges = np.abs(np.diff(gray, axis=1)).mean(axis=0)
        thresh = max(18.0, float(row_edges.mean() + row_edges.std() * 2))
        rows = np.where(row_edges > thresh)[0]
        cols = np.where(col_edges > thresh)[0]

        regions: list[tuple[int, int, int, int]] = []
        row_bands = _bands(rows, 160)
        col_bands = _bands(cols, 160)
        if row_bands or col_bands:
            for i in range(max(len(row_bands) - 1, 0)):
                regions.append((0, row_bands[i], 160, row_bands[i + 1]))
            for j in range(max(len(col_bands) - 1, 0)):
                regions.append((col_bands[j], 0, col_bands[j + 1], 160))
        stats["grid_regions"] = regions[:8]

        # 截图特征：存在多条水平分隔线，或上下边缘亮度突变明显
        top = float(gray[:6].mean())
        bottom = float(gray[-6:].mean())
        stats["is_screenshot_like"] = bool(
            len(row_bands) >= 2 or abs(top - bottom) > 60
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("numpy 结构统计失败: %s", exc)
    return stats


def _bands(indices, size: int, min_gap: int = 12) -> list[int]:
    """把连续索引聚成带，返回每带起始位置。"""
    if len(indices) == 0:
        return []
    bands: list[int] = []
    start = prev = int(indices[0])
    for idx in indices[1:]:
        i = int(idx)
        if i - prev > min_gap:
            bands.append(start)
            start = i
        prev = i
    bands.append(start)
    return [b for b in bands if 0 <= b < size]


# ---------------------------------------------------------------- OCR


def _run_ocr(img, engine: str) -> tuple[str, str]:
    """执行 OCR，返回 (文本, 引擎名)。引擎不可用时返回空串。"""
    Image = _load_pil()
    if Image is None:
        return "", ""

    if engine in ("auto", ""):
        engine = _pick_ocr_engine()

    if engine == "none":
        return "", ""

    try:
        if engine == "windows":
            return _ocr_windows(img), "windows"
        if engine in ("paddle", "rapid"):
            script = _ocr_python_script(engine)
            if not script:
                return "", ""
            out = _ocr_via_python(script, img)
            return out, engine
    except Exception as exc:  # noqa: BLE001
        log.debug("OCR(%s) 失败: %s", engine, exc)
    return "", ""


def _pick_ocr_engine() -> str:
    """按可用性挑选 OCR 引擎。"""
    if shutil.which("python") or shutil.which("python3"):
        if Path.home().joinpath(".dsh", "picturereader").exists():
            return "rapid"
    if _ocr_python_script("rapid"):
        return "rapid"
    if sys_platform_is_windows():
        return "windows"
    return "none"


def sys_platform_is_windows() -> bool:
    import sys  # noqa: PLC0415

    return sys.platform.startswith("win")


def _ocr_python_script(engine: str) -> Path | None:
    """查找 OCR 用的 Python 脚本。"""
    root = Path(__file__).resolve().parents[3]
    for candidate in (root / "tools" / "ocr_local.py", root / "tools" / "ocr.py"):
        if candidate.exists():
            return candidate
    return None


def _ocr_via_python(script: Path, img) -> str:
    """调用独立 Python 解释器执行 OCR（隔离依赖，避免污染主环境）。"""
    exe = _ocr_python_exe()
    if not exe:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.convert("RGB").save(tmp.name)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            [str(exe), str(script), tmp_path],
            capture_output=True, text=True, timeout=60,
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("外部 OCR 调用失败: %s", exc)
        return ""
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _ocr_python_exe() -> Path | None:
    """查找带 OCR 依赖的解释器。"""
    env = os_environ("FEIYU_OCR_PYTHON")
    if env and Path(env).exists():
        return Path(env)
    for name in ("ocr_venv", "rapid_venv", "paddle_venv"):
        for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
            exe = base / name / ("Scripts/python.exe" if sys_platform_is_windows() else "bin/python")
            if exe.exists():
                return exe
    return None


def os_environ(key: str) -> str:
    import os  # noqa: PLC0415

    return os.environ.get(key, "")


def _ocr_windows(img) -> str:
    """调用 Windows 内置 OCR（通过 PowerShell 调用 WinRT）。"""
    Image = _load_pil()
    if Image is None:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.convert("RGB").save(tmp.name)
        tmp_path = tmp.name
    script = Path(__file__).resolve().parent / "_win_ocr.ps1"
    try:
        if not script.exists():
            return ""
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), tmp_path,
            ],
            capture_output=True, text=True, timeout=45,
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("Windows OCR 失败: %s", exc)
        return ""
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------- 主入口


class VisionPipeline:
    """本地识图管线。"""

    def __init__(self, cfg: dict[str, Any]):
        self.enabled = bool(cfg.get("enabled", True))
        self.ocr_engine = str(cfg.get("ocr_engine", "auto"))
        self.use_local_ocr = bool(cfg.get("use_local_ocr", True))
        self.max_bytes = int(float(cfg.get("max_image_mb", 10)) * 1024 * 1024)
        self.detail = str(cfg.get("detail", "auto"))
        self.prompt = str(cfg.get("prompt", ""))
        self.download_timeout = float(cfg.get("download_timeout", 30))
        self.vlm_cfg = dict(cfg.get("vlm", {}) or {})

    # ------------------------------------------------------------ 分析

    def analyze(self, data: bytes, filename: str = "image",
                source_path: str | Path | None = None) -> ImageReport:
        """分析图片字节，返回结构化报告。"""
        report = ImageReport(size_bytes=len(data))
        Image = _load_pil()
        if Image is None:
            report.error = "Pillow 未安装"
            return report

        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:  # noqa: BLE001
            report.error = f"图片解析失败: {exc}"
            return report

        report.width, report.height = img.size
        report.format = (img.format or "").upper()
        report.ok = True

        try:
            report.palette = _dominant_colors(img)
        except Exception as exc:  # noqa: BLE001
            log.debug("调色分析失败: %s", exc)

        struct = _analyze_structure(img)
        report.brightness = struct["brightness"]
        report.contrast = struct["contrast"]
        report.colorfulness = struct["colorfulness"]
        report.edge_density = struct["edge_density"]
        report.grid_regions = struct["grid_regions"]
        report.is_screenshot_like = struct["is_screenshot_like"]

        report.face_count = _count_faces(img)

        if self.use_local_ocr:
            text, engine = _run_ocr(img, self.ocr_engine)
            report.ocr_text = text
            report.ocr_engine = engine

        # 表情包识别：库内比对 + 视觉特征 + 梗文归类
        try:
            from .sticker import StickerMatcher  # noqa: PLC0415

            matcher = getattr(self, "_sticker_matcher", None)
            if matcher is None:
                matcher = StickerMatcher(getattr(self, "media", None))
                self._sticker_matcher = matcher
            target = source_path or (filename if Path(filename).is_file() else None)
            if target:
                report.sticker = matcher.match(target, report.ocr_text)
        except Exception as exc:  # noqa: BLE001
            log.debug("表情包识别失败: %s", exc)

        return report

    def analyze_file(self, path: Path) -> ImageReport:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            rep = ImageReport()
            rep.error = f"读取失败: {exc}"
            return rep
        return self.analyze(data, Path(path).name, source_path=path)

    # ------------------------------------------------------------ 与 LLM 协作

    async def describe_for_llm(self, data: bytes, question: str = "") -> str:
        """给纯语言模型用：返回文字画像。

        若配置了本地 VLM，则优先用 VLM 描述并附加本地统计；
        否则完全靠本地管线 + 纯语言模型推理。
        """
        report = self.analyze(data)
        if not report.ok:
            return report.to_prompt_text()

        parts = [report.to_prompt_text()]

        vlm_text = await self._vlm_describe(data, question)
        if vlm_text:
            parts.append(f"本地视觉模型描述: {vlm_text}")

        return "\n\n".join(parts)

    async def _vlm_describe(self, data: bytes, question: str) -> str:
        """调用本地 VLM（Ollama / LM Studio 等 OpenAI 兼容端点）。"""
        if not self.vlm_cfg.get("enabled"):
            return ""
        base_url = str(self.vlm_cfg.get("base_url", "")).rstrip("/")
        if not base_url:
            return ""
        try:
            import httpx  # noqa: PLC0415
        except ImportError:
            return ""

        b64 = base64.b64encode(data).decode("ascii")
        mime = "image/png"
        payload = {
            "model": self.vlm_cfg.get("model", "qwen2.5vl"),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question or self.prompt or "描述这张图片"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            ],
            "max_tokens": int(self.vlm_cfg.get("max_tokens", 512)),
            "temperature": float(self.vlm_cfg.get("temperature", 0.2)),
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.vlm_cfg.get('api_key', '')}",
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                data_json = resp.json()
                return str(
                    ((data_json.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("本地 VLM 调用失败: %s", exc)
            return ""


def _count_faces(img) -> int:
    """检测人脸数量。

    优先用 OpenCV 的 Haar cascade（准确）；没有装 cv2 时退回到纯 PIL 的
    肤色区域聚类启发式，至少能识别出「明显有人」的图。
    """
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return _count_faces_pil(img)
    try:
        gray = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not cascade_path.exists():
            return _count_faces_pil(img)
        cascade = cv2.CascadeClassifier(str(cascade_path))
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        return len(faces)
    except Exception as exc:  # noqa: BLE001
        log.debug("人脸检测失败: %s", exc)
        return _count_faces_pil(img)


def _count_faces_pil(img) -> int:
    """纯 PIL 的肤色区域检测（无 OpenCV 时的兜底）。

    思路：找出「肤色像素」并按空间聚类成若干团，每团面积足够大就算一个人脸。
    精度不如 Haar cascade，但能区分「有人」和「没人」，让识图结论更可靠。
    """
    try:
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return 0

    try:
        small = img.convert("RGB").resize((96, 96))
        arr = np.asarray(small).astype(np.int16)
    except Exception:  # noqa: BLE001
        return 0

    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # 常见肤色范围：R 明显高于 G，G 明显高于 B，且亮度适中
    skin = (r > 95) & (g > 40) & (b > 20) & (r > g + 15) & (r > b + 20) & ((r - b) > 25)
    total = skin.size
    if total == 0:
        return 0
    ratio = float(skin.sum()) / total
    # 肤色占比太低基本没人；太高可能是整张肤色图（比如木头）
    if ratio < 0.02 or ratio > 0.75:
        return 0

    # 简单的连通块聚类（4 邻域 flood fill）
    ys, xs = np.nonzero(skin)
    if len(ys) == 0:
        return 0
    pixels = set(zip(ys.tolist(), xs.tolist()))
    min_area = max(12, int(total * 0.012))  # 每块至少占 1.2%
    clusters = 0
    seen: set[tuple[int, int]] = set()
    for seed in list(pixels):
        if seed in seen:
            continue
        stack = [seed]
        seen.add(seed)
        area = 0
        while stack:
            y, x = stack.pop()
            area += 1
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if (ny, nx) in pixels and (ny, nx) not in seen:
                    seen.add((ny, nx))
                    stack.append((ny, nx))
        if area >= min_area:
            clusters += 1
        if clusters >= 6:  # 最多报 6 张脸，再多就是误判
            break
    return clusters


async def _to_thread(fn, *args):  # noqa: ANN001
    return await asyncio.to_thread(fn, *args)
