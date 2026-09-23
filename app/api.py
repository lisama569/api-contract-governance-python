"""HTTP 入口：两个版本协商入口（路径 /api/{token}/... 与头 Api-Version）。

端点使用同步 def：FastAPI 会在线程池中执行，从而让幂等锁 / 乐观锁
在 TestClient 多线程并发下接受真实竞争（而不是被单事件循环串行化）。
"""
from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.errors import APIError, ErrorCode, register_exception_handlers
from app.idempotency import IdempotencyStore
from app.logging_setup import configure_logging, get_logger
from app.repository import NoteRepository
from app.service import NoteService
from app.tracing import TraceMiddleware, get_trace_id, set_trace_id
from app.versions import VersionInfo, VersionStatus, negotiate

logger = get_logger("contract.api")

DEPRECATION_HEADER = ("Deprecation", "true")
SUNSET_HEADER = ("Sunset", "Sun, 01 Jun 2026 00:00:00 GMT")


def resolve_version_or_raise(request: Request, token: str | None) -> VersionInfo:
    """统一协商：路径 token 优先；缺失时回退 Api-Version 头；二者不一致即拒绝。"""
    # 同步端点在线程池中执行：把中间件写入 scope 的 trace_id 绑定到工作线程上下文
    scope_trace = request.scope.get("trace_id")
    if scope_trace:
        set_trace_id(scope_trace)
    header_token = request.headers.get("api-version")

    if token and token.strip():
        version, failure = negotiate(token)
        if header_token and header_token.strip():
            header_version, header_failure = negotiate(header_token)
            if header_failure == "not_found":
                raise APIError(
                    ErrorCode.VERSION_NOT_FOUND,
                    f"Api-Version 头无法识别：{header_token!r}",
                    details={"supplied": header_token, "location": "header"},
                )
            if header_failure == "unsupported":
                raise APIError(
                    ErrorCode.VERSION_UNSUPPORTED,
                    f"接口版本 {header_version.name} 已停止支持，请迁移到受支持版本",
                    details={"version": header_version.name, "status": "retired"},
                )
            if header_version != version:
                raise APIError(
                    ErrorCode.VERSION_INCOMPATIBLE,
                    "路径版本与 Api-Version 头指向不同版本，请求语义有歧义",
                    details={
                        "path_version": version.name,
                        "header_version": header_version.name,
                    },
                )
    else:
        version, failure = negotiate(header_token)

    if failure == "not_found":
        supplied = token or header_token
        raise APIError(
            ErrorCode.VERSION_NOT_FOUND,
            f"无法识别的接口版本：{supplied!r}",
            details={"supplied": supplied, "supported": ["v1", "v2"]},
        )
    if failure == "unsupported":
        raise APIError(
            ErrorCode.VERSION_UNSUPPORTED,
            f"接口版本 {version.name} 已停止支持，请迁移到 v1 或 v2",
            details={"version": version.name, "status": "retired"},
        )
    return version


def _deprecation_headers(version: VersionInfo) -> dict[str, str]:
    if version.status is VersionStatus.DEPRECATED:
        return {DEPRECATION_HEADER[0]: DEPRECATION_HEADER[1], SUNSET_HEADER[0]: SUNSET_HEADER[1]}
    return {}


def _parse_if_match(request: Request) -> int:
    raw = request.headers.get("if-match")
    if not raw:
        raise APIError(
            ErrorCode.PRECONDITION_REQUIRED,
            "写操作必须携带 If-Match 头（值为资源当前 revision）以防覆盖并发写入",
            details={"header": "If-Match"},
        )
    value = raw.strip().strip('"')
    try:
        revision = int(value)
        if revision < 1:
            raise ValueError
    except ValueError:
        raise APIError(
            ErrorCode.VALIDATION_ERROR,
            f"If-Match 头必须是正整数 revision，收到：{raw!r}",
            details={"header": "If-Match", "supplied": raw},
        ) from None
    return revision


def _answer(version: VersionInfo, status_code: int, body: Any, no_body: bool = False) -> Response:
    headers = _deprecation_headers(version)
    if no_body or body is None:
        return Response(status_code=status_code, headers=headers)
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def create_app() -> FastAPI:
    app = FastAPI(
        title="API Contract Governance",
        version="0.1.0",
        description="可版本化契约 + 幂等 + 乐观并发治理的演示服务",
    )
    app.add_middleware(TraceMiddleware)
    configure_logging()
    register_exception_handlers(app)

    repo = NoteRepository()
    store = IdempotencyStore()
    service = NoteService(repo, store)
    app.state.repo = repo
    app.state.idempotency_store = store
    app.state.service = service

    # ---- 路径版本入口 ----
    @app.post("/api/{version_token}/notes", status_code=201)
    def create_note_path(
        version_token: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> Response:
        version = resolve_version_or_raise(request, version_token)
        status, body, replayed = service.create_note(
            version, payload, request.headers.get("idempotency-key")
        )
        logger.info("响应判定 入口=path/%s 结果=%s trace=%s",
                    version.name, "重放" if replayed else "首次", get_trace_id())
        return _answer(version, status, body)

    @app.get("/api/{version_token}/notes/{note_id}")
    def get_note_path(version_token: str, note_id: str, request: Request) -> Response:
        version = resolve_version_or_raise(request, version_token)
        return _answer(version, 200, service.get_note(version, note_id))

    @app.put("/api/{version_token}/notes/{note_id}")
    def update_note_path(
        version_token: str,
        note_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> Response:
        version = resolve_version_or_raise(request, version_token)
        revision = _parse_if_match(request)
        status, body, _replayed = service.update_note(
            version, note_id, payload, revision, request.headers.get("idempotency-key")
        )
        return _answer(version, status, body)

    @app.delete("/api/{version_token}/notes/{note_id}", status_code=204)
    def delete_note_path(version_token: str, note_id: str, request: Request) -> Response:
        version = resolve_version_or_raise(request, version_token)
        revision = _parse_if_match(request)
        status, _replayed = service.delete_note(
            note_id, revision, request.headers.get("idempotency-key")
        )
        return _answer(version, status, None, no_body=True)

    # ---- 头版本入口（无版本路径） ----
    @app.post("/notes", status_code=201)
    def create_note_header(request: Request, payload: dict[str, Any] = Body(...)) -> Response:
        version = resolve_version_or_raise(request, None)
        status, body, _replayed = service.create_note(
            version, payload, request.headers.get("idempotency-key")
        )
        return _answer(version, status, body)

    @app.get("/notes/{note_id}")
    def get_note_header(note_id: str, request: Request) -> Response:
        version = resolve_version_or_raise(request, None)
        return _answer(version, 200, service.get_note(version, note_id))

    @app.put("/notes/{note_id}")
    def update_note_header(
        note_id: str, request: Request, payload: dict[str, Any] = Body(...)
    ) -> Response:
        version = resolve_version_or_raise(request, None)
        revision = _parse_if_match(request)
        status, body, _replayed = service.update_note(
            version, note_id, payload, revision, request.headers.get("idempotency-key")
        )
        return _answer(version, status, body)

    @app.delete("/notes/{note_id}", status_code=204)
    def delete_note_header(note_id: str, request: Request) -> Response:
        version = resolve_version_or_raise(request, None)
        revision = _parse_if_match(request)
        status, _replayed = service.delete_note(
            note_id, revision, request.headers.get("idempotency-key")
        )
        return _answer(version, status, None, no_body=True)

    return app
