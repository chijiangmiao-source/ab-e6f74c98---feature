"""复勘 HTTP 接口测试：真实 ASGI 调用，覆盖结论字段、锁定与报错定位。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


RECHECK_URL = "/api/recheck"


def payload(**overrides):
    p = {
        "approvedSegments": [
            {"id": "a1", "from": "S", "to": "A", "delay": 1},
            {"id": "a2", "from": "A", "to": "T", "delay": 2},
        ],
        "recheckSegments": [
            {"id": "r1", "from": "p", "to": "q", "delay": 1},
            {"id": "r2", "from": "q", "to": "s", "delay": 2},
        ],
        "locks": [],
    }
    p.update(overrides)
    return p


def test_recheck_basic_conclusion():
    r = client.post(RECHECK_URL, json=payload())
    assert r.status_code == 200, str(r.json())
    data = r.json()
    assert data["status"] == "ok"
    assert dict((m["code"], m["target"]) for m in data["mapping"]) == {
        "p": "S",
        "q": "A",
        "s": "T",
    }
    assert data["metrics"]["exactCount"] == 2
    assert data["metrics"]["delayDiffSum"] == 0
    assert data["optimalCount"] == 1
    assert {p["recheck"]["id"] for p in data["exactPairs"]} == {"r1", "r2"}
    assert data["delayMismatches"] == []
    assert data["unmatchedRecheck"] == []
    assert data["unmatchedApproved"] == []
    # 全部代号必然
    assert all(v == "fixed" for v in data["certainty"].values())


def test_recheck_delay_mismatch_and_diff():
    p = payload()
    p["recheckSegments"][1]["delay"] = 9
    data = client.post(RECHECK_URL, json=p).json()
    assert data["metrics"] == {"exactCount": 1, "delayDiffSum": 7}
    mm = data["delayMismatches"]
    assert len(mm) == 1
    assert mm[0]["recheck"]["id"] == "r2"
    assert mm[0]["approved"]["id"] == "a2"
    assert mm[0]["diff"] == 7


def test_recheck_unmatched_both_sides():
    # 度序列不一致的错接：批准侧 T 入度 2，复勘侧 p 出度 2
    p = {
        "approvedSegments": [
            {"id": "a1", "from": "S", "to": "A", "delay": 1},
            {"id": "a2", "from": "A", "to": "T", "delay": 1},
            {"id": "a3", "from": "B", "to": "T", "delay": 1},
        ],
        "recheckSegments": [
            {"id": "r1", "from": "p", "to": "q", "delay": 1},
            {"id": "r2", "from": "q", "to": "s", "delay": 1},
            {"id": "r3", "from": "p", "to": "r", "delay": 1},
        ],
        "locks": [],
    }
    data = client.post(RECHECK_URL, json=p).json()
    assert data["metrics"]["exactCount"] == 2
    assert [s["id"] for s in data["unmatchedApproved"]] == ["a3"] or \
        len(data["unmatchedApproved"]) == 1
    assert len(data["unmatchedRecheck"]) == 1


def test_recheck_symmetry_optional_targets():
    p = {
        "approvedSegments": [
            {"id": "a1", "from": "S", "to": "A", "delay": 1},
            {"id": "a2", "from": "S", "to": "B", "delay": 1},
            {"id": "a3", "from": "T", "to": "A", "delay": 1},
            {"id": "a4", "from": "T", "to": "B", "delay": 1},
        ],
        "recheckSegments": [
            {"id": "r1", "from": "p", "to": "q", "delay": 1},
            {"id": "r2", "from": "p", "to": "r", "delay": 1},
            {"id": "r3", "from": "s", "to": "q", "delay": 1},
            {"id": "r4", "from": "s", "to": "r", "delay": 1},
        ],
        "locks": [],
    }
    data = client.post(RECHECK_URL, json=p).json()
    assert data["optimalCount"] == 4
    assert sorted(data["options"]["p"]) == ["S", "T"]
    assert data["certainty"]["p"] == "optional"


def test_recheck_lock_respected():
    # q 锁到 T：真实对应 q=A 的结论被排除，改取满足锁定的最优
    p = payload(locks=[{"code": "q", "target": "T"}])
    data = client.post(RECHECK_URL, json=p).json()
    mapping = dict((m["code"], m["target"]) for m in data["mapping"])
    assert mapping["q"] == "T"


def test_recheck_lock_conflict_returns_400():
    """重复锁定目标违反一一对应 -> 400 且定位。"""
    p = {
        "approvedSegments": [
            {"id": "a1", "from": "S", "to": "A", "delay": 1},
            {"id": "a2", "from": "A", "to": "T", "delay": 1},
        ],
        "recheckSegments": [
            {"id": "r1", "from": "p", "to": "q", "delay": 1},
            {"id": "r2", "from": "q", "to": "s", "delay": 1},
        ],
        "locks": [
            {"code": "p", "target": "S"},
            {"code": "s", "target": "S"},
        ],
    }
    r = client.post(RECHECK_URL, json=p)
    assert r.status_code == 400
    assert any(e["loc"] == "locks[1].target" for e in r.json()["errors"])


def test_recheck_duplicate_id_returns_400():
    p = payload()
    p["approvedSegments"][1]["id"] = "a1"
    r = client.post(RECHECK_URL, json=p)
    assert r.status_code == 400
    assert any(e["loc"].startswith("approvedSegments[1].id")
               for e in r.json()["errors"])


def test_recheck_unequal_node_sets_returns_400():
    p = payload()
    p["recheckSegments"].append(
        {"id": "r3", "from": "s", "to": "z", "delay": 1}
    )
    r = client.post(RECHECK_URL, json=p)
    assert r.status_code == 400
    assert any(e["loc"] == "recheckSegments" for e in r.json()["errors"])


def test_recheck_malformed_json_400():
    r = client.post(RECHECK_URL, content=b"{bad",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert any(e["loc"] == "body" for e in r.json()["errors"])


def test_recheck_page_section_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "recheck" in r.text
