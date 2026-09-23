"""统一错误契约与追踪标识测试：稳定可区分、无堆栈泄漏、trace_id 贯穿响应与日志。"""
from __future__ import annotations

import logging

from app.errors import ErrorCode

log = logging.getLogger("app")


def test_error_envelope_is_stable_and_distinguishable(client):
    """各类失败都归一化为 error.code/message/details/trace_id 结构。"""
    cases = [
        ("POST", "/api/v2/documents", {"id": "x"}, 400, "VALIDATION_ERROR"),  # 缺必填
        ("GET", "/api/v2/documents/nope", None, 404, "NOT_FOUND"),
        ("GET", "/api/v9/documents/x", None, 400, "VERSION_NOT_SUPPORTED"),
        (
            "PUT",
            "/api/v2/documents/nope",
            {"title": "t", "body": "b", "owner": "o", "tags": [], "revision": 0},
            404,
            "NOT_FOUND",
        ),
        (
            "PUT",
            "/api/v2/documents/nope",
            {"title": "t", "body": "b", "owner": "o", "tags": []},
            428,
            "PRECONDITION_REQUIRED",
        ),
    ]
    for method, path, payload, want_status, want_code in cases:
        r = client.request(method, path, json=payload)
        err = r.json()["error"]
        log.info(
            "event=error_case path=%s want=%s/%s got=%s/%s",
            path,
            want_status,
            want_code,
            r.status_code,
            err["code"],
        )
        assert r.status_code == want_status, (path, r.status_code, r.text)
        assert err["code"] == want_code
        assert isinstance(err["message"], str) and err["message"]
        assert isinstance(err["details"], dict)
        assert err["trace_id"]
        # 所有错误码都在稳定枚举中
        assert err["code"] in [c.value for c in ErrorCode]


def test_trace_id_returned_in_header_and_body(client):
    """服务端生成的 trace_id 同时出现在响应头与错误体。"""
    r = client.get("/api/v2/documents/missing")
    header_trace = r.headers.get("x-trace-id")
    body_trace = r.json()["error"]["trace_id"]
    log.info("event=trace_check header=%s body=%s", header_trace, body_trace)
    assert header_trace
    assert header_trace == body_trace


def test_client_trace_id_is_honored(client):
    """客户端透传 X-Trace-Id 时沿用该值，便于跨系统串联。"""
    sent = "my-trace-123"
    r = client.get("/api/v2/documents/missing", headers={"X-Trace-Id": sent})
    assert r.headers["x-trace-id"] == sent
    assert r.json()["error"]["trace_id"] == sent
    log.info("event=trace_passthrough respected=%s", sent)


def test_trace_id_present_on_success_too(client):
    """成功响应也带 trace_id 头。"""
    r = client.post(
        "/api/v1/documents",
        json={"id": "d-trace", "title": "t", "content": "c", "created_by": "a"},
    )
    assert r.status_code == 201 and r.headers.get("x-trace-id")


def test_malformed_json_is_validation_error_without_stacktrace(client):
    """非法 JSON：VALIDATION_ERROR，响应体不得包含堆栈/内部文件路径。"""
    r = client.post(
        "/api/v2/documents",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )
    err = r.json()["error"]
    log.info("event=malformed_json code=%s details=%s", err["code"], err["details"])
    assert r.status_code == 400
    assert err["code"] == "VALIDATION_ERROR"
    text = r.text
    for leak in ("Traceback", "File \"", "site-packages", "line "):
        assert leak not in text


def test_field_validation_error_is_sanitized(client):
    """字段类型错误保留 loc/msg/type，但不泄漏内部对象结构。"""
    r = client.post(
        "/api/v2/documents",
        json={"id": "d-bad", "title": "t", "body": 123, "owner": "o", "tags": "notalist"},
    )
    err = r.json()["error"]
    assert r.status_code == 400 and err["code"] == "VALIDATION_ERROR"
    locs = {tuple(e["loc"]) for e in err["details"]["errors"]}
    log.info("event=field_validation locs=%s", locs)
    assert ("body",) in locs or ("tags",) in locs
    assert "Traceback" not in r.text
