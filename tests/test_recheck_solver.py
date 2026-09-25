"""复勘求解器测试：对称歧义、贪心反例、锁定、并行段、稳定性与蛮力对拍。"""
from __future__ import annotations

import itertools
import random
from collections import Counter

from app.recheck import reconcile
from app.solver import Segment


def seg(sid, u, v, d):
    return Segment(sid, u, v, d)


def brute_optimum(approved, recheck, locks):
    """独立参考：枚举满足锁定的全部双射，返回最优结论（只用于测试）。"""
    anodes = sorted({n for s in approved for n in (s.src, s.dst)})
    rnodes = sorted({n for s in recheck for n in (s.src, s.dst)})
    adev: dict[tuple[str, str], Counter] = {}
    for s in approved:
        adev.setdefault((s.src, s.dst), Counter())[s.delay] += 1

    def score(mp):
        rc: dict[tuple[str, str], Counter] = {}
        for s in recheck:
            rc.setdefault((mp[s.src], mp[s.dst]), Counter())[s.delay] += 1
        exact = diff = 0
        for k in set(rc) | set(adev):
            r, a = rc.get(k, Counter()), adev.get(k, Counter())
            rr, aa = [], []
            for d in set(r) | set(a):
                cr, ca = r.get(d, 0), a.get(d, 0)
                exact += min(cr, ca)
                if cr > ca:
                    rr += [d] * (cr - ca)
                elif ca > cr:
                    aa += [d] * (ca - cr)
            rr.sort()
            aa.sort()
            diff += sum(abs(x - y) for x, y in zip(rr, aa))
        return exact, diff

    feasible = []
    for perm in itertools.permutations(anodes):
        mp = dict(zip(rnodes, perm))
        if all(mp[c] == t for c, t in locks.items()):
            feasible.append(mp)
    best_key = None
    best_seq = None
    for mp in feasible:
        e, d = score(mp)
        key = (e, -d)
        seq = tuple(mp[c] for c in rnodes)
        if best_key is None or key > best_key or (key == best_key and seq < best_seq):
            best_key = key
            best_seq = seq
    opts = {c: set() for c in rnodes}
    count = 0
    for mp in feasible:
        e, d = score(mp)
        if (e, -d) == best_key:
            count += 1
            for c in rnodes:
                opts[c].add(mp[c])
    return best_key, best_seq, count, opts, feasible


K22_APPROVED = [
    seg("a1", "S", "A", 1),
    seg("a2", "S", "B", 2),
    seg("a3", "A", "T", 2),
    seg("a4", "B", "T", 1),
]
# 真实对应：p=S q=A r=B s=T
K22_RECHECK = [
    seg("r1", "p", "q", 1),
    seg("r2", "p", "r", 2),
    seg("r3", "q", "s", 2),
    seg("r4", "r", "s", 1),
]


def test_unique_optimal_mapping_renamed_codes():
    """延迟交叉使 K2,2 的对应唯一确定：四段全部完全匹配。"""
    r = reconcile(K22_APPROVED, K22_RECHECK, {})
    assert r.exact_count == 4
    assert r.delay_diff_sum == 0
    assert dict(r.mapping) == {"p": "S", "q": "A", "r": "B", "s": "T"}
    assert r.optimal_count == 1
    assert all(r.certainty[c] == "fixed" for c in "pqrs")
    assert {p.recheck.id for p in r.exact_pairs} == {"r1", "r2", "r3", "r4"}
    assert r.unmatched_recheck == [] and r.unmatched_approved == []


def test_symmetric_ambiguity_reports_all_optimal_maps():
    """全同延迟完全二部图：源侧 2! x 汇侧 2! = 4 个同优映射，全部可选。"""
    approved = [
        seg("a1", "S", "A", 1),
        seg("a2", "S", "B", 1),
        seg("a3", "T", "A", 1),
        seg("a4", "T", "B", 1),
    ]
    recheck = [
        seg("r1", "p", "q", 1),
        seg("r2", "p", "r", 1),
        seg("r3", "s", "q", 1),
        seg("r4", "s", "r", 1),
    ]
    r = reconcile(approved, recheck, {})
    assert r.exact_count == 4
    assert r.optimal_count == 4
    # 源度 2 的 {p,s} 只能对应 {S,T}；汇侧 {q,r} 只能对应 {A,B}
    assert set(r.options["p"]) == {"S", "T"}
    assert set(r.options["s"]) == {"S", "T"}
    assert set(r.options["q"]) == {"A", "B"}
    assert set(r.options["r"]) == {"A", "B"}
    assert all(r.certainty[c] == "optional" for c in "pqrs")
    # 规范映射仍稳定：按代号排序的目标序列字典序最小
    assert [t for _, t in r.mapping] == ["S", "A", "B", "T"]


def test_greedy_segment_matching_counterexample():
    """逐段就近匹配会被延迟诱饵带偏：首段 (c1,c2,1) 的唯一最近已批准
    段是 (v2,v0,1)，锁定该对应后任何双射都只能救回 1 段；全局 QAP
    不预占任何段，得到 5 段全匹配（另 1 段延迟不一致）。"""
    approved = [
        seg("a0", "v3", "v1", 2),
        seg("a1", "v3", "v2", 2),
        seg("a2", "v1", "v0", 3),
        seg("a3", "v2", "v3", 0),
        seg("a4", "v2", "v0", 1),   # 贪心诱饵：唯一延迟差为 0 的段
        seg("a5", "v3", "v0", 2),
    ]
    recheck = [
        seg("r0", "c1", "c2", 1),   # 贪心会先配到 a4，逼后续全错
        seg("r1", "c1", "c0", 2),
        seg("r2", "c2", "c3", 3),
        seg("r3", "c0", "c1", 0),
        seg("r4", "c0", "c3", 1),
        seg("r5", "c1", "c3", 2),
    ]
    r = reconcile(approved, recheck, {})
    assert r.exact_count == 5
    assert r.delay_diff_sum == 1
    # 用"锁定贪心诱饵对应"复现逐段贪心的次优后果
    greedy = reconcile(approved, recheck, {"c1": "v2", "c2": "v0"})
    assert greedy.exact_count == 1
    assert greedy.exact_count < r.exact_count


def test_locks_pin_mapping_and_break_symmetry():
    """锁定一个代号即可消歧对称图。"""
    approved = [
        seg("a1", "S", "A", 1),
        seg("a2", "S", "B", 1),
        seg("a3", "T", "A", 1),
        seg("a4", "T", "B", 1),
    ]
    recheck = [
        seg("r1", "p", "q", 1),
        seg("r2", "p", "r", 1),
        seg("r3", "s", "q", 1),
        seg("r4", "s", "r", 1),
    ]
    r = reconcile(approved, recheck, {"q": "B"})
    assert r.optimal_count == 2  # q=B => r=A；p/s 仍可互换
    assert r.options["q"] == ["B"]
    assert r.certainty["q"] == "fixed"
    assert set(r.options["p"]) == {"S", "T"}
    r2 = reconcile(approved, recheck, {"q": "B", "p": "S"})
    assert r2.optimal_count == 1
    assert dict(r2.mapping) == {"p": "S", "q": "B", "r": "A", "s": "T"}


def test_delay_typo_classified_as_mismatch_not_miswire():
    """一段延迟录错：方向正确但延迟不一致，计入 delayMismatches 与绝对差。"""
    approved = [
        seg("a1", "S", "A", 1),
        seg("a2", "A", "T", 2),
    ]
    recheck = [
        seg("r1", "p", "q", 1),
        seg("r2", "q", "s", 9),  # 应为 2，录成 9
    ]
    r = reconcile(approved, recheck, {})
    assert r.exact_count == 1
    assert r.delay_diff_sum == 7
    assert len(r.mismatch_pairs) == 1
    m = r.mismatch_pairs[0]
    assert (m.recheck.id, m.approved.id, m.diff) == ("r2", "a2", 7)
    assert r.unmatched_recheck == [] and r.unmatched_approved == []


def test_miswire_leaves_unmatched_segments_on_both_sides():
    """错接（度序列不一致：批准侧汇点入度 2，复勘侧 p 出度 2）无法全部
    同向配对，两侧各留一条未匹配段，可直接区分于改名和时延录错。"""
    approved = [
        seg("a1", "S", "A", 1),
        seg("a2", "A", "T", 1),
        seg("a3", "B", "T", 1),
    ]
    recheck = [
        seg("r1", "p", "q", 1),
        seg("r2", "q", "s", 1),
        seg("r3", "p", "r", 1),
    ]
    r = reconcile(approved, recheck, {})
    assert r.exact_count == 2
    assert len(r.unmatched_approved) == 1
    assert len(r.unmatched_recheck) == 1
    assert r.mismatch_pairs == []


def test_parallel_segments_pair_by_delay():
    """同端点对多条并行段：同延迟先配满，其余排序配对，差和最小。"""
    approved = [
        seg("a1", "S", "T", 1),
        seg("a2", "S", "T", 1),
        seg("a3", "S", "T", 5),
    ]
    recheck = [
        seg("r1", "p", "q", 1),
        seg("r2", "p", "q", 1),
        seg("r3", "p", "q", 8),
    ]
    r = reconcile(approved, recheck, {})
    assert r.exact_count == 2
    assert r.delay_diff_sum == 3
    assert len(r.mismatch_pairs) == 1
    assert r.mismatch_pairs[0].diff == 3


def test_direction_matters_reverse_edges_do_not_pair():
    """方向是匹配要素：正确映射下一条记录反向的段落在反方向端点对上，
    不参与同向配对，作为未匹配（疑似接反/错接）证据分侧列出。"""
    approved = [
        seg("a1", "S", "A", 5),
        seg("a2", "S", "T", 1),
        seg("a3", "A", "T", 9),
    ]
    recheck = [
        seg("r1", "q", "p", 5),  # 应为 p→q，现场记反
        seg("r2", "p", "s", 1),
        seg("r3", "q", "s", 9),
    ]
    r = reconcile(approved, recheck, {})
    assert r.exact_count == 2
    assert r.delay_diff_sum == 0
    assert r.mismatch_pairs == []
    assert len(r.unmatched_recheck) == 1
    assert len(r.unmatched_approved) == 1
    assert r.unmatched_recheck[0].id == "r1"
    assert r.unmatched_approved[0].id == "a1"


def test_canonical_mapping_is_stable_and_sorted_by_code():
    """重复调用结论稳定；规范映射按复勘代号排序且字典序最小。"""
    approved = [
        seg("a1", "S", "A", 1),
        seg("a2", "S", "B", 1),
        seg("a3", "A", "T", 1),
        seg("a4", "B", "T", 1),
    ]
    recheck = [
        seg("r1", "p", "q", 1),
        seg("r2", "p", "r", 1),
        seg("r3", "s", "q", 1),
        seg("r4", "s", "r", 1),
    ]
    r1 = reconcile(approved, recheck, {})
    r2 = reconcile(approved, list(reversed(recheck)), {})
    codes1 = [c for c, _ in r1.mapping]
    assert codes1 == sorted(codes1)
    assert r1.mapping == r2.mapping
    assert r1.optimal_count == r2.optimal_count


def test_exhaustive_fuzz_matches_brute_force():
    """对 2..7 节点随机图与随机锁定做蛮力对拍（含同优数与可选目标）。"""
    rng = random.Random(20260925)
    checked = 0
    while checked < 1500:
        n = rng.randint(2, 7)
        names = [f"v{i}" for i in range(n)]
        codes = [f"c{i}" for i in range(n)]
        m = rng.randint(n - 1, min(14, n * (n - 1)))
        pairs = list(itertools.permutations(names, 2))
        rng.shuffle(pairs)
        approved = []
        idx = 0
        for u, v in pairs[:m]:
            k = 1 + (1 if rng.random() < 0.2 else 0)
            for _ in range(k):
                approved.append(seg(f"s{idx}", u, v, rng.randint(0, 4)))
                idx += 1
        truth = dict(zip(codes, rng.sample(names, n)))
        inv = {v: k for k, v in truth.items()}
        recheck = []
        for i, s in enumerate(approved):
            if rng.random() < 0.85:
                d = s.delay if rng.random() < 0.8 else rng.randint(0, 4)
                recheck.append(seg(f"r{i}", inv[s.src], inv[s.dst], d))
        if len({x for s in recheck for x in (s.src, s.dst)}) < n:
            continue
        if len(approved) > 24 or len(recheck) > 24:
            continue
        locks = {}
        if rng.random() < 0.4:
            for c in rng.sample(codes, rng.randint(1, min(2, n))):
                locks[c] = truth[c]
        r = reconcile(approved, recheck, locks)
        bkey, bseq, count, opts, _ = brute_optimum(approved, recheck, locks)
        assert (r.exact_count, -r.delay_diff_sum) == bkey
        assert tuple(t for _, t in r.mapping) == bseq
        assert r.optimal_count == count
        for c in codes:
            assert set(r.options[c]) == opts[c]
        checked += 1
    assert checked == 1500
