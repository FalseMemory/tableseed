"""Phase 2 · 反向回填：父表的 ``aggregate`` 组由子表汇总得出。

为什么需要两阶段
----------------
``aggregate`` 构成一条**反向边**（子 → 父）：父表的汇总字段依赖子表数据，
而子表又要引用父表的主键。二者互相依赖，在依赖图上成环 ——
所以不能靠拓扑排序解决，只能分两趟：

- Phase 1（正向）：父 → 子，生成全部行，`aggregate` 组先空着
- Phase 2（反向）：子表已就绪，按锚点聚合回填父表

典型场景：交易表的金额合计 = 明细之和；银行余额表由流水汇总。

关于 ``by`` 语法
----------------
tech-design 里写过 ``sum(x) by k``。实现时**没有**引入 ``by`` ——
回填发生在「每条父行」的上下文里，按 join 锚点天然已经分好组了，
再写一次 ``by`` 是重复的。分组键就是关系的 join 锚点，无需声明。
"""

from __future__ import annotations

from typing import Any

from ..errors import ExprError, GenerateError
from ..expr import Expression, build_functions
from ..models import GeneratedRow, GroupSpec, SeedConfig, TableData
from ..rng import SeededRandom
from ..expr.aggregate_expr import AggregateExpression

__all__ = ["backfill_aggregates", "has_aggregate"]


def has_aggregate(config: SeedConfig) -> bool:
    """配置里是否存在需要 Phase 2 的 aggregate 组。"""
    return any(
        group.type == "aggregate" for table in config.tables for group in table.groups
    )


def backfill_aggregates(
    config: SeedConfig,
    tables: dict[str, TableData],
    funcs: dict | None = None,
) -> list[str]:
    """把所有 aggregate 组回填到父表。返回警告清单。"""
    funcs = funcs or build_functions(SeededRandom(config.seed))
    warnings: list[str] = []

    for table in config.tables:
        aggregates = [g for g in table.groups if g.type == "aggregate"]
        if not aggregates:
            continue

        parent_data = tables.get(table.name)
        if parent_data is None:  # pragma: no cover - 拓扑序保证不会发生
            raise GenerateError(f"回填 {table.name} 时找不到该表的生成结果", "aggregate")

        for group in aggregates:
            warnings.extend(
                _backfill_group(config, group, table.name, parent_data, tables, funcs)
            )

    return warnings


# ---------------------------------------------------------------- 内部实现


def _backfill_group(
    config: SeedConfig,
    group: GroupSpec,
    parent_name: str,
    parent_data: TableData,
    tables: dict[str, TableData],
    funcs: dict,
) -> list[str]:
    """回填一个 aggregate 组。"""
    warnings: list[str] = []
    path = f"tables[{parent_name}].groups({group.name})"

    if not group.expr:
        raise GenerateError(f"aggregate 组 {group.name} 缺少 expr", path)

    source_name = _resolve_source(config, group, parent_name, path)
    source_data = tables.get(source_name)
    if source_data is None:
        raise GenerateError(
            f"aggregate 组 {group.name} 引用了不存在的源表 {source_name}", path
        )

    join = _resolve_join(config, group, parent_name, source_name, path)

    # 按锚点建索引：父行 join 值 → 匹配的子行
    child_index = _index_by(source_data, [key.child_field for key in join])
    compiled = AggregateExpression(group.expr, funcs, path)

    missing = 0
    for row in parent_data.rows:
        key = tuple(row.values.get(k.parent_field) for k in join)
        child_rows = child_index.get(key, [])
        if not child_rows:
            missing += 1

        env: dict[str, Any] = dict(row.values)
        env["parent"] = row.values  # 自身即 parent 命名空间
        env["row"] = row.values
        env["seq"] = row.seq

        try:
            result = compiled.eval_over(child_rows, env)
        except ExprError as exc:
            raise GenerateError(
                f"aggregate 组 {group.name} 求值失败: {exc}", path
            ) from exc

        _assign(group, row, result, path)

    if missing:
        warnings.append(
            f"tables[{parent_name}].groups({group.name}): "
            f"有 {missing} 行父数据在子表 {source_name} 中没有匹配行，"
            "聚合结果按空集合计算（count/sum/avg 为 0，min/max 为 NULL）"
        )
    return warnings


def _assign(group: GroupSpec, row: GeneratedRow, result: Any, path: str) -> None:
    if len(group.fields) == 1:
        row.values[group.fields[0]] = result
        return
    if isinstance(result, (list, tuple)) and len(result) == len(group.fields):
        for field, value in zip(group.fields, result):
            row.values[field] = value
        return
    raise GenerateError(
        f"aggregate 组 {group.name} 有 {len(group.fields)} 个字段，"
        f"但表达式返回了单个值（请返回等长元组）",
        path,
    )


def _resolve_source(
    config: SeedConfig, group: GroupSpec, parent_name: str, path: str
) -> str:
    """确定源表：优先 ``from``，否则用唯一的子表。"""
    if group.from_:
        # 允许写 "t_detail" 或 "child" 这类可读标注
        return group.from_.split(".")[0]

    children = [r.child for r in config.relations if r.parent == parent_name]
    if not children:
        raise GenerateError(
            f"aggregate 组 {group.name} 所在表 {parent_name} 没有任何子表，"
            "无法汇总（请用 from 显式指定源表）",
            path,
        )
    if len(children) > 1:
        raise GenerateError(
            f"aggregate 组 {group.name} 未指定 from，而表 {parent_name} 有多个子表"
            f"（{', '.join(children)}）—— 请显式声明 from",
            path,
        )
    return children[0]


def _resolve_join(config: SeedConfig, group: GroupSpec, parent_name: str, source_name: str, path: str):
    """确定父子之间的锚点。"""
    for relation in config.relations:
        if relation.parent == parent_name and relation.child == source_name:
            if not relation.join:
                raise GenerateError(
                    f"aggregate 组 {group.name} 需要锚点："
                    f"关系 {parent_name} → {source_name} 未声明 join",
                    path,
                )
            return relation.join
    raise GenerateError(
        f"aggregate 组 {group.name} 的源表 {source_name} 与 {parent_name} 之间没有声明关系",
        path,
    )


def _index_by(data: TableData, fields: list[str]) -> dict[tuple, list[dict[str, Any]]]:
    """按字段值建倒排索引。"""
    index: dict[tuple, list[dict[str, Any]]] = {}
    for row in data.rows:
        key = tuple(row.values.get(f) for f in fields)
        index.setdefault(key, []).append(row.values)
    return index
