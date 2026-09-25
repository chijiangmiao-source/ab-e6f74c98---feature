"""验收脚本：通过真实 HTTP 接口校验业务结果。

校验内容：
  1. 健康入口；
  2. 双路：贪心反例必须返回全局最优 12（用独立枚举对拍，而非信任求解器），
     两条路径边互不重复、链路连续、总延迟可复算；
  3. 共享瓶颈：返回源侧节点集合与**全部**外出割边，并独立验证割的容量与阻断性；
  4. 负延迟/重复段标识/不存在端点/不可达均定位报错；
  5. 并行光纤与零延迟；静态页面可访问；
  6. 接线复勘：
     - 对称歧义：同优映射数与每代号可选目标由独立全枚举复核；
     - 局部贪心误配反例：逐段就近锁死诱饵只得 1 段，全局得 5 段；
     - 锁定冲突 / 重复标识 / 端点集合不等均 400 且定位；
     - 旧双路规划接口与页面行为回归。

任何一项失败即以非零退出码退出。
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
import urllib.error
import urllib.request

GREEDY_CASE = {
    "source": "S",
    "target": "T",
    "segments": [
        {"id": "e1", "from": "S", "to": "A", "delay": 1},
        {"id": "e2", "from": "A", "to": "B", "delay": 1},
        {"id": "e3", "from": "B", "to": "T", "delay": 1},
        {"id": "e4", "from": "S", "to": "B", "delay": 5},
        {"id": "e5", "from": "A", "to": "T", "delay": 5},
    ],
}

BOTTLENECK_CASE = {
    "source": "S",
    "target": "T",
    "segments": [
        {"id": "e1", "from": "S", "to": "A", "delay": 2},
        {"id": "e2", "from": "S", "to": "B", "delay": 3},
        {"id": "e3", "from": "A", "to": "X", "delay": 4},
        {"id": "e4", "from": "B", "to": "X", "delay": 5},
        {"id": "e5", "from": "X", "to": "T", "delay": 1},
    ],
}

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def request(base_url: str, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url + "/api/protected-paths", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(base_url: str, path: str):
    with urllib.request.urlopen(base_url + path, timeout=10) as resp:
        return resp.status, resp.read().decode()


def wait_for_health(base_url: str, attempts: int = 30) -> bool:
    for i in range(attempts):
        try:
            status, _ = get(base_url, "/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def independent_optimum(segments, source, target):
    """独立参考实现：枚举全部简单路径及两两不相交组合的最小总延迟。"""
    adj = {}
    for i, s in enumerate(segments):
        adj.setdefault(s["from"], []).append((s["to"], i))
    paths = []

    def dfs(node, used_edges, used_nodes, cost):
        if node == target:
            paths.append((frozenset(used_edges), cost))
            return
        for nxt, ei in adj.get(node, []):
            if ei not in used_edges and nxt not in used_nodes:
                dfs(nxt, used_edges | {ei}, used_nodes | {nxt},
                    cost + segments[ei]["delay"])

    dfs(source, frozenset(), {source}, 0)
    best = None
    for (p1, c1), (p2, c2) in itertools.combinations_with_replacement(paths, 2):
        if p1.isdisjoint(p2):
            cand = c1 + c2
            best = cand if best is None else min(best, cand)
    return best


def edge_disjoint_path_count(segments, source, target):
    """独立 Ford-Fulkerson（BFS 增广，单位容量，支持并行段）。"""
    adj = {}
    for i, s in enumerate(segments):
        adj.setdefault(s["from"], []).append([s["to"], i, True])
        # 反向引用由增广时翻转，简化为残差邻接表：
    # 用显式残差边结构重做：
    graph = {}

    def edges_of(node):
        return graph.setdefault(node, [])

    edge_refs = []
    for s in segments:
        fwd = [s["to"], 1, None]
        rev = [s["from"], 0, None]
        fwd[2] = rev
        rev[2] = fwd
        edges_of(s["from"]).append(fwd)
        edges_of(s["to"]).append(rev)
        edge_refs.append((fwd, rev))

    count = 0
    while True:
        prev = {source: None}
        queue = [source]
        found = False
        while queue and not found:
            v = queue.pop(0)
            for e in edges_of(v):
                if e[1] > 0 and e[0] not in prev:
                    prev[e[0]] = (v, e)
                    if e[0] == target:
                        found = True
                        break
                    queue.append(e[0])
        if not found:
            break
        node = target
        while node != source:
            v, e = prev[node]
            e[1] -= 1
            e[2][1] += 1
            node = v
        count += 1
    return count


def verify_dual_paths(base_url: str) -> None:
    print("\n== 1. 双路全局最优（贪心反例） ==")
    status, data = request(base_url, GREEDY_CASE)
    check("HTTP 200", status == 200, f"got {status} {data}")
    check("status=ok", data.get("status") == "ok", str(data))
    paths = data.get("paths", [])
    check("返回两条路径", len(paths) == 2, str(paths))

    all_ids, contiguous = [], True
    source, target = GREEDY_CASE["source"], GREEDY_CASE["target"]
    for p in paths:
        segs = p["segments"]
        all_ids.extend(s["id"] for s in segs)
        if not segs or segs[0]["from"] != source or segs[-1]["to"] != target:
            contiguous = False
        for a, b in zip(segs, segs[1:]):
            if a["to"] != b["from"]:
                contiguous = False
        recomputed = sum(s["delay"] for s in segs)
        check(f"路径 {p} 延迟可复算", recomputed == p["delay"],
              f"{recomputed} != {p.get('delay')}")
    check("两条路径边互不重复", len(all_ids) == len(set(all_ids)), str(all_ids))
    check("每条路径为起点到终点的连续链路", contiguous)

    total = data.get("totalDelay")
    check("最小总延迟 = 12（全局最优，非贪心删边结果）", total == 12, str(total))
    check("总延迟 = 两路径延迟之和",
          total == sum(p["delay"] for p in paths), str(total))

    expected = independent_optimum(GREEDY_CASE["segments"], source, target)
    check("独立枚举对拍确认 12 确为全局最优", expected == 12, f"枚举得 {expected}")
    check("接口结果与独立枚举一致", total == expected)


def verify_cut_evidence(base_url: str) -> None:
    print("\n== 2. 共享瓶颈与割证据 ==")
    status, data = request(base_url, BOTTLENECK_CASE)
    check("HTTP 200", status == 200, f"got {status} {data}")
    check("status=insufficient", data.get("status") == "insufficient", str(data))

    cut = data.get("cut", {})
    src_set = set(cut.get("sourceSet", []))
    cut_edges = cut.get("edges", [])
    segments = BOTTLENECK_CASE["segments"]
    source, target = "S", "T"

    check("源侧集合含起点", source in src_set, str(src_set))
    check("源侧集合不含终点", target not in src_set, str(src_set))
    check("源侧集合包含汇聚节点 S,A,B",
          {"S", "A", "B"} <= src_set, str(src_set))

    crossing = {
        s["id"] for s in segments
        if s["from"] in src_set and s["to"] not in src_set
    }
    reported = {e["id"] for e in cut_edges}
    check("每条报告割边均跨源侧/外部集合", reported <= crossing, str(reported))
    check("列出全部外出割边（无遗漏）", reported == crossing,
          f"reported={reported}, actual={crossing}")
    check("外出割边仅 e5（共享光纤瓶颈）", reported == {"e5"}, str(reported))

    # 独立验证：该图边不相交 s-t 路径数确实不足 2；
    # 且移除全部割边后起点不可达终点（割的阻断性）。
    n_paths = edge_disjoint_path_count(segments, source, target)
    check("独立最大流确认双路不存在", n_paths < 2, f"flow={n_paths}")
    kept = [s for s in segments if s["id"] not in reported]
    n_after = edge_disjoint_path_count(kept, source, target)
    check("移除全部外出割边后路径数为 0（割有效）", n_after == 0,
          f"flow after cut={n_after}")


def verify_error_cases(base_url: str) -> None:
    print("\n== 3. 非法输入定位报错 ==")

    bad = {
        "source": "S", "target": "T",
        "segments": [{"id": "e1", "from": "S", "to": "T", "delay": -7}],
    }
    status, data = request(base_url, bad)
    check("负延迟返回 400", status == 400)
    check("负延迟定位到 segments[0].delay",
          any(e["loc"] == "segments[0].delay" for e in data.get("errors", [])),
          str(data))

    dup = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "x", "from": "S", "to": "A", "delay": 1},
            {"id": "x", "from": "A", "to": "T", "delay": 1},
        ],
    }
    status, data = request(base_url, dup)
    check("重复段标识返回 400", status == 400)
    check("重复段标识定位到具体下标",
          any(e["loc"] == "segments[1].id" for e in data.get("errors", [])),
          str(data))

    missing = {
        "source": "Z", "target": "T",
        "segments": [{"id": "e1", "from": "S", "to": "T", "delay": 1}],
    }
    status, data = request(base_url, missing)
    check("不存在端点返回 400", status == 400)
    check("不存在端点定位到 source",
          any(e["loc"] == "source" for e in data.get("errors", [])), str(data))

    unreachable = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "e1", "from": "S", "to": "A", "delay": 1},
            {"id": "e2", "from": "B", "to": "T", "delay": 1},
        ],
    }
    status, data = request(base_url, unreachable)
    check("不可达输入返回 400", status == 400)
    check("不可达定位到 target",
          any(e["loc"] == "target" for e in data.get("errors", [])), str(data))


def verify_parallel_and_page(base_url: str) -> None:
    print("\n== 4. 并行光纤/零延迟 与 页面/冒烟 ==")
    payload = {
        "source": "S", "target": "T",
        "segments": [
            {"id": "p1", "from": "S", "to": "T", "delay": 0},
            {"id": "p2", "from": "S", "to": "T", "delay": 3},
            {"id": "p3", "from": "S", "to": "T", "delay": 9},
        ],
    }
    status, data = request(base_url, payload)
    check("并行光纤 HTTP 200", status == 200, str(data))
    check("并行光纤取最小两条，总延迟 3", data.get("totalDelay") == 3, str(data))
    ids = sorted(s["id"] for p in data.get("paths", []) for s in p["segments"])
    check("选用 p1/p2 两条不同段", ids == ["p1", "p2"], str(ids))

    status, html = get(base_url, "/")
    check("首页 200", status == 200)
    check("首页为录入页面", "束线保护" in html and "protected-paths" in html)
    check("页面含过期请求防护（请求序号 + AbortController）",
          "requestSeq" in html and "AbortController" in html)
    check("页面在出错/编辑时清除旧结论",
          "clearResult" in html and "旧结论已清除" in html)
    # 复勘页面元素回归
    check("页面含接线复勘面板与 /api/recheck 调用",
          "接线复勘" in html and "/api/recheck" in html)
    check("复勘页展示规范映射/完全匹配/延迟不一致/未匹配分区",
          all(k in html for k in
              ["规范映射", "完全匹配段", "延迟不一致段", "未匹配的复勘段"]))
    check("复勘页同样有过期请求防护与结论清除",
          "rcSeq" in html and "本次结论已清除" in html)


# ===================== 接线复勘验收 =====================

# 全同延迟完全二部图：源侧 {S,T}、汇侧 {A,B}，真实代号对应存在
# 2! x 2! = 4 个同优映射。
SYM_APPROVED = [
    {"id": "a1", "from": "S", "to": "A", "delay": 1},
    {"id": "a2", "from": "S", "to": "B", "delay": 1},
    {"id": "a3", "from": "T", "to": "A", "delay": 1},
    {"id": "a4", "from": "T", "to": "B", "delay": 1},
]
SYM_RECHECK = [
    {"id": "r1", "from": "p", "to": "q", "delay": 1},
    {"id": "r2", "from": "p", "to": "r", "delay": 1},
    {"id": "r3", "from": "s", "to": "q", "delay": 1},
    {"id": "r4", "from": "s", "to": "r", "delay": 1},
]

# 局部贪心误配反例：首段 r0=(c1,c2,1) 的唯一零延迟诱饵是 a4=(v2,v0,1)，
# 逐段就近先配它后，任何全局双射都只能救回 1 段；全局 QAP 得 5 段。
GREEDY_RECHECK_APPROVED = [
    {"id": "a0", "from": "v3", "to": "v1", "delay": 2},
    {"id": "a1", "from": "v3", "to": "v2", "delay": 2},
    {"id": "a2", "from": "v1", "to": "v0", "delay": 3},
    {"id": "a3", "from": "v2", "to": "v3", "delay": 0},
    {"id": "a4", "from": "v2", "to": "v0", "delay": 1},
    {"id": "a5", "from": "v3", "to": "v0", "delay": 2},
]
GREEDY_RECHECK_RECHECK = [
    {"id": "r0", "from": "c1", "to": "c2", "delay": 1},
    {"id": "r1", "from": "c1", "to": "c0", "delay": 2},
    {"id": "r2", "from": "c2", "to": "c3", "delay": 3},
    {"id": "r3", "from": "c0", "to": "c1", "delay": 0},
    {"id": "r4", "from": "c0", "to": "c3", "delay": 1},
    {"id": "r5", "from": "c1", "to": "c3", "delay": 2},
]


def recheck_request(base_url: str, approved, recheck, locks=None):
    body = {
        "approvedSegments": approved,
        "recheckSegments": recheck,
        "locks": locks or [],
    }
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + "/api/recheck", data=raw,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def independent_recheck_score(approved, recheck, mp):
    """独立计算某双射 mp（代号->原节点）下的 (完全匹配数, 延迟差之和)。

    每个有向端点对上：同延迟先配满；剩余两侧按延迟排序配残差段，
    多者留下（未匹配，不扣分）。
    """
    from collections import Counter as _Counter
    ac = {}
    for s in approved:
        ac.setdefault((s["from"], s["to"]), _Counter())[s["delay"]] += 1
    rc = {}
    for s in recheck:
        rc.setdefault((mp[s["from"]], mp[s["to"]]), _Counter())[s["delay"]] += 1
    exact = diff = 0
    for key in set(ac) | set(rc):
        a, r = ac.get(key, _Counter()), rc.get(key, _Counter())
        ra, rr = [], []
        for d in set(a) | set(r):
            ca, cr = a.get(d, 0), r.get(d, 0)
            exact += min(ca, cr)
            if ca > cr:
                ra += [d] * (ca - cr)
            elif cr > ca:
                rr += [d] * (cr - ca)
        ra.sort()
        rr.sort()
        diff += sum(abs(x - y) for x, y in zip(ra, rr))
    return exact, diff


def enumerate_recheck_optima(approved, recheck, locks=None):
    """独立全枚举：满足锁定的全部双射，返回最优键/同优数/每代号可选目标。"""
    anodes = sorted({n for s in approved for n in (s["from"], s["to"])})
    rnodes = sorted({n for s in recheck for n in (s["from"], s["to"])})
    locks = locks or {}
    best_key = None
    count = 0
    options = {c: set() for c in rnodes}
    for perm in itertools.permutations(anodes):
        mp = dict(zip(rnodes, perm))
        if any(mp.get(c) != t for c, t in locks.items()):
            continue
        exact, diff = independent_recheck_score(approved, recheck, mp)
        key = (exact, -diff)
        if best_key is None or key > best_key:
            best_key = key
            count = 1
            options = {c: {mp[c]} for c in rnodes}
        elif key == best_key:
            count += 1
            for c in rnodes:
                options[c].add(mp[c])
    return best_key, count, options


def verify_recheck_symmetry(base_url: str) -> None:
    print("\n== 5. 复勘对称歧义：同优映射数与可选目标（独立全枚举对拍） ==")
    status, data = recheck_request(base_url, SYM_APPROVED, SYM_RECHECK)
    check("复勘 HTTP 200", status == 200, str(data))
    check("四段完全匹配", data.get("metrics", {}).get("exactCount") == 4, str(data))
    check("无延迟不一致", data.get("metrics", {}).get("delayDiffSum") == 0, str(data))

    best_key, count, options = enumerate_recheck_optima(
        SYM_APPROVED, SYM_RECHECK
    )
    check("同优映射数 = 4（接口）", data.get("optimalCount") == 4, str(data.get("optimalCount")))
    check("同优映射数与独立枚举一致", data.get("optimalCount") == count, str(count))
    check("接口最优键与独立枚举一致",
          (data["metrics"]["exactCount"], -data["metrics"]["delayDiffSum"]) == best_key)
    # 每个代号的必然/可选目标与枚举一致
    for code, targets in options.items():
        got = set(data.get("options", {}).get(code, []))
        check(f"代号 {code} 可选目标与枚举一致 {sorted(targets)}", got == targets,
              f"got {sorted(got)}")
    expect_cert = "optional" if count > 1 else "fixed"
    check("全部代号标记为可选",
          all(v == expect_cert for v in data["certainty"].values()),
          str(data["certainty"]))
    # 规范映射稳定：按代号排序的目标序列为字典序最小最优序列
    seq = [m["target"] for m in data["mapping"]]
    codes = [m["code"] for m in data["mapping"]]
    check("规范映射按代号排序", codes == sorted(codes))
    anodes = sorted({n for s in SYM_APPROVED for n in (s["from"], s["to"])})
    min_seq = None
    rnodes = sorted({n for s in SYM_RECHECK for n in (s["from"], s["to"])})
    for perm in itertools.permutations(anodes):
        mp = dict(zip(rnodes, perm))
        e, d = independent_recheck_score(SYM_APPROVED, SYM_RECHECK, mp)
        if (e, -d) == best_key:
            cand = [mp[c] for c in rnodes]
            if min_seq is None or cand < min_seq:
                min_seq = cand
    check("规范目标序列字典序最小", seq == min_seq, f"{seq} vs {min_seq}")

    # 锁定一个代号消歧：q=B 后同优数降为 2
    status2, data2 = recheck_request(
        base_url, SYM_APPROVED, SYM_RECHECK, [{"code": "q", "target": "B"}]
    )
    _, count_locked, _ = enumerate_recheck_optima(
        SYM_APPROVED, SYM_RECHECK, {"q": "B"}
    )
    check("锁定后 HTTP 200", status2 == 200, str(data2))
    check("锁定 q=B 被遵守",
          dict((m["code"], m["target"]) for m in data2["mapping"])["q"] == "B")
    check("锁定后同优数 = 2（与枚举一致）",
          data2.get("optimalCount") == count_locked == 2,
          f"{data2.get('optimalCount')} vs {count_locked}")
    check("被锁定代号标记必然", data2["certainty"]["q"] == "fixed")


def verify_recheck_greedy_counterexample(base_url: str) -> None:
    print("\n== 6. 复勘局部贪心误配反例（不得逐段匹配后拼接） ==")
    status, data = recheck_request(
        base_url, GREEDY_RECHECK_APPROVED, GREEDY_RECHECK_RECHECK
    )
    check("HTTP 200", status == 200, str(data))
    exact = data.get("metrics", {}).get("exactCount")
    diff = data.get("metrics", {}).get("delayDiffSum")
    check("全局完全匹配段数 = 5", exact == 5, str(data.get("metrics")))
    check("其余同向段延迟差之和 = 1", diff == 1, str(diff))
    # 独立枚举复核最优值
    best_key, count, _ = enumerate_recheck_optima(
        GREEDY_RECHECK_APPROVED, GREEDY_RECHECK_RECHECK
    )
    check("独立枚举确认 (5, -1) 为全局最优", best_key == (5, -1), str(best_key))
    check("同优映射数与枚举一致", data.get("optimalCount") == count, str(count))

    # 模拟"逐段就近"：把首段唯一最近诱饵 (c1,c2)->(v2,v0) 锁死
    status2, greedy = recheck_request(
        base_url, GREEDY_RECHECK_APPROVED, GREEDY_RECHECK_RECHECK,
        [{"code": "c1", "target": "v2"}, {"code": "c2", "target": "v0"}],
    )
    check("锁定诱饵对应后仍 200", status2 == 200, str(greedy))
    g_exact = greedy.get("metrics", {}).get("exactCount")
    check("逐段贪心诱饵锁死后只得 1 段全匹配", g_exact == 1, str(g_exact))
    check("全局结论严格优于逐段贪心", exact > g_exact, f"{exact} > {g_exact}")

    # 结论分区齐全：1 条延迟不一致，无未匹配（6 对 6）
    check("延迟不一致段 1 条", len(data.get("delayMismatches", [])) == 1,
          str(data.get("delayMismatches")))
    check("两侧均无未匹配段",
          data.get("unmatchedRecheck") == [] and data.get("unmatchedApproved") == [])
    mm = data["delayMismatches"][0]
    check("延迟差可复算 = 1",
          abs(mm["recheck"]["delay"] - mm["approved"]["delay"]) == mm["diff"] == 1)


def verify_recheck_errors(base_url: str) -> None:
    print("\n== 7. 复勘锁定冲突 / 重复标识 / 端点集合不等 ==")
    # 两个代号锁同一目标：无可行锁定
    conflict = {
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
    status, data = recheck_request(
        base_url, conflict["approvedSegments"],
        conflict["recheckSegments"], conflict["locks"],
    )
    check("锁定冲突返回 400", status == 400, str(status))
    check("锁定冲突定位到 locks[1].target",
          any(e["loc"] == "locks[1].target" for e in data.get("errors", [])),
          str(data))

    # 重复段标识
    dup_app = [
        {"id": "x", "from": "S", "to": "A", "delay": 1},
        {"id": "x", "from": "A", "to": "T", "delay": 1},
    ]
    rc = [
        {"id": "r1", "from": "p", "to": "q", "delay": 1},
        {"id": "r2", "from": "q", "to": "s", "delay": 1},
    ]
    status, data = recheck_request(base_url, dup_app, rc)
    check("复勘重复段标识返回 400", status == 400)
    check("重复标识定位到 approvedSegments[1].id",
          any(e["loc"] == "approvedSegments[1].id" for e in data.get("errors", [])),
          str(data))

    # 端点集合不相等（数目不等）
    app = [
        {"id": "a1", "from": "S", "to": "A", "delay": 1},
        {"id": "a2", "from": "A", "to": "T", "delay": 1},
    ]
    rc3 = [
        {"id": "r1", "from": "p", "to": "q", "delay": 1},
        {"id": "r2", "from": "q", "to": "s", "delay": 1},
        {"id": "r3", "from": "s", "to": "z", "delay": 1},
    ]
    status, data = recheck_request(base_url, app, rc3)
    check("端点集合不等返回 400", status == 400)
    check("端点集合不等定位到 recheckSegments",
          any(e["loc"] == "recheckSegments" for e in data.get("errors", [])),
          str(data))

    # 锁定未知代号 / 未知目标
    status, data = recheck_request(
        base_url, app, rc[:2], [{"code": "zz", "target": "S"}]
    )
    check("未知锁定代号 400", status == 400)
    check("未知代号定位到 locks[0].code",
          any(e["loc"] == "locks[0].code" for e in data.get("errors", [])))
    status, data = recheck_request(
        base_url, app, rc[:2], [{"code": "p", "target": "ZZ"}]
    )
    check("未知锁定目标 400 且定位", status == 400 and
          any(e["loc"] == "locks[0].target" for e in data.get("errors", [])))

    # 出错时接口不应返回任何结论字段
    check("错误响应清除本次结论（无 mapping 字段）",
          "mapping" not in data and "optimalCount" not in data)


def verify_old_api_regression(base_url: str) -> None:
    print("\n== 8. 旧双路规划接口与页面回归 ==")
    # 原有接口路径、请求体、字段保持不变
    status, data = request(base_url, GREEDY_CASE)
    check("旧接口 /api/protected-paths 仍 200", status == 200)
    check("旧接口总延迟仍为 12", data.get("totalDelay") == 12, str(data))
    check("旧接口字段结构保持",
          set(data.keys()) == {"status", "totalDelay", "paths"})
    status, html = get(base_url, "/")
    check("旧页面草稿与提交流程保持",
          all(k in html for k in
              ["SAMPLE", "/api/protected-paths", "提交求解", "添加光纤段"]))



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://app:8080")
    args = parser.parse_args()

    print(f"等待服务健康：{args.base_url}/health")
    if not wait_for_health(args.base_url):
        print("[FAIL] 健康入口不可达")
        return 1
    print("[PASS] 健康入口 200")

    verify_dual_paths(args.base_url)
    verify_cut_evidence(args.base_url)
    verify_error_cases(args.base_url)
    verify_parallel_and_page(args.base_url)
    verify_recheck_symmetry(args.base_url)
    verify_recheck_greedy_counterexample(args.base_url)
    verify_recheck_errors(args.base_url)
    verify_old_api_regression(args.base_url)

    print("\n" + "=" * 60)
    if failures:
        print(f"验收失败：{len(failures)} 项 -> {failures}")
        return 1
    print("验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
