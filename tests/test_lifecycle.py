"""版本生命周期测试：弃用期语义不变、下线明确拒绝、边界结论可区分、运行期调整并发一致。"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi.testclient import TestClient

from app.app import create_app
from app.store import DocumentStore
from conftest import run_async

log = logging.getLogger("app")

FUTURE_SUNSET = "2099-01-01T00:00:00Z"
PAST_SUNSET = "2020-01-01T00:00:00Z"

V1_CREATE = {"id": "doc-lc", "title": "标题", "content": "正文", "created_by": "alice"}
V1_FIELDS = {"id", "title", "content", "created_by", "rev"}


def _deprecate_v1(client, sunset_at=FUTURE_SUNSET):
    r = client.put(
        "/api/versions/1/lifecycle",
        json={"state": "deprecated", "successor": 2, "sunset_at": sunset_at},
    )
    assert r.status_code == 200, r.text
    log.info("event=test_adjust action=deprecate version=1 sunset_at=%s", sunset_at)
    return r


def _retire_v1(client):
    r = client.put("/api/versions/1/lifecycle", json={"state": "sunset"})
    assert r.status_code == 200, r.text
    log.info("event=test_adjust action=retire version=1")
    return r


def test_deprecated_version_serves_identical_semantics_with_deprecation_headers(client):
    """弃用期内：读写行为与字段语义和弃用前完全一致，响应头携带弃用信息。"""
    # 弃用前的基线行为
    created = client.post("/api/v1/documents", json=V1_CREATE)
    assert created.status_code == 201
    baseline_body = created.json()
    assert "deprecation" not in created.headers
    log.info("event=baseline_before_deprecation body=%s", baseline_body)

    _deprecate_v1(client)

    # 弃用期内：写（PUT）与读（GET）字段集合、字段名、取值完全不变
    updated = client.put(
        "/api/v1/documents/doc-lc",
        json={"title": "标题", "content": "改写", "created_by": "alice", "rev": 0},
    )
    assert updated.status_code == 200
    body = updated.json()
    log.info(
        "event=deprecated_write_response body=%s deprecation=%s sunset=%s link=%s",
        body,
        updated.headers.get("deprecation"),
        updated.headers.get("sunset"),
        updated.headers.get("link"),
    )
    assert set(body) == V1_FIELDS, "弃用后字段集合不得变化"
    assert body["content"] == "改写" and body["rev"] == 1
    for forbidden in ("tags", "body", "owner", "revision", "created_at", "updated_at"):
        assert forbidden not in body, "弃用不得让 v1 泄漏 v2 字段"

    got = client.get("/api/v1/documents/doc-lc")
    assert got.status_code == 200 and set(got.json()) == V1_FIELDS

    # 调用方能从响应头看出：已弃用、建议迁到哪、什么时候下线
    for resp in (updated, got):
        assert resp.headers.get("deprecation"), "弃用版本响应必须带 Deprecation 头"
        assert resp.headers.get("sunset") == "Thu, 01 Jan 2099 00:00:00 GMT"
        assert 'rel="successor-version"' in resp.headers.get("link", "")
        assert "/api/v2/" in resp.headers["link"]

    # 未弃用的 v2 不携带任何弃用信号
    v2 = client.get("/api/v2/documents/doc-lc")
    assert v2.status_code == 200
    assert "deprecation" not in v2.headers and "sunset" not in v2.headers
    log.info("event=verdict basis=弃用期字段集/取值与基线一致且仅响应头新增弃用信号")


def test_sunset_version_requests_rejected_with_distinguishable_error(client):
    """下线后：该版本所有方法的请求都以 VERSION_SUNSET（410）拒绝并带追踪标识。"""
    client.post("/api/v1/documents", json=V1_CREATE)
    _deprecate_v1(client)
    _retire_v1(client)

    cases = [
        ("GET", "/api/v1/documents/doc-lc", None),
        ("POST", "/api/v1/documents", V1_CREATE),
        ("PUT", "/api/v1/documents/doc-lc", {"title": "t", "content": "c", "created_by": "a", "rev": 1}),
        ("GET", "/api/v1", None),
    ]
    for method, path, payload in cases:
        r = client.request(method, path, json=payload)
        err = r.json()["error"]
        log.info(
            "event=sunset_verdict method=%s path=%s status=%s code=%s details=%s",
            method, path, r.status_code, err["code"], err["details"],
        )
        assert r.status_code == 410
        assert err["code"] == "VERSION_SUNSET"
        assert err["details"]["requested_version"] == 1
        assert err["details"]["successor"] == 2
        assert err["details"]["sunset_at"]
        assert err["trace_id"] and err["trace_id"] == r.headers["x-trace-id"]

    # 已产生的副作用不丢：同一资源在 v2 下仍可读
    survived = client.get("/api/v2/documents/doc-lc")
    assert survived.status_code == 200 and survived.json()["id"] == "doc-lc"
    log.info("event=verdict basis=下线拒绝410且既有数据在v2可读")


def test_sunset_time_reached_automatically_rejects(client):
    """弃用版本的下线时间一到，无需人工操作即按已下线拒绝。"""
    _deprecate_v1(client, sunset_at=PAST_SUNSET)
    r = client.get("/api/v1/documents/anything")
    log.info("event=auto_sunset status=%s code=%s", r.status_code, r.json()["error"]["code"])
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "VERSION_SUNSET"


def test_sunset_time_can_be_brought_forward_or_postponed(client):
    """运行期调整：下线时间可提前（立即生效）也可延后（恢复服务），查询端点同步反映。"""
    _deprecate_v1(client)
    assert client.get("/api/v1/documents/x").status_code == 404  # 服务中，仅资源不存在

    # 提前：把下线时间改到过去 -> 立即拒绝
    r = client.put("/api/versions/1/lifecycle", json={"state": "deprecated", "sunset_at": PAST_SUNSET})
    assert r.status_code == 200 and r.json()["sunset_at"].startswith("2020-01-01")
    rejected = client.get("/api/v1/documents/x")
    log.info("event=reschedule_forward status=%s code=%s", rejected.status_code, rejected.json()["error"]["code"])
    assert rejected.status_code == 410

    # 延后：把下线时间改回未来 -> 恢复服务（仍为弃用态，带头）
    r = client.put("/api/versions/1/lifecycle", json={"state": "deprecated", "sunset_at": FUTURE_SUNSET})
    assert r.status_code == 200
    back = client.get("/api/v1/documents/x")
    log.info("event=reschedule_postpone status=%s deprecation=%s", back.status_code, back.headers.get("deprecation"))
    assert back.status_code == 404  # 恢复服务（资源不存在而非版本被拒）
    assert back.headers.get("deprecation")

    # 查询端点能看到当前状态与下线时间
    listing = client.get("/api/versions").json()
    log.info("event=versions_listing body=%s", listing)
    v1 = next(v for v in listing["versions"] if v["version"] == 1)
    assert v1["state"] == "deprecated"
    assert v1["sunset_at"].startswith("2099-01-01")
    assert v1["successor"] == 2
    assert listing["latest_version"] == 2


def test_boundary_verdicts_are_distinguishable_and_traced(client):
    """五类边界结论互不相同、各带追踪标识，且都落在同一错误结构里。"""
    _retire_v1(client)
    cases = [
        ("GET", "/api/v1/documents/x", 410, "VERSION_SUNSET"),        # 已下线
        ("GET", "/api/v9/documents/x", 400, "VERSION_NOT_SUPPORTED"),  # 从未支持
        ("GET", "/api/vx/documents/x", 400, "VALIDATION_ERROR"),       # 版本号写法非法
        ("GET", "/api/v2/no/such/path", 404, "NOT_FOUND"),             # 路径不存在
        ("DELETE", "/api/v2/documents/x", 405, "METHOD_NOT_ALLOWED"),  # 方法不支持
    ]
    seen: dict[str, str] = {}
    for method, path, want_status, want_code in cases:
        r = client.request(method, path)
        err = r.json()["error"]
        log.info(
            "event=boundary_verdict path=%s status=%s code=%s trace_id=%s",
            path, r.status_code, err["code"], err["trace_id"],
        )
        assert r.status_code == want_status, (path, r.status_code, r.text)
        assert err["code"] == want_code
        # 同一套错误结构 + 可追踪标识
        assert set(err) == {"code", "message", "details", "trace_id"}
        assert err["trace_id"] and err["trace_id"] == r.headers["x-trace-id"]
        seen[path] = err["code"]
    assert len(set(seen.values())) == len(cases), "五类结论必须两两可区分"
    log.info("event=verdict basis=五类边界code两两不同且均带trace_id codes=%s", sorted(seen.values()))


def test_concurrent_requests_consistent_during_runtime_retirement():
    """弃用中的版本在并发流量下被运行期下线：每个请求结论自洽，副作用不重复不丢失。

    时序：6 个创建请求错开 0.2s 出发，0.5s 时把 v1 下线；
    前 3 个在下线前拿到快照（完整成功、带弃用头），后 3 个被 410 拒绝。
    """
    async def scenario():
        async def delay_every_create(document_id: str) -> None:
            await asyncio.sleep(0.2)

        app = create_app(store=DocumentStore(before_create=delay_every_create))
        await app.state.versions.deprecate(1, successor=2, sunset_at=FUTURE_SUNSET)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

            async def create(index: int):
                await asyncio.sleep(index * 0.2)
                started = time.perf_counter()
                body = dict(V1_CREATE, id=f"doc-conc-{index}")
                log.info("event=concurrent_start index=%s input=%s", index, body)
                resp = await http.post("/api/v1/documents", json=body)
                log.info(
                    "event=concurrent_done index=%s status=%s elapsed_ms=%.0f deprecation=%s",
                    index, resp.status_code, (time.perf_counter() - started) * 1000,
                    resp.headers.get("deprecation"),
                )
                return index, resp

            async def retire_midway():
                await asyncio.sleep(0.5)
                log.info("event=test_adjust action=retire version=1 during=concurrent_traffic")
                await app.state.versions.retire(1)

            results = await asyncio.gather(*[create(i) for i in range(6)], retire_midway())
        return app, sorted(results[:6], key=lambda x: x[0])

    app, results = run_async(scenario())

    succeeded = [r for _, r in results if r.status_code == 201]
    rejected = [r for _, r in results if r.status_code == 410]
    log.info(
        "event=concurrent_lifecycle_verdict succeeded=%s rejected=%s side_effect_count=%s",
        len(succeeded), len(rejected), app.state.store.side_effect_count,
    )

    # 每个请求的结论都必须自洽：要么完整成功（v1 全字段 + 弃用头），要么明确 410
    assert len(succeeded) + len(rejected) == 6
    assert len(succeeded) == 3 and len(rejected) == 3
    for r in succeeded:
        assert set(r.json()) == V1_FIELDS, "成功响应不得因并发调整缺字段/改字段"
        assert r.headers.get("deprecation"), "下线前快照为弃用态，响应必须带弃用头"
    for r in rejected:
        err = r.json()["error"]
        assert err["code"] == "VERSION_SUNSET"
        assert err["trace_id"] == r.headers["x-trace-id"]

    # 副作用恰好等于成功次数：不重复、不丢失
    assert app.state.store.side_effect_count == len(succeeded)

    # 已成功写入的数据在 v2 下全部可读
    with TestClient(app) as c:
        for r in succeeded:
            got = c.get(f"/api/v2/documents/{r.json()['id']}")
            assert got.status_code == 200
    log.info("event=verdict basis=3成功(字段完整+弃用头)+3个410 side_effect_count=3且v2可读")
