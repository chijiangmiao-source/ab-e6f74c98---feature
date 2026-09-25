"""复勘请求体校验测试：锁定冲突、重复标识、端点集合不等、规模限制等。"""
from __future__ import annotations

from app.reconcile_validation import validate_reconcile_payload


def base_payload(locks=None):
    return {
        "approved": [
            {"id": "a1", "from": "A", "to": "B", "delay": 1},
            {"id": "a2", "from": "B", "to": "C", "delay": 2},
        ],
        "survey": [
            {"id": "s1", "from": "x", "to": "y", "delay": 1},
            {"id": "s2", "from": "y", "to": "z", "delay": 2},
        ],
        "locks": locks or [],
    }


def test_valid_passes():
    parsed, errors = validate_reconcile_payload(base_payload())
    assert errors == []
    approved, survey, an, sn, locks = parsed
    assert [s.id for s in approved] == ["a1", "a2"]
    assert [s.id for s in survey] == ["s1", "s2"]
    assert an == ["A", "B", "C"] and sn == ["x", "y", "z"]
    assert locks == []


def test_duplicate_segment_id_localized_per_topology():
    p = base_payload()
    p["survey"][1]["id"] = "s1"
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "survey[1].id" for e in errors)


def test_negative_delay_localized():
    p = base_payload()
    p["approved"][0]["delay"] = -3
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "approved[0].delay" for e in errors)


def test_self_loop_rejected():
    p = base_payload()
    p["approved"][1]["to"] = "B"
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "approved[1].to" for e in errors)


def test_endpoint_sets_unequal_size_localized():
    p = base_payload()
    p["approved"].append({"id": "a3", "from": "C", "to": "Q", "delay": 1})
    _, errors = validate_reconcile_payload(p)
    locs = [e["loc"] for e in errors]
    assert "survey" in locs or "approved" in locs
    assert any("端点集合不相等" in e["message"] for e in errors)


def test_endpoint_count_bounds():
    # 只有 1 个端点（自环已另禁）：用两个同端点的段无法构造，
    # 直接造一个单端点场景需要自环；改为测 12 端点上限。
    p = base_payload()
    p["approved"] = [
        {"id": f"a{i}", "from": f"A{i}", "to": "hub", "delay": 0}
        for i in range(11)
    ]  # 12 个端点
    p["survey"] = [
        {"id": f"s{i}", "from": f"x{i}", "to": "z", "delay": 0}
        for i in range(11)
    ]
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] in ("approved", "survey") for e in errors)
    assert any("端点" in e["message"] for e in errors)


def test_too_many_segments():
    p = base_payload()
    p["approved"] = [
        {"id": f"a{i}", "from": "A", "to": f"B{i % 2}", "delay": 0}
        for i in range(25)
    ]
    # 端点集合大小需相等，避免噪声：给 survey 也补齐规模与端点数。
    p["survey"] = [
        {"id": f"s{i}", "from": "x", "to": f"y{i % 2}", "delay": 0}
        for i in range(25)
    ]
    _, errors = validate_reconcile_payload(p)
    assert any("24" in e["message"] for e in errors)


def test_lock_duplicate_code_localized():
    p = base_payload(locks=[
        {"code": "x", "node": "A"},
        {"code": "x", "node": "B"},
    ])
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "locks[1].code" and "重复锁定" in e["message"]
               for e in errors)


def test_lock_duplicate_target_localized():
    p = base_payload(locks=[
        {"code": "x", "node": "A"},
        {"code": "y", "node": "A"},
    ])
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "locks[1].node" for e in errors)


def test_lock_unknown_code_localized():
    p = base_payload(locks=[{"code": "ghost", "node": "A"}])
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "locks[0].code" for e in errors)


def test_lock_unknown_target_localized():
    p = base_payload(locks=[{"code": "x", "node": "ghost"}])
    _, errors = validate_reconcile_payload(p)
    assert any(e["loc"] == "locks[0].node" for e in errors)


def test_non_object_body():
    _, errors = validate_reconcile_payload(["nope"])
    assert errors and errors[0]["loc"] == "body"


def test_missing_topology_localized():
    _, errors = validate_reconcile_payload({"approved": []})
    assert any(e["loc"] == "approved" for e in errors)
    assert any(e["loc"] == "survey" for e in errors)
