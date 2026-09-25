"""复勘请求体校验：两份拓扑与锁定关系的所有错误都定位到具体字段。"""
from __future__ import annotations

from .reconcile import (
    MAX_SEGMENTS,
    MIN_ENDPOINTS,
    MAX_ENDPOINTS,
    TopoSegment,
)

ParsedReconcile = tuple[
    list[TopoSegment],   # approved
    list[TopoSegment],   # survey
    list[str],           # approved nodes（排序）
    list[str],           # survey nodes（排序）
    list[tuple[str, str]],
]


def _validate_topology(raw, field: str, errors: list[dict]):
    """校验一份拓扑（段数组）。成功返回 (segments, 端点集合)，否则 ([], set())。"""
    if not isinstance(raw, list):
        errors.append(
            {"loc": field, "message": "拓扑必须是光纤段数组"}
        )
        return [], set()

    segments: list[TopoSegment] = []
    endpoints: set[str] = set()
    id_pos: dict[str, int] = {}
    error_count_start = len(errors)

    for i, item in enumerate(raw):
        prefix = f"{field}[{i}]"
        if not isinstance(item, dict):
            errors.append(
                {"loc": prefix, "message": "光纤段必须是对象"}
            )
            continue
        seg_id_raw = item.get("id")
        src_raw = item.get("from")
        dst_raw = item.get("to")
        delay = item.get("delay")
        seg_id = seg_id_raw.strip() if isinstance(seg_id_raw, str) else seg_id_raw
        src = src_raw.strip() if isinstance(src_raw, str) else src_raw
        dst = dst_raw.strip() if isinstance(dst_raw, str) else dst_raw
        ok = True

        if not isinstance(seg_id, str) or not seg_id:
            errors.append(
                {"loc": f"{prefix}.id", "message": "段标识必须是非空字符串"}
            )
            ok = False
        elif seg_id in id_pos:
            errors.append(
                {
                    "loc": f"{prefix}.id",
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
                    {"loc": f"{prefix}.{name}", "message": "段端点必须是非空字符串"}
                )
                ok = False

        if (
            isinstance(src, str) and src and isinstance(dst, str) and dst
            and src == dst
        ):
            errors.append(
                {"loc": f"{prefix}.to", "message": "段不能连接同一节点"}
            )
            ok = False

        if isinstance(delay, bool) or not isinstance(delay, int):
            errors.append(
                {"loc": f"{prefix}.delay", "message": "延迟必须为非负整数"}
            )
            ok = False
        elif delay < 0:
            errors.append(
                {"loc": f"{prefix}.delay", "message": "不允许负延迟，延迟必须为非负整数"}
            )
            ok = False

        if ok:
            segments.append(TopoSegment(seg_id, src, dst, delay))
            endpoints.add(src)
            endpoints.add(dst)

    # 空拓扑（或所有段都非法）时：字段级错误已逐条给出；仅在字段都合法、
    # 只是没有段时给一条整体性错误。
    if not segments and len(errors) == error_count_start:
        errors.append(
            {"loc": field, "message": "拓扑至少包含一条光纤段"}
        )
        return [], set()

    # 段数/端点数等整体规模错误仅在本段无字段级错误时给出，避免与
    # 逐字段定位信息重复或互相矛盾。
    if len(errors) == error_count_start:
        if len(raw) > MAX_SEGMENTS:
            errors.append(
                {
                    "loc": field,
                    "message": f"每份拓扑段数不得超过 {MAX_SEGMENTS}（当前 {len(raw)}）",
                }
            )
        if segments:
            n_ep = len(endpoints)
            if not (MIN_ENDPOINTS <= n_ep <= MAX_ENDPOINTS):
                errors.append(
                    {
                        "loc": field,
                        "message": (
                            f"每份拓扑端点数须为 {MIN_ENDPOINTS}..{MAX_ENDPOINTS}"
                            f"（当前 {n_ep}）"
                        ),
                    }
                )
    return segments, endpoints


def validate_reconcile_payload(data: object):
    """校验复勘提交。

    返回 (parsed, errors)：parsed 为
    (approved_segments, survey_segments, approved_nodes, survey_nodes, locks)；
    errors 每项形如 {"loc": "locks[2].target", "message": "..."}。
    """
    if not isinstance(data, dict):
        return None, [{"loc": "body", "message": "请求体必须是 JSON 对象"}]

    errors: list[dict] = []
    approved_segments, approved_eps = _validate_topology(
        data.get("approved"), "approved", errors
    )
    survey_segments, survey_eps = _validate_topology(
        data.get("survey"), "survey", errors
    )

    # 锁定关系（可空）。
    raw_locks = data.get("locks", [])
    if raw_locks is None:
        raw_locks = []
    locks: list[tuple[str, str]] = []
    if not isinstance(raw_locks, list):
        errors.append({"loc": "locks", "message": "锁定关系必须是数组"})
    else:
        seen_code: dict[str, int] = {}
        seen_target: dict[str, int] = {}
        for i, item in enumerate(raw_locks):
            prefix = f"locks[{i}]"
            if not isinstance(item, dict):
                errors.append({"loc": prefix, "message": "锁定关系必须是对象"})
                continue
            code_raw = item.get("code")
            target_raw = item.get("node")
            code = code_raw.strip() if isinstance(code_raw, str) else code_raw
            target = (
                target_raw.strip() if isinstance(target_raw, str) else target_raw
            )
            ok = True
            if not isinstance(code, str) or not code:
                errors.append(
                    {"loc": f"{prefix}.code", "message": "复勘代号必须是非空字符串"}
                )
                ok = False
            elif code in seen_code:
                errors.append(
                    {
                        "loc": f"{prefix}.code",
                        "message": (
                            f"复勘代号 '{code}' 已在 locks[{seen_code[code]}] 锁定，"
                            "不能重复锁定"
                        ),
                    }
                )
                ok = False
            if not isinstance(target, str) or not target:
                errors.append(
                    {"loc": f"{prefix}.node", "message": "原节点必须是非空字符串"}
                )
                ok = False
            elif target in seen_target:
                errors.append(
                    {
                        "loc": f"{prefix}.node",
                        "message": (
                            f"原节点 '{target}' 已在 locks[{seen_target[target]}] "
                            "被锁定，一个原节点不能对应多个代号"
                        ),
                    }
                )
                ok = False

            # 端点存在性（拓扑本身非法时跳过，避免连带噪声）。
            if ok and survey_eps and code not in survey_eps:
                errors.append(
                    {
                        "loc": f"{prefix}.code",
                        "message": f"复勘拓扑中不存在代号 '{code}'",
                    }
                )
                ok = False
            if ok and approved_eps and target not in approved_eps:
                errors.append(
                    {
                        "loc": f"{prefix}.node",
                        "message": f"已批准拓扑中不存在原节点 '{target}'",
                    }
                )
                ok = False
            if ok:
                seen_code[code] = i
                seen_target[target] = i
                locks.append((code, target))

    # 一一映射的前提：两侧端点集合大小相等（代号与原节点逐一对应）。
    if approved_segments and survey_segments and not errors:
        if len(survey_eps) != len(approved_eps):
            errors.append(
                {
                    "loc": "survey",
                    "message": (
                        f"端点集合不相等：复勘拓扑 {len(survey_eps)} 个端点、"
                        f"已批准拓扑 {len(approved_eps)} 个端点，无法一一对应"
                    ),
                }
            )

    if errors:
        return None, errors

    parsed = (
        approved_segments,
        survey_segments,
        sorted(approved_eps),
        sorted(survey_eps),
        locks,
    )
    return parsed, []
