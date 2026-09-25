"""复勘请求校验测试：定位到具体字段，并覆盖锁定冲突与端点数目不等。"""
from __future__ import annotations

from app.recheck_validation import validate_recheck_payload


def topo(segments):
    return segments


def base_payload(**overrides):
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


def test_valid_without_locks():
    parsed, errors = validate_recheck_payload(base_payload())
    assert errors == []
    approved, recheck, locks = parsed
    assert [s.id for s in approved] == ["a1", "a2"]
    assert [s.id for s in recheck] == ["r1", "r2"]
    assert locks == {}


def test_valid_locks_parsed():
    p = base_payload(locks=[{"code": "p", "target": "S"}])
    parsed, errors = validate_recheck_payload(p)
    assert errors == []
    assert parsed[2] == {"p": "S"}


def test_negative_delay_localized_per_topology():
    p = base_payload()
    p["recheckSegments"][0]["delay"] = -3
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "recheckSegments[0].delay" for e in errors)


def test_duplicate_id_localized_per_topology():
    p = base_payload()
    p["approvedSegments"][1]["id"] = "a1"
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "approvedSegments[1].id" for e in errors)


def test_self_loop_rejected():
    p = base_payload()
    p["approvedSegments"][0]["to"] = "S"
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "approvedSegments[0].to" for e in errors)


def test_endpoint_count_mismatch_localized():
    """端点集合不相等（数目不同）必须定位反馈。"""
    p = base_payload()
    p["recheckSegments"].append(
        {"id": "r3", "from": "s", "to": "x", "delay": 1}
    )
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "recheckSegments" for e in errors)
    assert any("端点集合不相等" in e["message"] for e in errors)


def test_node_and_segment_limits_enforced():
    # 12 个端点超出上限 11
    segments = [
        {"id": f"a{i}", "from": f"n{i}", "to": f"n{i+1}", "delay": 0}
        for i in range(11)
    ]
    p = base_payload()
    p["approvedSegments"] = segments
    p["recheckSegments"] = [
        {"id": f"r{i}", "from": f"c{i}", "to": f"c{i+1}", "delay": 0}
        for i in range(11)
    ]
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "approvedSegments" and "11" in e["message"]
               for e in errors)

    # 25 段超出上限 24
    seg25 = []
    nodes = ["S", "A", "B", "T"]
    i = 0
    while len(seg25) < 25:
        u, v = nodes[i % 4], nodes[(i + 1) % 4]
        if u != v:
            seg25.append({"id": f"a{i}", "from": u, "to": v, "delay": 0})
        i += 1
    p2 = base_payload()
    p2["approvedSegments"] = seg25
    p2["recheckSegments"] = [
        {"id": "r1", "from": "p", "to": "q", "delay": 1},
        {"id": "r2", "from": "q", "to": "s", "delay": 2},
    ]
    _, errors = validate_recheck_payload(p2)
    assert any("24" in e["message"] for e in errors)


def test_lock_unknown_code_localized():
    p = base_payload(locks=[{"code": "zz", "target": "S"}])
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "locks[0].code" for e in errors)


def test_lock_unknown_target_localized():
    p = base_payload(locks=[{"code": "p", "target": "ZZ"}])
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "locks[0].target" for e in errors)


def test_lock_duplicate_code_localized():
    p = base_payload(locks=[
        {"code": "p", "target": "S"},
        {"code": "p", "target": "A"},
    ])
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "locks[1].code" for e in errors)


def test_lock_two_codes_same_target_localized():
    """两个代号锁到同一目标违反一一对应，定位到第二条的 target。"""
    p = base_payload(locks=[
        {"code": "p", "target": "S"},
        {"code": "q", "target": "S"},
    ])
    _, errors = validate_recheck_payload(p)
    assert any(e["loc"] == "locks[1].target" for e in errors)
    assert any("一一对应" in e["message"] for e in errors)


def test_non_object_body_rejected():
    _, errors = validate_recheck_payload(["nope"])
    assert errors and errors[0]["loc"] == "body"


def test_missing_topology_localized():
    _, errors = validate_recheck_payload({"approvedSegments": []})
    assert any(e["loc"] == "approvedSegments" for e in errors)
    assert any(e["loc"] == "recheckSegments" for e in errors)
