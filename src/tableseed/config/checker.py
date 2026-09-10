"""配置静态校验（``tableseed check``）。

核心是**分组完备性**：一张表的每个字段恰好属于一个组，不重不漏。
这是分组模型成立的前提（README 1.1），必须强制检查。
"""

from __future__ import annotations

from collections import Counter

from ..errors import ExprError
from ..expr import Expression, build_functions
from ..models import FINITE_GROUP_TYPES, SeedConfig, TableSpec
from ..rng import SeededRandom
from ..engine.group_expander import combo_count, finite_groups


def check_config(config: SeedConfig) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    problems: list[str] = []
    funcs = build_functions(SeededRandom(config.seed))

    problems.extend(_check_table_names(config))
    for table in config.tables:
        problems.extend(_check_table(table, funcs))
    problems.extend(_check_relations(config))
    problems.extend(_check_limits(config))
    problems.extend(_check_expressions(config, funcs))
    return problems


def _check_table_names(config: SeedConfig) -> list[str]:
    counter = Counter(t.name for t in config.tables)
    return [
        f"表名重复: {name}（出现 {count} 次）"
        for name, count in counter.items()
        if count > 1
    ]


def _check_table(table: TableSpec, funcs: dict) -> list[str]:
    problems: list[str] = []
    path = f"tables[{table.name}]"

    if not table.groups:
        return [f"{path}: 未定义任何字段组"]

    # ---- 分组完备性：每个字段恰好属于一个组 ----
    occurrences = Counter()
    for index, group in enumerate(table.groups):
        if not group.fields:
            problems.append(f"{path}.groups[{index}]({group.name}): 组内没有任何字段")
        for field in group.fields:
            occurrences[field] += 1

    duplicated = [f for f, c in occurrences.items() if c > 1]
    if duplicated:
        problems.append(
            f"{path}: 字段被重复分组: {', '.join(sorted(duplicated))}"
            "（每个字段只能属于一个组）"
        )

    if table.columns:
        declared = {c.name for c in table.columns}
        ungrouped = sorted(declared - set(occurrences))
        unknown = sorted(set(occurrences) - declared)
        if ungrouped:
            problems.append(
                f"{path}: 已声明但未分组的字段: {', '.join(ungrouped)}"
                "（每个字段都必须归入某个组）"
            )
        if unknown:
            problems.append(
                f"{path}: 分组中出现未声明的字段: {', '.join(unknown)}"
            )

    # ---- 组内取值与类型相关校验 ----
    for index, group in enumerate(table.groups):
        group_path = f"{path}.groups[{index}]({group.name})"

        if group.type in FINITE_GROUP_TYPES:
            if not group.values:
                problems.append(f"{group_path}: 有限取值组必须提供 values")
            else:
                for pos, item in enumerate(group.values):
                    if len(item) != len(group.fields):
                        problems.append(
                            f"{group_path}.values[{pos}]: 取值元组长度 {len(item)} "
                            f"与字段数 {len(group.fields)} 不一致"
                        )
            if group.allocation == "weighted" and not group.buckets:
                problems.append(f"{group_path}: allocation=weighted 需要提供 buckets 权重")

        if group.type == "sequence" and not group.format_ and len(group.fields) > 1:
            problems.append(f"{group_path}: 多字段 sequence 组建议提供 format 模板")

        if group.type in {"derive", "aggregate"} and not group.expr:
            problems.append(f"{group_path}: {group.type} 组必须提供 expr")

        if group.type == "const" and not group.value:
            problems.append(f"{group_path}: const 组必须提供 value")

        if group.type == "random" and not group.range_ and (group.generator or "int") not in {
            "uuid",
            "choice",
            "weighted_choice",
        }:
            problems.append(f"{group_path}: random 组缺少 range")

        if group.type == "ref" and not group.from_:
            problems.append(f"{group_path}: ref 组必须提供 from（源表.源字段）")

    if not finite_groups(table):
        problems.append(
            f"{path}: 没有任何有限取值组，无法通过笛卡尔积确定行数"
            "（请至少提供一个 enum / boundary / dict 组）"
        )

    return problems


def _check_relations(config: SeedConfig) -> list[str]:
    problems: list[str] = []
    names = {t.name for t in config.tables}

    for index, relation in enumerate(config.relations):
        path = f"relations[{index}]"
        if relation.parent not in names:
            problems.append(f"{path}: 父表不存在: {relation.parent}")
        if relation.child not in names:
            problems.append(f"{path}: 子表不存在: {relation.child}")
        if not relation.join:
            problems.append(f"{path}: 未声明关联锚点 join")
        if relation.existence == "conditional" and not relation.condition:
            problems.append(f"{path}: existence=conditional 需要提供 condition")

        if relation.parent in names and relation.child in names:
            parent_fields = _all_fields(config.table(relation.parent))
            child_fields = _all_fields(config.table(relation.child))
            for key in relation.join:
                if key.parent_field not in parent_fields:
                    problems.append(
                        f"{path}: 父表 {relation.parent} 中不存在字段 {key.parent_field}"
                    )
                if key.child_field not in child_fields:
                    problems.append(
                        f"{path}: 子表 {relation.child} 中不存在字段 {key.child_field}"
                    )

    return problems


def _check_limits(config: SeedConfig) -> list[str]:
    problems: list[str] = []
    funcs = build_functions(SeededRandom(config.seed))

    for table in config.tables:
        combos = combo_count(finite_groups(table))
        if combos == 0:
            continue
        if combos > config.limits.max_rows:
            problems.append(
                f"tables[{table.name}]: 理论组合数 {combos} 超过 max_rows "
                f"{config.limits.max_rows}（请调高 max_rows 或改用 pairwise / sample 策略）"
            )

    if config.limits.strategy == "sample" and not config.limits.sample_size:
        problems.append("limits: strategy=sample 需要提供 sample_size")

    for index, src in enumerate(config.limits.exclude):
        try:
            Expression(src, funcs, f"limits.exclude[{index}]")
        except ExprError as exc:
            problems.append(str(exc))

    return problems


def _check_expressions(config: SeedConfig, funcs: dict) -> list[str]:
    problems: list[str] = []

    for table in config.tables:
        for index, group in enumerate(table.groups):
            for attribute in ("when", "expr"):
                source = getattr(group, attribute)
                if not source:
                    continue
                try:
                    Expression(source, funcs, f"tables[{table.name}].groups[{index}].{attribute}")
                except ExprError as exc:
                    problems.append(str(exc))

    for index, source in enumerate(config.invariants):
        try:
            Expression(source, funcs, f"invariants[{index}]")
        except ExprError as exc:
            problems.append(str(exc))

    return problems


def _all_fields(table: TableSpec) -> set[str]:
    fields: set[str] = set()
    for group in table.groups:
        fields.update(group.fields)
    return fields
