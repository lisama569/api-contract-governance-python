"""请求追踪标识与结构化请求日志。

- 每个请求都会获得一个 ``trace_id``：优先沿用客户端 ``X-Trace-Id``，否则服务端生成；
- ``trace_id`` 同时出现在响应头、统一错误体与所有相关日志中；
- 日志只记录面向诊断的信息（方法、路径、查询串、状态、耗时），不记录请求体，
  请求体与判定依据由各业务处理函数在其日志中显式打印。
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from typing import Any

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

TRACE_HEADER = "x-trace-id"
SCOPE_KEY = "app.trace_id"
# 生命周期检查在请求处理早期写入的弃用响应头（dict[str, str]），
# 由中间件统一附加到该请求的任何响应（成功或错误）上
DEPRECATION_HEADERS_SCOPE_KEY = "app.deprecation_headers"

logger = logging.getLogger("app")

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)


def new_trace_id() -> str:
    """生成新的追踪标识。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(trace_id: str) -> contextvars.Token[str | None]:
    return _trace_id.set(trace_id)


def trace_id_from_scope(scope: Scope | None) -> str | None:
    """从 ASGI scope 解析 trace_id（跨线程池边界时 contextvar 可能丢失）。"""
    if scope is None:
        return None
    return scope.get(SCOPE_KEY)


class TraceIdLogFilter(logging.Filter):
    """给每条日志记录附加当前上下文的 trace_id 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True


class TraceFormatter(logging.Formatter):
    """带默认 trace_id 的格式化器：即使记录未经过过滤器也不会 KeyError。"""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "trace_id"):
            record.trace_id = get_trace_id() or "-"
        return super().format(record)


class TraceMiddleware:
    """纯 ASGI 中间件：解析/生成 trace_id、注入响应头、记录请求日志。"""

    def __init__(self, app: ASGIApp) -> ASGIApp:  # type: ignore[assignment]
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        trace_id = headers.get(TRACE_HEADER) or new_trace_id()
        scope[SCOPE_KEY] = trace_id
        token = set_trace_id(trace_id)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                raw_headers = message.setdefault("headers", [])
                # 错误响应可能已自带该头，先剔除再写入，避免出现重复头
                raw_headers[:] = [
                    (k, v) for k, v in raw_headers if k.lower() != TRACE_HEADER.encode()
                ]
                raw_headers.append((TRACE_HEADER.encode(), trace_id.encode()))
                # 版本弃用信息（若本请求命中弃用版本）：成功与错误响应都携带
                for name, value in scope.get(DEPRECATION_HEADERS_SCOPE_KEY, {}).items():
                    raw_headers.append((name.encode(), value.encode()))
            await send(message)

        query = scope.get("query_string", b"").decode("latin1")
        try:
            logger.info(
                "event=request_started method=%s path=%s query=%r",
                scope.get("method"),
                scope.get("path"),
                query,
            )
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "event=request_finished method=%s path=%s status=%s duration_ms=%.2f",
                scope.get("method"),
                scope.get("path"),
                status_code,
                duration_ms,
            )
            _trace_id.reset(token)
