"""错误归一化：稳定错误码、无堆栈泄露、trace_id 贯穿响应头与日志。"""
from __future__ import annotations

import logging

import pytest

logger = logging.getLogger("contract.test")


def _assert_envelope(resp, expected_status: int, expected_code: str):
    assert resp.status_code == expected_status, resp.text
    err = resp.json()["error"]
    assert set(err.keys()) == {"code", "message", "details", "trace_id"}
    assert err["code"] == expected_code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)
    return err


def test_not_found_envelope(client):
    resp = client.get("/api/v2/notes/missing")
    err = _assert_envelope(resp, 404, "NOT_FOUND")
    assert err["details"]["id"] == "missing"
    logger.info("404 归一化判定通过：%s", err)


def test_validation_error_envelope(client):
    # 缺 content / tags 类型错误 -> pydantic 校验失败归一化
    resp = client.post("/api/v2/notes", json={"title": "t"})
    err = _assert_envelope(resp, 400, "VALIDATION_ERROR")
    assert any(f["field"] == "content" for f in err["details"]["fields"])
    logger.info("校验失败归一化判定通过：fields=%s", err["details"]["fields"])


def test_unknown_version_distinguishable_from_retired(client):
    unknown = client.get("/api/v99/notes/x")
    retired = client.get("/api/v0/notes/x")
    e1 = _assert_envelope(unknown, 400, "VERSION_NOT_FOUND")
    e2 = _assert_envelope(retired, 400, "VERSION_UNSUPPORTED")
    logger.info("版本失败可区分：not_found details=%s ; unsupported details=%s",
                e1["details"], e2["details"])
    assert e1["details"]["supported"] == ["v1", "v2"]
    assert e2["details"]["status"] == "retired"


def test_trace_id_returned_and_propagated_in_logs(client, caplog):
    """响应头带 trace_id，且与日志记录上下文中的 trace 一致。"""
    with caplog.at_level(logging.INFO, logger="contract"):
        resp = client.get("/api/v2/notes/whatever")
    err = resp.json()["error"]
    trace_header = resp.headers.get("x-trace-id")
    assert trace_header
    assert err["trace_id"] == trace_header
    # 日志 filter 给每条记录注入的 trace_id 必须与响应一致（贯穿日志）
    contract_records = [
        r for r in caplog.records if r.name.startswith("contract") and getattr(r, "trace_id", "-") != "-"
    ]
    assert contract_records, "未捕获到带 trace_id 的契约日志"
    assert all(r.trace_id == trace_header for r in contract_records)
    logger.info("trace 贯穿判定：trace_id=%s 已出现在响应体、响应头与 %d 条日志",
                trace_header, len(contract_records))


def test_client_supplied_trace_id_is_honored(client):
    resp = client.get("/api/v2/notes/x", headers={"X-Trace-Id": "client-trace-abc123"})
    assert resp.headers.get("x-trace-id") == "client-trace-abc123"
    assert resp.json()["error"]["trace_id"] == "client-trace-abc123"


def test_malformed_trace_id_is_replaced(client):
    """入站 trace 含非法字符（头注入风险）时必须被替换而不是原样反射。"""
    resp = client.get("/api/v2/notes/x", headers={"X-Trace-Id": "bad\ninjected"})
    trace = resp.headers.get("x-trace-id")
    assert trace and trace != "bad\ninjected"
    assert "\n" not in trace


def test_internal_error_does_not_leak(client, monkeypatch, caplog):
    """内部异常对外只给稳定契约，堆栈仅进入服务端日志。"""

    def boom(_version, _note_id):
        raise RuntimeError("super secret internal detail /db/password=123")

    monkeypatch.setattr(client.app.state.service, "get_note", boom)
    with caplog.at_level(logging.ERROR, logger="contract.errors"):
        resp = client.get("/api/v2/notes/x")

    err = _assert_envelope(resp, 500, "INTERNAL_ERROR")
    body_text = resp.text
    logger.info("500 判定：对外 message=%r trace=%s", err["message"], err["trace_id"])
    assert "super secret" not in body_text
    assert "password" not in body_text
    assert "Traceback" not in body_text
    assert "RuntimeError" not in body_text
    # 内部细节仍可通过 trace_id 在服务端日志关联
    assert "RuntimeError" in caplog.text
    error_records = [
        r for r in caplog.records if r.name == "contract.errors"
    ]
    assert any(getattr(r, "trace_id", None) == err["trace_id"] for r in error_records)


@pytest.mark.parametrize("path", ["/api/v2/notes/no/such/route", "/nope"])
def test_unknown_route_normalized(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
