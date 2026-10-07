"""应用装配：中间件、归一化异常处理、共享存储与多版本路由。"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app import contracts
from app.errors import register_exception_handlers
from app.idempotency import IdempotencyStore
from app.lifecycle import LifecycleRegistry
from app.routers import (
    create_admin_router,
    create_router,
    create_unsupported_version_router,
)
from app.store import DocumentStore
from app.tracing import TraceFormatter, TraceIdLogFilter, TraceMiddleware

LOG_FORMAT = "%(asctime)s %(levelname)s [trace_id=%(trace_id)s] %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """配置带 trace_id 字段的结构化日志（可被 pytest caplog 捕获）。"""
    app_logger = logging.getLogger("app")
    # logger 级过滤器先于一切 handler 执行，保证 pytest 等外部 handler 也能格式化 trace_id
    if not any(isinstance(f, TraceIdLogFilter) for f in app_logger.filters):
        app_logger.addFilter(TraceIdLogFilter())
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(TraceFormatter(LOG_FORMAT))
        handler.addFilter(TraceIdLogFilter())
        app_logger.addHandler(handler)
    app_logger.setLevel(level)
    app_logger.propagate = True


def create_app(
    store: DocumentStore | None = None,
    idempotency: IdempotencyStore | None = None,
    lifecycle: LifecycleRegistry | None = None,
) -> FastAPI:
    configure_logging()
    app = FastAPI(title="api-contract-governance", version="0.1.0")

    # 单进程内共享的存储；测试可注入（如带延迟的存储）并通过 app.state 断言副作用次数
    store = store if store is not None else DocumentStore()
    idempotency = idempotency if idempotency is not None else IdempotencyStore()
    lifecycle = (
        lifecycle
        if lifecycle is not None
        else LifecycleRegistry(contracts.SUPPORTED_VERSIONS)
    )
    app.state.store = store
    app.state.idempotency = idempotency
    app.state.lifecycle = lifecycle

    app.add_middleware(TraceMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # 先注册受支持版本的具体路由，再注册未支持版本兜底与生命周期管理接口
    app.include_router(create_router(1, store, idempotency, lifecycle))
    app.include_router(create_router(2, store, idempotency, lifecycle))
    app.include_router(create_unsupported_version_router(lifecycle))
    app.include_router(create_admin_router(lifecycle))

    return app
