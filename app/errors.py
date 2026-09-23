"""统一对外错误契约。

所有错误响应均为：
    {"error": {"code": ..., "message": ..., "details": {...}, "trace_id": ...}}
内部异常只写服务端日志（含 trace_id），对外仅返回稳定分类信息。
"""
from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import get_logger
from app.tracing import get_trace_id, set_trace_id

logger = get_logger("contract.errors")


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    STATE_CONFLICT = "STATE_CONFLICT"
    VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
    VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
    VERSION_INCOMPATIBLE = "VERSION_INCOMPATIBLE"
    IDEMPOTENCY_REUSE = "IDEMPOTENCY_REUSE"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# error_code -> (http_status, 默认 message)
ERROR_STATUS: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.VALIDATION_ERROR: (400, "请求参数校验失败"),
    ErrorCode.NOT_FOUND: (404, "资源不存在"),
    ErrorCode.STATE_CONFLICT: (409, "资源状态冲突"),
    ErrorCode.VERSION_NOT_FOUND: (400, "无法识别的接口版本"),
    ErrorCode.VERSION_UNSUPPORTED: (400, "接口版本已停止支持"),
    ErrorCode.VERSION_INCOMPATIBLE: (400, "请求字段与协商的接口版本不兼容"),
    ErrorCode.IDEMPOTENCY_REUSE: (422, "幂等键被不同请求复用"),
    ErrorCode.PRECONDITION_FAILED: (412, "前置条件失败：基于陈旧状态的写入"),
    ErrorCode.PRECONDITION_REQUIRED: (428, "写操作要求携带 If-Match 版本条件"),
    ErrorCode.INTERNAL_ERROR: (500, "服务内部错误"),
}


class APIError(Exception):
    """所有对外可解释错误的基类。"""

    def __init__(self, code: ErrorCode, message: str | None = None, *, details: dict | None = None) -> None:
        status, default_message = ERROR_STATUS[code]
        self.code = code
        self.status_code = status
        self.message = message or default_message
        self.details = details or {}
        super().__init__(self.message)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict
    trace_id: str | None = None


def _envelope(code: ErrorCode, message: str, details: dict, trace_id: str | None = None) -> dict:
    return {"error": ErrorBody(
        code=code.value,
        message=message,
        details=details,
        trace_id=trace_id or get_trace_id(),
    ).model_dump()}


def register_exception_handlers(app: FastAPI) -> None:
    """把 APIError / 校验错误 / 未捕获异常归一化为统一错误契约。"""

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError):  # noqa: ANN202
        trace_id = request.scope.get("trace_id") or get_trace_id()
        if trace_id:
            set_trace_id(trace_id)
        logger.info(
            "业务拒绝 code=%s status=%s message=%s details=%s",
            exc.code.value, exc.status_code, exc.message, exc.details,
        )
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details, trace_id),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError):  # noqa: ANN202
        trace_id = request.scope.get("trace_id") or get_trace_id()
        if trace_id:
            set_trace_id(trace_id)
        # 仅抽取字段路径与错误类型，不回显原始输入或内部上下文
        fields = [
            {"field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
             "issue": err.get("type", "invalid")}
            for err in exc.errors()
        ]
        from fastapi.responses import JSONResponse
        logger.info("参数校验失败 fields=%s", fields)
        return JSONResponse(
            status_code=400,
            content=_envelope(
                ErrorCode.VALIDATION_ERROR,
                ERROR_STATUS[ErrorCode.VALIDATION_ERROR][1],
                {"fields": fields},
                request.scope.get("trace_id") or get_trace_id(),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException):  # noqa: ANN202
        trace_id = request.scope.get("trace_id") or get_trace_id()
        if trace_id:
            set_trace_id(trace_id)
        from fastapi.responses import JSONResponse
        if exc.status_code == 404:
            code, message = ErrorCode.NOT_FOUND, "路由或资源不存在"
        elif exc.status_code == 405:
            code, message = ErrorCode.VALIDATION_ERROR, "HTTP 方法不被允许"
        else:
            code, message = ErrorCode.VALIDATION_ERROR, "请求不合法"
        logger.info("HTTP 异常 status=%s", exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                code, message, {}, request.scope.get("trace_id") or get_trace_id()
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unknown(request: Request, exc: Exception):  # noqa: ANN202
        trace_id = request.scope.get("trace_id") or get_trace_id()
        if trace_id:
            set_trace_id(trace_id)
        # 完整堆栈只进日志，对外不暴露任何实现细节
        logger.exception("未处理异常: %s", type(exc).__name__)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content=_envelope(
                ErrorCode.INTERNAL_ERROR,
                ERROR_STATUS[ErrorCode.INTERNAL_ERROR][1],
                {},
                request.scope.get("trace_id") or get_trace_id(),
            ),
        )
