"""单表生成主流程。

职责：把一张表的「骨架行（有限取值组笛卡尔积 或 分配得到的组合）」与
「附着组（逐行现算）」合成完整的行集合，再套用父表的传播规则。
引擎不碰 IO —— 产出 TableData 内存对象交给 sink 处理。

两种生成形态
------------
:func:`generate_table`
    独立表（无父表）：有限取值组做**完整笛卡尔积**，行数 = 组合数。

:func:`generate_child`
    子表：按关系基数为**每条父行**生成子行 ——
    1:1 / 1:0..1 用 :class:`ComboAllocator` 分配（固定行数内保覆盖），
    1:N 用笛卡尔积放大（放大行数换覆盖）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterator

from ..errors import GenerateError
from ..expr import Expression, build_functions
from ..models import (
    GeneratedRow,
    GroupSpec,
    RelationSpec,
    SeedConfig,
    TableData,
    TableSpec,
)
from ..rng import SeededRandom
from .allocator import ComboAllocator, drive_fields_of
from .coverage import expand_by_strategy
from .group_expander import attach_groups, combo_count, expand_skeleton, finite_groups
from .propagator import apply_propagate, build_env, effective_rules
from .splitter import split_value

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S")


def generate_table(
    config: SeedConfig,
    table: TableSpec,
    rng: SeededRandom,
    limit: int | None = None,
) -> TableData:
    """生成一张独立表（无父表）的全部数据 —— 完整笛卡尔积。"""
    funcs = build_functions(rng)
    finite = finite_groups(table)
    attaches = attach_groups(table)

    # 注意：空集合的笛卡尔积是 1（不是 0）—— 「没有有限组」必须用 not finite 判断，
    # 用 combo_count == 0 当条件会永远不成立
    if finite and combo_count(finite) == 0:
        raise GenerateError(
            f"表 {table.name} 的有限取值组组合数为 0（某个组的 values 为空）",
            f"tables[{table.name}]",
        )

    declared_rows = table.rows or 0
    if not finite and not declared_rows:
        raise GenerateError(
            f"表 {table.name} 没有任何有限取值组，也没有声明 rows，无法确定行数",
            f"tables[{table.name}]",
        )

    max_rows = _resolve_max_rows(config, table, limit)
    excludes = _compile_excludes(config, funcs)

    strategy = config.limits.strategy
    if not finite:
        # 全为逐行组（random / sequence / derive…）→ 没有笛卡尔积可展开，
        # 行数由声明式 rows 给出。给"纯随机表"留一条行数来源。
        skeletons: Iterator[dict[str, Any]] = iter([{} for _ in range(declared_rows)])
    elif strategy == "full":
        skeletons = expand_skeleton(finite)
    else:
        skeletons = expand_by_strategy(
            finite, strategy, config.limits.sample_size, rng, max_rows
        )

    rows: list[GeneratedRow] = []
    truncated = False

    for skeleton in skeletons:
        seq = len(rows)
        values = _build_row(skeleton, attaches, seq, funcs, rng, parent_values=None)

        env = build_env(values, {}, seq)
        if any(expr(env) for expr in excludes):
            continue

        rows.append(GeneratedRow(table=table.name, seq=seq, values=values))
        if len(rows) >= max_rows:
            truncated = bool(finite) and combo_count(finite) > max_rows or (
                bool(finite) and strategy != "full"
            )
            break

    return _finish(table, rows, truncated)


def generate_child(
    config: SeedConfig,
    table: TableSpec,
    relation: RelationSpec,
    parent_data: TableData,
    rng: SeededRandom,
    limit: int | None = None,
) -> TableData:
    """为父表的每一行生成子表数据。

    - ``1:1`` / ``1:0..1``：每条父行至多一条子行，有限组改用**分配**策略
    - ``1:N``：每条父行展开子表的**完整笛卡尔积**（per_parent 作用域）
    """
    if relation.cardinality == "N:M":
        raise GenerateError(
            f"N:M 关系（{relation.parent} → {relation.child}）计划在 M4 支持",
            f"relations[{relation.parent}→{relation.child}]",
        )

    funcs = build_functions(rng)
    finite = finite_groups(table)
    attaches = attach_groups(table)
    rules = effective_rules(relation)

    allocation = _allocation_of(finite)
    allocator = ComboAllocator(
        finite,
        strategy=allocation,
        rng=rng,
        drive_fields=drive_fields_of(relation, rules),
        path=f"tables[{table.name}]",
    )
    per_parent_combos = combo_count(finite)

    max_rows = _resolve_max_rows(config, table, limit)
    excludes = _compile_excludes(config, funcs)

    rows: list[GeneratedRow] = []
    truncated = False
    if rules and any(r.mode == "split" for r in rules):
        per_parent = max(_split_parts_of(next(r for r in rules if r.mode == "split")), 1)
    else:
        per_parent = 1 if _is_one_to_one(relation) else max(combo_count(finite), 1)
    theoretical = len(parent_data) * per_parent

    for parent_row in parent_data.rows:
        if not _should_exist(relation, parent_row, funcs):
            continue

        # split 模式：行数由 parts 决定（每父行恰好 parts 行），需要预先拆好各份金额
        split_rule = next((r for r in rules if r.mode == "split"), None)
        split_pieces: dict[str, list] | None = None
        part_count = 0
        if split_rule is not None:
            part_count = _split_parts_of(split_rule)
            split_pieces = {}
            for rule in (r for r in rules if r.mode == "split"):
                total = parent_row.values.get(rule.from_)  # from_ 已由 checker 保证存在
                scale = _scale_of(config, table, rule.to)
                split_pieces[rule.to] = split_value(
                    total,
                    part_count,
                    rng,
                    scale=scale,
                    ratio=rule.ratio,
                    path=f"tables[{table.name}].propagate({rule.mode}→{rule.to})",
                )

        skeletons: Iterator[dict[str, Any]]
        part_total = 1
        if split_rule is not None:
            # 拆分模式：每父行恰好 parts 行，骨架为空（行数不来自笛卡尔积）
            skeletons = iter([{} for _ in range(part_count)])
            part_total = part_count
        elif _is_one_to_one(relation):
            # 1:1 —— 分配一个组合，行数不放大
            skeletons = iter([allocator.assign(parent_row.values, len(rows))])
        else:
            # 1:N —— 按策略展开（默认笛卡尔积放大，覆盖换行数）
            strategy = config.limits.strategy
            if strategy == "full":
                skeletons = expand_skeleton(finite) if finite else iter([{}])
            else:
                skeletons = expand_by_strategy(
                    finite, strategy, config.limits.sample_size, rng, max_rows
                ) or iter([{}])

        part_index = 0
        for skeleton in skeletons:
            seq = len(rows)
            values = _build_row(
                skeleton, attaches, seq, funcs, rng, parent_row.values
            )

            apply_propagate(
                rules,
                values,
                parent_row.values,
                funcs,
                f"tables[{table.name}]",
                seq,
                split_pieces=split_pieces,
                part_index=part_index if split_rule is not None else 0,
            )
            part_index += 1

            env = build_env(values, parent_row.values, seq)
            if any(expr(env) for expr in excludes):
                continue

            rows.append(
                GeneratedRow(
                    table=table.name, seq=seq, values=values, parent_seq=parent_row.seq
                )
            )
            if len(rows) >= max_rows:
                truncated = theoretical > max_rows
                break
        if truncated:
            break

    return _finish(table, rows, truncated)


# ---------------------------------------------------------------- 内部实现


def _is_one_to_one(relation: RelationSpec) -> bool:
    return relation.cardinality in {"1:1", "1:0..1"}


def _split_parts_of(rule) -> int:
    """split 规则的份数：优先 parts，否则 ratio 的项数。"""
    if rule.parts:
        return rule.parts
    if rule.ratio:
        return len(rule.ratio)
    return 0


def _scale_of(config: SeedConfig, table: TableSpec, field: str) -> int:
    """拆分精度取自列声明的 scale（缺省 2，即「分」）。"""
    if table.columns:
        for column in table.columns:
            if column.name == field and column.scale is not None:
                return column.scale
    return 2


def _allocation_of(finite: list[GroupSpec]) -> str:
    """分配策略以**第一个有限取值组**为准（它是驱动组）。"""
    return finite[0].allocation if finite else "follow_parent"


def _compile_excludes(config: SeedConfig, funcs: dict) -> list[Expression]:
    return [
        Expression(src, funcs, f"limits.exclude[{i}]")
        for i, src in enumerate(config.limits.exclude)
    ]


def _should_exist(relation: RelationSpec, parent_row: GeneratedRow, funcs: dict) -> bool:
    """按存在性判定父行是否需要子行。"""
    if relation.existence == "required":
        return True
    if not relation.condition:
        return True  # optional 但无 condition —— 等同 required，checker 会提示
    expression = Expression(
        relation.condition, funcs, f"relations[{relation.parent}→{relation.child}].condition"
    )
    env: dict[str, Any] = dict(parent_row.values)
    env["parent"] = parent_row.values
    env["row"] = parent_row.values
    return bool(expression(env))


def _build_row(
    skeleton: dict[str, Any],
    attaches: list[GroupSpec],
    seq: int,
    funcs: dict,
    rng: SeededRandom,
    parent_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合成一行：骨架 + 附着组。

    组带 ``when`` 条件且不满足时，该组字段不会被写入（保持骨架值或缺失）。
    """
    values: dict[str, Any] = dict(skeleton)
    parent_values = parent_values or {}

    for group in attaches:
        _apply_group(group, values, seq, funcs, rng, parent_values)

    return values


def _finish(table: TableSpec, rows: list[GeneratedRow], truncated: bool) -> TableData:
    data = TableData(
        table=table.name,
        columns=_resolve_columns(table),
        rows=rows,
    )
    data.truncated = truncated  # type: ignore[attr-defined]
    return data


def _resolve_max_rows(config: SeedConfig, table: TableSpec, limit: int | None) -> int:
    """行数上限。

    ``table.rows`` **不是截断上限** —— 用户写 ``rows: 3`` 的意图是"我要 3 行"，
    不是"最多 3 行"。把它当上限会让组合展开被截断（6 种组合只出 3 行），
    预演却按"取满组合数"算，两边对不上（用户报过"预估 3 行超过上限 1，将截断"）。
    所以这里只看全局 ``limits.max_rows`` 与显式 ``limit``。
    """
    candidates = [config.limits.max_rows]
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
    parent_values: dict[str, Any] | None = None,
) -> None:
    """把一个附着组的值写入当前行。"""
    parent_values = parent_values or {}
    env_main = build_env(values, parent_values, seq)

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
        result = expression(build_env(values, parent_values, seq))
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
        _assign(group, values, [_ref_value(group, parent_values) for _ in group.fields])
        return

    if group.type == "aggregate":
        return  # Phase 2 回填（M3）

    raise GenerateError(f"未知组类型: {group.type}", f"group[{group.name}]")


def _ref_value(group: GroupSpec, parent_values: dict[str, Any]) -> Any:
    """ref 组：引用父表字段。

    ``from`` 支持三种写法：
    ``parent.acct_no`` / ``t_account.acct_no`` / ``acct_no``。
    M2 只支持引用直接父表，跨级引用留待后续版本。
    """
    if not group.from_:
        raise GenerateError(f"ref 组 {group.name} 缺少 from", f"group[{group.name}]")

    raw = group.from_
    field = raw.split(".")[-1]

    if field not in parent_values:
        raise GenerateError(
            f"ref 组 {group.name} 引用了父表字段 {field!r}，但父行中不存在"
            f"（可用字段: {', '.join(sorted(parent_values)) or '无'}）"
            "。M2 只支持引用直接父表字段。",
            f"group[{group.name}]",
        )
    return parent_values[field]


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
            raise GenerateError(f"random 组 {group.name} 缺少 range", f"group[{group.name}]")
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
