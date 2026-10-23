"""Agent 主循环：接收 QQ 消息 → 组装上下文 → LLM 生成 → 发送回复。

职责：

- 消息入口（来自 QQ 网关）
- 时段守卫 / 预算守卫 / 上下文管理
- 识图：图片附件走本地管线，把文字画像注入上下文
- 工具调用：发图 / 发语音 / 搜图（模型可主动使用）
- 输出：被动回复优先（回复池），@ 用户，分片发送
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..llm import ChatMessage, LLMClient
from ..llm.budget import BudgetGuard
from ..logging_setup import get_logger
from ..media import MediaLibrary
from ..personas import Persona, PersonaManager
from ..qq import ParsedMessage, ReplyPool
from ..runtime_guard import RuntimeGuard
from ..vision import VisionPipeline
from .context import ContextManager

log = get_logger("feiyu.agent")

# 分片长度（QQ 单条消息建议不超过一定长度）
CHUNK_SIZE = 480


@dataclass
class AgentStats:
    """运行统计。"""

    messages_in: int = 0
    messages_out: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    images_seen: int = 0
    voices_sent: int = 0
    images_sent: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        return {
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "images_seen": self.images_seen,
            "voices_sent": self.voices_sent,
            "images_sent": self.images_sent,
            "errors": self.errors,
            "uptime_min": round((time.time() - self.started_at) / 60, 1),
        }


class Agent:
    """AI Agent 主循环。"""

    def __init__(
        self,
        cfg: Config,
        llm: LLMClient,
        personas: PersonaManager,
        media: MediaLibrary,
        vision: VisionPipeline,
        ctx_mgr: ContextManager,
        reply_pool: ReplyPool,
        runtime_guard: RuntimeGuard,
        budget: BudgetGuard,
        sender=None,  # bot 层注入的发送器
    ):
        self.cfg = cfg
        self.llm = llm
        self.personas = personas
        self.media = media
        self.vision = vision
        self.ctx = ctx_mgr
        self.reply_pool = reply_pool
        self.runtime = runtime_guard
        self.budget = budget
        self.sender = sender

        self.stats = AgentStats()
        self.current_persona_id = str(cfg.get("persona.current", "feiyu"))
        self._busy_sessions: set[str] = set()

    # ------------------------------------------------------------ 人格

    @property
    def persona(self) -> Persona | None:
        p = self.personas.get(self.current_persona_id)
        if p is None:
            p = self.personas.default()
        return p

    def set_persona(self, persona_id: str) -> bool:
        if self.personas.get(persona_id) is None:
            return False
        self.current_persona_id = persona_id
        self.cfg.set("persona.current", persona_id)
        return True

    # ------------------------------------------------------------ 消息入口

    async def handle_message(self, msg: ParsedMessage) -> None:
        """处理一条入站消息。"""
        self.stats.messages_in += 1

        # 谷时守卫
        if not self.runtime.should_handle():
            if self.runtime.behavior == "notice" and self.sender:
                await self.sender.send_text(
                    msg.reply_target, self.runtime.notice_text(), msg_id=msg.msg_id
                )
            log.debug("非谷时段，静默: %s", msg.session_key)
            return

        # 空内容且无图片 → 忽略
        has_image = any(a.kind == "image" for a in msg.attachments)
        if not msg.content.strip() and not has_image:
            return

        # 内置指令（在群里也能用，改动类指令限制管理员）
        if await self._maybe_handle_command(msg):
            return

        # 同一会话串行处理，防止乱序
        if msg.session_key in self._busy_sessions:
            log.debug("会话忙，跳过: %s", msg.session_key)
            return
        self._busy_sessions.add(msg.session_key)
        try:
            await self._process(msg)
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            log.exception("处理消息失败: %s", exc)
        finally:
            self._busy_sessions.discard(msg.session_key)

    # ------------------------------------------------------------ 内置指令

    async def _maybe_handle_command(self, msg: ParsedMessage) -> bool:
        """识别并处理内置指令，返回 True 表示已处理（不再走 AI）。

        指令表：
          帮助 / help          —— 列出可用指令
          重置 / 重置记忆       —— 清空当前会话上下文
          人格 / 人格列表       —— 查看或切换人格（切换需管理员）
          状态 / status        —— 查看用量与运行状态
        """
        if not bool(self.cfg.get("qq.commands_enabled", True)):
            return False
        if not self.sender:
            return False

        raw = (msg.content or "").strip()
        # 允许带 @ 前缀或 / 前缀
        text = raw.lstrip("/／").strip()
        text = re.sub(r"^(<@!?\d+>|@\S+)\s*", "", text).strip()
        if not text or len(text) > 40:
            return False

        low = text.lower()
        reply = ""

        if low in ("帮助", "help", "指令", "命令"):
            reply = (
                "可用指令：\n"
                "· 帮助 —— 看这份说明\n"
                "· 重置记忆 —— 清空我这个会话的记忆\n"
                "· 人格列表 —— 看看有哪几套人格\n"
                "· 状态 —— 查看今日用量"
            )
        elif text in ("重置", "重置记忆", "清空记忆", "忘掉", "reset"):
            self.ctx.drop(msg.session_key)
            reply = "好，这个会话的记忆清空了。"
        elif low in ("人格列表", "人格", "personas"):
            names = [f"· {p.get('id')}：{p.get('name')}" for p in self.personas.list()]
            reply = ("现有的人格：\n" + "\n".join(names)
                     + "\n\n切换请用「切换人格 <id>」（需要管理员）")
        elif text.startswith("切换人格") or low.startswith("set persona"):
            if not self._is_admin(msg):
                reply = "切换人格需要管理员权限。"
            else:
                target = re.sub(r"^(切换人格|set\s+persona)", "", text, flags=re.I).strip()
                if not target:
                    reply = "用法：切换人格 <人格id>"
                elif self.set_persona(target):
                    reply = f"已切换到人格：{target}"
                else:
                    reply = f"没有这个人格：{target}"
        elif low in ("状态", "status"):
            reply = (
                f"今日 AI 调用 {self.stats.llm_calls} 次，"
                f"收到 {self.stats.messages_in} 条、发出 {self.stats.messages_out} 条。"
            )
        else:
            return False

        try:
            await self.sender.send_text(msg.reply_target, reply, msg_id=msg.msg_id)
            self.stats.messages_out += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("发送指令回复失败: %s", exc)
        return True

    def _is_admin(self, msg: ParsedMessage) -> bool:
        """判断发送者是否管理员。

        配置 ``qq.admin_users`` 为空时，私聊（c2c / 频道私信）视为管理员，
        群聊一律不算，避免群里任何人改机器人人格。
        """
        admins = self.cfg.get("qq.admin_users", []) or []
        if isinstance(admins, str):
            admins = [x.strip() for x in admins.split(",") if x.strip()]
        if admins:
            allowed = {str(a) for a in admins}
            candidates = {
                str(getattr(msg, "user_id", "") or ""),
                str(getattr(msg, "user_openid", "") or ""),
                str(getattr(msg, "openid", "") or ""),
            }
            return bool(candidates & allowed)
        return getattr(msg, "kind", "") in ("c2c", "guild_dm")


    async def _process(self, msg: ParsedMessage) -> None:
        persona = self.persona
        if persona is None:
            log.error("没有可用人格")
            return

        conv = self.ctx.get(msg.session_key, persona.build_system_prompt(
            str(self.cfg.get("persona.extra_instructions", ""))
        ))

        # ---- 组装用户消息（含识图） ----
        user_text = msg.content.strip() or "[图片]"
        image_desc = ""
        if has_image and self.vision.enabled:
            image_desc = await self._describe_images(msg)
            self.stats.images_seen += 1

        if image_desc:
            display_text = f"{user_text}\n\n{self.ctx.trim_image_text(image_desc)}"
        else:
            display_text = user_text

        conv.add(ChatMessage(role="user", content=display_text))

        # ---- 上下文压缩 ----
        await self.ctx.compress_if_needed(conv, summarizer=self._summarize)

        # ---- 预算守卫 ----
        ok, reason = self.budget.can_call()
        if not ok:
            log.info("预算守卫拒绝调用（%s）", reason)
            if reason == "out_of_hours":
                await self._reply(msg, str(self.cfg.get("llm.out_of_hours_fallback", "模型当前不可用")))
            return

        # ---- LLM 调用（含工具循环） ----
        self.budget.record_call()
        self.stats.llm_calls += 1
        reply_text, tool_events = await self._chat_with_tools(conv, msg)

        # ---- 发送 ----
        if reply_text.strip():
            await self._reply(msg, reply_text)
        if tool_events:
            for event in tool_events:
                log.info("工具事件: %s", event)

    # ------------------------------------------------------------ 识图

    async def _describe_images(self, msg: ParsedMessage) -> str:
        """下载并分析消息里的图片，返回合并的文字画像。"""
        descs: list[str] = []
        for att in msg.attachments:
            if att.kind != "image":
                continue
            url = att.url
            if not url and att.file_openid:
                descs.append(f"[图片 file_openid={att.file_openid}：无法直接下载，需富媒体接口换取链接]")
                continue
            if not url:
                continue
            data = await self._download(url)
            if data is None:
                descs.append("[图片下载失败]")
                continue
            report = await asyncio.to_thread(self.vision.analyze, data)
            descs.append(report.to_prompt_text())
        return "\n\n".join(descs)

    async def _download(self, url: str) -> bytes | None:
        try:
            import httpx  # noqa: PLC0415

            async with httpx.AsyncClient(
                timeout=float(self.cfg.get("vision.download_timeout", 30)),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                if len(resp.content) > 20 * 1024 * 1024:
                    return None
                return resp.content
        except Exception as exc:  # noqa: BLE001
            log.warning("图片下载失败 %s: %s", url[:100], exc)
            return None

    # ------------------------------------------------------------ LLM + 工具

    def _tool_defs(self) -> list[dict[str, Any]]:
        """注册给模型的工具。"""
        tools: list[dict[str, Any]] = []
        if self.media.allow_online:
            tools.append({
                "type": "function",
                "function": {
                    "name": "search_image",
                    "description": "在网上搜索图片并加入图片库，之后可以用 send_image 发送。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string", "description": "搜索关键词"},
                            "count": {"type": "integer", "description": "下载数量，默认1"},
                        },
                        "required": ["keyword"],
                    },
                },
            })
        if self.media.list("image"):
            tools.append({
                "type": "function",
                "function": {
                    "name": "send_image",
                    "description": "从图片库选一张图发给用户。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string", "description": "图片关键词，匹配名称或标签"},
                        },
                        "required": ["keyword"],
                    },
                },
            })
        if self.media.list("voice"):
            tools.append({
                "type": "function",
                "function": {
                    "name": "send_voice",
                    "description": "从语音库选一条语音发给用户。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "keyword": {"type": "string", "description": "语音关键词"},
                        },
                        "required": ["keyword"],
                    },
                },
            })
        return tools

    async def _chat_with_tools(
        self, conv, msg: ParsedMessage
    ) -> tuple[str, list[str]]:
        """带工具循环的对话，返回 (最终文本, 工具事件列表)。"""
        tools = self._tool_defs()
        events: list[str] = []
        max_rounds = int(self.cfg.get("context.max_tool_rounds", 4))

        for _round in range(max_rounds):
            assistant = await self.llm.chat(
                self.ctx.build(conv),
                tools=tools or None,
                temperature=self.persona.temperature if self.persona else 0.9,
            )
            conv.add(assistant)

            if not assistant.tool_calls:
                return _clean_reply(str(assistant.content)), events

            for tc in assistant.tool_calls:
                result = await self._run_tool(tc.name, tc.parsed_args(), msg)
                events.append(f"{tc.name}({tc.parsed_args()}) -> {result[:120]}")
                conv.add(ChatMessage(role="tool", content=result, tool_call_id=tc.id))
                self.stats.tool_calls += 1
            # 工具结果回填后继续对话

        # 轮次用尽，强制出文本
        final = await self.llm.chat(self.ctx.build(conv), temperature=0.7)
        conv.add(final)
        return _clean_reply(str(final.content)), events

    async def _run_tool(self, name: str, args: dict, msg: ParsedMessage) -> str:
        """执行工具调用。"""
        try:
            if name == "search_image":
                keyword = str(args.get("keyword", "")).strip()
                count = int(args.get("count", 1) or 1)
                items = await self.media.search_and_download(
                    keyword, min(count, 3),
                    image_api=str(self.cfg.get("media.image_api", "")),
                    image_api_key=str(self.cfg.get("media.image_api_key", "")),
                )
                if not items:
                    return f"没找到关于「{keyword}」的图片"
                return "已入库: " + ", ".join(i.name for i in items)

            if name == "send_image":
                keyword = str(args.get("keyword", "")).strip()
                matches = self.media.find(keyword, kind="image") or self.media.list("image")
                if not matches:
                    return "图片库是空的"
                item = self.media.get(matches[0]["id"])
                if item is None or self.sender is None:
                    return "图片不可用"
                result = await self.sender.send_image(
                    msg.reply_target, file_path=item.path,
                    target=msg.target, msg_id=msg.msg_id,
                )
                if result.ok:
                    self.stats.images_sent += 1
                    return f"已发送图片 {item.path.name}"
                return f"发送失败: {result.error}"

            if name == "send_voice":
                keyword = str(args.get("keyword", "")).strip()
                matches = self.media.find(keyword, kind="voice") or self.media.list("voice")
                if not matches:
                    return "语音库是空的"
                item = self.media.get(matches[0]["id"])
                if item is None or self.sender is None:
                    return "语音不可用"
                result = await self.sender.send_voice(
                    msg.reply_target, file_path=item.path,
                    target=msg.target, msg_id=msg.msg_id,
                )
                if result.ok:
                    self.stats.voices_sent += 1
                    return f"已发送语音 {item.path.name}"
                return f"发送失败: {result.error}"

            return f"未知工具: {name}"
        except Exception as exc:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            return f"工具执行出错: {exc}"

    # ------------------------------------------------------------ 摘要

    async def _summarize(self, old_text: str) -> str:
        """调用模型压缩旧上下文（受预算保护）。"""
        ok, reason = self.budget.can_call()
        if not ok:
            return ""
        try:
            self.budget.record_call()
            reply = await self.llm.chat(
                [
                    ChatMessage(
                        role="system",
                        content="把以下对话压缩成不超过150字的摘要，保留用户意图、关键事实与未完成事项。只输出摘要。",
                    ),
                    ChatMessage(role="user", content=old_text[-4000:]),
                ],
                temperature=0.3,
                max_tokens=200,
            )
            return str(reply.content).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("摘要失败: %s", exc)
            return ""

    # ------------------------------------------------------------ 发送

    async def _reply(self, msg: ParsedMessage, text: str) -> None:
        """回复消息：走被动回复链，@ 用户，超长分片。"""
        if self.sender is None:
            log.warning("没有发送器，无法回复")
            return

        chunks = _split_chunks(text, CHUNK_SIZE)

        for i, chunk in enumerate(chunks):
            at_user = msg.user_openid if (i == 0 and msg.kind == "group") else ""
            # msg_seq 由回复池统一分配，保证同一 msg_id + seq 不重复
            acquired = self.reply_pool.acquire(msg.target, msg.reply_target)
            if acquired:
                reply_msg_id, seq = acquired
            else:
                reply_msg_id, seq = msg.msg_id, i + 1

            result = await self.sender.send_text(
                msg.reply_target,
                chunk,
                target=msg.target,
                msg_id=reply_msg_id,
                msg_seq=seq,
                at_user=at_user,
            )
            if result.ok:
                self.stats.messages_out += 1
            else:
                log.warning("发送失败: %s (%s)", result.error, result.code)
                break
            if len(chunks) > 1:
                await asyncio.sleep(0.8)

    # ------------------------------------------------------------ 主动发言

    async def proactive_speak(self, target: str, openid: str, text: str) -> bool:
        """主动发言：优先复用被动回复池。"""
        if not self.cfg.get("qq.allow_proactive", True):
            return False
        if not self.runtime.should_proactive():
            return False

        acquired = self.reply_pool.acquire(target, openid)
        msg_id = acquired[0] if acquired else ""
        seq = acquired[1] if acquired else 1
        del ctx

        if self.sender is None:
            return False
        result = await self.sender.send_text(
            openid, text, target=target, msg_id=msg_id, msg_seq=seq
        )
        if result.ok:
            self.stats.messages_out += 1
            return True
        log.warning("主动发言失败: %s (%s)", result.error, result.code)
        return False

    # ------------------------------------------------------------ 状态

    def status(self) -> dict[str, Any]:
        return {
            "persona": self.current_persona_id,
            "stats": self.stats.summary(),
            "budget": self.budget.stats(),
            "runtime": self.runtime.stats(),
            "context": self.ctx.stats(),
            "reply_pool": self.reply_pool.stats(),
        }


def _split_chunks(text: str, size: int) -> list[str]:
    """按长度分片，优先在换行/句号处断开。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > size:
        window = rest[:size]
        cut = max(
            window.rfind("\n"),
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("，"),
        )
        if cut < size * 0.4:
            cut = size - 1
        chunks.append(rest[: cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        chunks.append(rest)
    return chunks
