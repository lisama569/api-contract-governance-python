"""幂等语义测试：重复与并发提交只产生一次真实副作用，结果一致回放。"""
from __future__ import annotations

import json
import logging

from conftest import gather_concurrently, run_async

log = logging.getLogger("app")

CREATE_BODY = {
    "id": "doc-concurrent",
    "title": "并发标题",
    "body": "并发正文",
    "owner": "alice",
    "tags": ["a", "b"],
}


def test_sequential_duplicate_request_replays_single_side_effect(client):
    """顺序重复：第二次直接回放首次结果，无第二次副作用。"""
    headers = {"Idempotency-Key": "key-seq-1"}
    log.info("event=test_case case=sequential_duplicate input=%s headers=%s", CREATE_BODY, headers)

    r1 = client.post("/api/v2/documents", json=CREATE_BODY, headers=headers)
    log.info("event=first_response status=%s body=%s", r1.status_code, r1.json())
    assert r1.status_code == 201

    r2 = client.post("/api/v2/documents", json=CREATE_BODY, headers=headers)
    log.info("event=duplicate_response status=%s body=%s", r2.status_code, r2.json())

    assert r2.status_code == 201
    assert r2.json() == r1.json()
    assert client.app.state.store.side_effect_count == 1
    log.info("event=verdict basis=相同键+相同体第二次回放 side_effect_count=1")


def test_concurrent_duplicate_requests_execute_once(slow_app):
    """8 个相同 Idempotency-Key 的创建请求在同一事件循环中同时到达。

    预期时序：1 个 EXECUTE，其余在其 IN_FLIGHT 期间 acquire 到 IN_FLIGHT 并等待，
    首个完成后全部 REPLAY；真实副作用只发生一次。
    """
    count = 8
    key = "key-concurrent-1"
    log.info(
        "event=test_case case=concurrent_duplicate concurrency=%s key=%s input=%s",
        count,
        key,
        CREATE_BODY,
    )

    async def make_request(http, index):
        return await http.post(
            "/api/v2/documents", json=CREATE_BODY, headers={"Idempotency-Key": key}
        )

    results = run_async(gather_concurrently(slow_app, make_request, count))

    statuses = [r.status_code for _, r in results]
    encoded = [json.dumps(r.json(), sort_keys=True, ensure_ascii=False) for _, r in results]
    log.info(
        "event=concurrent_verdict statuses=%s unique_bodies=%s side_effect_count=%s",
        statuses,
        len(set(encoded)),
        slow_app.state.store.side_effect_count,
    )

    assert statuses == [201] * count
    assert len(set(encoded)) == 1, "所有并发请求必须回放完全一致的结果"
    assert slow_app.state.store.side_effect_count == 1, "真实副作用只能发生一次"

    # 最终状态：资源唯一且内容正确
    from fastapi.testclient import TestClient

    with TestClient(slow_app) as c:
        got = c.get("/api/v2/documents/doc-concurrent")
    assert got.status_code == 200
    assert got.json()["revision"] == 0
    assert got.json()["body"] == "并发正文"
    log.info("event=verdict basis=1个EXECUTE+其余IN_FLIGHT等待后REPLAY side_effect_count=1 final_revision=0")


def test_same_key_different_payload_is_conflict(client):
    """相同幂等键但请求体不同：必须以 IDEMPOTENCY_CONFLICT 明确拒绝。"""
    body_a = dict(CREATE_BODY, id="doc-conflict-a")
    body_b = dict(CREATE_BODY, id="doc-conflict-b", body="不同的正文")
    log.info("event=test_case case=idempotency_conflict body_a=%s body_b=%s", body_a, body_b)

    r1 = client.post(
        "/api/v2/documents", json=body_a, headers={"Idempotency-Key": "key-conflict"}
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/v2/documents", json=body_b, headers={"Idempotency-Key": "key-conflict"}
    )
    log.info(
        "event=conflict_response status=%s code=%s details=%s",
        r2.status_code,
        r2.json()["error"]["code"],
        r2.json()["error"]["details"],
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert client.app.state.store.side_effect_count == 1
    log.info("event=verdict basis=同键指纹不同->CONFLICT side_effect_count=1")


def test_concurrent_update_with_same_key_replays(slow_app, slow_client):
    """并发重复更新（PUT）携带同一幂等键：只执行一次替换，revision 只前进一格。"""
    created = slow_client.post(
        "/api/v2/documents",
        json=CREATE_BODY,
        headers={"Idempotency-Key": "key-create"},
    )
    assert created.status_code == 201

    update_body = {
        "title": "并发标题",
        "body": "更新后的正文",
        "owner": "alice",
        "tags": ["a", "b"],
        "revision": 0,
    }
    count = 6
    key = "key-update-concurrent"
    log.info(
        "event=test_case case=concurrent_duplicate_update concurrency=%s input=%s",
        count,
        update_body,
    )

    async def make_request(http, index):
        return await http.put(
            "/api/v2/documents/doc-concurrent",
            json=update_body,
            headers={"Idempotency-Key": key},
        )

    results = run_async(gather_concurrently(slow_app, make_request, count))
    statuses = [r.status_code for _, r in results]
    revisions = [r.json()["revision"] for _, r in results]
    log.info("event=concurrent_update_verdict statuses=%s revisions=%s", statuses, revisions)

    assert statuses == [200] * count
    assert revisions == [1] * count
    assert slow_app.state.store.side_effect_count == 2  # 一次 create + 一次 replace
    log.info("event=verdict basis=并发更新只一次replace side_effect_count=2 final_revision=1")
