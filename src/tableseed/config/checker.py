"""配置静态校验（``tableseed check``）。

核心是**分组完备性**：一张表的每个字段恰好属于一个组，不重不漏。
这是分组模型成立的前提（README 1.1），必须强制检查。
"""

from __future__ import annotations

from collections import Counter

from ..errors import ExprError, PlanError
from ..expr import Expression, build_functions
from ..models import FINITE_GROUP_TYPES, SeedConfig, TableSpec
from ..rng import SeededRandom
from ..engine.group_expander import combo_count, finite_groups
from ..engine.propagator import effective_rules
from ..engine.topology import topo_order


def check_config(config: SeedConfig) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    problems: list[str] = []
    funcs = build_functions(SeededRandom(config.seed))

    # 由 propagate 提供取值的字段 —— 它们不必再归入任何组
    propagated = _propagated_fields(config)

    problems.extend(_check_table_names(config))
    for table in config.tables:
        problems.extend(
            _check_table(
                config,
                table,
                funcs,
                has_parent=table.name in {r.child for r in config.relations},
                propagated=propagated.get(table.name, set()),
            )
        )
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


def _check_table(
    config: SeedConfig,
    table: TableSpec,
    funcs: dict,
    has_parent: bool = False,
    propagated: set[str] | None = None,
) -> list[str]:
    problems: list[str] = []
    path = f"tables[{table.name}]"
    propagated = propagated or set()

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
        # 由 propagate 提供取值的字段视为「已有着落」，不要求再归组
        ungrouped = sorted(declared - set(occurrences) - propagated)
        unknown = sorted(set(occurrences) - declared)
        if ungrouped:
            problems.append(
                f"{path}: 已声明但未分组的字段: {', '.join(ungrouped)}"
                "（每个字段都必须归入某个组，或由 propagate 提供取值）"
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

        if group.type == "aggregate":
            problems.extend(_check_aggregate(config, table, group, group_path))

    if not finite_groups(table) and not has_parent and not table.rows:
        problems.append(
            f"{path}: 没有任何有限取值组，无法通过笛卡尔积确定行数"
            "（请至少提供一个 enum / boundary / dict 组，或用 rows 声明行数）"
        )
        # 有父表的表行数由父表决定；声明了 rows 的表按声明行数生成

    return problems


def _check_relations(config: SeedConfig) -> list[str]:
    problems: list[str] = []
    names = {t.name for t in config.tables}

    # ---- 全局结构：环 / 多父 ----
    try:
        topo_order(config)
    except PlanError as exc:
        problems.append(str(exc))

    parents_of: dict[str, list[str]] = {}
    for relation in config.relations:
        parents_of.setdefault(relation.child, []).append(relation.parent)
    for child, parents in parents_of.items():
        if len(parents) > 1:
            problems.append(
                f"relations: 表 {child} 有多个父表（{', '.join(parents)}）"
                " —— 多父关系计划在 M4 支持"
            )

    propagated = _propagated_fields(config)
    anchors = _join_anchor_fields(config)

    for index, relation in enumerate(config.relations):
        path = f"relations[{index}]"
        if relation.parent not in names:
            problems.append(f"{path}: 父表不存在: {relation.parent}")
        if relation.child not in names:
            problems.append(f"{path}: 子表不存在: {relation.child}")
        if not relation.join and not relation.propagate:
            problems.append(
                f"{path}: 既未声明关联锚点 join，也没有任何 propagate 规则"
                "（父子表之间将完全无关）"
            )
        if relation.existence == "conditional" and not relation.condition:
            problems.append(f"{path}: existence=conditional 需要提供 condition")
        if relation.existence == "optional" and not relation.condition:
            problems.append(
                f"{path}: existence=optional 但未给 condition，当前等同 required"
                "（如需按比例缺失，请写条件表达式）"
            )
        if relation.cardinality == "N:M":
            problems.append(f"{path}: N:M 基数计划在 M4 支持")

        if relation.parent in names and relation.child in names:
            # 子表字段 = 组字段 ∪ 声明列 ∪ join 锚点字段
            parent_fields = _known_fields(config.table(relation.parent))
            child_fields = (
                _known_fields(config.table(relation.child))
                | anchors.get(relation.child, set())
            )
            for key in relation.join:
                if key.parent_field not in parent_fields:
                    problems.append(
                        f"{path}: 父表 {relation.parent} 中不存在字段 {key.parent_field}"
                    )
                if key.child_field not in child_fields:
                    problems.append(
                        f"{path}: 子表 {relation.child} 中不存在字段 {key.child_field}"
                    )
            problems.extend(
                _check_propagate(
                    config,
                    relation,
                    index,
                    child_fields,
                    parent_fields,
                    config.table(relation.child),
                )
            )

    return problems


def _check_propagate(
    config: SeedConfig,
    relation,
    index: int,
    child_fields: set[str],
    parent_fields: set[str],
    child_table: TableSpec,
) -> list[str]:
    """逐条校验传播规则 —— 「字段要对得上」最容易在这里写错。"""
    problems: list[str] = []
    declared: set[str] = set()

    for pos, rule in enumerate(relation.propagate):
        path = f"relations[{index}].propagate[{pos}]({rule.mode}→{rule.to})"

        if rule.to in declared:
            problems.append(f"{path}: 同一子字段被多条传播规则声明")
        declared.add(rule.to)

        if rule.mode == "free":
            continue

        if rule.to not in child_fields:
            problems.append(
                f"{path}: 子表 {relation.child} 中不存在字段 {rule.to}"
                f"（可用字段: {', '.join(sorted(child_fields))}）"
            )

        if rule.mode in {"copy", "map"}:
            if not rule.from_:
                problems.append(f"{path}: {rule.mode} 传播缺少 from（父字段名）")
            elif rule.from_ not in parent_fields:
                problems.append(
                    f"{path}: 父表 {relation.parent} 中不存在字段 {rule.from_}"
                    f"（可用字段: {', '.join(sorted(parent_fields))}）"
                )

        if rule.mode == "map" and not rule.mapping:
            problems.append(f"{path}: map 传播必须提供 mapping 映射表")

        if rule.mode == "derive":
            if not rule.expr:
                problems.append(f"{path}: derive 传播必须提供 expr")
            if rule.expr and not rule.from_ and not _mentions_parent(rule.expr, parent_fields):
                problems.append(
                    f"{path}: 表达式 {rule.expr!r} 没有引用任何父表字段"
                    "（如需引用请写 parent.xxx）"
                )

        if rule.mode == "split":
            problems.extend(_check_split(config, relation, rule, path, child_table))

    return problems


def _mentions_parent(expr: str, parent_fields: set[str]) -> bool:
    """表达式是否引用了父表字段（``parent.x`` 或直接的父字段名）。"""
    try:
        names = Expression(expr, build_functions(SeededRandom(0)), "check").names
    except ExprError:
        return True  # 表达式本身有问题 —— 交给 _check_expressions 报错
    return bool(names & parent_fields) or "parent" in names




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
        problems.extend(_check_invariant(config, index, source, funcs))

    return problems


def _check_invariant(config: SeedConfig, index: int, invariant, funcs: dict) -> list[str]:
    """静态校验一条不变量：表存在、from 是子表且有 join、表达式可编译。"""
    problems: list[str] = []
    path = f"invariants[{index}]"
    names = {t.name for t in config.tables}

    if invariant.table and invariant.table not in names:
        problems.append(f"{path}: 表不存在: {invariant.table}")

    if invariant.table and invariant.from_:
        source = invariant.from_.split(".")[0]
        if source not in names:
            problems.append(f"{path}: 源表不存在: {source}")
        else:
            matched = [
                r
                for r in config.relations
                if r.parent == invariant.table and r.child == source
            ]
            if not matched:
                problems.append(
                    f"{path}: 源表 {source} 不是 {invariant.table} 的子表"
                )
            elif not matched[0].join:
                problems.append(
                    f"{path}: 跨表不变量需要锚点，"
                    f"但关系 {invariant.table} → {source} 未声明 join"
                )

    try:
        Expression(invariant.expr, funcs, path)
    except ExprError as exc:
        problems.append(str(exc))

    return problems


def _all_fields(table: TableSpec) -> set[str]:
    """组里出现过的字段。"""
    fields: set[str] = set()
    for group in table.groups:
        fields.update(group.fields)
    return fields


def _check_aggregate(
    config: SeedConfig, table: TableSpec, group, group_path: str
) -> list[str]:
    """aggregate 组必须能找到唯一的源表，且两表之间要有带 join 的关系。"""
    problems: list[str] = []
    children = [r for r in config.relations if r.parent == table.name]

    if group.from_:
        source = group.from_.split(".")[0]
        matched = [r for r in children if r.child == source]
        if not matched:
            available = ", ".join(r.child for r in children) or "无"
            problems.append(
                f"{group_path}: 源表 {source} 不是 {table.name} 的子表"
                f"（可用子表: {available}）"
            )
        elif not matched[0].join:
            problems.append(
                f"{group_path}: 聚合需要锚点，但关系 {table.name} → {source} 未声明 join"
            )
        return problems

    if not children:
        problems.append(
            f"{group_path}: aggregate 组需要子表才能汇总，"
            f"但 {table.name} 没有任何子表（请用 from 显式指定源表）"
        )
    elif len(children) > 1:
        problems.append(
            f"{group_path}: {table.name} 有多个子表（{', '.join(r.child for r in children)}），"
            "aggregate 组必须用 from 显式指定源表"
        )
    elif not children[0].join:
        problems.append(
            f"{group_path}: 聚合需要锚点，"
            f"但关系 {table.name} → {children[0].child} 未声明 join"
        )
    return problems


def _check_split(config: SeedConfig, relation, rule, path: str, child_table: TableSpec) -> list[str]:
    """split 拆分的静态校验：份数声明、占比合法性、行数语义冲突。"""
    problems: list[str] = []

    if not rule.parts and not rule.ratio:
        problems.append(f"{path}: split 必须声明 parts（份数）或 ratio（占比）之一")
    if rule.parts is not None and rule.parts < 1:
        problems.append(f"{path}: split 的 parts 必须 >= 1")
    if rule.ratio:
        if any(r <= 0 for r in rule.ratio):
            problems.append(f"{path}: split 的 ratio 每项必须 > 0")
        total = sum(rule.ratio)
        if abs(total - 1) > 0.001:
            problems.append(f"{path}: split 的 ratio 之和应为 1，当前为 {total:.4f}")
    if rule.parts and rule.ratio and len(rule.ratio) != rule.parts:
        problems.append(
            f"{path}: split 的 ratio 有 {len(rule.ratio)} 项，与 parts={rule.parts} 不一致"
        )

    # 行数语义：split 的行数 = 父行数 × 份数，与子表有限组的笛卡尔积展开冲突
    if finite_groups(child_table):
        problems.append(
            f"{path}: split 模式下子表 {relation.child} 的行数由份数决定，"
            "不能再声明有限取值组（请把该组改为 random / derive 等逐行组）"
        )
    if relation.cardinality in {"1:1", "1:0..1"} and (rule.parts or 0) > 1:
        problems.append(
            f"{path}: 1:1 关系下每条父行只有一条子行，无法拆成 {rule.parts} 份"
            "（请改用 1:N）"
        )

    return problems


def _known_fields(table: TableSpec) -> set[str]:
    """这张表**存在**的字段 = 组字段 ∪ 已声明的列。

    校验传播规则时要用这个 —— 子表字段可能完全由 propagate 提供值，
    压根不在任何组里。
    """
    fields = _all_fields(table)
    if table.columns:
        fields.update(c.name for c in table.columns)
    return fields


def _propagated_fields(config: SeedConfig) -> dict[str, set[str]]:
    """每张子表里「由传播规则提供取值」的字段（用于豁免「必须归组」）。

    ``free`` 模式表示不传播，因此不算提供。
    """
    result: dict[str, set[str]] = {}
    for relation in config.relations:
        bucket = result.setdefault(relation.child, set())
        for rule in effective_rules(relation):  # 含 join 自动补齐的 copy
            if rule.mode != "free":
                bucket.add(rule.to)
    return result


def _join_anchor_fields(config: SeedConfig) -> dict[str, set[str]]:
    """``join`` 锚点声明的子字段。

    锚点字段由 join 本身赋予存在性 —— 声明了 ``{child_field: txn_no}``
    就等于声明了子表有 txn_no 这个字段。

    与之相对，用户**显式**书写的 ``propagate.to`` 必须严格校验：
    拼错字段名是要报出来的，否则「对不上」会静默失败。
    """
    result: dict[str, set[str]] = {}
    for relation in config.relations:
        if relation.join:
            bucket = result.setdefault(relation.child, set())
            bucket.update(key.child_field for key in relation.join)
    return result
