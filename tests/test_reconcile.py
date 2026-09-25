"""复勘求解器测试：歧义、贪心反例、锁定、并行段、延迟错录与对拍。"""
from __future__ import annotations

import itertools
import random

import pytest

from app.reconcile import (
    TopoSegment,
    _pair_bucket,
    reconcile,
)


def seg(sid, u, v, d):
    return TopoSegment(sid, u, v, d)


# --------------------------------------------------------------------------
# 组内配对：同延迟交叉指派、延迟差最优
# --------------------------------------------------------------------------

def test_bucket_equal_delay_uses_cross_assignment():
    # [1,2,5] 对 [2,4,5]：单调拉链只得 1 个同延迟，
    # 交叉指派（1-4,2-2,5-5）可得 2 个；禁止就近贪心。
    pairs, ua, us = _pair_bucket(
        [(0, 1), (1, 2), (2, 5)], [(10, 2), (11, 4), (12, 5)]
    )
    exact = [(a, s) for a, s, eq, _ in pairs if eq]
    assert len(exact) == 2
    assert (1, 10) in exact   # 2 == 2
    assert (2, 12) in exact   # 5 == 5
    assert ua == [] and us == []


def test_bucket_forced_pairs_minimize_abs_diff():
    # 批准 [0,10]，复勘三条 [1,2,3]：必须配两对，最优为 (0,1),(10,3) 或
    # (0,1),(10,2)？最小总差：0-1 + 10-3 = 8；0-? 取最小可能组合。
    pairs, ua, us = _pair_bucket([(0, 0), (1, 10)], [(10, 1), (11, 2), (12, 3)])
    assert len(pairs) == 2
    assert sum(c for _, _, _, c in pairs) == 8
    assert len(us) == 1  # 复勘多出一段未配


def test_bucket_empty_side_all_unmatched():
    pairs, ua, us = _pair_bucket([(0, 1), (1, 2)], [])
    assert pairs == [] and ua == [0, 1] and us == []


# --------------------------------------------------------------------------
# 整图：唯一改名、对称歧义、贪心反例、延迟错录、错接
# --------------------------------------------------------------------------

def test_unique_rename_recovered():
    approved = [seg("e1", "A", "B", 1), seg("e2", "B", "C", 2),
                seg("e3", "C", "D", 3)]
    survey = [seg("f1", "x", "y", 1), seg("f2", "y", "z", 2),
              seg("f3", "z", "w", 3)]
    r = reconcile(approved, survey, ["A", "B", "C", "D"],
                  ["w", "x", "y", "z"], [])
    assert r.status == "ok"
    assert r.mapping_count == 1
    assert dict(r.mapping) == {"x": "A", "y": "B", "z": "C", "w": "D"}
    assert r.exact_count == 3
    assert r.delay_cost == 0
    assert not r.delay_pairs
    assert not r.unmatched_approved and not r.unmatched_survey
    assert set(r.certain) == {"x", "y", "z", "w"}
    assert r.optional == {}


def test_symmetric_ambiguity_count_and_options():
    # 中心 C 接两个同构叶 L1/L2，复勘 p/q 不可区分。
    approved = [seg("e1", "C", "L1", 2), seg("e2", "C", "L2", 2)]
    survey = [seg("f1", "c", "p", 2), seg("f2", "c", "q", 2)]
    r = reconcile(approved, survey, ["C", "L1", "L2"],
                  ["c", "p", "q"], [])
    assert r.mapping_count == 2
    assert r.certain == {"c": "C"}
    assert r.optional == {"p": ["L1", "L2"], "q": ["L1", "L2"]}
    # 规范映射按目标序列取字典序最小：p->L1, q->L2
    assert dict(r.mapping) == {"c": "C", "p": "L1", "q": "L2"}
    assert r.exact_count == 2


def test_lock_breaks_symmetry():
    approved = [seg("e1", "C", "L1", 2), seg("e2", "C", "L2", 2)]
    survey = [seg("f1", "c", "p", 2), seg("f2", "c", "q", 2)]
    r = reconcile(approved, survey, ["C", "L1", "L2"],
                  ["c", "p", "q"], [("p", "L1")])
    assert r.mapping_count == 1
    assert dict(r.mapping) == {"c": "C", "p": "L1", "q": "L2"}


def test_conflicting_locks_no_bijection():
    approved = [seg("e1", "A", "B", 1)]
    survey = [seg("f1", "x", "y", 1)]
    with pytest.raises(ValueError):
        reconcile(approved, survey, ["A", "B"], ["x", "y"],
                  [("x", "A"), ("y", "A")])


def test_local_greedy_misfit_counterexample():
    """逐段就近匹配会误配；必须整张映射择优。

    批准：A->B, B->C, A->C（延迟全 3）。
    复勘：x->y, z->y, x->z（延迟全 3）。
    结构同构要求 x=A, z=B, y=C，可配 3 对完全匹配；
    把 y 当 B 的就近猜测只能配 1 对。
    """
    approved = [seg("e1", "A", "B", 3), seg("e2", "B", "C", 3),
                seg("e3", "A", "C", 3)]
    survey = [seg("f1", "x", "y", 3), seg("f2", "z", "y", 3),
              seg("f3", "x", "z", 3)]
    r = reconcile(approved, survey, ["A", "B", "C"], ["x", "y", "z"], [])
    assert r.exact_count == 3
    assert dict(r.mapping) == {"x": "A", "y": "C", "z": "B"}


def test_delay_typo_classified_from_wrong_connection():
    # 锁定固定方向后：f1（x->y => A->B）同向但延迟录错（1 vs 5）；
    # f2（y->Z => B->D）在批准拓扑中不存在（错接），
    # 批准侧 a2（A->D）也无对应 => 两侧各留未匹配段。
    approved = [seg("e1", "A", "B", 5), seg("e2", "A", "D", 9)]
    survey = [seg("f1", "x", "y", 1), seg("f2", "y", "Z", 4)]
    locks = [("x", "A"), ("y", "B"), ("Z", "D")]
    r = reconcile(approved, survey, ["A", "B", "D"], ["x", "y", "Z"], locks)
    assert r.exact_count == 0
    assert len(r.delay_pairs) == 1
    p = r.delay_pairs[0]
    assert p.approved.id == "e1" and p.survey.id == "f1"
    assert p.delay_diff == 4
    assert r.delay_cost == 4
    assert [s.id for s in r.unmatched_approved] == ["e2"]
    assert [s.id for s in r.unmatched_survey] == ["f2"]


def test_opposite_direction_not_paired():
    # 锁定 x->A、y->B 后，复勘段 y->x（B->A）与批准 A->B 方向相反，不可配。
    approved = [seg("e1", "A", "B", 3)]
    survey = [seg("f1", "y", "x", 3)]
    r = reconcile(approved, survey, ["A", "B", "C"], ["x", "y", "z"],
                  [("x", "A"), ("y", "B"), ("z", "C")])
    assert r.exact_count == 0
    assert r.delay_pairs == []
    assert {s.id for s in r.unmatched_approved} == {"e1"}
    assert {s.id for s in r.unmatched_survey} == {"f1"}


def test_parallel_segments_paired_within_group():
    approved = [seg("a1", "A", "B", 1), seg("a2", "A", "B", 4)]
    survey = [seg("s1", "x", "y", 4), seg("s2", "x", "y", 1)]
    r = reconcile(approved, survey, ["A", "B"], ["x", "y"], [])
    assert r.exact_count == 2
    assert r.delay_cost == 0


def test_canonical_mapping_is_lexicographically_stable():
    # 两个孤立点（无段）造成任意置换都同优；规范映射取目标序列字典序最小。
    approved = [seg("e1", "A", "B", 1)]
    survey = [seg("f1", "x", "y", 1)]
    r = reconcile(approved, survey, ["A", "B", "C", "D"],
                  ["x", "y", "z", "w"], [])
    seq = [v for _, v in r.mapping]  # 代号已排序
    assert seq == ["A", "B", "C", "D"]
    assert r.mapping_count == 2  # C/D 在 z/w 间 2 种


# --------------------------------------------------------------------------
# 对拍：小规模随机图全枚举
# --------------------------------------------------------------------------

def _brute_optimum(approved, survey, a_nodes, s_nodes, locks):
    """独立参考：枚举全部一一映射，组内全排列配对，返回最优解集合。"""
    n = len(s_nodes)
    ai = {v: i for i, v in enumerate(a_nodes)}
    si = {v: i for i, v in enumerate(s_nodes)}
    bA: dict[tuple, list] = {}
    bS: dict[tuple, list] = {}
    for e in approved:
        bA.setdefault((ai[e.src], ai[e.dst]), []).append(e.delay)
    for e in survey:
        bS.setdefault((si[e.src], si[e.dst]), []).append(e.delay)
    lockm = {si[c]: ai[t] for c, t in locks}

    def bucket_score(av, sv):
        k = min(len(av), len(sv))
        if k == 0:
            return 0, 0
        best = None
        if len(av) >= len(sv):
            for combo in itertools.combinations(range(len(av)), k):
                for perm in itertools.permutations(combo):
                    eq = sum(av[a] == sv[j] for j, a in enumerate(perm))
                    c = sum(abs(av[a] - sv[j]) for j, a in enumerate(perm))
                    if best is None or (eq, -c) > best:
                        best = (eq, -c)
        else:
            for combo in itertools.combinations(range(len(sv)), k):
                for perm in itertools.permutations(combo):
                    eq = sum(av[j] == sv[a] for j, a in enumerate(perm))
                    c = sum(abs(av[j] - sv[a]) for j, a in enumerate(perm))
                    if best is None or (eq, -c) > best:
                        best = (eq, -c)
        return best[0], -best[1]

    opts = []
    for perm in itertools.permutations(range(n)):
        if any(perm[x] != u for x, u in lockm.items()):
            continue
        ex = dc = 0
        for (x, y), sv in bS.items():
            e, c = bucket_score(bA.get((perm[x], perm[y]), []), sv)
            ex += e
            dc += c
        opts.append((ex, dc, perm))
    best_ex = max(o[0] for o in opts)
    best_dc = min(o[1] for o in opts if o[0] == best_ex)
    return [o for o in opts if o[0] == best_ex and o[1] == best_dc]


def _rand_topo(rng, names, prefix):
    cands = list(itertools.permutations(names, 2))
    rng.shuffle(cands)
    chosen = cands[:rng.randint(max(1, len(names) - 1), len(names) * 2)]
    segs = []
    for u, v in chosen:
        mult = 1 if rng.random() < 0.7 else rng.randint(2, 3)
        for _ in range(mult):
            if len(segs) >= 24:
                break
            segs.append(seg(f"{prefix}{len(segs)}", u, v, rng.randint(0, 4)))
    return segs[:24]


def test_exhaustive_fuzz_matches_brute_force():
    rng = random.Random(20260925)
    checked = 0
    for trial in range(600):
        n = rng.randint(2, 6)
        a_nodes = [f"a{i}" for i in range(n)]
        s_nodes = [f"s{i}" for i in range(n)]
        approved = _rand_topo(rng, a_nodes, "A")
        survey = _rand_topo(rng, s_nodes, "S")
        locks = []
        if rng.random() < 0.4:
            k = rng.randint(1, min(2, n))
            locks = [(s_nodes[x], a_nodes[u])
                     for x, u in zip(rng.sample(range(n), k),
                                     rng.sample(range(n), k))]
        r = reconcile(approved, survey, a_nodes, s_nodes, locks)
        opts = _brute_optimum(approved, survey, a_nodes, s_nodes, locks)
        best_ex, best_dc = opts[0][0], opts[0][1]
        assert r.exact_count == best_ex, (trial, r.exact_count, best_ex)
        assert r.delay_cost == best_dc, (trial, r.delay_cost, best_dc)
        assert r.mapping_count == len(opts), (trial, r.mapping_count, len(opts))
        # 规范映射：代号升序的目标序列字典序最小
        got_seq = [v for _, v in r.mapping]
        ref_seq = min(
            [a_nodes[o[2][x]] for x in range(n)] for o in opts
        )
        assert got_seq == ref_seq
        # 必然/可选集合
        for x, code in enumerate(s_nodes):
            vals = sorted({a_nodes[o[2][x]] for o in opts})
            got = sorted({dict(r.mapping)[code]} | set(r.optional.get(code, [])))
            assert got == vals
        checked += 1
    assert checked == 600
