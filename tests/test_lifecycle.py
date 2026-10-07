"""版本生命周期测试：弃用期语义不变、下线拒绝、边界结论可区分、运行期调整并发一致。

通过注入可控时钟（FakeClock）让"到达下线时间"成为确定性事件，
并发用例在同一事件循环内发起真实并发请求，日志可见请求输入、时序与判定依据。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import contracts
from app.app import create_app
from app.errors import ErrorCode
from app.lifecycle import LifecycleRegistry
from app.store import DocumentStore
from conftest import error_code, run_async

log = logging.getLogger("app")

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T_SUNSET = datetime(2026, 6, 1, tzinfo=timezone.utc)
T_AFTER_SUNSET = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_FAR_FUTURE = datetime(2027, 1, 1, tzinfo=timezone.utc)

V1_CREATE = {"id": "doc-lc", "title": "标题", "content": "正文", "created_by": "alice"}


@dataclass
class FakeClock:
    """可控时钟：测试显式推进"现在"，让下线时刻确定性触发。"""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture()
def lc_app():
    clock = FakeClock(T0)
    registry = LifecycleRegistry(contracts.SUPPORTED_VERSIONS, clock=clock)
    app = create_app(lifecycle=registry)
    app.state.fake_clock = clock
    return app


@pytest.fixture()
def lc_client(lc_app):
    with TestClient(lc_app) as c:
        yield c


def _deprecate_v1(client, sunset_at=T_SUNSET) -> None:
    r = client.put(
        "/admin/versions/1/lifecycle",
        json={
            "deprecated": True,
            "successor_version": 2,
            "sunset_at": sunset_at.isoformat(),
        },
    )
    log.info("event=setup_deprecate status=%s body=%s", r.status_code, r.json())
    assert r.status_code == 200


def test_deprecated_version_read_write_semantics_unchanged(lc_client):
    """弃用期内：读写行为、字段集合与弃用前完全一致，弃用信息只出现在响应头。"""
    before = lc_client.post("/api/v1/documents", json=V1_CREATE)
    assert before.status_code == 201
    body_before = before.json()
    assert "deprecation" not in before.headers  # 弃用前无告知头

    _deprecate_v1(lc_client)

    # 读：字段集合与值与弃用前完全一致
    read = lc_client.get("/api/v1/documents/doc-lc")
    log.info(
        "event=deprecated_read status=%s body=%s headers=%s",
        read.status_code,
        read.json(),
        {k: read.headers.get(k) for k in ("deprecation", "sunset", "link")},
    )
    assert read.status_code == 200
    assert read.json() == body_before
    assert set(read.json()) == {"id", "title", "content", "created_by", "rev"}

    # 写：整体替换的字段契约与乐观锁语义不变
    put = lc_client.put(
        "/api/v1/documents/doc-lc",
        json={"title": "新", "content": "新正文", "created_by": "alice", "rev": 0},
    )
    log.info("event=deprecated_write status=%s body=%s", put.status_code, put.json())
    assert put.status_code == 200
    assert set(put.json()) == {"id", "title", "content", "created_by", "rev"}
    assert put.json()["rev"] == 1

    # 调用方能从响应头看出：已弃用、建议迁到哪、什么时候下线
    for response in (read, put):
        assert response.headers["deprecation"] == "true"
        assert response.headers["sunset"] == "Mon, 01 Jun 2026 00:00:00 GMT"
        assert response.headers["link"] == '</api/v2>; rel="successor-version"'
        # 响应体本身不出现任何弃用字段，结果不被污染
        assert "deprecation" not in response.json()

    # 弃用版本上的错误响应同样携带告知头，且错误语义不变
    missing = lc_client.get("/api/v1/documents/nope")
    log.info(
        "event=deprecated_error status=%s code=%s deprecation_header=%s",
        missing.status_code,
        error_code(missing),
        missing.headers.get("deprecation"),
    )
    assert missing.status_code == 404
    assert error_code(missing) == "NOT_FOUND"
    assert missing.headers["deprecation"] == "true"


def test_sunset_version_requests_rejected(lc_app, lc_client):
    """到达下线时间后：该版本所有方法都被 410 VERSION_SUNSET 明确拒绝，其它版本不受影响。"""
    assert lc_client.post("/api/v1/documents", json=V1_CREATE).status_code == 201
    _deprecate_v1(lc_client)

    lc_app.state.fake_clock.now = T_AFTER_SUNSET
    log.info("event=clock_advanced now=%s sunset_at=%s", T_AFTER_SUNSET, T_SUNSET)

    cases = [
        ("GET", "/api/v1/documents/doc-lc", None),
        ("POST", "/api/v1/documents", V1_CREATE),
        ("PUT", "/api/v1/documents/doc-lc", {"title": "t", "content": "c", "created_by": "a", "rev": 0}),
    ]
    for method, path, payload in cases:
        r = lc_client.request(method, path, json=payload)
        err = r.json()["error"]
        log.info(
            "event=sunset_verdict method=%s path=%s status=%s code=%s details=%s",
            method, path, r.status_code, err["code"], err["details"],
        )
        assert r.status_code == 410
        assert err["code"] == "VERSION_SUNSET"
        assert err["details"]["status"] == "sunset"
        assert err["details"]["sunset_at"] == T_SUNSET.isoformat()
        assert err["details"]["successor_version"] == 2
        assert err["trace_id"] and err["trace_id"] == r.headers["x-trace-id"]

    # 已落库数据不丢失：v2 仍能读到 v1 下线前创建的文档
    v2_read = lc_client.get("/api/v2/documents/doc-lc")
    assert v2_read.status_code == 200 and v2_read.json()["body"] == "正文"
    log.info("event=side_effect_preserved v2_read_status=%s", v2_read.status_code)


def test_boundary_conclusions_are_distinguishable_and_traced(lc_app, lc_client):
    """五类边界结论互不相同、与实际原因对应，且都落在统一错误结构并带追踪标识。"""
    _deprecate_v1(lc_client)
    lc_app.state.fake_clock.now = T_AFTER_SUNSET

    cases = [
        # (方法, 路径, 期望状态, 期望错误码, 实际原因)
        ("GET", "/api/v1/documents/x", 410, "VERSION_SUNSET", "版本已下线"),
        ("GET", "/api/v9/documents/x", 400, "VERSION_NOT_SUPPORTED", "版本从未支持"),
        ("GET", "/api/vx/documents/x", 400, "VALIDATION_ERROR", "版本号写法非法"),
        ("GET", "/api/v2/no-such-path", 404, "NOT_FOUND", "路径不存在"),
        ("DELETE", "/api/v2/documents/x", 405, "METHOD_NOT_ALLOWED", "方法不支持"),
    ]
    seen_codes: set[str] = set()
    for method, path, want_status, want_code, reason in cases:
        r = lc_client.request(method, path)
        err = r.json()["error"]
        log.info(
            "event=boundary_verdict reason=%s path=%s status=%s code=%s trace=%s",
            reason, path, r.status_code, err["code"], err["trace_id"],
        )
        assert r.status_code == want_status, (reason, r.status_code, r.text)
        assert err["code"] == want_code
        assert err["code"] in [c.value for c in ErrorCode]
        assert isinstance(err["message"], str) and err["message"]
        assert isinstance(err["details"], dict)
        # 可追踪：响应头与错误体 trace_id 一致且非空
        assert err["trace_id"]
        assert err["trace_id"] == r.headers["x-trace-id"]
        seen_codes.add(err["code"])
    assert len(seen_codes) == 5, f"五类结论必须彼此可区分: {seen_codes}"
    log.info("event=boundary_summary distinct_codes=%s", sorted(seen_codes))


def test_lifecycle_status_query_and_runtime_adjustment(lc_app, lc_client):
    """状态可查：active -> deprecated -> sunset ->（延后下线）-> deprecated 全程可见。"""
    def statuses() -> dict[int, dict]:
        body = lc_client.get("/admin/versions").json()
        return {v["version"]: v for v in body["versions"]}

    assert statuses()[1]["status"] == "active"

    _deprecate_v1(lc_client)
    v1 = statuses()[1]
    log.info("event=status_query stage=deprecated record=%s", v1)
    assert v1["status"] == "deprecated"
    assert v1["sunset_at"] == T_SUNSET.isoformat()
    assert v1["successor_version"] == 2

    lc_app.state.fake_clock.now = T_AFTER_SUNSET
    assert statuses()[1]["status"] == "sunset"

    # 运行期延后下线：状态立即回到 deprecated，请求恢复服务
    r = lc_client.put(
        "/admin/versions/1/lifecycle", json={"sunset_at": T_FAR_FUTURE.isoformat()}
    )
    assert r.status_code == 200
    v1 = statuses()[1]
    log.info("event=status_query stage=postponed record=%s", v1)
    assert v1["status"] == "deprecated"
    assert v1["sunset_at"] == T_FAR_FUTURE.isoformat()
    assert lc_client.post("/api/v1/documents", json=V1_CREATE).status_code == 201

    # 非法调整被拒绝且原因明确
    bad_time = lc_client.put("/admin/versions/1/lifecycle", json={"sunset_at": "not-a-time"})
    assert bad_time.status_code == 400 and error_code(bad_time) == "VALIDATION_ERROR"
    unknown_version = lc_client.put("/admin/versions/9/lifecycle", json={"sunset_at": None})
    assert unknown_version.status_code == 400
    assert error_code(unknown_version) == "VERSION_NOT_SUPPORTED"
    bad_successor = lc_client.put(
        "/admin/versions/1/lifecycle", json={"deprecated": True, "successor_version": 9}
    )
    assert bad_successor.status_code == 400
    assert error_code(bad_successor) == "VALIDATION_ERROR"
    log.info("event=admin_validation_verdict all_rejected_with_distinct_reasons=true")


def test_concurrent_requests_consistent_during_sunset_adjustment():
    """状态调整与并发请求交错：在途请求按其进入时的快照一致完成，副作用不重复不丢失。"""
    state = {"creates": 0}

    async def before_create(document_id: str) -> None:
        state["creates"] += 1
        if state["creates"] == 1:
            log.info("event=test_delay reason=首个写入让出事件循环制造调整窗口")
            await asyncio.sleep(0.4)

    clock = FakeClock(T0)
    registry = LifecycleRegistry(contracts.SUPPORTED_VERSIONS, clock=clock)
    app = create_app(store=DocumentStore(before_create=before_create), lifecycle=registry)

    with TestClient(app) as client:
        _deprecate_v1(client)

    payload = dict(V1_CREATE, id="doc-conc")
    headers = {"Idempotency-Key": "lc-conc-1"}
    log.info("event=test_case input=%s idempotency_key=%s", payload, headers)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            async def post(index: int) -> httpx.Response:
                r = await http.post("/api/v1/documents", json=payload, headers=headers)
                log.info(
                    "event=concurrent_worker_finished index=%s status=%s deprecation=%s",
                    index, r.status_code, r.headers.get("deprecation"),
                )
                return r

            tasks = [asyncio.create_task(post(i)) for i in range(6)]
            await asyncio.sleep(0.1)  # 首个请求 IN_FLIGHT，其余已按弃用期快照进入
            # 运行期调整：时钟越过下线时间 -> 版本立即下线
            clock.now = T_AFTER_SUNSET
            adjust = await http.put(
                "/admin/versions/1/lifecycle",
                json={"sunset_at": T_SUNSET.isoformat()},
            )
            log.info(
                "event=mid_flight_adjust status=%s record=%s",
                adjust.status_code, adjust.json(),
            )
            responses = await asyncio.gather(*tasks)
            after = await http.get("/api/v1/documents/doc-conc")
            return responses, after

    responses, after = run_async(scenario())

    # 在途请求：按其进入时的弃用期快照一致完成（全部 201、同一响应体、带弃用头）
    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]
    bodies = {json.dumps(r.json(), sort_keys=True) for r in responses}
    assert len(bodies) == 1, f"并发响应必须一致: {bodies}"
    assert all(r.headers.get("deprecation") == "true" for r in responses)
    # 副作用恰好一次，没有因状态调整被重复计算
    assert app.state.store.side_effect_count == 1
    # 调整后进入的请求：一致地被拒绝，结论与原因对应
    assert after.status_code == 410 and error_code(after) == "VERSION_SUNSET"
    assert after.json()["error"]["trace_id"] == after.headers["x-trace-id"]
    log.info(
        "event=consistency_verdict in_flight=all_201 side_effects=%s after_adjust=%s",
        app.state.store.side_effect_count,
        error_code(after),
    )
