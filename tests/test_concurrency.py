"""乐观并发：陈旧 revision 被拒绝；互相覆盖的并发更新只有一个成功。"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("contract.test")


def _seed(client, key="seed", title="t", content="v0"):
    resp = client.post("/api/v2/notes", json={"title": title, "content": content},
                       headers={"Idempotency-Key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_stale_update_rejected(client):
    note = _seed(client)
    note_id, rev = note["id"], note["revision"]
    logger.info("用例输入：note=%s 初始revision=%s", note_id, rev)

    # 第一次更新成功，revision 1 -> 2
    ok = client.put(f"/api/v2/notes/{note_id}", json={"content": "v1"},
                    headers={"If-Match": str(rev)})
    assert ok.status_code == 200
    assert ok.json()["revision"] == 2

    # 拿着旧 revision=1 的更新必须被拒绝
    stale = client.put(f"/api/v2/notes/{note_id}", json={"content": "覆盖"},
                       headers={"If-Match": str(rev)})
    err = stale.json()["error"]
    logger.info("陈旧写入判定：status=%s code=%s details=%s",
                stale.status_code, err["code"], err["details"])
    assert stale.status_code == 412
    assert err["code"] == "PRECONDITION_FAILED"
    assert err["details"] == {
        "id": note_id, "expected_revision": 1, "current_revision": 2
    }
    # 最终状态未被陈旧写入污染
    final = client.get(f"/api/v2/notes/{note_id}").json()
    assert final["content"] == "v1" and final["revision"] == 2


def test_concurrent_updates_one_wins_one_rejected(client):
    note = _seed(client, key="seed-conc")
    note_id = note["id"]
    barrier = threading.Barrier(2)

    def updater(name: str, content: str):
        barrier.wait()  # 两个线程都基于 revision=1 同时提交
        logger.info("时序 %s 放行 PUT id=%s If-Match=1 content=%s",
                    name, note_id, content)
        resp = client.put(f"/api/v2/notes/{note_id}", json={"content": content},
                          headers={"If-Match": "1"})
        logger.info("时序 %s 完成 status=%s", name, resp.status_code)
        return resp.status_code, resp.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner, loser = list(pool.map(lambda f: f(), [
            lambda: updater("A", "A-win"),
            lambda: updater("B", "B-lost"),
        ]))

    statuses = sorted([winner[0], loser[0]])
    logger.info("并发更新判定：状态码=%s（必须恰好一胜一负）", statuses)
    assert statuses == [200, 412]

    final = client.get(f"/api/v2/notes/{note_id}").json()
    logger.info("最终状态 content=%r revision=%s（恰好一次写入生效）",
                final["content"], final["revision"])
    assert final["revision"] == 2
    assert final["content"] in {"A-win", "B-lost"}
    # 真实写库次数：创建 1 + 成功更新 1 = 2；被拒更新不落库
    assert client.app.state.repo.side_effect_count == 2


def test_if_match_required_for_writes(client):
    note = _seed(client, key="seed-pre")
    resp = client.put(f"/api/v2/notes/{note['id']}", json={"content": "x"})
    err = resp.json()["error"]
    logger.info("缺失前置条件判定：status=%s code=%s", resp.status_code, err["code"])
    assert resp.status_code == 428
    assert err["code"] == "PRECONDITION_REQUIRED"


def test_concurrent_delete_then_update(client):
    note = _seed(client, key="seed-del")
    note_id = note["id"]

    deleted = client.delete(f"/api/v2/notes/{note_id}", headers={"If-Match": "1"})
    assert deleted.status_code == 204

    stale_upd = client.put(f"/api/v2/notes/{note_id}", json={"content": "x"},
                           headers={"If-Match": "1"})
    logger.info("删除后更新判定：status=%s code=%s",
                stale_upd.status_code, stale_upd.json()["error"]["code"])
    assert stale_upd.status_code == 404
    assert stale_upd.json()["error"]["code"] == "NOT_FOUND"


def test_malformed_if_match_is_validation_error(client):
    note = _seed(client, key="seed-mal")
    resp = client.put(f"/api/v2/notes/{note['id']}", json={"content": "x"},
                      headers={"If-Match": "abc"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
