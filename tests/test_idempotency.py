"""并发重复提交：同键只产生一次副作用，重放结果一致；异指纹复用被拒。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

logger = logging.getLogger("contract.test")

N = 8


def _concurrent_post(client, key: str, payload: dict, version: str = "v2"):
    """N 个线程在同一个 Barrier 上同时放行，模拟并发重复提交。"""
    barrier = threading.Barrier(N)

    def one(i: int):
        barrier.wait()  # 等待所有线程就绪后同时发起
        logger.info("时序 t%s 放行 发起 POST key=%s 输入=%s", i, key, payload)
        resp = client.post(
            f"/api/{version}/notes", json=payload, headers={"Idempotency-Key": key}
        )
        logger.info("时序 t%s 完成 status=%s", i, resp.status_code)
        return resp.status_code, resp.json(), dict(resp.headers)

    with ThreadPoolExecutor(max_workers=N) as pool:
        return list(pool.map(one, range(N)))


def test_concurrent_same_idempotency_key_single_side_effect(client):
    key = "order-1001"
    payload = {"title": "并发单", "content": "相同内容", "tags": ["x"]}
    results = _concurrent_post(client, key, payload)

    statuses = [r[0] for r in results]
    bodies = [r[1] for r in results]
    exec_count = client.app.state.idempotency_store.execution_count
    side_effects = client.app.state.repo.side_effect_count

    logger.info(
        "判定依据：8 并发状态码=%s；幂等真实执行=%s；仓储副作用=%s",
        statuses, exec_count, side_effects,
    )
    assert statuses == [201] * N
    # 只有一次真实副作用，其余全部重放首次结果
    assert exec_count == 1
    assert side_effects == 1
    # 所有响应体逐字节一致（同一资源 id 与 revision）
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0]["id"] == "note-1"


def test_sequential_duplicate_returns_same_result(client):
    key = "order-2002"
    payload = {"title": "顺序重放", "content": "c"}
    first = client.post("/api/v2/notes", json=payload, headers={"Idempotency-Key": key})
    second = client.post("/api/v2/notes", json=payload, headers={"Idempotency-Key": key})
    logger.info("顺序重复判定：首次=%s 重放=%s 一致=%s",
                first.json(), second.json(), first.json() == second.json())
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert client.app.state.repo.side_effect_count == 1


def test_same_key_different_payload_rejected(client):
    key = "order-3003"
    client.post("/api/v2/notes", json={"title": "a", "content": "1"},
                headers={"Idempotency-Key": key})
    resp = client.post("/api/v2/notes", json={"title": "b", "content": "2"},
                       headers={"Idempotency-Key": key})
    err = resp.json()["error"]
    logger.info("异指纹复用判定：status=%s code=%s", resp.status_code, err["code"])
    assert resp.status_code == 422
    assert err["code"] == "IDEMPOTENCY_REUSE"
    assert err["details"]["key"] == key
    # 被拒绝的请求不产生额外副作用
    assert client.app.state.repo.side_effect_count == 1


def test_concurrent_same_key_accesed_via_two_entries(client):
    """同一幂等键分别走路径入口与头入口，仍然只执行一次、结论一致。"""
    key = "order-4004"
    payload = {"title": "跨入口", "content": "c"}
    barrier = threading.Barrier(2)

    def via_path():
        barrier.wait()
        return client.post("/api/v2/notes", json=payload,
                           headers={"Idempotency-Key": key})

    def via_header():
        barrier.wait()
        return client.post("/notes", json=payload,
                           headers={"Idempotency-Key": key, "Api-Version": "v2"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = list(pool.map(lambda f: f(), [via_path, via_header]))

    logger.info("跨入口判定：path=%s header=%s 副作用=%s",
                r1.json(), r2.json(), client.app.state.repo.side_effect_count)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert client.app.state.repo.side_effect_count == 1


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_idempotency_works_per_version(client, version):
    key = f"order-ver-{version}"
    payload = (
        {"title": "t", "body": "b"} if version == "v1"
        else {"title": "t", "content": "c", "tags": []}
    )
    results = _concurrent_post(client, key, payload, version=version)
    assert all(s == 201 for s, _, _ in results)
    assert client.app.state.idempotency_store.execution_count == 1
