"""运行时段守卫：谷时运行总开关。

与 ``llm.budget`` 的区别：

- ``llm.budget`` 管的是「能不能调模型」（省钱）。
- 本模块管的是「bot 要不要工作」（谷时运行总开关）。

打开 ``runtime.offpeak_only`` 后，非谷时段收到消息直接静默丢弃，
不调用模型、不消耗任何配额。
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any

from .logging_setup import get_logger

log = get_logger("feiyu.runtime")


class RuntimeGuard:
    """谷时运行守卫。"""

    def __init__(self, cfg: dict[str, Any]):
        self.offpeak_only = bool(cfg.get("offpeak_only", False))
        self.offpeak_hours = cfg.get("offpeak_hours") or []
        self.behavior = str(cfg.get("offpeak_behavior", "silent"))
        self.allow_proactive = bool(cfg.get("offpeak_allow_proactive", False))

    # ------------------------------------------------------------ 判定

    @staticmethod
    def _in_window(now: dtime, start: str, end: str) -> bool:
        try:
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except (ValueError, AttributeError):
            return True
        begin, end_t = dtime(sh, sm), dtime(eh, em)
        if begin <= end_t:
            return begin <= now < end_t
        return now >= begin or now < end_t

    def in_offpeak(self, now: datetime | None = None) -> bool:
        """当前是否处于谷时窗口。"""
        if not self.offpeak_hours:
            return True
        current = (now or datetime.now()).time()
        for pair in self.offpeak_hours:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            if self._in_window(current, str(pair[0]), str(pair[1])):
                return True
        return False

    def should_handle(self, now: datetime | None = None) -> bool:
        """是否应该处理这条消息。"""
        if not self.offpeak_only:
            return True
        return self.in_offpeak(now)

    def should_proactive(self, now: datetime | None = None) -> bool:
        """是否允许主动发言。"""
        if not self.offpeak_only:
            return True
        if not self.in_offpeak(now):
            return self.allow_proactive
        return True

    def notice_text(self) -> str:
        """非谷时段的提示语（behavior=notice 时使用）。"""
        return "当前不在服务时段，稍后再来撩本鱼吧～"

    def stats(self) -> dict[str, Any]:
        return {
            "offpeak_only": self.offpeak_only,
            "offpeak_hours": self.offpeak_hours,
            "in_offpeak": self.in_offpeak(),
            "behavior": self.behavior,
            "allow_proactive": self.allow_proactive,
        }

    def describe_windows(self) -> str:
        """给面板看的可读描述。"""
        if not self.offpeak_hours:
            return "未设置（视为全天）"
        parts = []
        for pair in self.offpeak_hours:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                parts.append(f"{pair[0]}~{pair[1]}")
        return "、".join(parts)
