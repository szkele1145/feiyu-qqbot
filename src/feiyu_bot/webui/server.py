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
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..logging_setup import get_logger

log = get_logger("feiyu.webui")


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

    def api_vision_test(self, _query: dict, body: dict) -> dict[str, Any]:
        """对本地图片跑一次识图管线，返回文字画像。"""
        path = str(body.get("path", "")).strip()
        if not path:
            return {"ok": False, "error": "缺少 path"}
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": "文件不存在"}
        report = self.bot.vision.analyze_file(p)
        return {
            "ok": report.ok,
            "error": report.error,
            "report": report.to_prompt_text(),
        }

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

            def _send_media(self, item_id: str) -> None:
                """按媒体 id 返回原始文件（图片缩略图 / 语音试听）。"""
                import mimetypes  # noqa: PLC0415

                # self.path 可能含 %XX 或已解码；统一处理成干净的 id
                raw_id = item_id or ""
                fixed: str | None = None
                for candidate in (
                    lambda: urllib.parse.unquote(raw_id, encoding="utf-8"),
                    lambda: urllib.parse.unquote(raw_id, encoding="utf-8", errors="replace"),
                    lambda: raw_id.encode("latin-1", "ignore").decode("utf-8", "replace"),
                    lambda: raw_id,
                ):
                    try:
                        got = candidate()
                    except (UnicodeDecodeError, LookupError, ValueError):
                        continue
                    if got and "\ufffd" not in got:
                        fixed = got.strip()
                        break
                item_id = fixed or raw_id.strip()
                if not item_id:
                    self._send_json({"error": "缺少媒体 id"}, 400)
                    return
                try:
                    items = server.bot.media.list("")
                except Exception:  # noqa: BLE001
                    items = []

                # 先按 id 精确找，再按文件名找（前端可能传中文名）
                target = next(
                    (it for it in items if str(it.get("id")) == item_id), None
                )
                if target is None:
                    # 宽松匹配：去引号/空白后再比
                    clean = item_id.strip().strip("\"'")
                    target = next(
                        (it for it in items if str(it.get("id")).strip() == clean), None
                    )
                if target is None:
                    target = next(
                        (it for it in items if str(it.get("name")) == item_id), None
                    )
                if target is None:
                    # id 可能被截断（前端显示用），按前缀匹配
                    target = next(
                        (it for it in items
                         if str(it.get("id")).startswith(item_id) and len(item_id) >= 4),
                        None,
                    )
                if target is None:
                    ids = [str(it.get("id")) for it in items]
                    self._send_json({"error": "媒体不存在",
                                     "requested": item_id,
                                     "available": ids[:10],
                                     "count": len(ids)}, 404)
                    return

                file_path: Path | None = None
                try:
                    raw = server.bot.media.get(str(target.get("id")))
                    if raw is not None and getattr(raw, "path", None):
                        p = Path(raw.path)
                        # 媒体库存的是相对路径（相对 base_dir），要拼回绝对路径
                        if not p.is_absolute():
                            p = Path(str(server.bot.cfg.base_dir)) / p
                        if p.is_file():
                            file_path = p
                except Exception:  # noqa: BLE001
                    file_path = None

                if file_path is None:
                    name = str(target.get("name") or "")
                    media_dir = Path(str(server.bot.cfg.get("media.dir", "assets")))
                    if not media_dir.is_absolute():
                        media_dir = Path(str(server.bot.cfg.base_dir)) / media_dir
                    for cand in (media_dir / "images" / name,
                                 media_dir / "voices" / name):
                        if cand.is_file():
                            file_path = cand
                            break
                    # 再宽松一点：按 id 找带任意扩展名的文件
                    if file_path is None:
                        for sub in ("images", "voices"):
                            for hit in (media_dir / sub).glob(f"{target.get('id')}.*"):
                                if hit.is_file():
                                    file_path = hit
                                    break
                            if file_path is not None:
                                break
                if file_path is None or not file_path.is_file():
                    self._send_json({"error": "文件不存在"}, 404)
                    return

                try:
                    data = file_path.read_bytes()
                except OSError as exc:
                    self._send_json({"error": str(exc)}, 500)
                    return
                ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(data)

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

                # 媒体文件直读：/media/<id>，用于在前端显示缩略图
                if path.startswith("/media/"):
                    if not self._authorized(query):
                        self._send_json({"error": "unauthorized"}, 401)
                        return
                    try:
                        self._send_media(path[len("/media/"):])
                    except Exception as exc:  # noqa: BLE001
                        log.exception("媒体读取失败 id=%s", path)
                        try:
                            self._send_json({"error": str(exc)}, 500)
                        except Exception:  # noqa: BLE001
                            pass
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
