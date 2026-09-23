"""版本契约：新增/改名字段不影响旧版本；未知/停用版本可区分拒绝。"""
from __future__ import annotations

import logging

logger = logging.getLogger("contract.test")


def _create_v2_note(client, title="t", content="c", tags=("a", "b")):
    resp = client.post(
        "/api/v2/notes",
        json={"title": title, "content": content, "tags": list(tags)},
        headers={"Idempotency-Key": f"seed-{title}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_v1_response_keeps_body_field(client):
    """v2 新增的 tags、改名的 content 不得出现在 v1 响应中，body 语义保持不变。"""
    note_id = _create_v2_note(client)
    logger.info("用例输入：以 v2 创建 note(%s) 后用 v1 读取", note_id)

    resp = client.get(f"/api/v1/notes/{note_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    logger.info("v1 响应=%s 判定依据：必须含 body，且不含 content/tags", body)
    assert set(body.keys()) == {"id", "title", "body", "revision"}
    assert body["body"] == "c"
    assert "content" not in body and "tags" not in body
    # v2 读取则必须能看到新字段
    assert set(client.get(f"/api/v2/notes/{note_id}").json()) == {
        "id", "title", "content", "tags", "revision"
    }


def test_v1_cannot_use_renamed_v2_field(client):
    """v1 调用方携带 v2 字段 content 必须被明确拒绝，不能静默错配到 body。"""
    logger.info("用例输入：POST /api/v1/notes payload={'title','body','content'}")
    resp = client.post("/api/v1/notes", json={"title": "t", "body": "b", "content": "c"})
    assert resp.status_code == 400
    err = resp.json()["error"]
    logger.info("响应错误=%s 判定依据：VERSION_INCOMPATIBLE 且指出越版本字段", err["code"])
    assert err["code"] == "VERSION_INCOMPATIBLE"
    assert err["details"]["field"] == "content"


def test_date_alias_resolves_to_version(client):
    """发布日期别名 2025-06-01 等价于 v2；2024-06-01 等价于 v1。"""
    note_id = _create_v2_note(client, title="alias")
    v2_resp = client.get(f"/api/2025-06-01/notes/{note_id}")
    v1_resp = client.get(f"/api/2024-06-01/notes/{note_id}")
    logger.info("日期别名判定：2025-06-01->%s 2023-06-01->%s",
                "content" in v2_resp.json(), "body" in v1_resp.json())
    assert "content" in v2_resp.json()
    assert "body" in v1_resp.json()


def test_unknown_and_retired_versions_are_distinguishable(client):
    """拼写错误/未来版本 -> VERSION_NOT_FOUND；已知但停用 v0 -> VERSION_UNSUPPORTED。"""
    logger.info("用例输入：请求 v9（未知）与 v0（已停用）")
    unknown = client.get("/api/v9/notes/x").json()["error"]
    retired = client.get("/api/v0/notes/x").json()["error"]
    logger.info("判定：v9=%s v0=%s", unknown["code"], retired["code"])
    assert unknown["code"] == "VERSION_NOT_FOUND"
    assert retired["code"] == "VERSION_UNSUPPORTED"
    assert unknown["code"] != retired["code"]
    assert retired["details"]["status"] == "retired"


def test_header_entry_equivalent_to_path_entry(client):
    """同一资源经 Api-Version 头入口访问，契约结论与路径入口一致。"""
    note_id = _create_v2_note(client, title="hdr")
    resp = client.get(f"/notes/{note_id}", headers={"Api-Version": "v1"})
    logger.info("头入口 v1 读取=%s", resp.json())
    assert resp.status_code == 200
    assert set(resp.json()) == {"id", "title", "body", "revision"}


def test_conflicting_path_and_header_versions_rejected(client):
    note_id = _create_v2_note(client, title="conflict")
    resp = client.get(f"/api/v1/notes/{note_id}", headers={"Api-Version": "v2"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VERSION_INCOMPATIBLE"


def test_deprecated_version_emits_deprecation_headers(client):
    note_id = _create_v2_note(client, title="dep")
    resp = client.get(f"/api/v1/notes/{note_id}")
    assert resp.headers.get("Deprecation") == "true"
    assert "Sunset" in resp.headers
    # 当前版本 v2 不应带废弃头
    assert client.get(f"/api/v2/notes/{note_id}").headers.get("Deprecation") is None


def test_field_deletion_does_not_silently_misalign(client):
    """v1 用 body 写入，v2 读出必须落在 content（语义对齐），反之 v2 字段打 v1 被拒。"""
    resp = client.post("/api/v1/notes", json={"title": "t", "body": "legacy"})
    assert resp.status_code == 201, resp.text
    note_id = resp.json()["id"]
    v2 = client.get(f"/api/v2/notes/{note_id}").json()
    logger.info("v1 写入 body='legacy' 后 v2 读取 content=%r", v2["content"])
    assert v2["content"] == "legacy"
    assert v2["tags"] == []
