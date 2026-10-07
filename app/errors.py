"""统一对外错误契约：错误码、应用异常、归一化处理器。

所有失败都归一化为如下稳定结构，响应体不包含堆栈或内部实现信息：

    {
      "error": {
        "code": "STALE_WRITE",
        "message": "...面向调用方的说明...",
        "details": {...可机读的判定依据...},
        "trace_id": "..."
      }
    }
"""
from __future__ import annotations

import logging
from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.tracing import (
    TRACE_HEADER,
    get_trace_id,
    set_trace_id,
    trace_id_from_scope,
)

logger = logging.getLogger("app")


class ErrorCode(str, Enum):
    """对外稳定可区分的错误分类。"""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    VERSION_NOT_SUPPORTED = "VERSION_NOT_SUPPORTED"
    VERSION_SUNSET = "VERSION_SUNSET"
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    STALE_WRITE = "STALE_WRITE"
    STATE_CONFLICT = "STATE_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """业务层抛出的归一化异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict = details or {}


_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.VERSION_NOT_SUPPORTED: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.VERSION_SUNSET: 410,
    ErrorCode.PRECONDITION_REQUIRED: 428,
    ErrorCode.STALE_WRITE: 409,
    ErrorCode.STATE_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.INTERNAL_ERROR: 500,
}


def http_status_for(code: ErrorCode) -> int:
    """错误码到 HTTP 状态码的稳定映射。"""
    return _HTTP_STATUS[code]


def error_body(
    code: ErrorCode, message: str, details: dict | None = None, trace_id: str | None = None
) -> dict:
    """构造统一错误响应体。"""
    return {
        "error": {
            "code": code.value,
            "message": message,
            "details": details or {},
            "trace_id": trace_id,
        }
    }


def _bind_trace(request: Request) -> str | None:
    """确保处理器内（可能运行在线程池中）能拿到 trace_id：优先 contextvar，回退 scope。"""
    trace_id = get_trace_id() or trace_id_from_scope(request.scope)
    if trace_id is not None:
        set_trace_id(trace_id)
    return trace_id


def _json_error(
    code: ErrorCode,
    message: str,
    details: dict | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    headers = {TRACE_HEADER: trace_id} if trace_id else None
    return JSONResponse(
        status_code=http_status_for(code),
        content=error_body(code, message, details, trace_id),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """向应用注册归一化异常处理器。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        trace_id = _bind_trace(request)
        logger.info(
            "event=app_error code=%s details=%s",
            exc.code.value,
            exc.details,
        )
        return _json_error(exc.code, exc.message, exc.details, trace_id)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        trace_id = _bind_trace(request)
        # 仅保留 loc / msg / type，避免泄漏内部对象表示
        sanitized = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        logger.info("event=validation_error field_errors=%s", sanitized)
        return _json_error(
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
            {"errors": sanitized},
            trace_id,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架层 404/405（路径不存在 / 方法不支持）也归一化为统一错误结构。"""
        trace_id = _bind_trace(request)
        if exc.status_code == 404:
            code, message = ErrorCode.NOT_FOUND, "请求的路径不存在"
        elif exc.status_code == 405:
            code, message = ErrorCode.METHOD_NOT_ALLOWED, "该资源不支持此请求方法"
        else:
            code, message = ErrorCode.INTERNAL_ERROR, "服务内部错误，请稍后重试"
        details = {"method": request.method, "path": request.url.path}
        logger.info(
            "event=http_exception status=%s code=%s method=%s path=%s",
            exc.status_code,
            code.value,
            request.method,
            request.url.path,
        )
        return _json_error(code, message, details, trace_id)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        trace_id = _bind_trace(request)
        # 堆栈只进入服务端日志，绝不进入响应
        logger.exception("event=unhandled_exception error=%s", type(exc).__name__)
        return _json_error(
            ErrorCode.INTERNAL_ERROR,
            "服务内部错误，请稍后重试",
            trace_id=trace_id,
        )
