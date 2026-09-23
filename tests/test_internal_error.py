"""内部异常归一化测试：500 只暴露稳定契约，堆栈只进服务端日志。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

log = logging.getLogger("app")


def test_unexpected_exception_becomes_internal_error_without_leak(app, caplog):
    # 注入一个必然抛异常的路由，模拟未预期的内部错误
    @app.get("/boom")
    async def boom():
        raise RuntimeError("super secret internal detail XYZ")

    # raise_server_exceptions=False：像真实 HTTP 边界一样收到归一化 500，而非直接抛出
    with TestClient(app, raise_server_exceptions=False) as client:
        with caplog.at_level(logging.ERROR, logger="app"):
            r = client.get("/boom")

    err = r.json()["error"]
    log.info("event=internal_error_response status=%s code=%s", r.status_code, err["code"])
    assert r.status_code == 500
    assert err["code"] == "INTERNAL_ERROR"
    assert err["trace_id"] == r.headers["x-trace-id"]

    # 响应中绝不能出现内部异常信息或堆栈
    assert "XYZ" not in r.text
    for leak in ("Traceback", "RuntimeError", "site-packages", "File \""):
        assert leak not in r.text

    # 服务端日志必须记录完整异常，且带同一 trace_id
    stack_logs = [rec for rec in caplog.records if "unhandled_exception" in rec.getMessage()]
    assert stack_logs, "内部异常应写入服务端日志"
    assert getattr(stack_logs[0], "trace_id", None) == err["trace_id"]
    log.info("event=verdict basis=响应无泄漏且日志含完整堆栈 trace_id=%s", err["trace_id"])
