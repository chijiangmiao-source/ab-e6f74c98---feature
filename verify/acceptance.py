"""验收脚本：通过真实 HTTP 接口校验业务结果。

校验内容：
  1. 健康入口；
  2. 双路：贪心反例必须返回全局最优 12（用独立枚举对拍，而非信任求解器），
     两条路径边互不重复、链路连续、总延迟可复算；
  3. 共享瓶颈：返回源侧节点集合与**全部**外出割边，并独立验证割的容量与阻断性；
  4. 负延迟/重复段标识/不存在端点/不可达均定位报错；
  5. 并行光纤与零延迟；静态页面可访问；
  6. 接线复勘：对称歧义（同优映射数与必然/可选目标）、局部贪心误配反例
     （必须整体择优而非逐段就近）、锁定改变/冲突定位、错接与延迟录错
     的段级分类、端点集合不等/重复标识定位；
  7. 旧双路规划接口与页面回归。

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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def reconcile_request(base_url: str, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base_url + "/api/reconcile", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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
    check("首页含双路规划录入页", "束线保护" in html and "protected-paths" in html)
    check("页面含接线复勘录入与接口", "reconcile" in html and "已批准拓扑" in html)
    check("双路页含过期请求防护（请求序号 + AbortController）",
          "requestSeq" in html and "AbortController" in html)
    check("复勘页同样带过期请求防护（独立序号 + AbortController）",
          "reconcileSeq" in html)
    check("页面在出错/编辑时清除旧结论",
          "clearResult" in html and "旧结论已清除" in html)
    check("复勘出错清除本次结论", "本次结论已清除" in html)
    # 页面呈现四类结论：规范映射、完全匹配、延迟不一致、两侧未匹配。
    for marker in ("规范映射", "同优映射数", "完全匹配段", "延迟不一致段",
                   "未匹配段", "必然", "可选"):
        check(f"复勘结果区含「{marker}」", marker in html)
    # 页面内置局部贪心误配反例（延迟全 3 的结构陷阱），供人工复勘演示。
    check("页面内置复勘贪心反例数据",
          '"a2", from: "B", to: "C", delay: 3' in html
          and '"f2", from: "z", to: "y", delay: 3' in html)
    # 复勘提交确实打到 /api/reconcile，而非前端自行拼凑结论。
    check("复勘结论只来自真实接口 /api/reconcile",
          'fetch("/api/reconcile"' in html)


# ---------------------------------------------------------------------------
# 接线复勘（对真实接口发请求；最优性用独立全枚举复核）
# ---------------------------------------------------------------------------

SYMMETRIC_CASE = {
    "approved": [
        {"id": "a1", "from": "C", "to": "L1", "delay": 2},
        {"id": "a2", "from": "C", "to": "L2", "delay": 2},
    ],
    "survey": [
        {"id": "f1", "from": "c", "to": "p", "delay": 2},
        {"id": "f2", "from": "c", "to": "q", "delay": 2},
    ],
    "locks": [],
}

# 延迟全相同（3）的结构反例：逐段就近会把 y 猜成 B，
# 只有整体一一映射 x=A、z=B、y=C 才能让三段全部完全匹配。
GREEDY_RECONCILE_CASE = {
    "approved": [
        {"id": "a1", "from": "A", "to": "B", "delay": 3},
        {"id": "a2", "from": "B", "to": "C", "delay": 3},
        {"id": "a3", "from": "A", "to": "C", "delay": 3},
    ],
    "survey": [
        {"id": "f1", "from": "x", "to": "y", "delay": 3},
        {"id": "f2", "from": "z", "to": "y", "delay": 3},
        {"id": "f3", "from": "x", "to": "z", "delay": 3},
    ],
    "locks": [],
}

# 锁定方向后：f1 同向但延迟录错（|Δ|=4）；f2 映射到 B->D 在批准拓扑中
# 不存在（错接），批准侧 a2（A->D）也无对应 => 两侧各有未匹配段。
DELAY_AND_WRONG_CASE = {
    "approved": [
        {"id": "a1", "from": "A", "to": "B", "delay": 5},
        {"id": "a2", "from": "A", "to": "D", "delay": 9},
    ],
    "survey": [
        {"id": "f1", "from": "x", "to": "y", "delay": 1},
        {"id": "f2", "from": "y", "to": "Z", "delay": 4},
    ],
    "locks": [
        {"code": "x", "node": "A"},
        {"code": "y", "node": "B"},
        {"code": "Z", "node": "D"},
    ],
}


def independent_reconcile_enum(body):
    """独立参考：枚举所有满足锁定的一一映射，组内全排列配对。

    返回 (best_exact, best_cost, 最优映射序列列表)，映射序列按复勘代号
    排序后的已批准节点名表示。
    """
    approved = body["approved"]
    survey = body["survey"]
    a_nodes = sorted({n for s in approved for n in (s["from"], s["to"])})
    s_nodes = sorted({n for s in survey for n in (s["from"], s["to"])})
    ai = {n: i for i, n in enumerate(a_nodes)}
    si = {n: i for i, n in enumerate(s_nodes)}
    bA, bS = {}, {}
    for s in approved:
        bA.setdefault((ai[s["from"]], ai[s["to"]]), []).append(s["delay"])
    for s in survey:
        bS.setdefault((si[s["from"]], si[s["to"]]), []).append(s["delay"])
    lockm = {si[l["code"]]: ai[l["node"]] for l in body.get("locks", [])}

    def bucket(av, sv):
        k = min(len(av), len(sv))
        if k == 0:
            return 0, 0
        best = None
        for ia in itertools.permutations(range(len(av)), k):
            for js in itertools.permutations(range(len(sv)), k):
                eq = sum(av[a] == sv[b] for a, b in zip(ia, js))
                c = sum(abs(av[a] - sv[b]) for a, b in zip(ia, js))
                if best is None or (eq, -c) > best:
                    best = (eq, -c)
        return best[0], -best[1]

    results = []
    for perm in itertools.permutations(range(len(a_nodes))):
        if any(perm[x] != u for x, u in lockm.items()):
            continue
        ex = dc = 0
        for (x, y), sv in bS.items():
            e, c = bucket(bA.get((perm[x], perm[y]), []), sv)
            ex += e
            dc += c
        seq = tuple(a_nodes[perm[si[n]]] for n in s_nodes)
        results.append((ex, dc, seq))
    be = max(r[0] for r in results)
    bc = min(r[1] for r in results if r[0] == be)
    opts = sorted({r[2] for r in results if r[0] == be and r[1] == bc})
    return be, bc, opts


def verify_reconcile_symmetry(base_url: str) -> None:
    print("\n== 5. 复勘对称歧义（真实接口 + 独立全枚举） ==")
    status, data = reconcile_request(base_url, SYMMETRIC_CASE)
    check("复勘 HTTP 200", status == 200, str(data))
    check("同优映射数 = 2（两片叶子互换）", data.get("mappingCount") == 2,
          str(data.get("mappingCount")))
    mapping = {m["code"]: m["node"] for m in data.get("mapping", [])}
    check("中心代号必然对应 C", mapping.get("c") == "C", str(mapping))
    optional = {o["code"]: o["nodes"] for o in data.get("optional", [])}
    check("叶子代号均可选 L1/L2",
          optional.get("p") == ["L1", "L2"] and optional.get("q") == ["L1", "L2"],
          str(optional))
    certain = {c["code"]: c["node"] for c in data.get("certain", [])}
    check("仅中心为必然", certain == {"c": "C"}, str(certain))
    check("规范映射字典序稳定（p->L1, q->L2）",
          mapping == {"c": "C", "p": "L1", "q": "L2"}, str(mapping))
    check("两段均为完全匹配", data.get("exactCount") == 2, str(data))

    # 独立全枚举复核同优映射数与规范序列。
    be, bc, opts = independent_reconcile_enum(SYMMETRIC_CASE)
    check("独立枚举确认最优完全匹配数 2", be == 2, str(be))
    check("独立枚举确认同优映射数 2", len(opts) == 2, str(opts))
    check("规范映射 = 枚举中字典序最小序列",
          tuple(mapping[c] for c in sorted(mapping)) == opts[0], str(opts[0]))


def verify_reconcile_greedy_trap(base_url: str) -> None:
    print("\n== 6. 复勘局部贪心误配反例（整体择优） ==")
    status, data = reconcile_request(base_url, GREEDY_RECONCILE_CASE)
    check("HTTP 200", status == 200, str(data))
    check("三段全部完全匹配（非逐段就近的 1 段）",
          data.get("exactCount") == 3, str(data.get("exactCount")))
    mapping = {m["code"]: m["node"] for m in data.get("mapping", [])}
    check("整体映射 x=A, z=B, y=C",
          mapping == {"x": "A", "y": "C", "z": "B"}, str(mapping))
    check("无延迟差、无未匹配段",
          data.get("delayCost") == 0
          and not data.get("unmatchedApproved")
          and not data.get("unmatchedSurvey"), str(data))

    be, bc, opts = independent_reconcile_enum(GREEDY_RECONCILE_CASE)
    check("独立枚举确认 3 段确为全局最优", be == 3, str(be))
    check("独立枚举确认最优映射唯一", len(opts) == 1, str(opts))
    check("接口映射与枚举一致",
          tuple(mapping[c] for c in sorted(mapping)) == opts[0], str(opts[0]))


def verify_reconcile_locks(base_url: str) -> None:
    print("\n== 7. 复勘锁定：破歧义与锁定冲突定位 ==")
    body = json.loads(json.dumps(SYMMETRIC_CASE))
    body["locks"] = [{"code": "p", "node": "L2"}]
    status, data = reconcile_request(base_url, body)
    check("加锁后 HTTP 200", status == 200, str(data))
    check("加锁后映射唯一", data.get("mappingCount") == 1, str(data))
    mapping = {m["code"]: m["node"] for m in data.get("mapping", [])}
    check("锁定 p->L2 生效，q 被迫 ->L1",
          mapping == {"c": "C", "p": "L2", "q": "L1"}, str(mapping))

    # 锁定冲突：同一原节点锁给两个代号。
    bad = json.loads(json.dumps(SYMMETRIC_CASE))
    bad["locks"] = [
        {"code": "p", "node": "L1"},
        {"code": "q", "node": "L1"},
    ]
    status, data = reconcile_request(base_url, bad)
    check("锁定冲突返回 400", status == 400, str(status))
    check("冲突定位到具体 locks 下标",
          any(e["loc"].startswith("locks[1]") for e in data.get("errors", [])),
          str(data))

    # 锁定代号在复勘拓扑中不存在。
    bad2 = json.loads(json.dumps(SYMMETRIC_CASE))
    bad2["locks"] = [{"code": "ghost", "node": "C"}]
    status, data = reconcile_request(base_url, bad2)
    check("未知锁定代号 400 且定位", status == 400 and
          any(e["loc"] == "locks[0].code" for e in data.get("errors", [])),
          str(data))


def verify_reconcile_classification(base_url: str) -> None:
    print("\n== 8. 复勘段级分类：延迟录错 vs 错接 ==")
    status, data = reconcile_request(base_url, DELAY_AND_WRONG_CASE)
    check("HTTP 200", status == 200, str(data))
    check("无完全匹配段", data.get("exactCount") == 0, str(data))
    dm = data.get("delayMismatches", [])
    check("恰好一段延迟不一致（a1 vs f1）",
          len(dm) == 1 and dm[0]["approved"]["id"] == "a1"
          and dm[0]["survey"]["id"] == "f1" and dm[0]["delayDiff"] == 4,
          str(dm))
    check("延迟绝对差之和 = 4", data.get("delayCost") == 4, str(data))
    ua = {s["id"] for s in data.get("unmatchedApproved", [])}
    us = {s["id"] for s in data.get("unmatchedSurvey", [])}
    check("错接段两侧分别未匹配（a2 / f2）", ua == {"a2"} and us == {"f2"},
          f"{ua} / {us}")

    be, bc, _ = independent_reconcile_enum(DELAY_AND_WRONG_CASE)
    check("独立枚举复核延迟差之和 4", bc == 4 and be == 0, f"{be}/{bc}")


def verify_reconcile_bad_inputs(base_url: str) -> None:
    print("\n== 9. 复勘非法输入：重复标识 / 端点集合不等 / 规模 ==")
    dup = json.loads(json.dumps(GREEDY_RECONCILE_CASE))
    dup["survey"][2]["id"] = "f1"
    status, data = reconcile_request(base_url, dup)
    check("复勘重复段标识 400", status == 400)
    check("重复定位到 survey[2].id",
          any(e["loc"] == "survey[2].id" for e in data.get("errors", [])),
          str(data))

    unequal = {
        "approved": [
            {"id": "a1", "from": "A", "to": "B", "delay": 1},
            {"id": "a2", "from": "B", "to": "C", "delay": 1},
            {"id": "a3", "from": "C", "to": "D", "delay": 1},
        ],
        "survey": [
            {"id": "f1", "from": "x", "to": "y", "delay": 1},
            {"id": "f2", "from": "y", "to": "z", "delay": 1},
        ],
        "locks": [],
    }
    status, data = reconcile_request(base_url, unequal)
    check("端点集合不相等 400", status == 400)
    check("端点集合不等有明确反馈",
          any("端点集合不相等" in e["message"] for e in data.get("errors", [])),
          str(data))

    neg = json.loads(json.dumps(GREEDY_RECONCILE_CASE))
    neg["approved"][0]["delay"] = -2
    status, data = reconcile_request(base_url, neg)
    check("复勘负延迟 400 且定位", status == 400 and
          any(e["loc"] == "approved[0].delay" for e in data.get("errors", [])),
          str(data))

    over = {
        "approved": [
            {"id": f"a{i}", "from": "A", "to": f"B{i}", "delay": 0}
            for i in range(25)
        ],
        "survey": [
            {"id": f"f{i}", "from": "x", "to": f"y{i}", "delay": 0}
            for i in range(25)
        ],
        "locks": [],
    }
    status, data = reconcile_request(base_url, over)
    check("段数超过 24 返回 400", status == 400)
    check("段数限制反馈含 24",
          any("24" in e["message"] for e in data.get("errors", [])), str(data))


def verify_legacy_regression(base_url: str) -> None:
    print("\n== 10. 旧双路规划接口回归 ==")
    status, data = request(base_url, GREEDY_CASE)
    check("旧接口贪心反例仍为全局最优 12",
          status == 200 and data.get("totalDelay") == 12, str(data))
    status, _ = get(base_url, "/health")
    check("健康入口仍可用", status == 200)


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
    verify_reconcile_symmetry(args.base_url)
    verify_reconcile_greedy_trap(args.base_url)
    verify_reconcile_locks(args.base_url)
    verify_reconcile_classification(args.base_url)
    verify_reconcile_bad_inputs(args.base_url)
    verify_legacy_regression(args.base_url)

    print("\n" + "=" * 60)
    if failures:
        print(f"验收失败：{len(failures)} 项 -> {failures}")
        return 1
    print("验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
