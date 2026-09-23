"""并发修改与乐观锁测试：识别互相覆盖的冲突写入，拒绝基于陈旧状态的更新。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from conftest import gather_concurrently, run_async

log = logging.getLogger("app")


def _create(client, document_id="doc-lock"):
    body = {
        "id": document_id,
        "title": "原始标题",
        "body": "原始正文",
        "owner": "alice",
        "tags": [],
    }
    r = client.post("/api/v2/documents", json=body)
    log.info("event=setup_create id=%s status=%s rev=%s", document_id, r.status_code, r.json().get("revision"))
    assert r.status_code == 201


def test_stale_update_rejected_with_expected_and_actual(client):
    """先成功更新一次，再拿旧 revision 更新：STALE_WRITE，details 带 expected/actual。"""
    _create(client)
    first = client.put(
        "/api/v2/documents/doc-lock",
        json={"title": "一", "body": "正文1", "owner": "alice", "tags": [], "revision": 0},
    )
    assert first.status_code == 200 and first.json()["revision"] == 1
    log.info("event=first_update revision_now=1")

    stale = client.put(
        "/api/v2/documents/doc-lock",
        json={"title": "二", "body": "正文2", "owner": "alice", "tags": [], "revision": 0},
    )
    err = stale.json()["error"]
    log.info(
        "event=stale_write status=%s code=%s expected=%s actual=%s",
        stale.status_code,
        err["code"],
        err["details"]["expected_revision"],
        err["details"]["actual_revision"],
    )
    assert stale.status_code == 409
    assert err["code"] == "STALE_WRITE"
    assert err["details"]["expected_revision"] == 0
    assert err["details"]["actual_revision"] == 1

    # 被拒更新不得改变最终状态
    final = client.get("/api/v2/documents/doc-lock").json()
    assert final["body"] == "正文1" and final["revision"] == 1
    log.info("event=verdict basis=expected0!=actual1 final_body=正文1 final_revision=1")


def test_update_without_version_field_is_precondition_required(client):
    """更新缺少 revision 前提字段：PRECONDITION_REQUIRED（428），可与其它失败区分。"""
    _create(client, "doc-pre")
    r = client.put(
        "/api/v2/documents/doc-pre",
        json={"title": "t", "body": "b", "owner": "alice", "tags": []},
    )
    err = r.json()["error"]
    log.info("event=missing_precondition status=%s code=%s details=%s", r.status_code, err["code"], err["details"])
    assert r.status_code == 428
    assert err["code"] == "PRECONDITION_REQUIRED"
    assert err["details"]["required_field"] == "revision"


def test_v1_uses_rev_field_for_optimistic_lock(client):
    """v1 用旧字段名 rev 做乐观锁；漏带 rev 同样是前提缺失。"""
    client.post(
        "/api/v1/documents",
        json={"id": "doc-v1lock", "title": "t", "content": "c", "created_by": "alice"},
    )
    missing = client.put(
        "/api/v1/documents/doc-v1lock",
        json={"title": "t", "content": "c2", "created_by": "alice"},
    )
    assert missing.status_code == 428
    assert missing.json()["error"]["details"]["required_field"] == "rev"

    ok = client.put(
        "/api/v1/documents/doc-v1lock",
        json={"title": "t", "content": "c2", "created_by": "alice", "rev": 0},
    )
    log.info("event=v1_update status=%s body=%s", ok.status_code, ok.json())
    assert ok.status_code == 200 and ok.json()["rev"] == 1


def test_concurrent_updates_only_one_wins_others_get_stale_write(slow_app, slow_client):
    """6 个并发更新都基于 revision=0：恰好一个成功，其余全部 STALE_WRITE。

    判定依据：成功响应 revision=1；失败响应 error.code=STALE_WRITE 且
    details.actual_revision=1；最终资源 revision=1、副作用恰好 +1。
    """
    _create(slow_client)

    count = 6
    base = {
        "title": "并发改",
        "body": "并发正文",
        "owner": "alice",
        "tags": [],
        "revision": 0,
    }
    log.info(
        "event=test_case case=concurrent_updates_same_revision concurrency=%s input=%s",
        count,
        base,
    )

    async def make_request(http, index):
        # 各自不同 body 以便确认成功写入的是其中一个，且全部基于陈旧/相同 revision=0
        body = dict(base, body=f"来自请求{index}")
        return await http.put("/api/v2/documents/doc-lock", json=body)

    results = run_async(gather_concurrently(slow_app, make_request, count))
    statuses = [r.status_code for _, r in results]
    codes = []
    for _, r in results:
        if r.status_code == 409:
            codes.append(r.json()["error"]["code"])
        else:
            codes.append(f"OK:{r.json()['revision']}")
    log.info(
        "event=concurrent_update_verdict statuses=%s codes=%s side_effect_count=%s",
        statuses,
        codes,
        slow_app.state.store.side_effect_count,
    )

    success = [r for _, r in results if r.status_code == 200]
    conflicts = [r for _, r in results if r.status_code == 409]
    assert len(success) == 1, f"应恰好一个成功，实际 {len(success)}"
    assert len(conflicts) == count - 1
    assert success[0].json()["revision"] == 1
    for r in conflicts:
        err = r.json()["error"]
        assert err["code"] == "STALE_WRITE"
        assert err["details"]["expected_revision"] == 0
        assert err["details"]["actual_revision"] == 1

    with TestClient(slow_app) as c:
        final = c.get("/api/v2/documents/doc-lock").json()
    log.info(
        "event=verdict basis=1成功+5个expected0!=actual1 final_body=%s final_revision=%s",
        final["body"],
        final["revision"],
    )
    assert final["revision"] == 1
    assert final["body"].startswith("来自请求")
    # 一次 create + 一次成功 replace
    assert slow_app.state.store.side_effect_count == 2
