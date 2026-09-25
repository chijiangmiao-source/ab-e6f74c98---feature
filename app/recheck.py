"""新控制器接线复勘：已批准光纤拓扑 vs 复勘代号拓扑的全局比对。

现场采集器用临时代号记录端点，本模块在**所有满足锁定关系的一一映射**
（复勘代号 -> 原节点的双射）中寻找结论，目标按字典序为：

1. 最大化"方向与延迟均相同"的可配对段数（完全匹配段）；
2. 在完全匹配数相同的映射中，最小化**其余同向可配对段**的延迟绝对差
   之和（延迟不一致段，即时延录错）；
3. 仍并列时取"复勘代号排序后的目标节点序列"字典序最小者作为规范映射。

关键约束：**不得为各段分别匹配后拼接**。每一段如何对应取决于唯一一个
节点双射，因此这是二次指派问题而非逐段贪心——逐段贪心会在"先匹配的段
占用代号、逼后续段错配"时得到非全局最优结论（见验收中的贪心反例）。

实现为带回溯上界剪枝的深度优先搜索（按代号字典序依次指派原节点）：

- 指派增长时只有"两端都已指派"的段其有向端点对才落定，立即累计该对
  上的完全匹配数与残差延迟差（该端点对不会再被后续指派改变）；
- 乐观上界 = 已定精确数 + 剩余代号到未用原节点的**最大权二部匹配**
  （边界段同延迟潜力，强制锁定行列）+ 内部段按延迟的全局重叠潜力；
- 已批准侧互换同构（交换二者是固定已指派节点的自同构）且都不是后续
  锁定目标的候选原节点，在同一层只搜一个代表，按等价类大小累计映射
  数，避免对称情形阶乘爆炸；
- 到达叶子且与全局最优并列时累计同优映射数，并记录每个代号在所有同优
  映射中可取的目标（必然 / 可选）。

并行段处理：同一有向端点对之间，先按延迟逐一配完全匹配，剩余两侧按
延迟排序配对（最小化绝对差之和），多者留下作为未匹配段（错接证据）。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .solver import Segment

MAX_NODES = 11
MIN_NODES = 2
MAX_SEGMENTS = 24

# 上界矩阵中的"禁止指派"费用（真实费用不超过段数 24）。
FORBID_COST = 10_000


@dataclass(frozen=True)
class Pair:
    """一一同向配对：一条复勘段对应一条已批准段。"""

    recheck: Segment
    approved: Segment


@dataclass
class MismatchPair:
    recheck: Segment
    approved: Segment
    diff: int


@dataclass
class RecheckConclusion:
    # 规范映射，按复勘代号排序：[(code, target), ...]
    mapping: list[tuple[str, str]]
    # 每个代号在全部同优映射中的确定性："fixed"（必然）/ "optional"（可选）
    certainty: dict[str, str]
    # 每个代号的全部可选目标（必然时只有一个），按名称排序
    options: dict[str, list[str]]
    optimal_count: int
    exact_pairs: list[Pair]
    mismatch_pairs: list[MismatchPair]
    unmatched_recheck: list[Segment]
    unmatched_approved: list[Segment]
    exact_count: int
    delay_diff_sum: int


def _group_contribution(rc: Counter, ac: Counter) -> tuple[int, int]:
    """一个有向端点对落定时的 (完全匹配数, 残差最小绝对差之和)。

    同延迟先配满；两侧剩余延迟的取值集合互不相交，按延迟排序配对即
    最小化绝对差之和，配不上的留在多的一侧（未匹配）。
    """
    exact = 0
    rest_r: list[int] = []
    rest_a: list[int] = []
    for d in set(rc) | set(ac):
        cr, ca = rc.get(d, 0), ac.get(d, 0)
        exact += min(cr, ca)
        if cr > ca:
            rest_r.extend([d] * (cr - ca))
        elif ca > cr:
            rest_a.extend([d] * (ca - cr))
    rest_r.sort()
    rest_a.sort()
    diff = sum(abs(x - y) for x, y in zip(rest_r, rest_a))
    return exact, diff


def _hungarian_max_minus(cost: list[list[int]]) -> int:
    """方阵最小费用指派（匈牙利算法），返回最小费用。

    入参为"费用 = -价值"，故 -返回值即最大权匹配价值。
    """
    n = len(cost)
    u = [0] * (n + 1)
    v = [0] * (n + 1)
    p = [0] * (n + 1)
    way = [0] * (n + 1)
    inf = float("inf")
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used_v = [False] * (n + 1)
        while True:
            used_v[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, n + 1):
                if not used_v[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used_v[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return int(-v[0])


def reconcile(
    approved: list[Segment],
    recheck: list[Segment],
    locks: dict[str, str],
) -> RecheckConclusion:
    """求复勘结论。locks 已由校验层保证为可行的部分双射。"""
    anodes = sorted({n for s in approved for n in (s.src, s.dst)})
    rnodes = sorted({n for s in recheck for n in (s.src, s.dst)})
    assert len(anodes) == len(rnodes)
    aset = set(anodes)
    n = len(rnodes)
    order = rnodes  # 复勘代号按字典序固定指派顺序

    # 已批准侧：有向端点对 -> 延迟多重集
    adev: dict[tuple[str, str], Counter] = {}
    alinks: dict[tuple[str, str], list[Segment]] = {}
    for s in approved:
        adev.setdefault((s.src, s.dst), Counter())[s.delay] += 1
        alinks.setdefault((s.src, s.dst), []).append(s)

    # 两侧邻接（用于跨边界需求、内部签名与增量计数）
    radj: dict[str, list[tuple[str, bool, int]]] = {c: [] for c in rnodes}
    for s in recheck:
        radj[s.src].append((s.dst, True, s.delay))
        radj[s.dst].append((s.src, False, s.delay))
    aadj: dict[str, list[tuple[str, bool, int]]] = {v: [] for v in anodes}
    for s in approved:
        aadj[s.src].append((s.dst, True, s.delay))
        aadj[s.dst].append((s.src, False, s.delay))

    locked_targets = set(locks.values())

    # ---- 全搜索过程中的最优结论累计 ----
    best_key: tuple[int, int] | None = None  # (完全匹配数, -延迟差)
    best_tuple: tuple[str, ...] | None = None
    best_map: dict[str, str] | None = None
    total_optimal = 0
    options_seen: dict[str, set[str]] = {c: set() for c in rnodes}

    def _equivalent(v1: str, v2: str, used: frozenset[str]) -> bool:
        """交换 v1/v2 是否为已批准图上固定 used 节点的自同构。

        成立时，在同一搜索层、同一份部分指派下二者给出同构子问题，
        只需展开一个代表，按等价类大小计数即可。二者还都不能是后续
        锁定代号的目标（否则锁定会区分两条子问题）。
        """
        if v1 in locked_targets or v2 in locked_targets:
            return False
        # 二者之间：交换使 (v1,v2) 落到 (v2,v1)，双向延迟多重集须相同
        if adev.get((v1, v2), Counter()) != adev.get((v2, v1), Counter()):
            return False
        # 其余未指派节点 w 保持不动，两侧边界邻接须一致
        for w in aset - used - {v1, v2}:
            if adev.get((v1, w), Counter()) != adev.get((v2, w), Counter()):
                return False
            if adev.get((w, v1), Counter()) != adev.get((w, v2), Counter()):
                return False
        # 已指派节点 vi 不动，候选到它们的边界也须一致
        for vi in used:
            if adev.get((v1, vi), Counter()) != adev.get((v2, vi), Counter()):
                return False
            if adev.get((vi, v1), Counter()) != adev.get((vi, v2), Counter()):
                return False
        return True

    def dfs(
        depth: int,
        f: dict[str, str],
        used: frozenset[str],
        rg_counter: dict[tuple[str, str], Counter],
        exact_fixed: int,
        diff_fixed: int,
        inner_r: Counter,
        inner_a: Counter,
        multiplicity: int,
        dsu: dict[str, frozenset[str]],
        code_orbit: dict[str, frozenset[str]],
    ) -> None:
        nonlocal best_key, best_tuple, best_map, total_optimal

        if depth == n:
            key = (exact_fixed, -diff_fixed)
            seq = tuple(f[c] for c in order)
            key_better = best_key is None or key > best_key
            tie = best_key is not None and key == best_key
            # 代号 c 的像只可能被"其指派层及更早"的交换移动，故其可选
            # 目标取指派时刻并查集分量的快照 code_orbit[c]。
            if key_better:
                best_key = key
                best_tuple = seq
                best_map = dict(f)
                total_optimal = multiplicity
                for c in order:
                    options_seen[c] = set(code_orbit[c])
            elif tie:
                # 同优映射：计数与可选目标累加；若序列字典序更小则只
                # 更换规范映射，绝不能重置同优计数。
                total_optimal += multiplicity
                for c in order:
                    options_seen[c].update(code_orbit[c])
                if best_tuple is None or seq < best_tuple:
                    best_tuple = seq
                    best_map = dict(f)
            return

        c = order[depth]
        unused_set = aset - used
        locked = locks.get(c)
        if locked is not None:
            if locked not in unused_set:
                # 锁定目标已被占用（理论上校验层已排除双锁同一目标）
                return
            candidates = [locked]
        else:
            # 未锁定代号不能占用后续锁定代号的目标节点
            candidates = sorted(unused_set - locked_targets)
            if not candidates:
                return

        # ---- 边界需求：一端已指派、另一端尚未指派的复勘段 ----
        # crossing[code]：把该代号指派给 v 时，对 v 与已指派节点 vi 之间
        # 有向段的同延迟需求计数，键为 (vi, forward, delay)。
        crossing: dict[str, Counter] = {x: Counter() for x in order[depth:]}
        for s in recheck:
            sr, sd = s.src in f, s.dst in f
            if sr == sd:
                continue
            if sr:
                crossing[s.dst][(f[s.src], True, s.delay)] += 1
            else:
                crossing[s.src][(f[s.dst], False, s.delay)] += 1

        def boundary_value(code: str, v: str) -> int:
            total = 0
            for (vi, forward, d), mult in crossing[code].items():
                key = (vi, v) if forward else (v, vi)
                total += min(mult, adev.get(key, Counter()).get(d, 0))
            return total

        # ---- 乐观上界：剩余代号 x 未用节点 的最大权指派 + 内部段潜力 ----
        rem_codes = order[depth:]
        rem_nodes = sorted(unused_set)
        k = len(rem_codes)
        if k == len(rem_nodes):
            cost: list[list[int]] = [[0] * k for _ in range(k)]
            for i, code in enumerate(rem_codes):
                code_locked = locks.get(code)
                for j, v in enumerate(rem_nodes):
                    forbidden = (
                        (code_locked is not None and v != code_locked)
                        or (
                            code_locked is None
                            and v in locked_targets
                        )
                    )
                    cost[i][j] = FORBID_COST if forbidden else -boundary_value(code, v)
            min_cost = _hungarian_max_minus(cost)
            match_bound = -min_cost
            # 指派被强制经过禁止格（费用 FORBID_COST）时，锁定不可满足
            if min_cost >= FORBID_COST // 2:
                return
        else:
            # 代号多于节点（当前候选占用会导致锁定冲突），此枝不可行
            return

        inner_potential = sum(
            min(inner_r[d], inner_a[d]) for d in set(inner_r) | set(inner_a)
        )
        upper_bound = exact_fixed + match_bound + inner_potential
        if best_key is not None and upper_bound < best_key[0]:
            return

        # 内部邻接延迟签名，用于候选启发式排序（尽早找到高质量可行解）
        sig_r: Counter = Counter()
        for c2, _out, d in radj[c]:
            if c2 not in f:
                sig_r[d] += 1

        def heuristic(v: str) -> int:
            sig_a: Counter = Counter()
            for w, _out2, d in aadj[v]:
                if w not in used:
                    sig_a[d] += 1
            overlap = sum(min(sig_r[d], sig_a[d]) for d in set(sig_r) | set(sig_a))
            return -(boundary_value(c, v) + overlap)

        if locked is None:
            candidates.sort(key=lambda v: (heuristic(v), v))

        # ---- 候选按自同构等价类去重，代表携带类大小作为倍数 ----
        reps: list[tuple[str, tuple[str, ...]]]
        if locked is not None:
            reps = [(locked, (locked,))]
        else:
            reps = []
            for v in candidates:
                for i, (rep, _m) in enumerate(reps):
                    if _equivalent(v, rep, used):
                        reps[i] = (rep, tuple(sorted(reps[i][1] + (v,))))
                        break
                else:
                    reps.append((v, (v,)))

        for v, members in reps:
            nf = dict(f)
            nf[c] = v
            nused = used | {v}
            nrg = {key: cnt.copy() for key, cnt in rg_counter.items()}
            ninner_r = inner_r.copy()
            ninner_a = inner_a.copy()
            nexact = exact_fixed
            ndiff = diff_fixed

            # 复勘侧：c 落定后，与已指派代号之间的有向端点对落定；
            # 与未指派代号相连的段离开"双端未落定"计数。
            completed: set[tuple[str, str]] = set()
            pending: Counter = Counter()
            for c2, out, d in radj[c]:
                if c2 in nf and c2 != c:
                    key = (v, nf[c2]) if out else (nf[c2], v)
                    pending[(key, d)] += 1
                elif c2 not in nf:
                    ninner_r[d] -= 1
            for (key, d), mult in pending.items():
                nrg.setdefault(key, Counter())[d] += mult
                completed.add(key)
            for key in completed:
                e, df = _group_contribution(nrg[key], adev.get(key, Counter()))
                nexact += e
                ndiff += df

            # 已批准侧：v 落定后，指向仍未指派节点的段离开"内部"计数
            for w, _out, d in aadj[v]:
                if w not in nused:
                    ninner_a[d] -= 1

            # 沿路径折叠的交换自同构生成群：合并本层等价类成员。
            ndsu = dict(dsu)
            if len(members) > 1:
                comps = [ndsu.get(m, frozenset({m})) for m in members]
                merged = frozenset().union(*comps)
                for m in merged:
                    ndsu[m] = merged
            # c 在本层完成指派，其可选目标只含截至本层的交换轨道，
            # 更深层的交换固定 c 的像，不再扩张该快照。
            ncode_orbit = dict(code_orbit)
            ncode_orbit[c] = ndsu.get(v, frozenset({v}))

            dfs(
                depth + 1,
                nf,
                frozenset(nused),
                nrg,
                nexact,
                ndiff,
                ninner_r,
                ninner_a,
                multiplicity * len(members),
                ndsu,
                ncode_orbit,
            )

    init_inner_r = Counter(s.delay for s in recheck)
    init_inner_a = Counter(s.delay for s in approved)
    dfs(0, {}, frozenset(), {}, 0, 0, init_inner_r, init_inner_a, 1, {}, {})

    assert best_map is not None and best_key is not None

    # ---- 在规范映射上做具体配对（带段标识，供页面区分三类结论） ----
    r_links: dict[tuple[str, str], list[Segment]] = {}
    for s in recheck:
        r_links.setdefault((best_map[s.src], best_map[s.dst]), []).append(s)

    exact_pairs: list[Pair] = []
    mismatch_pairs: list[MismatchPair] = []
    unmatched_r: list[Segment] = []
    unmatched_a: list[Segment] = []

    for key in sorted(set(r_links) | set(alinks)):
        rl = sorted(r_links.get(key, []), key=lambda s: (s.delay, s.id))
        al = sorted(alinks.get(key, []), key=lambda s: (s.delay, s.id))
        r_by_delay: dict[int, list[Segment]] = {}
        a_by_delay: dict[int, list[Segment]] = {}
        for s in rl:
            r_by_delay.setdefault(s.delay, []).append(s)
        for s in al:
            a_by_delay.setdefault(s.delay, []).append(s)
        used_r: set[str] = set()
        used_a: set[str] = set()
        # 同延迟优先配满（两侧各自按标识排序，确定且稳定）
        for d in sorted(set(r_by_delay) & set(a_by_delay)):
            for rs, ast in zip(r_by_delay[d], a_by_delay[d]):
                exact_pairs.append(Pair(rs, ast))
                used_r.add(rs.id)
                used_a.add(ast.id)
        rest_r = sorted(
            (s for s in rl if s.id not in used_r), key=lambda s: (s.delay, s.id)
        )
        rest_a = sorted(
            (s for s in al if s.id not in used_a), key=lambda s: (s.delay, s.id)
        )
        paired_r: set[str] = set()
        paired_a: set[str] = set()
        for rs, ast in zip(rest_r, rest_a):
            mismatch_pairs.append(
                MismatchPair(rs, ast, abs(rs.delay - ast.delay))
            )
            paired_r.add(rs.id)
            paired_a.add(ast.id)
        unmatched_r.extend(s for s in rest_r if s.id not in paired_r)
        unmatched_a.extend(s for s in rest_a if s.id not in paired_a)

    exact_pairs.sort(key=lambda p: (p.recheck.id, p.approved.id))
    mismatch_pairs.sort(key=lambda p: (p.recheck.id, p.approved.id))
    unmatched_r.sort(key=lambda s: s.id)
    unmatched_a.sort(key=lambda s: s.id)

    mapping = [(c, best_map[c]) for c in order]
    certainty: dict[str, str] = {}
    options: dict[str, list[str]] = {}
    for c in order:
        targets = sorted(options_seen[c])
        options[c] = targets
        certainty[c] = "fixed" if len(targets) == 1 else "optional"

    return RecheckConclusion(
        mapping=mapping,
        certainty=certainty,
        options=options,
        optimal_count=total_optimal,
        exact_pairs=exact_pairs,
        mismatch_pairs=mismatch_pairs,
        unmatched_recheck=unmatched_r,
        unmatched_approved=unmatched_a,
        exact_count=best_key[0],
        delay_diff_sum=-best_key[1],
    )
