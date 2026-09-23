"""请求追踪标识：生成、上下文存取、ASGI 中间件。"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

# 当前请求的 trace_id（协程/线程上下文隔离）
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)

_HEADER_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(value: str) -> None:
    _trace_id.set(value)


def sanitize_incoming(value: str | None) -> str | None:
    """只接受形态合法的入站 trace id，防止头注入/换行污染日志。"""
    if value and _HEADER_RE.match(value.strip()):
        return value.strip()
    return None


class TraceMiddleware:
    """为每个请求生成/透传 trace_id，并写入响应头与日志上下文。"""

    def __init__(self, app, header_name: str = "X-Trace-Id") -> None:  # noqa: ANN001
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        trace_id = sanitize_incoming(headers.get(self.header_name.lower())) or new_trace_id()
        token = _trace_id.set(trace_id)
        # 同步端点在线程池中执行，contextvar 不跨线程传播；
        # 同时把 trace_id 放进 scope，供请求处理链（含异常处理器）显式取用。
        scope["trace_id"] = trace_id
        header_bytes = (self.header_name.lower().encode(), trace_id.encode())

        async def send_with_trace(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), header_bytes]
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            _trace_id.reset(token)
