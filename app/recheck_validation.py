"""复勘请求校验：两份拓扑与锁定关系都必须逐字段定位报错。"""
from __future__ import annotations

from .recheck import MAX_NODES, MAX_SEGMENTS, MIN_NODES
from .solver import Segment

ParsedRecheck = tuple[list[Segment], list[Segment], dict[str, str]]


def _validate_topology(
    data: dict, field: str, errors: list[dict]
) -> list[Segment] | None:
    """校验一份拓扑（段列表），返回段或 None（错误已追加）。

    field 形如 "approvedSegments"，错误 loc 一律定位到具体下标字段。
    """
    raw_segments = data.get(field)
    if not isinstance(raw_segments, list) or not raw_segments:
        errors.append(
            {"loc": field, "message": "光纤段列表必须是非空数组"}
        )
        return None
    if len(raw_segments) > MAX_SEGMENTS:
        errors.append(
            {
                "loc": field,
                "message": f"每份拓扑段数至多 {MAX_SEGMENTS}，当前 {len(raw_segments)} 段",
            }
        )

    segments: list[Segment] = []
    endpoints: set[str] = set()
    id_pos: dict[str, int] = {}

    for i, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            errors.append(
                {"loc": f"{field}[{i}]", "message": "光纤段必须是对象"}
            )
            continue
        seg_id_raw = raw.get("id")
        src_raw = raw.get("from")
        dst_raw = raw.get("to")
        delay = raw.get("delay")
        seg_id = seg_id_raw.strip() if isinstance(seg_id_raw, str) else seg_id_raw
        src = src_raw.strip() if isinstance(src_raw, str) else src_raw
        dst = dst_raw.strip() if isinstance(dst_raw, str) else dst_raw
        ok = True

        if not isinstance(seg_id, str) or not seg_id:
            errors.append(
                {"loc": f"{field}[{i}].id", "message": "段标识必须是非空字符串"}
            )
            ok = False
        elif seg_id in id_pos:
            errors.append(
                {
                    "loc": f"{field}[{i}].id",
                    "message": (
                        f"段标识 '{seg_id}' 与 {field}[{id_pos[seg_id]}].id 重复"
                    ),
                }
            )
            ok = False
        else:
            id_pos[seg_id] = i

        for name, value in (("from", src), ("to", dst)):
            if not isinstance(value, str) or not value:
                errors.append(
                    {
                        "loc": f"{field}[{i}].{name}",
                        "message": "段端点必须是非空字符串",
                    }
                )
                ok = False

        if (
            isinstance(src, str)
            and src
            and isinstance(dst, str)
            and dst
            and src == dst
        ):
            errors.append(
                {"loc": f"{field}[{i}].to", "message": "段不能连接同一节点"}
            )
            ok = False

        if isinstance(delay, bool) or not isinstance(delay, int):
            errors.append(
                {"loc": f"{field}[{i}].delay", "message": "延迟必须为非负整数"}
            )
            ok = False
        elif delay < 0:
            errors.append(
                {
                    "loc": f"{field}[{i}].delay",
                    "message": "不允许负延迟，延迟必须为非负整数",
                }
            )
            ok = False

        if ok:
            segments.append(Segment(seg_id, src, dst, delay))
            endpoints.add(src)
            endpoints.add(dst)

    if errors or not segments:
        return None

    node_count = len(endpoints)
    if node_count < MIN_NODES or node_count > MAX_NODES:
        errors.append(
            {
                "loc": field,
                "message": (
                    f"每份拓扑端点数须在 {MIN_NODES} 至 {MAX_NODES} 之间，"
                    f"当前 {node_count} 个"
                ),
            }
        )
        return None
    return segments


def validate_recheck_payload(
    data: object,
) -> tuple[ParsedRecheck | None, list[dict]]:
    """校验复勘提交：两份拓扑 + 锁定代号对应关系。"""
    if not isinstance(data, dict):
        return None, [{"loc": "body", "message": "请求体必须是 JSON 对象"}]

    errors: list[dict] = []
    approved = _validate_topology(data, "approvedSegments", errors)
    recheck = _validate_topology(data, "recheckSegments", errors)

    approved_nodes = {n for s in (approved or []) for n in (s.src, s.dst)}
    recheck_nodes = {n for s in (recheck or []) for n in (s.src, s.dst)}

    # 代号与原节点允许不同名（现场临时代号），能建立一一映射的前提是
    # 两侧端点**数目相等**；数目不等时无法一一对应，定位到复勘拓扑。
    if approved and recheck and len(approved_nodes) != len(recheck_nodes):
        errors.append(
            {
                "loc": "recheckSegments",
                "message": (
                    f"两份拓扑端点集合不相等：已批准侧 {len(approved_nodes)} 个端点"
                    f"（{', '.join(sorted(approved_nodes))}），复勘侧 "
                    f"{len(recheck_nodes)} 个端点（{', '.join(sorted(recheck_nodes))}），"
                    "无法建立一一映射"
                ),
            }
        )

    # ---- 锁定关系：可缺省；重复代号/重复目标/未知端点都定位反馈 ----
    raw_locks = data.get("locks", [])
    if raw_locks is None:
        raw_locks = []
    locks: dict[str, str] = {}
    if not isinstance(raw_locks, list):
        errors.append({"loc": "locks", "message": "锁定关系必须是数组"})
    else:
        code_pos: dict[str, int] = {}
        target_pos: dict[str, int] = {}
        for i, raw in enumerate(raw_locks):
            if not isinstance(raw, dict):
                errors.append(
                    {"loc": f"locks[{i}]", "message": "锁定关系必须是对象"}
                )
                continue
            code_raw, target_raw = raw.get("code"), raw.get("target")
            code = code_raw.strip() if isinstance(code_raw, str) else code_raw
            target = (
                target_raw.strip() if isinstance(target_raw, str) else target_raw
            )
            code_ok = True
            target_ok = True

            if not isinstance(code, str) or not code:
                errors.append(
                    {"loc": f"locks[{i}].code", "message": "复勘代号必须是非空字符串"}
                )
                code_ok = False
            elif recheck_nodes and code not in recheck_nodes:
                errors.append(
                    {
                        "loc": f"locks[{i}].code",
                        "message": f"复勘代号 '{code}' 不存在于复勘拓扑端点中",
                    }
                )
                code_ok = False
            elif code in code_pos:
                errors.append(
                    {
                        "loc": f"locks[{i}].code",
                        "message": (
                            f"代号 '{code}' 已在 locks[{code_pos[code]}] 锁定，"
                            "一个代号只能锁定一个目标"
                        ),
                    }
                )
                code_ok = False

            if not isinstance(target, str) or not target:
                errors.append(
                    {"loc": f"locks[{i}].target", "message": "目标节点必须是非空字符串"}
                )
                target_ok = False
            elif approved_nodes and target not in approved_nodes:
                errors.append(
                    {
                        "loc": f"locks[{i}].target",
                        "message": f"目标节点 '{target}' 不存在于已批准拓扑端点中",
                    }
                )
                target_ok = False
            elif target in target_pos:
                errors.append(
                    {
                        "loc": f"locks[{i}].target",
                        "message": (
                            f"目标 '{target}' 已被 locks[{target_pos[target]}] 锁定，"
                            "锁定必须一一对应，不能两个代号指向同一目标"
                        ),
                    }
                )
                target_ok = False

            if code_ok:
                code_pos[code] = i
            if target_ok:
                target_pos[target] = i
            if code_ok and target_ok:
                locks[code] = target

    if errors:
        return None, errors
    return (approved, recheck, locks), []
