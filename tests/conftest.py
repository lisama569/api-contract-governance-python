"""公共测试夹具：全新应用、实时日志、真并发请求与首个写入延迟注入。

* 顺序场景使用 Starlette TestClient；
* 并发场景使用 httpx.AsyncClient + ASGITransport，在同一事件循环中真正同时发请求，
  不依赖任何外部服务或凭据，``uv run pytest`` 即可本地运行；
* 请求输入、并发时序与判定依据通过 logger "app" 打印（pytest log_cli 实时显示）。
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.app import create_app
from app.store import DocumentStore
from app.tracing import TraceFormatter, TraceIdLogFilter

log = logging.getLogger("app")


def pytest_configure(config):
    """让 pytest 自带的所有日志处理器都能安全格式化 trace_id。"""
    fmt = "%(asctime)s %(levelname)s [trace_id=%(trace_id)s] %(name)s: %(message)s"
    root = logging.getLogger()
    if not any(isinstance(f, TraceIdLogFilter) for f in root.filters):
        root.addFilter(TraceIdLogFilter())
    plugin = config.pluginmanager.get_plugin("logging-plugin")
    if plugin is not None:
        formatter = TraceFormatter(fmt)
        for name in ("caplog_handler", "report_handler", "log_cli_handler", "log_file_handler"):
            handler = getattr(plugin, name, None)
            if handler is not None:
                handler.setFormatter(formatter)


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def slow_app():
    """首个 create 前 await 一段时间，制造真实的并发重叠窗口。"""
    state = {"creates": 0, "delay": 0.4}
    lock = asyncio.Lock()

    async def before_create(document_id: str) -> None:
        async with lock:
            is_first = state["creates"] == 0
            state["creates"] += 1
        if is_first:
            log.info(
                "event=test_delay id=%s delay_seconds=%.2f reason=首个写入让出事件循环制造重叠",
                document_id,
                state["delay"],
            )
            await asyncio.sleep(state["delay"])

    return create_app(store=DocumentStore(before_create=before_create))


@pytest.fixture()
def slow_client(slow_app):
    """顺序访问带延迟存储（用于先造数据）。"""
    with TestClient(slow_app) as c:
        yield c


async def gather_concurrently(app, make_request, count: int):
    """在同一事件循环中通过 ASGI 同时发起 count 个请求，返回按 index 排序的响应列表。

    所有任务在同一个 asyncio.gather 中调度；首个任务在 before_create 处 await，
    事件循环随即调度其余任务，确保它们在首个请求 IN_FLIGHT 期间完成 acquire。
    """
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

        async def one(index: int) -> tuple[int, httpx.Response]:
            started = time.perf_counter()
            response = await make_request(http, index)
            elapsed_ms = (time.perf_counter() - started) * 1000
            log.info(
                "event=concurrent_worker_finished index=%s status=%s elapsed_ms=%.2f",
                index,
                response.status_code,
                elapsed_ms,
            )
            return index, response

        results = await asyncio.gather(*[one(i) for i in range(count)])
    return sorted(results, key=lambda x: x[0])


def run_async(coro):
    return asyncio.run(coro)


def error_code(response) -> str | None:
    return response.json().get("error", {}).get("code")
