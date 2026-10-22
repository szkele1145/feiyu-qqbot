"""日志初始化：控制台 + 按天滚动的文件。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class _ColorFormatter(logging.Formatter):
    """给控制台输出加 ANSI 颜色。"""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;41m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if sys.stderr.isatty():
            color = self.COLORS.get(record.levelname, "")
            if color:
                text = f"{color}{text}{self.RESET}"
        return text


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str | None = None,
    *,
    quiet: bool = False,
) -> logging.Logger:
    """初始化根日志器。重复调用不会叠加 handler。"""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        return root

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(_ColorFormatter(_FMT, _DATE_FMT))
    root.addHandler(stream)

    if log_dir is not None and not quiet:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            d / "feiyu-qqbot.log", when="midnight", backupCount=14, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
        root.addHandler(file_handler)

    # 第三方库降噪
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
