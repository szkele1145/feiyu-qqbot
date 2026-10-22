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

    def api_context_clear(self, _query: dict) -> dict[str, Any]:
        self.bot.ctx.clear_all()
        return {"ok": True}

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
