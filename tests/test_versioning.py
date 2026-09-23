"""接口版本契约测试：新增字段不影响旧版本、改名不静默错位、不支持版本被明确拒绝。"""
from __future__ import annotations

import logging

log = logging.getLogger("app")

V1_CREATE = {
    "id": "doc-v",
    "title": "标题",
    "content": "v1正文",
    "created_by": "alice",
}


def _create_v1(client):
    log.info("event=test_case input=%s", V1_CREATE)
    r = client.post("/api/v1/documents", json=V1_CREATE)
    assert r.status_code == 201, r.text
    return r


def test_v1_response_never_exposes_v2_fields(client):
    """v2 新增的 tags/时间戳绝不能出现在 v1 响应里，旧响应语义保持不变。"""
    r = _create_v1(client)
    body = r.json()
    log.info("event=v1_create_response body=%s", body)
    assert set(body) == {"id", "title", "content", "created_by", "rev"}
    for forbidden in ("tags", "body", "owner", "revision", "created_at", "updated_at"):
        assert forbidden not in body

    got = client.get("/api/v1/documents/doc-v")
    assert set(got.json()) == {"id", "title", "content", "created_by", "rev"}


def test_same_resource_readable_under_v2_with_new_fields(client):
    """同一资源在 v2 下可访问，出现新字段且字段名采用 v2 命名。"""
    _create_v1(client)
    r = client.get("/api/v2/documents/doc-v")
    log.info("event=v2_read_response body=%s", r.json())
    assert r.status_code == 200
    body = r.json()
    assert body["body"] == "v1正文"          # v1 的 content 映射到 v2 的 body
    assert body["owner"] == "alice"         # v1 的 created_by 映射到 v2 的 owner
    assert body["revision"] == 0
    assert body["tags"] == []               # v1 写入无标签，v2 读到空列表
    assert "created_at" in body and "updated_at" in body


def test_renamed_fields_rejected_on_old_version_instead_of_silent_misalignment(client):
    """向 v1 提交 v2 字段名（body/owner）必须被拒绝，防止数据静默错位。"""
    payload = {"id": "doc-renamed", "title": "t", "body": "b", "owner": "bob"}
    log.info("event=test_case case=renamed_field_on_v1 input=%s", payload)
    r = client.post("/api/v1/documents", json=payload)
    err = r.json()["error"]
    log.info("event=verdict status=%s code=%s details=%s", r.status_code, err["code"], err["details"])
    assert r.status_code == 400
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["unknown_fields"] == ["body", "owner"]


def test_v2_unknown_v3_field_rejected(client):
    """v2 契约之外的字段同样拒绝（向前未知字段不被静默吞掉）。"""
    payload = {
        "id": "doc-x",
        "title": "t",
        "body": "b",
        "owner": "bob",
        "tags": [],
        "future_field": "nope",
    }
    r = client.post("/api/v2/documents", json=payload)
    err = r.json()["error"]
    assert r.status_code == 400 and err["code"] == "VALIDATION_ERROR"
    assert err["details"]["unknown_fields"] == ["future_field"]
    log.info("event=verdict code=%s unknown=%s", err["code"], err["details"]["unknown_fields"])


def test_v2_new_field_does_not_change_v1_semantics(client):
    """用 v2 打标签后，v1 读到的内容字段保持旧语义、且仍不出现 tags。"""
    created = client.post(
        "/api/v2/documents",
        json={"id": "doc-mix", "title": "t", "body": "b", "owner": "bob", "tags": ["vip"]},
    )
    assert created.status_code == 201
    r1 = client.get("/api/v1/documents/doc-mix")
    body = r1.json()
    log.info("event=v1_view_of_v2_resource body=%s", body)
    assert body["content"] == "b" and body["created_by"] == "bob"
    assert "tags" not in body


def test_unsupported_version_explicitly_rejected_with_distinguishable_reason(client):
    """不支持的版本：VERSION_NOT_SUPPORTED（区别于 NOT_FOUND），并给出请求版本/支持范围。"""
    cases = [
        ("GET", "/api/v3/documents/anything", None),
        ("POST", "/api/v9/documents", {"id": "x"}),
        ("GET", "/api/v3", None),
    ]
    for method, path, payload in cases:
        r = client.request(method, path, json=payload)
        err = r.json()["error"]
        log.info("event=unsupported_version path=%s code=%s details=%s", path, err["code"], err["details"])
        assert r.status_code == 400
        assert err["code"] == "VERSION_NOT_SUPPORTED"
        assert err["details"]["supported_versions"] == [1, 2]
        assert err["details"]["latest_version"] == 2
        assert err["details"]["requested_version"] in (3, 9)


def test_non_numeric_version_is_validation_error(client):
    """版本段不是数字：VALIDATION_ERROR，同样有明确原因，不返回框架默认 404。"""
    r = client.get("/api/vx/documents/anything")
    err = r.json()["error"]
    log.info("event=non_numeric_version code=%s", err["code"])
    assert r.status_code == 400
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["received_version"] == "x"
