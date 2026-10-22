"""预算与时段守卫：防止在模型不可用时段或余额不足时反复重试烧钱。

三层保护：

1. **时段守卫** —— 只在 ``llm.allowed_hours`` 声明的窗口内调用模型。
2. **日调用上限** —— ``llm.daily_call_limit``，超限后直接降级不请求。
3. **单次 token 上限** —— ``llm.token_ceiling``，硬顶每次请求的 max_tokens。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("feiyu.budget")


class BudgetGuard:
    """时段 + 调用次数守卫。"""

    def __init__(self, cfg: dict[str, Any], state_path: Path | None = None):
        self.allowed_hours = cfg.get("allowed_hours") or []
        self.daily_limit = int(cfg.get("daily_call_limit", 0) or 0)
        self.token_ceiling = int(cfg.get("token_ceiling", 0) or 0)
        self.fallback = str(
            cfg.get("out_of_hours_fallback", "模型当前不可用，稍后再问吧～")
        )
        self._state_path = state_path
        self._calls_today = 0
        self._day = ""
        self._load()

    # ------------------------------------------------------------ 状态

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._calls_today = int(data.get("calls", 0))
            self._day = str(data.get("day", ""))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"day": self._day, "calls": self._calls_today}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.debug("预算状态保存失败: %s", exc)

    def _roll_day(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._calls_today = 0
            self._save()

    # ------------------------------------------------------------ 判定

    @staticmethod
    def _in_window(now: dtime, start: str, end: str) -> bool:
        """判断当前时间是否落在 [start, end) 窗口内，支持跨午夜。"""
        try:
            sh, sm = (int(x) for x in start.split(":"))
            eh, em = (int(x) for x in end.split(":"))
        except (ValueError, AttributeError):
            return True
        begin, end_t = dtime(sh, sm), dtime(eh, em)
        if begin <= end_t:
            return begin <= now < end_t
        # 跨午夜，例如 18:00 -> 08:00
        return now >= begin or now < end_t

    def in_service_window(self, now: datetime | None = None) -> bool:
        """当前是否处于允许调用模型的时间窗。"""
        if not self.allowed_hours:
            return True
        current = (now or datetime.now()).time()
        for pair in self.allowed_hours:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            if self._in_window(current, str(pair[0]), str(pair[1])):
                return True
        return False

    def can_call(self) -> tuple[bool, str]:
        """返回 (是否允许调用, 拒绝原因)。"""
        self._roll_day()
        if not self.in_service_window():
            return False, "out_of_hours"
        if self.daily_limit and self._calls_today >= self.daily_limit:
            return False, "daily_limit"
        return True, ""

    def record_call(self, count: int = 1) -> None:
        self._roll_day()
        self._calls_today += count
        self._save()

    def clamp_tokens(self, requested: int) -> int:
        """把请求的 max_tokens 压到上限以内。"""
        if self.token_ceiling and requested > self.token_ceiling:
            return self.token_ceiling
        return requested

    @property
    def calls_today(self) -> int:
        self._roll_day()
        return self._calls_today

    def stats(self) -> dict[str, Any]:
        return {
            "calls_today": self.calls_today,
            "daily_limit": self.daily_limit,
            "token_ceiling": self.token_ceiling,
            "allowed_hours": self.allowed_hours,
            "in_window": self.in_service_window(),
        }
