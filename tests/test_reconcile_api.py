"""复勘 HTTP 接口测试：成功结论、歧义、锁定、各类 400 定位与旧接口回归。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def payload(approved, survey, locks=None):
    return {"approved": approved, "survey": survey, "locks": locks or []}


def seg(sid, u, v, d):
    return {"id": sid, "from": u, "to": v, "delay": d}


def test_reconcile_unique_mapping_full_result():
    body = payload(
        [seg("a1", "A", "B", 1), seg("a2", "B", "C", 2)],
        [seg("s1", "x", "y", 1), seg("s2", "y", "z", 2)],
    )
    r = client.post("/api/reconcile", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "ok"
    assert data["mappingCount"] == 1
    assert {m["code"]: m["node"] for m in data["mapping"]} == {
        "x": "A", "y": "B", "z": "C",
    }
    assert data["exactCount"] == 2
    assert data["delayCost"] == 0
    assert len(data["exactMatches"]) == 2
    assert data["unmatchedApproved"] == []
    assert data["unmatchedSurvey"] == []
    assert {c["code"] for c in data["certain"]} == {"x", "y", "z"}
    assert data["optional"] == []
    # 完全匹配段两侧都带完整段信息
    m = data["exactMatches"][0]
    assert {"id", "from", "to", "delay"} <= set(m["approved"])
    assert m["delayDiff"] == 0


def test_reconcile_symmetric_ambiguity():
    body = payload(
        [seg("a1", "C", "L1", 2), seg("a2", "C", "L2", 2)],
        [seg("s1", "c", "p", 2), seg("s2", "c", "q", 2)],
    )
    r = client.post("/api/reconcile", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["mappingCount"] == 2
    # 规范映射稳定：p->L1（字典序最小目标序列）
    assert {m["code"]: m["node"] for m in data["mapping"]} == {
        "c": "C", "p": "L1", "q": "L2",
    }
    opt = {o["code"]: o["nodes"] for o in data["optional"]}
    assert opt == {"p": ["L1", "L2"], "q": ["L1", "L2"]}
    assert [c["code"] for c in data["certain"]] == ["c"]


def test_reconcile_lock_changes_result():
    base = payload(
        [seg("a1", "C", "L1", 2), seg("a2", "C", "L2", 2)],
        [seg("s1", "c", "p", 2), seg("s2", "c", "q", 2)],
    )
    base["locks"] = [{"code": "p", "node": "L2"}]
    r = client.post("/api/reconcile", json=base)
    assert r.status_code == 200
    data = r.json()
    assert data["mappingCount"] == 1
    assert {m["code"]: m["node"] for m in data["mapping"]} == {
        "c": "C", "p": "L2", "q": "L1",
    }


def test_reconcile_lock_conflict_returns_400():
    # 同一原节点锁给两个代号（校验层去重前先经过重复目标检查；
    # 此处给不同字段组合，测无可行一一映射的定位反馈）。
    body = payload(
        [seg("a1", "A", "B", 1)],
        [seg("s1", "x", "y", 1)],
        [{"code": "x", "node": "A"}, {"code": "y", "node": "A"}],
    )
    r = client.post("/api/reconcile", json=body)
    assert r.status_code == 400
    assert any(e["loc"].startswith("locks[1]") for e in r.json()["errors"])


def test_reconcile_duplicate_id_400_localized():
    body = payload(
        [seg("dup", "A", "B", 1), seg("dup", "B", "C", 1)],
        [seg("s1", "x", "y", 1), seg("s2", "y", "z", 1)],
    )
    r = client.post("/api/reconcile", json=body)
    assert r.status_code == 400
    assert any(e["loc"] == "approved[1].id" for e in r.json()["errors"])


def test_reconcile_unequal_endpoint_sets_400():
    body = payload(
        [seg("a1", "A", "B", 1), seg("a2", "B", "C", 1),
         seg("a3", "C", "D", 1)],
        [seg("s1", "x", "y", 1), seg("s2", "y", "z", 1)],
    )
    r = client.post("/api/reconcile", json=body)
    assert r.status_code == 400
    msgs = " ".join(e["message"] for e in r.json()["errors"])
    assert "端点集合不相等" in msgs


def test_reconcile_malformed_json_400():
    r = client.post("/api/reconcile", content=b"{bad",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_reconcile_negative_delay_400_localized():
    body = payload([seg("a1", "A", "B", -1)], [seg("s1", "x", "y", 1)])
    r = client.post("/api/reconcile", json=body)
    assert r.status_code == 400
    assert any(e["loc"] == "approved[0].delay" for e in r.json()["errors"])


def test_reconcile_page_still_serves_legacy():
    r = client.get("/")
    assert r.status_code == 200
    assert "protected-paths" in r.text and "reconcile" in r.text


def test_legacy_protected_paths_unchanged():
    """旧双路规划接口行为回归：贪心反例仍得总延迟 12。"""
    r = client.post("/api/protected-paths", json={
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "A", "to": "B", "delay": 1},
            {"id": "e3", "from": "B", "to": "T", "delay": 1},
            {"id": "e4", "from": "S", "to": "B", "delay": 5},
            {"id": "e5", "from": "A", "to": "T", "delay": 5},
        ],
    })
    assert r.status_code == 200
    assert r.json()["totalDelay"] == 12
