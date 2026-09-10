"""单表生成主流程。

职责：把一张表的「骨架行（有限取值组笛卡尔积）」与「附着组（逐行现算）」
合成完整的行集合。引擎不碰 IO —— 产出 TableData 内存对象交给 sink 处理。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ..errors import GenerateError
from ..expr import Expression, build_functions
from ..models import (
    GeneratedRow,
    GroupSpec,
    SeedConfig,
    TableData,
    TableSpec,
)
from ..rng import SeededRandom
from .group_expander import attach_groups, combo_count, expand_skeleton, finite_groups

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S")


def generate_table(
    config: SeedConfig,
    table: TableSpec,
    rng: SeededRandom,
    limit: int | None = None,
) -> TableData:
    """生成一张表的全部数据。"""
    funcs = build_functions(rng)
    finite = finite_groups(table)
    attaches = attach_groups(table)

    total_combos = combo_count(finite)
    if total_combos == 0:
        raise GenerateError(
            f"表 {table.name} 的有限取值组组合数为 0（某个组的 values 为空）",
            f"tables[{table.name}]",
        )

    max_rows = _resolve_max_rows(config, table, limit)
    excludes = [
        Expression(src, funcs, f"limits.exclude[{i}]")
        for i, src in enumerate(config.limits.exclude)
    ]

    rows: list[GeneratedRow] = []
    truncated = False

    for skeleton in expand_skeleton(finite):
        values: dict[str, Any] = dict(skeleton)
        seq = len(rows)  # 行序号按**产出**行递增，保证 sequence 组连续编号

        for group in attaches:
            _apply_group(group, values, seq, funcs, rng)

        env = dict(values)
        env["seq"] = seq
        env["row"] = values

        if any(expr(env) for expr in excludes):
            continue

        rows.append(GeneratedRow(table=table.name, seq=seq, values=values))
        if len(rows) >= max_rows:
            truncated = total_combos > max_rows or len(rows) < total_combos
            break

    data = TableData(
        table=table.name,
        columns=_resolve_columns(table),
        rows=rows,
    )
    data.truncated = truncated  # type: ignore[attr-defined]
    return data


# ---------------------------------------------------------------- 内部实现


def _resolve_max_rows(config: SeedConfig, table: TableSpec, limit: int | None) -> int:
    candidates = [config.limits.max_rows]
    if table.rows:
        candidates.append(table.rows)
    if limit:
        candidates.append(limit)
    return min(candidates)


def _resolve_columns(table: TableSpec) -> list[str]:
    """列顺序：优先使用手工声明的列定义，否则按组声明顺序收集字段。"""
    if table.columns:
        return [c.name for c in table.columns]
    columns: list[str] = []
    for group in table.groups:
        for field in group.fields:
            if field not in columns:
                columns.append(field)
    return columns


def _apply_group(
    group: GroupSpec,
    values: dict[str, Any],
    seq: int,
    funcs: dict,
    rng: SeededRandom,
) -> None:
    """把一个附着组的值写入当前行。"""
    env_main = dict(values)
    env_main["seq"] = seq
    env_main["row"] = values

    if group.when:
        condition = Expression(group.when, funcs, f"group[{group.name}].when")
        if not condition(env_main):
            return

    if group.type == "const":
        _assign(group, values, list(group.value or []))
        return

    if group.type == "sequence":
        base = group.start + group.step * seq
        if group.format_:
            rendered = group.format_.format(
                seq=base, n=base, value=base, v=base, i=seq + 1
            )
            _assign(group, values, [rendered])
        else:
            _assign(group, values, [base] * len(group.fields))
        return

    if group.type == "random":
        _assign(group, values, [_random_value(group, rng) for _ in group.fields])
        return

    if group.type == "derive":
        if not group.expr:
            raise GenerateError(f"derive 组 {group.name} 缺少 expr", f"group[{group.name}]")
        expression = Expression(group.expr, funcs, f"group[{group.name}].expr")
        result = expression(env_main)
        if len(group.fields) == 1:
            values[group.fields[0]] = result
        elif isinstance(result, (list, tuple)) and len(result) == len(group.fields):
            _assign(group, values, list(result))
        else:
            raise GenerateError(
                f"derive 组 {group.name} 有 {len(group.fields)} 个字段，"
                f"但表达式返回了单个值（请返回等长元组）",
                f"group[{group.name}]",
            )
        return

    if group.type == "ref":
        raise GenerateError(
            f"ref 组 {group.name} 需要父表上下文，将在 M2 支持",
            f"group[{group.name}]",
        )

    if group.type == "aggregate":
        return  # Phase 2 回填

    raise GenerateError(f"未知组类型: {group.type}", f"group[{group.name}]")


def _assign(group: GroupSpec, values: dict[str, Any], payload: list[Any]) -> None:
    for field, value in zip(group.fields, payload):
        values[field] = value


def _random_value(group: GroupSpec, rng: SeededRandom) -> Any:
    """按 generator 生成一个随机值。"""
    generator = (group.generator or "int").lower()
    low_high = group.range_

    if generator in {"int", "weighted_int"}:
        if not low_high:
            raise GenerateError(f"random 组 {group.name} 缺少 range", f"group[{group.name}]")
        low, high = int(low_high[0]), int(low_high[1])
        if generator == "weighted_int" and group.buckets:
            return _weighted_int(low, high, group.buckets, rng)
        return rng.rand_int(low, high)

    if generator in {"decimal", "weighted_decimal", "float"}:
        if not low_high:
            raise GenerateError(f"random 组 {group.name} 缺少 range", f"group[{group.name}]")
        return rng.rand_decimal(low_high[0], low_high[1], group.scale or 2)

    if generator in {"choice", "weighted_choice"}:
        pool = [item[0] for item in (group.values or [])]
        if not pool:
            raise GenerateError(f"random choice 组 {group.name} 缺少候选 values", f"group[{group.name}]")
        return rng.rand_choice(pool, group.buckets)

    if generator == "uuid":
        return rng.rand_uuid()

    if generator == "date":
        if not low_high:
            raise GenerateError(f"random date 组 {group.name} 缺少 range", f"group[{group.name}]")
        start, end = _parse_date(low_high[0]), _parse_date(low_high[1])
        span = (end - start).days
        return (start + timedelta(days=rng.rand_int(0, max(span, 0)))).isoformat()

    if generator in {"string", "text"}:
        length = int(low_high[0]) if low_high else 8
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(rng.rand_choice(list(alphabet)) for _ in range(length))

    raise GenerateError(f"不支持的随机生成器: {generator}", f"group[{group.name}]")


def _weighted_int(low: int, high: int, buckets: list[float], rng: SeededRandom) -> int:
    """按权重把区间分段，先选段、再在段内均匀取值。"""
    count = len(buckets)
    span = (high - low + 1) / count
    index = rng.rand_choice(list(range(count)), weights=buckets)
    seg_low = int(low + index * span)
    seg_high = min(int(low + (index + 1) * span) - 1, high)
    if seg_high < seg_low:
        seg_high = seg_low
    return rng.rand_int(seg_low, seg_high)


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    raise GenerateError(f"无法解析为日期: {value!r}")
