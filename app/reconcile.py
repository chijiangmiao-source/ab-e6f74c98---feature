"""接线复勘求解器：在已批准拓扑与复勘拓扑之间确定代号对应关系。

现场采集器以临时代号记录端点，工程师需要确认每个复勘代号对应已批准
拓扑中的哪个原节点。问题形式化为：在所有满足人工锁定关系的**一一映射**
（复勘代号 -> 原节点）中：

1. 先最大化「方向与延迟均相同」的可配对段数（完全匹配段，说明接线与
   台账一致——既非错接也非改名造成的差异）；
2. 再最小化其余「同向但延迟不同」的可配对段延迟绝对差之和（这些段接线
   方向正确、仅延迟录错）；
3. 若仍有多个同优映射，按复勘代号（字典序）排序后的目标节点序列取字典
   序最小者作为稳定呈现的规范映射；同时报告全部同优映射总数及每个代号
   的必然/可选目标。

关键约束：评估的是**整张映射**下的拓扑配对，绝不把各段分别就近匹配后
拼接——逐段贪心会在多个代号同时疑似同一原节点时做出互不相容的配对，
与"一一映射"矛盾。

固定一张映射后，复勘段只可能与同一有向端点对（并行段组）内的批准段配
对，组间互不竞争；组内配对是小规模指派问题：以「同延迟配对数最多」为
首要目标、「延迟绝对差之和最小」为次要目标做整体最大权二分匹配
（同延迟最优配对可能交叉，不能按延迟排序后就近拉链）。

搜索：对满足锁定的所有一一映射做带界深度优先枚举（上限 11!）。
- 两级乐观上界剪枝：未定段间忽略节点一致性的 Kuhn 最大匹配（廉价），
  以及节点变量层面的最大权指派界（匈牙利，紧但较贵，仅浅层启用）；
- 子问题记忆化：闭合累计是部分指派的确定函数，与指派次序无关；
- 孪生节点对称归并：对当前剩余子问题完全等价（批准拓扑固定全部已用节
  点后的对换自同构、且不涉及任何剩余锁定目标）的候选原节点只探一个代
  表，最优解数按等价类大小倍增、必然/可选集合沿对换轨道展开——星型与
  并行等对称歧义因此转瞬可解，而不必逐条遍历成千上万的同构排列。
"""
from __future__ import annotations

from dataclasses import dataclass

# 端点 2..11、每拓扑段数 0..24（与校验层的限制保持一致）。
MIN_ENDPOINTS = 2
MAX_ENDPOINTS = 11
MAX_SEGMENTS = 24


@dataclass(frozen=True)
class TopoSegment:
    id: str
    src: str
    dst: str
    delay: int


@dataclass
class SegmentPair:
    approved: TopoSegment
    survey: TopoSegment
    delay_diff: int          # 完全匹配时为 0


@dataclass
class ReconcileResult:
    status: str                           # "ok"
    mapping: list[tuple[str, str]]        # 规范映射 [(复勘代号, 原节点)]，按代号排序
    mapping_count: int                    # 全部同优映射数
    certain: dict[str, str]               # 所有同优映射中目标唯一的代号
    optional: dict[str, list[str]]        # 其余代号 -> 全部可选目标（排序）
    exact_pairs: list[SegmentPair]        # 方向与延迟均相同
    delay_pairs: list[SegmentPair]        # 同向但延迟不一致
    unmatched_approved: list[TopoSegment]
    unmatched_survey: list[TopoSegment]
    exact_count: int
    delay_cost: int


# ---------------------------------------------------------------------------
# 并行段组内配对：最大权二分指派
# ---------------------------------------------------------------------------

def _hungarian_min(cost: list[list[int]]):
    """方阵上的最小权完美匹配（e-maxx 匈牙利算法）。

    返回 (最小费用, 行->列)。同费并列时取列下标较小者（按列升序扫描、
    严格取小），保证结果确定。费用可为任意大整数（延迟无录入上限），
    哨兵按矩阵实际量级动态取，避免写死常量在极大延迟下失效。
    """
    k = len(cost)
    # 任意增广路的总费用绝对值不超过 k * max|cost|；哨兵取其上界 +1 即可。
    flat_max = max(
        (abs(cost[i][j]) for i in range(k) for j in range(k)), default=0
    )
    big = k * flat_max + 1
    # 1 基索引：p[j] = 与列 j 配对的行，0 为虚拟列。
    u = [0] * (k + 1)
    v = [0] * (k + 1)
    p = [0] * (k + 1)
    way = [0] * (k + 1)
    for i in range(1, k + 1):
        p[0] = i
        j0 = 0
        minv = [big] * (k + 1)
        used = [False] * (k + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = big
            j1 = 0
            for j in range(1, k + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(k + 1):
                if used[j]:
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
    row_to_col = [0] * k
    total = 0
    for j in range(1, k + 1):
        row_to_col[p[j] - 1] = j - 1
        total += cost[p[j] - 1][j - 1]
    return total, row_to_col


def _km_max(weight: list[list[int]]):
    """方阵上的最大权完美匹配，返回 (最大权, 行->列)。"""
    total, row_to_col = _hungarian_min(
        [[-x for x in row] for row in weight]
    )
    return -total, row_to_col


def _pair_bucket(a_items: list[tuple[int, int]], s_items: list[tuple[int, int]]):
    """同一有向端点组内的最优配对（完整二分指派，非单调就近配对）。

    组内强制配出 k=min(p,q) 对（同向段"可配对"即应配上，较长一侧多出的
    段才算未匹配）；在该配对数下：(a) 同延迟（完全匹配）对数最多；
    (b) 延迟绝对差之和最小。注意 (a) 的最优解可能是"交叉"指派
    （如 [1,2,5] 对 [2,4,5] 时差开配可得 2 个同延迟），故不能按延迟
    排序后拉链，必须做整体最大权匹配（并行段之间本就可任意两两配对）。

    做法：补成 max(p,q) 阶方阵，较短侧为真实行、补虚拟行（权 0），
    真实边权 = BASE*同延迟 - |Δd|，BASE 大于组内任何延迟差总和，
    虚拟行自然"领走"较长一侧未配上的段。

    返回 (pairs, unmatched_a, unmatched_s)，
    pairs 为 [(批准段下标, 复勘段下标, 是否同延迟, |Δdelay|), ...]，按批准段下标排序。
    """
    p, q = len(a_items), len(s_items)
    if p == 0 or q == 0:
        return [], [i for i, _ in a_items], [i for i, _ in s_items]

    k = max(p, q)
    delays = [d for _, d in a_items] + [d for _, d in s_items]
    span = max(delays) - min(delays)
    base = k * max(span, 0) + 1  # 一个同延迟抵得过任何总延迟差
    # 次级目标：并列时偏好按段录入次序顺序配对（总扰动 < 一个主目标单位）。
    scale = k * k + 1

    def primary(ai: tuple[int, int], sj: tuple[int, int]) -> int:
        return base - abs(ai[1] - sj[1]) if ai[1] == sj[1] else -abs(ai[1] - sj[1])

    matrix = [[0] * k for _ in range(k)]
    if p <= q:
        short, long_, swapped = a_items, s_items, False
    else:
        short, long_, swapped = s_items, a_items, True
    for i in range(len(short)):
        for j in range(k):
            pr = primary(long_[j], short[i]) if swapped else primary(short[i], long_[j])
            matrix[i][j] = pr * scale + (k - abs(i - j))
    # 虚拟行（补到 k 行）对任意列权为 0。

    _, row_to_col = _km_max(matrix)
    pairs: list[tuple[int, int, bool, int]] = []
    used_a: set[int] = set()
    used_s: set[int] = set()
    for i in range(len(short)):
        j = row_to_col[i]
        if not swapped:
            ai, sj = short[i][0], long_[j][0]
            da, ds = short[i][1], long_[j][1]
        else:
            ai, sj = long_[j][0], short[i][0]
            da, ds = long_[j][1], short[i][1]
        used_a.add(ai)
        used_s.add(sj)
        pairs.append((ai, sj, da == ds, abs(da - ds)))
    pairs.sort(key=lambda t: t[0])
    unmatched_a = [i for i, _ in a_items if i not in used_a]
    unmatched_s = [i for i, _ in s_items if i not in used_s]
    return pairs, unmatched_a, unmatched_s


# ---------------------------------------------------------------------------
# 映射搜索
# ---------------------------------------------------------------------------

class _Engine:
    def __init__(self, approved, survey, a_nodes, s_nodes, locks):
        self.A = approved
        self.S = survey
        self.an = a_nodes
        self.sn = s_nodes
        self.n = len(s_nodes)
        self.ai = {name: i for i, name in enumerate(a_nodes)}
        self.si_map = {name: i for i, name in enumerate(s_nodes)}

        # 有向组桶：(u,v) -> [(seg_idx, delay), ...]
        self.bucket_a: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self.bucket_s: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for k, e in enumerate(approved):
            self.bucket_a.setdefault(
                (self.ai[e.src], self.ai[e.dst]), []
            ).append((k, e.delay))
        for k, e in enumerate(survey):
            self.bucket_s.setdefault(
                (self.si_map[e.src], self.si_map[e.dst]), []
            ).append((k, e.delay))

        # 节点 -> 相邻段对端节点（含并行重复，闭合判断只需"是否存在"）。
        self.adj_a: dict[int, set[int]] = {}
        self.adj_s: dict[int, set[int]] = {}
        for e in approved:
            u, v = self.ai[e.src], self.ai[e.dst]
            self.adj_a.setdefault(u, set()).add(v)
            self.adj_a.setdefault(v, set()).add(u)
        for e in survey:
            u, v = self.si_map[e.src], self.si_map[e.dst]
            self.adj_s.setdefault(u, set()).add(v)
            self.adj_s.setdefault(v, set()).add(u)

        # 锁定：survey 下标 -> approved 下标。
        self.fixed: dict[int, int] = {}
        for code, target in locks:
            self.fixed[self.si_map[code]] = self.ai[target]
        # 乐观上界只依赖 (已定段集合, 部分指派) 与次序无关，按状态缓存。
        self._ub_cache: dict[tuple, int] = {}
        self._cap_cache: dict[tuple, int] = {}

        # 位掩码加速（边数 ≤ 24，一个整数装下）。
        self.na = len(approved)
        self.ns = len(survey)
        self.full_a = (1 << self.na) - 1
        self.full_s = (1 << self.ns) - 1
        self.a_src = [0] * self.na
        self.a_dst = [0] * self.na
        self.a_delay = [0] * self.na
        self.s_src = [0] * self.ns
        self.s_dst = [0] * self.ns
        self.s_delay = [0] * self.ns
        # (u,v,d) -> 批准段位掩码
        self.a_dir_mask: dict[tuple[int, int, int], int] = {}
        # (u,d) -> 源为 u 的延迟 d 段掩码；(v,d) -> 汇为 v 的延迟 d 段掩码。
        self.a_from_mask: dict[tuple[int, int], int] = {}
        self.a_to_mask: dict[tuple[int, int], int] = {}
        # (x,y) -> 复勘段掩码（两方向都登记）
        self.s_dir_mask: dict[tuple[int, int], int] = {}
        # d -> 全部延迟 d 的批准段掩码
        self.a_delay_mask: dict[int, int] = {}
        for k, e in enumerate(approved):
            u, v = self.ai[e.src], self.ai[e.dst]
            self.a_src[k], self.a_dst[k], self.a_delay[k] = u, v, e.delay
            self.a_dir_mask[(u, v, e.delay)] = (
                self.a_dir_mask.get((u, v, e.delay), 0) | (1 << k)
            )
            self.a_from_mask[(u, e.delay)] = (
                self.a_from_mask.get((u, e.delay), 0) | (1 << k)
            )
            self.a_to_mask[(v, e.delay)] = (
                self.a_to_mask.get((v, e.delay), 0) | (1 << k)
            )
            self.a_delay_mask[e.delay] = (
                self.a_delay_mask.get(e.delay, 0) | (1 << k)
            )
        for k, e in enumerate(survey):
            self.s_src[k] = self.si_map[e.src]
            self.s_dst[k] = self.si_map[e.dst]
            self.s_delay[k] = e.delay
            x, y = self.s_src[k], self.s_dst[k]
            self.s_dir_mask[(x, y)] = self.s_dir_mask.get((x, y), 0) | (1 << k)
        # 自由批准节点集合（至多 2^11=2048 种）对应的延迟段掩码，懒计算。
        self._from_free_cache: dict[tuple, int] = {}
        self._to_free_cache: dict[tuple, int] = {}
        self._both_free_cache: dict[tuple, int] = {}
        # 邻接节点自由掩码（容量上界用）。
        self._adj_a_masks = [0] * self.n
        for u in range(self.n):
            self._adj_a_masks[u] = sum(
                (1 << v for v in self.adj_a.get(u, ())), 0
            )

    # -- 组闭合 -------------------------------------------------------------

    def _closed_groups(self, x, u, m_sa, m_as):
        """指派 x->u 时恰好闭合的无向组：[(另一 survey 节点, 另一批准节点)]。"""
        groups: dict[tuple[int, int], bool] = {}
        for t in self.adj_s.get(x, ()):
            if m_sa[t] != -1:
                groups[(t, m_sa[t])] = True
        for v in self.adj_a.get(u, ()):
            x2 = m_as[v]
            if x2 != -1:
                groups[(x2, v)] = True
        return list(groups)

    def _pair_group(self, x, t, u, v):
        """闭合组（复勘 x,t / 批准 u,v）：正反两个有向方向分别配对后合并。"""
        p1, ua1, us1 = _pair_bucket(
            self.bucket_a.get((u, v), []), self.bucket_s.get((x, t), [])
        )
        p2, ua2, us2 = _pair_bucket(
            self.bucket_a.get((v, u), []), self.bucket_s.get((t, x), [])
        )
        return p1 + p2, ua1 + ua2, us1 + us2

    # -- 乐观上界：位掩码 Kuhn 最大匹配 -------------------------------------

    def _segments_from_free(self, u, d, free_a_mask):
        """源固定为 u、延迟 d、汇在自由批准节点集合中的批准段掩码。"""
        key = (u, d, free_a_mask)
        cached = self._from_free_cache.get(key)
        if cached is not None:
            return cached
        mask = 0
        base = self.a_from_mask.get((u, d), 0)
        while base:
            bit = base & -base
            k = bit.bit_length() - 1
            if (free_a_mask >> self.a_dst[k]) & 1:
                mask |= bit
            base -= bit
        self._from_free_cache[key] = mask
        return mask

    def _segments_to_free(self, v, d, free_a_mask):
        """汇固定为 v、延迟 d、源在自由批准节点集合中的批准段掩码。"""
        key = (v, d, free_a_mask)
        cached = self._to_free_cache.get(key)
        if cached is not None:
            return cached
        mask = 0
        base = self.a_to_mask.get((v, d), 0)
        while base:
            bit = base & -base
            k = bit.bit_length() - 1
            if (free_a_mask >> self.a_src[k]) & 1:
                mask |= bit
            base -= bit
        self._to_free_cache[key] = mask
        return mask

    def _segments_both_free(self, d, free_a_mask):
        """两端均在自由批准节点集合中的延迟 d 批准段掩码。"""
        key = (d, free_a_mask)
        cached = self._both_free_cache.get(key)
        if cached is not None:
            return cached
        mask = 0
        base = self.a_delay_mask.get(d, 0)
        while base:
            bit = base & -base
            k = bit.bit_length() - 1
            if ((free_a_mask >> self.a_src[k]) & 1
                    and (free_a_mask >> self.a_dst[k]) & 1):
                mask |= bit
            base -= bit
        self._both_free_cache[key] = mask
        return mask

    def _exact_candidates(self, sk, m_sa, free_a_mask):
        """复勘段 sk 在当前部分指派下可成完全匹配的批准段掩码。"""
        x, y, d = self.s_src[sk], self.s_dst[sk], self.s_delay[sk]
        ux, uy = m_sa[x], m_sa[y]
        if ux != -1 and uy != -1:
            return self.a_dir_mask.get((ux, uy, d), 0)
        if ux != -1:  # y 自由 => 汇自由
            return self._segments_from_free(ux, d, free_a_mask)
        if uy != -1:  # x 自由 => 源自由
            return self._segments_to_free(uy, d, free_a_mask)
        return self._segments_both_free(d, free_a_mask)

    def _optimistic_exact(self, m_sa, m_as, decided_a, decided_s):
        """未定段之间「同延迟且方向与当前部分映射相容」的最大匹配数。

        松弛上界：逐对检查段对所需的两个节点指派互不冲突，但不检查多对
        段之间的节点一致性，故只可能高估（剪枝安全）。结果只依赖已定段
        集合与已固定的节点指派，与指派次序无关，故按状态缓存。
        """
        key = (decided_a, decided_s, tuple(m_sa))
        cached = self._ub_cache.get(key)
        if cached is not None:
            return cached

        free_a_mask = 0
        for u in range(self.n):
            if m_as[u] == -1:
                free_a_mask |= 1 << u
        undecided_s = self.full_s & ~decided_s

        # 各复勘段的候选批准段掩码。
        cand: dict[int, int] = {}
        bits = undecided_s
        while bits:
            bit = bits & -bits
            sk = bit.bit_length() - 1
            cand[sk] = self._exact_candidates(sk, m_sa, free_a_mask) & ~decided_a
            bits -= bit

        match_a: dict[int, int] = {}  # 批准段 -> 复勘段

        def augment(sk, seen):
            col = cand[sk]
            while col:
                bit = col & -col
                ak = bit.bit_length() - 1
                col -= bit
                if (seen >> ak) & 1:
                    continue
                seen |= 1 << ak
                old = match_a.get(ak)
                if old is None or augment(old, seen):
                    match_a[ak] = sk
                    return True
            return False

        size = 0
        bits = undecided_s
        while bits:
            bit = bits & -bits
            sk = bit.bit_length() - 1
            bits -= bit
            if augment(sk, 0):
                size += 1
        self._ub_cache[key] = size
        return size

    def _node_match_bound(self, m_sa, m_as, decided_a, decided_s):
        """节点变量层面的最大权指派上界（比逐节点独立取 max 更紧）。

        对每个空闲复勘节点 x 与空闲原节点 u 计算权 c(x,u)：x 的未定入射/
        出射段中，在 x→u 后能找到同延迟、方向与对端均相容的未配批准段
        的条数。对 x-u 做最大权二分指派（每个原节点只能服务一个代号）得
        W；记 O 为恰有一个端点空闲的未定复勘段数（这些段在 W 中至多计一
        次），则新增完全匹配数 ≤ (W + O) // 2。

        匈牙利指派 O(k³)，只在剩余自由节点较多（≥ NODE_BOUND_MIN_FREE）
        时启用——此时节点一致性松弛最严重、收紧收益最大；深层状态自由度
        小，廉价的边匹配界已足够。结果按状态缓存。
        """
        NODE_BOUND_MIN_FREE = 7
        free_s = [x for x in range(self.n) if m_sa[x] == -1]
        k = len(free_s)
        if k == 0:
            return 0
        if k < NODE_BOUND_MIN_FREE:
            return 10**9
        key = (decided_a, decided_s, tuple(m_sa))
        cached = self._cap_cache.get(key)
        if cached is not None:
            return cached

        free_a = [u for u in range(self.n) if m_as[u] == -1]
        free_a_mask = sum(1 << u for u in free_a)
        # 各空闲复勘节点的未定邻接段：(正向?, 对端已映射节点 or -1, 延迟)。
        incident: dict[int, list[tuple[bool, int, int]]] = {x: [] for x in free_s}
        one_free = 0
        for sk in range(self.ns):
            if (decided_s >> sk) & 1:
                continue
            x, y, d = self.s_src[sk], self.s_dst[sk], self.s_delay[sk]
            fx, fy = m_sa[x] == -1, m_sa[y] == -1
            if fx and fy:
                incident[x].append((True, -1, d))
                incident[y].append((False, -1, d))
            elif fx:
                incident[x].append((True, m_sa[y], d))
                one_free += 1
            elif fy:
                incident[y].append((False, m_sa[x], d))
                one_free += 1

        cost = [[0] * k for _ in range(k)]
        for i, x in enumerate(free_s):
            items = incident[x]
            for j, u in enumerate(free_a):
                wgt = 0
                for forward, other, d in items:
                    if other != -1:
                        pair = self.a_dir_mask.get(
                            (u, other, d) if forward else (other, u, d), 0
                        )
                    elif forward:
                        pair = self._segments_from_free(u, d, free_a_mask)
                    else:
                        pair = self._segments_to_free(u, d, free_a_mask)
                    if pair & ~decided_a:
                        wgt += 1
                cost[i][j] = -wgt
        neg_w, _ = _hungarian_min(cost)
        result = (-neg_w + one_free) // 2
        self._cap_cache[key] = result
        return result

    # -- 变量/候选选择与对称归并 -------------------------------------------

    def _choose_survey(self, m_sa, m_as):
        def score(x):
            if x in self.fixed:
                return (2, 10**9, -x)
            closes = sum(1 for t in self.adj_s.get(x, ()) if m_sa[t] != -1)
            degree = len(self.adj_s.get(x, ()))
            return (0, closes * 100 + degree, -x)

        free = [x for x in range(self.n) if m_sa[x] == -1]
        return max(free, key=score)

    def _future_lock_targets(self, m_sa):
        return {
            u
            for z, u in self.fixed.items()
            if m_sa[z] == -1
        }

    def _candidate_classes(self, x, m_sa, m_as):
        """可行批准节点按「剩余子问题同构」归并，每类按下标升序返回。"""
        if x in self.fixed:
            u = self.fixed[x]
            return [[u]] if m_as[u] == -1 else []
        unused = sorted(u for u in range(self.n) if m_as[u] == -1)
        used = [v for v in range(self.n) if m_as[v] != -1]
        future_locked = self._future_lock_targets(m_sa)

        classes: list[list[int]] = []
        for u in unused:
            for cls in classes:
                if self._equivalent(u, cls[0], used, unused, future_locked):
                    cls.append(u)
                    break
            else:
                classes.append([u])
        return classes  # 各类首元素即最小下标（unused 已排序、顺序插入）

    def _delay_multiset(self, p, q):
        return tuple(sorted(d for _, d in self.bucket_a.get((p, q), [])))

    def _equivalent(self, u, v, used, unused, future_locked):
        # 对换 (u v) 不得扰动任何尚未生效的锁定（锁定绑定具体代号）。
        if u in future_locked or v in future_locked:
            return False
        # 对换须为批准拓扑（固定其余节点）的自同构：
        # 与每个第三节点 w 之间两方向的延迟多重集必须相同；
        for w in used:
            if self._delay_multiset(u, w) != self._delay_multiset(v, w) or \
               self._delay_multiset(w, u) != self._delay_multiset(w, v):
                return False
        for w in unused:
            if w == u or w == v:
                continue
            if self._delay_multiset(u, w) != self._delay_multiset(v, w) or \
               self._delay_multiset(w, u) != self._delay_multiset(w, v):
                return False
        # u、v 之间两方向延迟多重集对调后不变。
        return self._delay_multiset(u, v) == self._delay_multiset(v, u)

    def _heuristic(self, x, u, m_sa, m_as):
        """临时试指派的闭合收益（完全匹配数, 延迟差），仅用于候选排序。"""
        m_sa[x], m_as[u] = u, x
        exact = dcost = 0
        try:
            for t, v in self._closed_groups(x, u, m_sa, m_as):
                pairs, _, _ = self._pair_group(x, t, u, v)
                for _, _, eq, c in pairs:
                    if eq:
                        exact += 1
                    else:
                        dcost += c
        finally:
            m_sa[x], m_as[u] = -1, -1
        return exact, dcost

    @staticmethod
    def _orbit_expand(vals: set[int], rep: int, members: list[int]) -> set[int]:
        """代表分支的目标集合沿对换 (rep, member) 轨道展开。"""
        out = set(vals)
        for c in members[1:]:
            for val in vals:
                if val == rep:
                    out.add(c)
                elif val == c:
                    out.add(rep)
        return out

    # -- 最优搜索 ------------------------------------------------------------

    def search(self):
        n = self.n
        m_sa = [-1] * n
        m_as = [-1] * n
        decided_a = decided_s = 0
        closed_exact = closed_dcost = 0
        # 多种子贪心构造可行解初值（锁定冲突时为 (-1, INF)），尽快抬高剪枝阈值。
        import random
        alpha = self._greedy_seed()
        rng = random.Random(20260924)
        for _ in range(80):
            cand = self._greedy_seed(rng)
            if cand[0] > alpha[0] or (
                cand[0] == alpha[0] and cand[1] < alpha[1]
            ):
                alpha = cand
        # 子问题结果只取决于部分指派本身（闭合累计是指派集合的确定函数），
        # 与指派次序无关，故按 m_sa 记忆化合并同状态分支。
        memo: dict[tuple, tuple] = {}

        def assign(x, u):
            nonlocal decided_a, decided_s, closed_exact, closed_dcost
            snap = (decided_a, decided_s, closed_exact, closed_dcost)
            m_sa[x], m_as[u] = u, x
            for t, v in self._closed_groups(x, u, m_sa, m_as):
                pairs, una, uns = self._pair_group(x, t, u, v)
                bits_a = bits_s = 0
                for ak, sk, eq, c in pairs:
                    bits_a |= 1 << ak
                    bits_s |= 1 << sk
                    if eq:
                        closed_exact += 1
                    else:
                        closed_dcost += c
                for ak in una:
                    bits_a |= 1 << ak
                for sk in uns:
                    bits_s |= 1 << sk
                decided_a |= bits_a
                decided_s |= bits_s
            return snap

        def undo(x, u, snap):
            nonlocal decided_a, decided_s, closed_exact, closed_dcost
            decided_a, decided_s, closed_exact, closed_dcost = snap
            m_sa[x], m_as[u] = -1, -1

        def pruned(m_sa_, m_as_):
            """两级上界剪枝：先廉价的边匹配界，不够紧再算节点容量界。"""
            ub_edge = self._optimistic_exact(
                m_sa_, m_as_, decided_a, decided_s
            )
            if closed_exact + ub_edge < alpha[0]:
                return True
            if (
                closed_exact + ub_edge == alpha[0]
                and closed_dcost > alpha[1]
            ):
                return True
            ub_node = self._node_match_bound(
                m_sa_, m_as_, decided_a, decided_s
            )
            if closed_exact + min(ub_edge, ub_node) < alpha[0]:
                return True
            return (
                closed_exact + min(ub_edge, ub_node) == alpha[0]
                and closed_dcost > alpha[1]
            )

        def dfs():
            state = tuple(m_sa)
            cached = memo.get(state)
            if cached is not None:
                return None if cached is False else cached

            free = [x for x in range(n) if m_sa[x] == -1]
            if not free:
                result = (
                    closed_exact, closed_dcost, 1,
                    {i: {m_sa[i]} for i in range(n)},
                )
                memo[state] = result
                return result

            if pruned(m_sa, m_as):
                memo[state] = False
                return None

            x = self._choose_survey(m_sa, m_as)
            classes = self._candidate_classes(x, m_sa, m_as)
            ordered = sorted(
                classes,
                key=lambda cls: (
                    -self._heuristic(x, cls[0], m_sa, m_as)[0],
                    self._heuristic(x, cls[0], m_sa, m_as)[1],
                ),
            )

            best_key = None
            count = 0
            sets: dict[int, set[int]] = {}
            for members in ordered:
                rep = members[0]
                snap = assign(x, rep)
                res = dfs()
                undo(x, rep, snap)
                if res is None:
                    continue
                ex, dc, cnt, img = res
                if alpha[0] < ex or (alpha[0] == ex and alpha[1] > dc):
                    alpha[0], alpha[1] = ex, dc
                if best_key is None or (ex, -dc) > best_key:
                    best_key = (ex, -dc)
                    count = cnt * len(members)
                    sets = {
                        i: self._orbit_expand(vals, rep, members)
                        for i, vals in img.items()
                    }
                elif (ex, -dc) == best_key:
                    count += cnt * len(members)
                    for i, vals in img.items():
                        sets.setdefault(i, set()).update(
                            self._orbit_expand(vals, rep, members)
                        )
            if best_key is None:
                memo[state] = False
                return None
            result = (best_key[0], -best_key[1], count, sets)
            memo[state] = result
            return result

        return dfs()

    # -- 贪心种子 ------------------------------------------------------------

    def _greedy_seed(self, rng=None):
        """逐次指派构造可行解；rng 给定时以 30% 概率做随机扰动（多起点）。"""
        n = self.n
        m_sa = [-1] * n
        m_as = [-1] * n
        exact_total = dcost_total = 0
        while True:
            free = [x for x in range(n) if m_sa[x] == -1]
            if not free:
                break
            x = self._choose_survey(m_sa, m_as)
            if x in self.fixed:
                u = self.fixed[x]
                if m_as[u] != -1:
                    return [-1, 10**30]  # 锁定冲突
            else:
                cands = [u for u in range(n) if m_as[u] == -1]
                if rng is not None and rng.random() < 0.3:
                    u = rng.choice(cands)
                else:
                    u = min(
                        cands,
                        key=lambda cand: (
                            -self._heuristic(x, cand, m_sa, m_as)[0],
                            self._heuristic(x, cand, m_sa, m_as)[1],
                            cand,
                        ),
                    )
            m_sa[x], m_as[u] = u, x
            for t, v in self._closed_groups(x, u, m_sa, m_as):
                pairs, _, _ = self._pair_group(x, t, u, v)
                for _, _, eq, c in pairs:
                    if eq:
                        exact_total += 1
                    else:
                        dcost_total += c
        return [exact_total, dcost_total]

    # -- 规范映射：代号升序下的字典序最小最优映射 ---------------------------

    def canonical(self, best_exact, best_dcost):
        n = self.n
        m_sa = [-1] * n
        m_as = [-1] * n
        decided_a = decided_s = 0
        closed_exact = closed_dcost = 0

        def assign(x, u):
            nonlocal decided_a, decided_s, closed_exact, closed_dcost
            snap = (decided_a, decided_s, closed_exact, closed_dcost)
            m_sa[x], m_as[u] = u, x
            for t, v in self._closed_groups(x, u, m_sa, m_as):
                pairs, una, uns = self._pair_group(x, t, u, v)
                bits_a = bits_s = 0
                for ak, sk, eq, c in pairs:
                    bits_a |= 1 << ak
                    bits_s |= 1 << sk
                    if eq:
                        closed_exact += 1
                    else:
                        closed_dcost += c
                for ak in una:
                    bits_a |= 1 << ak
                for sk in uns:
                    bits_s |= 1 << sk
                decided_a |= bits_a
                decided_s |= bits_s
            return snap

        def undo(x, u, snap):
            nonlocal decided_a, decided_s, closed_exact, closed_dcost
            decided_a, decided_s, closed_exact, closed_dcost = snap
            m_sa[x], m_as[u] = -1, -1

        def dfs():
            free = [x for x in range(n) if m_sa[x] == -1]
            if not free:
                return closed_exact == best_exact and closed_dcost == best_dcost
            ub_edge = self._optimistic_exact(
                m_sa, m_as, decided_a, decided_s
            )
            if (
                closed_exact + ub_edge < best_exact
                or (closed_exact + ub_edge == best_exact
                    and closed_dcost > best_dcost)
            ):
                return False
            ub_node = self._node_match_bound(
                m_sa, m_as, decided_a, decided_s
            )
            if closed_exact + min(ub_edge, ub_node) < best_exact or (
                closed_exact + min(ub_edge, ub_node) == best_exact
                and closed_dcost > best_dcost
            ):
                return False

            x = min(free)  # 复勘代号按字典序（下标即排序）
            if x in self.fixed:
                u = self.fixed[x]
                if m_as[u] != -1:
                    return False
                classes = [[u]]
            else:
                # 对称归并安全：代表为类中最小下标，若其分支可行即字典序最小。
                classes = self._candidate_classes(x, m_sa, m_as)
                classes.sort(key=lambda cls: cls[0])
            for members in classes:
                snap = assign(x, members[0])
                if dfs():
                    return True
                undo(x, members[0], snap)
            return False

        if not dfs():
            raise ValueError("规范映射求解失败")  # pragma: no cover
        return m_sa[:]

    # -- 按完整映射复算配对明细 ---------------------------------------------

    def materialize(self, m_sa):
        m_as = [-1] * self.n
        for x, u in enumerate(m_sa):
            m_as[u] = x
        pairs_all: list[tuple[int, int, bool, int]] = []
        seen: set[frozenset[int]] = set()

        def close_group(x, t):
            key = frozenset((x, t))
            if key in seen:
                return
            seen.add(key)
            u, v = m_sa[x], m_sa[t]
            pairs, _, _ = self._pair_group(x, t, u, v)
            pairs_all.extend(pairs)

        # 复勘侧的组（可能映射到空的批准组）；
        for x in range(self.n):
            for t in self.adj_s.get(x, ()):
                close_group(x, t)
        # 批准侧独有的组（错接段：复勘侧两个方向都没有段），映射回复勘节点。
        for u in range(self.n):
            for v in self.adj_a.get(u, ()):
                close_group(m_as[u], m_as[v])

        paired_a = {ak for ak, _, _, _ in pairs_all}
        paired_s = {sk for _, sk, _, _ in pairs_all}
        exact = [(ak, sk) for ak, sk, eq, _ in pairs_all if eq]
        diff = [(ak, sk, c) for ak, sk, eq, c in pairs_all if not eq]
        unmatched_a = [k for k in range(len(self.A)) if k not in paired_a]
        unmatched_s = [k for k in range(len(self.S)) if k not in paired_s]
        return exact, diff, unmatched_a, unmatched_s


def reconcile(
    approved: list[TopoSegment],
    survey: list[TopoSegment],
    approved_nodes: list[str],
    survey_nodes: list[str],
    locks: list[tuple[str, str]],
) -> ReconcileResult:
    """在锁定关系约束下求最优代号对应（节点/段数合法性由校验层保证）。"""
    engine = _Engine(approved, survey, approved_nodes, survey_nodes, locks)

    best = engine.search()
    if best is None:
        raise ValueError("无满足锁定关系的一一映射")
    exact_n, dcost, count, img_sets = best

    canonical_map = engine.canonical(exact_n, dcost)
    exact_pp, diff_pp, un_a, un_s = engine.materialize(canonical_map)

    mapping = [
        (survey_nodes[x], approved_nodes[canonical_map[x]])
        for x in range(len(survey_nodes))
    ]
    certain: dict[str, str] = {}
    optional: dict[str, list[str]] = {}
    for x, code in enumerate(survey_nodes):
        targets = sorted(approved_nodes[u] for u in img_sets.get(x, set()))
        if not targets:  # pragma: no cover - 至少存在一个最优映射
            targets = [approved_nodes[canonical_map[x]]]
        if len(targets) == 1:
            certain[code] = targets[0]
        else:
            optional[code] = targets

    exact_pairs = [
        SegmentPair(approved[ak], survey[sk], 0)
        for ak, sk in sorted(exact_pp)
    ]
    delay_pairs = [
        SegmentPair(approved[ak], survey[sk], c)
        for ak, sk, c in sorted(diff_pp)
    ]

    return ReconcileResult(
        status="ok",
        mapping=mapping,
        mapping_count=count,
        certain=certain,
        optional=optional,
        exact_pairs=exact_pairs,
        delay_pairs=delay_pairs,
        unmatched_approved=[approved[k] for k in un_a],
        unmatched_survey=[survey[k] for k in un_s],
        exact_count=len(exact_pairs),
        delay_cost=dcost,
    )
