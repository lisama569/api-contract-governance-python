"""日志配置：结构化字段 + trace_id 注入，不向客户端暴露内部细节。"""
from __future__ import annotations

import logging
import sys

from app.tracing import get_trace_id

_CONFIGURED = False
_PREVIOUS_FACTORY = None


def _install_record_factory() -> None:
    """在每条 LogRecord 创建时注入当前请求的 trace_id（无上下文时为 "-"）。

    用 record factory 而不是 handler filter，保证任何 handler
    （含 pytest caplog）都能读到 record.trace_id。
    """
    global _PREVIOUS_FACTORY
    previous = logging.getLogRecordFactory()

    def factory(name, level, fn, lno, msg, args, exc_info,  # noqa: PLR0913
                func=None, sinfo=None, **kwargs):
        record = previous(name, level, fn, lno, msg, args, exc_info,
                          func=func, sinfo=sinfo, **kwargs)
        trace_id = get_trace_id()
        record.trace_id = trace_id if trace_id else "-"
        return record

    logging.setLogRecordFactory(factory)
    _PREVIOUS_FACTORY = previous


def configure_logging(level: int = logging.INFO) -> None:
    """配置全局日志（幂等，重复调用不会叠加 handler）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _install_record_factory()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [trace=%(trace_id)s] %(name)s :: %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # 应用自身的契约日志始终开到 DEBUG，便于单测观察时序
    logging.getLogger("contract").setLevel(logging.DEBUG)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
