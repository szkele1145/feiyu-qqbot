"""WebUI 面板：浏览器访问的配置与控制中心。

只用 Python 标准库实现（http.server），零额外依赖，打包体积小。

功能：

- 总览：连接状态、运行统计、预算、上下文、媒体库数量
- 配置：全量可编辑（敏感字段掩码回显，保存即加密落盘）
- 人格：切换当前人格
- 媒体库：语音/图片列表、导入、删除
- 识图：开关、OCR 引擎、本地 VLM 配置
- 保活：立即执行一次保活检查

安全：默认绑定 127.0.0.1；配置 token 时需带 ``?token=xxx`` 或 header。
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..logging_setup import get_logger

log = get_logger("feiyu.webui")


def _mask(value: str, keep: int = 4) -> str:
    """脱敏显示：保留头尾各 keep 位。"""
    s = str(value or "")
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}{'*' * max(4, len(s) - keep * 2)}{s[-keep:]}"


def _get_dotted(data: dict, dotted: str) -> Any:
    """按 ``a.b.c`` 点号路径读取嵌套字典。"""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node

UI_HTML = Path(__file__).parent / "static" / "index.html"


class WebUIController:
    """WebUI 控制器：持有 bot 引用并提供 API。"""

    def __init__(self, bot):
        self.bot = bot
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ API 实现

    def api_status(self, _query: dict) -> dict[str, Any]:
        return self.bot.status()

    def api_selfcheck(self, _query: dict) -> dict[str, Any]:
        """开跑前自检：把「为什么不工作」一次性说清楚。

        每条返回 ``{key, label, ok, detail, fix}``，前端直接渲染红绿清单。
        """
        cfg = self.bot.cfg
        vault = getattr(self.bot, "vault", None)
        checks: list[dict[str, Any]] = []

        def add(key: str, label: str, ok: bool, detail: str = "", fix: str = "") -> None:
            checks.append({"key": key, "label": label, "ok": bool(ok),
                           "detail": detail, "fix": fix})

        # ---- QQ 凭证 ----
        appid = str(_get_dotted(cfg.data, "qq.appid") or "")
        secret = str(_get_dotted(cfg.data, "qq.secret") or "")
        if not appid:
            add("qq.appid", "QQ AppID", False, "未填写",
                "到 QQ 开放平台 → 机器人 → 开发设置里复制 AppID，填到「设置 → QQ」")
        elif appid.startswith("ENC:"):
            add("qq.appid", "QQ AppID", False, "存储值仍是密文，未能解密",
                "点「设置」重新填一次 AppID 和 Secret 并保存（会自动加密落盘）")
        else:
            add("qq.appid", "QQ AppID", True, _mask(appid))

        if not secret:
            add("qq.secret", "QQ Secret", False, "未填写",
                "开放平台 → 开发设置 → AppSecret，填好后保存")
        elif secret.startswith("ENC:"):
            add("qq.secret", "QQ Secret", False, "存储值仍是密文，未能解密",
                "重新填一次并保存")
        else:
            add("qq.secret", "QQ Secret", True, _mask(secret))

        # ---- LLM ----
        base_url = str(_get_dotted(cfg.data, "llm.base_url") or "").strip()
        model = str(_get_dotted(cfg.data, "llm.model") or "").strip()
        api_key = str(_get_dotted(cfg.data, "llm.api_key") or "").strip()

        if not base_url:
            add("llm.base_url", "AI 接口地址", False, "未填写",
                "例如 https://api.deepseek.com/v1（注意结尾的 /v1）")
        elif not re.match(r"^https?://", base_url):
            add("llm.base_url", "AI 接口地址", False, f"格式不对：{base_url}",
                "必须以 http:// 或 https:// 开头")
        else:
            add("llm.base_url", "AI 接口地址", True, base_url)

        if not model:
            add("llm.model", "模型名称", False, "未填写",
                "填服务商给的模型 id，如 deepseek-chat；也可点「拉取列表」自动获取")
        else:
            add("llm.model", "模型名称", True, model)

        if not api_key:
            add("llm.api_key", "API Key", False, "未填写", "在服务商后台创建并填入")
        else:
            add("llm.api_key", "API Key", True, _mask(api_key))

        # ---- 目录可写 ----
        for key, label, path in (
            ("dir.images", "图片库目录", _get_dotted(cfg.data, "media.image_dir")),
            ("dir.voices", "语音库目录", _get_dotted(cfg.data, "media.voice_dir")),
            ("dir.logs", "日志目录", _get_dotted(cfg.data, "app.log_dir")),
        ):
            raw = str(path or "").strip()
            if not raw:
                add(key, label, False, "未配置", "在「设置」里填一个可写目录")
                continue
            try:
                p = Path(raw)
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                add(key, label, True, str(p))
            except OSError as exc:
                add(key, label, False, f"{raw}（{exc}）",
                    "换一个有写权限的目录，或手动创建并授权")

        # ---- 识图 ----
        try:
            from ..vision.pipeline import _pick_ocr_engine  # noqa: PLC0415

            engine = _pick_ocr_engine()
            add("vision.ocr", "识图 OCR 引擎", engine != "none",
                engine if engine != "none" else "没有可用的 OCR 引擎",
                "Windows 自带 OCR 一般可直接用；Linux 需装 paddle/rapid")
        except Exception as exc:  # noqa: BLE001
            add("vision.ocr", "识图 OCR 引擎", False, f"检测失败：{exc}",
                "检查 vision.ocr_engine 配置")

        # ---- 人格 ----
        try:
            pid = str(_get_dotted(cfg.data, "persona.current") or "")
            names = [p.get("id") for p in (self.bot.personas() or [])] \
                if hasattr(self.bot, "personas") else []
            ok = not names or pid in names
            add("persona.current", "当前人格", ok,
                pid or "未设置",
                "在「人格」页选一个可用人格")
        except Exception:  # noqa: BLE001
            add("persona.current", "当前人格", True, "无法校验（不影响运行）")

        # ---- 运行状态 ----
        try:
            st = self.bot.status() or {}
            gw = st.get("gateway") or {}
            running = bool(st.get("running"))
            connected = bool(gw.get("connected"))
            add("runtime", "机器人运行状态", running or connected,
                ("已连接" if connected else "运行中但未连上 QQ" if running else "未启动"),
                "点「启动」开始；连不上时看日志里的网关报错")
        except Exception:  # noqa: BLE001
            add("runtime", "机器人运行状态", True, "无法读取状态")

        total = len(checks)
        passed = sum(1 for c in checks if c["ok"])
        return {"ok": passed == total, "passed": passed, "total": total,
                "checks": checks}

    def api_vision_learn(self, _query: dict, body: dict) -> dict[str, Any]:
        """把本次识图上传的图片保存到媒体库（表情包入库），便于后续哈希比对。

        直接复用 base64 数据，存进图片库并重建表情包索引。
        """
        import base64  # noqa: PLC0415
        import binascii  # noqa: PLC0415
        import time  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        name = str(body.get("name", "")).strip() or f"sticker_{int(time.time())}.png"
        data_b64 = str(body.get("data", ""))
        if not data_b64:
            return {"ok": False, "error": "缺少图片内容"}
        if "," in data_b64 and data_b64.lstrip().startswith("data:"):
            data_b64 = data_b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return {"ok": False, "error": f"图片解码失败：{exc}"}
        if not raw:
            return {"ok": False, "error": "图片为空"}

        suffix = Path(name).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            suffix = ".png"
        dest = self.bot.media.image_dir / f"learn_{int(time.time())}_{Path(name).stem[:40]}{suffix}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            item = self.bot.media.import_file(dest, kind="image")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"入库失败：{exc}"}

        # 重建表情包索引用的哈希索引，立刻能被识别
        try:
            vision = getattr(self.bot, "vision", None)
            matcher = getattr(vision, "sticker_matcher", None)
            if matcher is not None and hasattr(matcher, "build_index"):
                matcher.build_index(force=True)
        except Exception:  # noqa: BLE001
            pass

        return {"ok": True, "item": item.summary()}

    def api_config(self, _query: dict) -> dict[str, Any]:
        """返回配置（敏感字段掩码 + 中文字段标签）。"""
        from ..config import FIELD_LABELS, SECTION_TITLES, VALUE_HINTS  # noqa: PLC0415

        cfg = self.bot.cfg
        data = cfg.masked(self.bot.vault)
        return {
            "config": data,
            "path": str(cfg._path or ""),
            "labels": FIELD_LABELS,
            "sections": SECTION_TITLES,
            "hints": VALUE_HINTS,
        }

    def api_config_update(self, _query: dict, body: dict) -> dict[str, Any]:
        """保存配置（敏感字段加密落盘）。"""
        updates = body.get("config")
        if not isinstance(updates, dict):
            return {"ok": False, "error": "缺少 config 字段"}

        # 掩码值视为「未修改」，跳过：
        #  - "******"（加密值掩码）
        #  - 与回显掩码完全一致（头尾掩码 xxxx****xxxx）
        #  - 空字符串且原值已加密（清空即不改）
        masked = self.bot.cfg.masked(self.bot.vault)
        for key, value in list(updates.items()):
            if not isinstance(value, str):
                continue
            cur_masked = _get_dotted(masked, key)
            if value == cur_masked or value == "******":
                updates.pop(key)
                continue
            cur_raw = self.bot.cfg.get(key)
            if value == "" and isinstance(cur_raw, str) and self.bot.vault.is_encrypted(cur_raw):
                updates.pop(key)

        cfg = self.bot.cfg
        for dotted, value in updates.items():
            cfg.set(dotted, value)
        try:
            cfg.save(vault=self.bot.vault)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        log.info("配置已更新: %s", ", ".join(updates.keys()))
        return {"ok": True, "saved": list(updates.keys())}

    def api_personas(self, _query: dict) -> dict[str, Any]:
        return {
            "personas": self.bot.personas.list(),
            "current": self.bot.agent.current_persona_id,
        }

    def api_persona_set(self, _query: dict, body: dict) -> dict[str, Any]:
        pid = str(body.get("id", ""))
        if not self.bot.agent.set_persona(pid):
            return {"ok": False, "error": f"未知人格: {pid}"}
        self.bot.cfg.save(vault=self.bot.vault)
        return {"ok": True, "current": pid}

    def api_media_list(self, query: dict) -> dict[str, Any]:
        kind = str(query.get("kind", [""])[0])
        return {
            "items": self.bot.media.list(kind),
            "allow_online": self.bot.media.allow_online,
        }

    def api_media_import(self, _query: dict, body: dict) -> dict[str, Any]:
        """导入本地文件（路径由本机用户指定）。"""
        path = str(body.get("path", "")).strip()
        kind = str(body.get("kind", "auto"))
        if not path:
            return {"ok": False, "error": "缺少 path"}
        try:
            item = self.bot.media.import_file(Path(path), kind)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "item": item.summary()}

    def api_media_update(self, _query: dict, body: dict) -> dict[str, Any]:
        item_id = str(body.get("id", "")).strip()
        if not item_id:
            return {"ok": False, "error": "缺少 id"}
        name = body.get("name")
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, list):
            return {"ok": False, "error": "tags 必须是数组"}
        res = self.bot.media.update(item_id, name=name, tags=tags)
        if res is None:
            return {"ok": False, "error": "找不到素材"}
        return {"ok": True, "item": res}

    def api_media_delete(self, _query: dict, body: dict) -> dict[str, Any]:
        ok = self.bot.media.remove(str(body.get("id", "")))
        return {"ok": ok}

    def api_media_upload(self, _query: dict, body: dict) -> dict[str, Any]:
        """接收浏览器上传的文件（base64），按内容自动识别格式后入库。

        前端用 FileReader 读出 base64，这里解码落盘再交给媒体库处理。
        这样网页里的「选择文件」和「拖拽上传」都能用。
        """
        import base64  # noqa: PLC0415
        import binascii  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        name = str(body.get("name", "")).strip()
        data_b64 = str(body.get("data", ""))
        kind = str(body.get("kind", "auto"))
        if not data_b64:
            return {"ok": False, "error": "缺少文件内容"}

        # 去掉 data URL 前缀
        if "," in data_b64 and data_b64.lstrip().startswith("data:"):
            data_b64 = data_b64.split(",", 1)[1]

        try:
            raw = base64.b64decode(data_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return {"ok": False, "error": f"文件解码失败：{exc}"}

        if not raw:
            return {"ok": False, "error": "文件为空"}

        # 从文件名 / 内容魔数推断扩展名
        suffix = self._guess_media_suffix(name, raw)
        if suffix is None:
            return {"ok": False, "error": "无法识别的文件格式（支持图片、语音）"}

        tmpdir = Path(tempfile.mkdtemp(prefix="feiyu-upload-"))
        stem = Path(name).stem if name else "upload"
        # 防路径穿越
        stem = "".join(c for c in stem if c.isalnum() or c in "-_")[:60] or "upload"
        tmp = tmpdir / f"{stem}{suffix}"
        try:
            tmp.write_bytes(raw)
            item = self.bot.media.import_file(tmp, kind)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                tmp.unlink(missing_ok=True)
                tmpdir.rmdir()
            except OSError:
                pass
        return {"ok": True, "item": item.summary()}

    @staticmethod
    def _guess_media_suffix(name: str, raw: bytes) -> str | None:
        """按魔数优先、扩展名兜底，判断这是图片还是语音。"""
        head = raw[:16]

        # --- 图片 ---
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if head[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if head[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return ".webp"
        if head[:2] == b"BM":
            return ".bmp"

        # --- 语音 ---
        if head[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            return ".wav"
        if head[:4] == b"OggS":
            return ".ogg"
        if head[:3] == b"ID3":
            return ".mp3"
        if head[:4] == b"fLaC":
            return ".flac"
        if head[:4] == b"\x1aE\xdf\xa3":
            return ".webm"
        # MP3 无 ID3 头：帧同步 0xFFEx
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
            return ".mp3"
        # AMR
        if head[:6] == b"#!AMR\n":
            return ".amr"

        # --- 扩展名兜底 ---
        suffix = Path(name).suffix.lower()
        allowed = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                   ".wav", ".mp3", ".ogg", ".flac", ".webm", ".amr", ".silk"}
        if suffix in allowed:
            return suffix
        return None

    def api_media_search(self, _query: dict, body: dict) -> dict[str, Any]:
        """联网搜索图片并入库。"""
        import asyncio  # noqa: PLC0415

        keyword = str(body.get("keyword", "")).strip()
        count = int(body.get("count", 3) or 3)
        if not keyword:
            return {"ok": False, "error": "缺少 keyword"}

        async def _run():
            return await self.bot.media.search_and_download(
                keyword, min(count, 10),
                image_api=str(self.bot.cfg.get("media.image_api", "")),
                image_api_key=str(self.bot.cfg.get("media.image_api_key", "")),
            )

        try:
            items = asyncio.run_coroutine_threadsafe(
                _run(), self.bot.loop
            ).result(timeout=60)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "items": [i.summary() for i in items]}

    def api_keepalive(self, _query: dict) -> dict[str, Any]:
        import asyncio  # noqa: PLC0415

        try:
            asyncio.run_coroutine_threadsafe(
                self.bot._keepalive_once(), self.bot.loop
            ).result(timeout=30)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "health": self.bot.gateway.health()}

    def api_llm_test(self, _query: dict) -> dict[str, Any]:
        """测试 LLM 连通性。"""
        import asyncio  # noqa: PLC0415

        try:
            ok, detail = asyncio.run_coroutine_threadsafe(
                self.bot.llm.ping(), self.bot.loop
            ).result(timeout=60)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        return {"ok": ok, "detail": detail}

    def api_llm_models(self, _query: dict) -> dict[str, Any]:
        """拉取 LLM 服务端的可用模型列表（供前端下拉选择）。"""
        import asyncio  # noqa: PLC0415

        try:
            ok, result = asyncio.run_coroutine_threadsafe(
                self.bot.llm.list_models(), self.bot.loop
            ).result(timeout=45)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "models": []}

        if ok and isinstance(result, list):
            return {"ok": True, "models": result}
        # 拉不到就退回配置里的 model，至少让用户能选
        fallback = str(self.bot.cfg.get("llm.model", "") or "")
        models = [fallback] if fallback else []
        return {"ok": False, "error": str(result), "models": models}

    def api_context_clear(self, _query: dict) -> dict[str, Any]:
        self.bot.ctx.clear_all()
        return {"ok": True}

    def api_logs(self, query: dict) -> dict[str, Any]:
        """增量读取日志文件（前端按 offset 轮询）。

        参数：offset（字节偏移）、path（可选，指定日志文件）。
        返回：{ok, text, offset, path, size}
        """
        try:
            offset = int((query.get("offset") or ["0"])[0])
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)

        log_dir = Path(str(self.bot.cfg.get("app.log_dir", "logs")))
        candidates: list[Path] = []
        explicit = str((query.get("path") or [""])[0]).strip()
        if explicit:
            candidates.append(Path(explicit))
        if log_dir.exists():
            candidates.extend(sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime,
                                     reverse=True))
        if not candidates:
            return {"ok": False, "error": "没有找到日志文件", "text": "", "offset": offset}

        target = next((p for p in candidates if p.is_file()), None)
        if target is None:
            return {"ok": False, "error": "日志文件不存在", "text": "", "offset": offset}

        try:
            size = target.stat().st_size
            if offset > size:  # 文件被截断/轮转，从头读
                offset = 0
            with open(target, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(256 * 1024)  # 单次最多 256KB，避免卡住
            text = chunk.decode("utf-8", "replace")
            return {"ok": True, "text": text, "offset": offset + len(chunk),
                    "path": target.name, "size": size}
        except OSError as exc:
            return {"ok": False, "error": str(exc), "text": "", "offset": offset}

    def api_start(self, _query: dict) -> dict[str, Any]:
        """启动机器人（已运行则直接返回状态）。"""
        import asyncio  # noqa: PLC0415

        try:
            if getattr(self.bot, "running", False):
                return {"ok": True, "running": True, "note": "已在运行"}
            loop = getattr(self.bot, "loop", None)
            if loop is None or loop.is_closed():
                return {"ok": False, "error": "机器人事件循环不可用，请重启程序", "running": False}
            fut = asyncio.run_coroutine_threadsafe(self.bot.run(), loop)
            # 不等它跑完（run 是常驻循环），只确认任务已启动
            self._run_future = fut
            return {"ok": True, "running": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "running": False}

    def api_stop(self, _query: dict) -> dict[str, Any]:
        """停止机器人。"""
        import asyncio  # noqa: PLC0415

        try:
            loop = getattr(self.bot, "loop", None)
            if loop is None or loop.is_closed():
                return {"ok": False, "error": "事件循环不可用", "running": False}
            asyncio.run_coroutine_threadsafe(self.bot.stop(), loop).result(timeout=30)
            return {"ok": True, "running": False}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "running": False}

    def api_vision_upload(self, _query: dict, body: dict) -> dict[str, Any]:
        """上传图片（base64）并立即跑一次识图分析。

        浏览器拿不到本机真实路径，所以走 base64 上传；
        临时文件分析完即删，不会污染媒体库。
        """
        import base64  # noqa: PLC0415
        import binascii  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        name = str(body.get("name", "")).strip() or "upload.png"
        data_b64 = str(body.get("data", ""))
        if not data_b64:
            return {"ok": False, "error": "缺少图片内容"}
        if "," in data_b64 and data_b64.lstrip().startswith("data:"):
            data_b64 = data_b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return {"ok": False, "error": f"图片解码失败：{exc}"}
        if not raw:
            return {"ok": False, "error": "图片为空"}

        suffix = Path(name).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            suffix = ".png"
        tmpdir = Path(tempfile.mkdtemp(prefix="feiyu-vision-"))
        tmp = tmpdir / f"vision{suffix}"
        try:
            tmp.write_bytes(raw)
            report = self.bot.vision.analyze_file(tmp)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"分析失败：{exc}"}
        finally:
            try:
                tmp.unlink(missing_ok=True)
                tmpdir.rmdir()
            except OSError:
                pass

        payload: dict[str, Any] = {
            "ok": report.ok,
            "error": report.error,
            "report": report.to_prompt_text(),
            "scene": report.scene_guess(),
            "face_count": report.face_count,
            "ocr_text": report.ocr_text,
        }
        st = report.sticker
        if st is not None:
            payload["sticker"] = {
                "matched": bool(getattr(st, "matched", False)),
                "matched_name": getattr(st, "matched_name", ""),
                "similarity": round(float(getattr(st, "similarity", 0.0)), 3),
                "is_sticker": bool(getattr(st, "is_sticker", False)),
                "mood": getattr(st, "mood", ""),
                "known_meme": getattr(st, "known_meme", ""),
                "meme_text": getattr(st, "meme_text", ""),
                "features": list(getattr(st, "features", []) or []),
            }
        return payload

    def api_vision_test(self, _query: dict, body: dict) -> dict[str, Any]:
        """对本地图片跑一次识图管线，返回文字画像。"""
        path = str(body.get("path", "")).strip()
        if not path:
            return {"ok": False, "error": "缺少 path"}
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": "文件不存在"}
        report = self.bot.vision.analyze_file(p)
        payload: dict[str, Any] = {
            "ok": report.ok,
            "error": report.error,
            "report": report.to_prompt_text(),
            "scene": report.scene_guess(),
            "face_count": report.face_count,
            "ocr_text": report.ocr_text,
        }
        # 表情包识别的结构化结果
        st = report.sticker
        if st is not None:
            payload["sticker"] = {
                "matched": bool(getattr(st, "matched", False)),
                "matched_name": getattr(st, "matched_name", ""),
                "similarity": round(float(getattr(st, "similarity", 0.0)), 3),
                "is_sticker": bool(getattr(st, "is_sticker", False)),
                "mood": getattr(st, "mood", ""),
                "known_meme": getattr(st, "known_meme", ""),
                "meme_text": getattr(st, "meme_text", ""),
                "features": list(getattr(st, "features", []) or []),
            }
        return payload

    # ------------------------------------------------------------ HTTP

    def serve(self, host: str, port: int, token: str = "") -> None:
        """启动 HTTP 服务（阻塞）。"""
        controller = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "feiyu-webui/0.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                log.debug("webui: " + fmt, *args)

            # ---- 工具 ----

            def _authorized(self, query: dict) -> bool:
                if not token:
                    return True
                supplied = query.get("token", [""])[0]
                if not supplied:
                    supplied = self.headers.get("X-Token", "")
                return supplied == token

            def _send_json(self, obj: Any, status: int = 200) -> None:
                body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self) -> None:
                try:
                    html = UI_HTML.read_bytes()
                except OSError:
                    self._send_json({"error": "UI 文件缺失"}, 500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def _send_media_file(self, item: Any) -> None:
                """直接把媒体文件本体回给浏览器（图片预览 / 语音播放）。

                支持 Range 请求，这样 <audio> 拖动进度条、Safari 播放都能正常。
                """
                import mimetypes  # noqa: PLC0415

                try:
                    path = Path(item.path)
                    total = path.stat().st_size
                    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    # silk 浏览器不认识，按音频流给
                    if path.suffix.lower() == ".silk":
                        ctype = "audio/silk"
                    rng = self.headers.get("Range", "")
                    start, end = 0, total - 1
                    partial = False
                    if rng.startswith("bytes="):
                        spec = rng[len("bytes="):].split(",")[0].strip()
                        a, _, b = spec.partition("-")
                        try:
                            if a:
                                start = int(a)
                            if b:
                                end = min(int(b), total - 1)
                            partial = True
                        except ValueError:
                            start, end, partial = 0, total - 1, False
                    if start > end or start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.end_headers()
                        return
                    length = end - start + 1
                    self.send_response(206 if partial else 200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    if partial:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    with path.open("rb") as fp:
                        fp.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = fp.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (OSError, ValueError) as exc:
                    log.warning("读取媒体文件失败: %s", exc)
                    try:
                        self._send_json({"error": "文件读取失败"}, 404)
                    except OSError:
                        pass

            def _read_body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length <= 0:
                        return {}
                    raw = self.rfile.read(length)
                    return json.loads(raw.decode("utf-8"))
                except (ValueError, OSError):
                    return {}

            # ---- 路由 ----

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if path in ("/", "/index.html"):
                    if not self._authorized(query):
                        self._send_json({"error": "unauthorized"}, 401)
                        return
                    self._send_html()
                    return

                # 媒体文件本体（图片预览 / 语音在线播放）
                if path.startswith("/media/"):
                    if not self._authorized(query):
                        self._send_json({"error": "unauthorized"}, 401)
                        return
                    mid = urllib.parse.unquote(path[len("/media/"):])
                    item = controller.bot.media.get(mid)
                    if item is None:
                        self._send_json({"error": f"媒体不存在: {mid}"}, 404)
                        return
                    self._send_media_file(item)
                    return

                if not path.startswith("/api/"):
                    self._send_json({"error": "not found"}, 404)
                    return

                if not self._authorized(query):
                    self._send_json({"error": "unauthorized"}, 401)
                    return

                name = path[len("/api/"):]
                handler = getattr(controller, f"api_{name}", None)
                if handler is None:
                    self._send_json({"error": f"未知接口: {name}"}, 404)
                    return
                try:
                    self._send_json(handler(query))
                except Exception as exc:  # noqa: BLE001
                    log.exception("API %s 异常", name)
                    self._send_json({"error": str(exc)}, 500)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if not path.startswith("/api/"):
                    self._send_json({"error": "not found"}, 404)
                    return
                if not self._authorized(query):
                    self._send_json({"error": "unauthorized"}, 401)
                    return

                name = path[len("/api/"):]
                handler = getattr(controller, f"api_{name}", None)
                if handler is None:
                    self._send_json({"error": f"未知接口: {name}"}, 404)
                    return
                body = self._read_body()
                try:
                    self._send_json(handler(query, body))
                except TypeError:
                    # 只接受 query 参数的接口
                    try:
                        self._send_json(handler(query))
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"error": str(exc)}, 500)
                except Exception as exc:  # noqa: BLE001
                    log.exception("API %s 异常", name)
                    self._send_json({"error": str(exc)}, 500)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        log.info("WebUI 已启动: http://%s:%d", host, port)
        self._server.serve_forever()

    def serve_background(self, host: str, port: int, token: str = "") -> threading.Thread:
        """后台启动（不阻塞）。"""
        t = threading.Thread(
            target=self.serve, args=(host, port, token), daemon=True, name="webui"
        )
        t.start()
        self._thread = t
        return t

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
