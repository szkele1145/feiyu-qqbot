"""媒体库：语音库 + 图片库。

来源两类：

1. **手动导入** —— 用户把文件放进 assets/voices 与 assets/images，或在面板上传。
2. **联网搜索** —— Agent 主动在网上找图（公共图片源），存进图片库。

语音：支持 mp3/wav/ogg/amr/silk，使用前按需转成 QQ 需要的格式。
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("feiyu.media")

VOICE_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".amr", ".silk", ".aac", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


@dataclass
class MediaItem:
    """媒体库中的一个条目。"""

    id: str
    path: Path
    kind: str  # voice / image
    name: str = ""
    size: int = 0
    duration: float = 0.0
    tags: list[str] = field(default_factory=list)
    source: str = "local"  # local / web
    created_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.path.name,
            "kind": self.kind,
            "size": self.size,
            "duration": round(self.duration, 2),
            "tags": self.tags,
            "source": self.source,
            "path": self.path.name,
            "created_at": self.created_at,
        }


class MediaLibrary:
    """语音库 + 图片库。"""

    def __init__(self, cfg: dict[str, Any]):
        self.voice_dir = Path(cfg.get("voice_dir", "assets/voices"))
        self.image_dir = Path(cfg.get("image_dir", "assets/images"))
        self.allow_online = bool(cfg.get("allow_online_search", True))
        self.max_voice_seconds = float(cfg.get("max_voice_seconds", 60))
        self.ffmpeg = str(cfg.get("ffmpeg", "ffmpeg"))
        self._items: dict[str, MediaItem] = {}

    # ------------------------------------------------------------ 初始化

    def init(self) -> None:
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.scan()

    def scan(self) -> int:
        """扫描目录建立索引。"""
        self._items.clear()
        for kind, directory, exts in (
            ("voice", self.voice_dir, VOICE_EXTS),
            ("image", self.image_dir, IMAGE_EXTS),
        ):
            if not directory.exists():
                continue
            for file in sorted(directory.iterdir()):
                if not file.is_file() or file.suffix.lower() not in exts:
                    continue
                item = MediaItem(
                    id=_file_id(file),
                    path=file,
                    kind=kind,
                    name=file.stem,
                    size=file.stat().st_size,
                    source="local",
                    created_at=file.stat().st_mtime,
                )
                if kind == "voice":
                    item.duration = self.probe_duration(file)
                self._items[item.id] = item
        log.info("媒体库索引: %d 项", len(self._items))
        return len(self._items)

    # ------------------------------------------------------------ 导入

    def import_file(self, src: Path, kind: str = "auto", tags: list[str] | None = None) -> MediaItem:
        """导入本地文件到库。"""
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError(src)
        suffix = src.suffix.lower()

        if kind == "auto":
            kind = "voice" if suffix in VOICE_EXTS else "image" if suffix in IMAGE_EXTS else "image"

        target_dir = self.voice_dir if kind == "voice" else self.image_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / src.name
        if target.resolve() != src.resolve():
            shutil.copy2(src, target)

        item = MediaItem(
            id=_file_id(target),
            path=target,
            kind=kind,
            name=target.stem,
            size=target.stat().st_size,
            tags=tags or [],
            source="local",
            created_at=target.stat().st_mtime,
        )
        if kind == "voice":
            item.duration = self.probe_duration(target)
        self._items[item.id] = item
        log.info("已导入 %s: %s", kind, target.name)
        return item

    def import_dir(self, directory: Path, kind: str = "auto") -> list[MediaItem]:
        """批量导入目录。"""
        out: list[MediaItem] = []
        for file in sorted(Path(directory).iterdir()):
            if file.is_file():
                try:
                    out.append(self.import_file(file, kind))
                except Exception as exc:  # noqa: BLE001
                    log.warning("导入失败 %s: %s", file, exc)
        return out

    def remove(self, item_id: str) -> bool:
        item = self._items.pop(item_id, None)
        if item is None:
            return False
        try:
            item.path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("删除失败 %s: %s", item.path, exc)
            return False
        return True

    # ------------------------------------------------------------ 查询

    def list(self, kind: str = "") -> list[dict[str, Any]]:
        items = [i for i in self._items.values() if not kind or i.kind == kind]
        return [i.summary() for i in sorted(items, key=lambda x: -x.created_at)]

    def find(self, keyword: str, kind: str = "") -> list[MediaItem]:
        """按名称/标签/文件名模糊查找。"""
        kw = keyword.lower().strip()
        if not kw:
            return []
        result = []
        for item in self._items.values():
            if kind and item.kind != kind:
                continue
            hay = " ".join([item.name, item.path.name, " ".join(item.tags)]).lower()
            if kw in hay:
                result.append(item)
        return result

    def get(self, item_id: str) -> MediaItem | None:
        return self._items.get(item_id)

    # ------------------------------------------------------------ 语音处理

    def ffmpeg_available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None

    def probe_duration(self, path: Path) -> float:
        """探测音频时长（秒）。"""
        if not self.ffmpeg_available():
            return 0.0
        try:
            proc = subprocess.run(
                [self.ffmpeg, "-i", str(path)],
                capture_output=True, text=True, timeout=20,
            )
            # ffprobe 信息在 stderr
            for line in (proc.stderr or "").splitlines():
                if "Duration:" in line:
                    ts = line.split("Duration:")[1].split(",")[0].strip()
                    h, m, s = ts.split(":")
                    return int(h) * 3600 + int(m) * 60 + float(s)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        return 0.0

    def convert_voice(self, src: Path, out_format: str = "silk", target: Path | None = None) -> Path | None:
        """把语音转成指定格式（QQ 群语音需要 silk / amr）。"""
        if not self.ffmpeg_available():
            log.warning("未找到 ffmpeg，无法转码")
            return None
        out = target or src.with_suffix(f".{out_format}")
        try:
            cmd = [self.ffmpeg, "-y", "-i", str(src)]
            if out_format == "silk":
                # ffmpeg 原生不支持 silk，需借助外部编码器；这里先转 pcm 再由 silk 编码器处理
                cmd += ["-ar", "16000", "-ac", "1", "-f", "pcm_s16le", str(out.with_suffix(".pcm"))]
            elif out_format == "amr":
                cmd += ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb", str(out)]
            else:
                cmd += [str(out)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                log.warning("转码失败: %s", (proc.stderr or "")[-300:])
                return None
            return out
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("转码异常: %s", exc)
            return None

    def clip_voice(self, src: Path, seconds: float, target: Path) -> Path | None:
        """截取语音前 N 秒（QQ 语音有时长限制）。"""
        if not self.ffmpeg_available():
            return None
        try:
            proc = subprocess.run(
                [self.ffmpeg, "-y", "-i", str(src), "-t", str(seconds),
                 "-ar", "16000", "-ac", "1", str(target)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                return None
            return target
        except (subprocess.SubprocessError, OSError):
            return None

    # ------------------------------------------------------------ 联网搜图

    async def search_and_download(
        self,
        keyword: str,
        limit: int = 3,
        *,
        image_api: str = "",
        image_api_key: str = "",
    ) -> list[MediaItem]:
        """联网搜索图片并下载入库。

        未配置图片 API 时使用公共免密图床源。
        """
        if not self.allow_online:
            log.info("已禁用联网搜图")
            return []

        urls: list[str] = []
        if image_api:
            urls = await self._search_via_api(keyword, limit, image_api, image_api_key)
        if not urls:
            urls = await self._search_public(keyword, limit)
        if not urls:
            return []

        items: list[MediaItem] = []
        for url in urls[:limit]:
            item = await self._download_image(url, keyword)
            if item:
                items.append(item)
        return items

    async def _search_via_api(
        self, keyword: str, limit: int, api: str, api_key: str
    ) -> list[str]:
        try:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    api,
                    params={"q": keyword, "count": limit, "format": "json"},
                    headers={"Authorization": f"Bearer {api_key}"} if image_api_key else {},
                )
                resp.raise_for_status()
                data = resp.json()
            items = data.get("data") or data.get("results") or []
            return [str(i.get("url", "")) for i in items if isinstance(i, dict) and i.get("url")]
        except Exception as exc:  # noqa: BLE001
            log.warning("图片 API 搜索失败: %s", exc)
            return []

    async def _search_public(self, keyword: str, limit: int) -> list[str]:
        """公共源搜索（无需 key）。"""
        try:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://api.pexels.com/v1/search",
                    params={"query": keyword, "per_page": limit},
                    headers={"Authorization": ""},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return [
                        str(p.get("src", {}).get("medium", ""))
                        for p in data.get("photos", [])
                        if p.get("src")
                    ]
        except Exception as exc:  # noqa: BLE001
            log.debug("Pexels 搜索失败: %s", exc)

        # 兜底：Wikimedia Commons
        try:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query", "format": "json", "generator": "search",
                        "gsrsearch": keyword, "gsrlimit": limit,
                        "prop": "imageinfo", "iiprop": "url",
                    },
                    headers={"User-Agent": "feiyu-qqbot/0.1 (contact: local)"},
                )
                resp.raise_for_status()
                data = resp.json()
            pages = (data.get("query") or {}).get("pages") or {}
            urls = []
            for page in pages.values():
                info = (page.get("imageinfo") or [{}])[0]
                if info.get("url"):
                    urls.append(str(info["url"]))
            return urls
        except Exception as exc:  # noqa: BLE001
            log.warning("公共图源搜索失败: %s", exc)
            return []

    async def _download_image(self, url: str, keyword: str) -> MediaItem | None:
        try:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
            if len(data) > 20 * 1024 * 1024:
                log.warning("图片过大，跳过: %s", url)
                return None
            suffix = mimetypes.guess_extension(resp.headers.get("content-type", "")) or ".jpg"
            if suffix not in IMAGE_EXTS:
                suffix = ".jpg"
            digest = hashlib.sha1(data).hexdigest()[:10]
            safe_kw = "".join(c for c in keyword if c.isalnum() or c in "-_")[:24] or "img"
            target = self.image_dir / f"web_{safe_kw}_{digest}{suffix}"
            target.write_bytes(data)
            item = MediaItem(
                id=_file_id(target),
                path=target,
                kind="image",
                name=target.stem,
                size=len(data),
                tags=[keyword] if keyword else [],
                source="web",
                created_at=target.stat().st_mtime,
                meta={"origin": url},
            )
            self._items[item.id] = item
            log.info("已下载图片: %s", target.name)
            return item
        except Exception as exc:  # noqa: BLE001
            log.warning("图片下载失败 %s: %s", url, exc)
            return None


def _file_id(path: Path) -> str:
    """用文件内容 hash 做 id。"""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return hashlib.sha1(str(path).encode()).hexdigest()[:16]
