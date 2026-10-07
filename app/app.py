"""应用装配：中间件、归一化异常处理、共享存储与多版本路由。"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app import contracts
from app.errors import register_exception_handlers
from app.idempotency import IdempotencyStore
from app.lifecycle import VersionLifecycleMiddleware, VersionRegistry
from app.routers import (
    create_lifecycle_router,
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
    versions: VersionRegistry | None = None,
) -> FastAPI:
    configure_logging()
    app = FastAPI(title="api-contract-governance", version="0.1.0")

    # 单进程内共享的存储；测试可注入（如带延迟的存储）并通过 app.state 断言副作用次数
    store = store if store is not None else DocumentStore()
    idempotency = idempotency if idempotency is not None else IdempotencyStore()
    versions = (
        versions
        if versions is not None
        else VersionRegistry(contracts.SUPPORTED_VERSIONS, contracts.LATEST_VERSION)
    )
    app.state.store = store
    app.state.idempotency = idempotency
    app.state.versions = versions

    # 后添加的中间件在最外层：Trace 最外（先生成 trace_id），生命周期判定在其内
    app.add_middleware(VersionLifecycleMiddleware, registry=versions)
    app.add_middleware(TraceMiddleware)
    register_exception_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # 先注册生命周期查询/调整与受支持版本的具体路由，再注册版本兜底
    app.include_router(create_lifecycle_router(versions))
    app.include_router(create_router(1, store, idempotency))
    app.include_router(create_router(2, store, idempotency))
    app.include_router(create_unsupported_version_router(versions))

    return app
