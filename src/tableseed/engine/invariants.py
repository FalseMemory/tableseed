"""不变量校验 —— 把「对得上」从生成时的保证升级为可证明的断言。

传播规则只负责**造**出一致的数据；invariant 负责**证明**它是一致的。
生成结束后逐条求值，任何一行不满足都算违例，并列出违例行。

两种形态（与 ``models.InvariantSpec`` 对应）：

- **行级**：``fee <= amount`` —— 在每行上下文里求值，结果必须为真
- **跨表**：``sum(net_amount) = amount - fee`` —— 对每条父行取其匹配子行集合，
  聚合求值（复用 aggregate 的求值器），结果必须为真

为什么独立于 ``gen``：生成与验证是两件事 ——
造数追求覆盖率，验证追求正确性；``verify`` 子命令单独跑，
M4 的「生成后自检」再把两者串起来。
"""

from __future__ import annotations

from typing import Any

from ..errors import ExprError, GenerateError
from ..expr import build_functions
from ..expr.aggregate_expr import AggregateExpression
from ..models import InvariantFailure, InvariantSpec, SeedConfig, TableData
from ..rng import SeededRandom

__all__ = ["verify_invariants"]


def verify_invariants(
    config: SeedConfig,
    tables: dict[str, TableData],
    funcs: dict | None = None,
) -> list[InvariantFailure]:
    """对生成结果逐条求值不变量，返回违例清单（空 = 全部通过）。"""
    funcs = funcs or build_functions(SeededRandom(config.seed))
    failures: list[InvariantFailure] = []

    for index, invariant in enumerate(config.invariants):
        targets = (
            [config.table(invariant.table)] if invariant.table else list(config.tables)
        )

        for table in targets:
            data = tables.get(table.name)
            if data is None:  # pragma: no cover - 生成结果总是含全部表
                continue

            child_index = (
                _index_children(config, invariant, table.name, tables)
                if invariant.from_
                else None
            )

            for row in data.rows:
                try:
                    if child_index is not None:
                        ok = _eval_cross_table(invariant, row, child_index, funcs, index)
                    else:
                        ok = _eval_row_level(invariant, row, funcs, index)
                except ExprError as exc:
                    # 带上**表名**与可操作建议：不变量不写 table 时会对所有表求值，
                    # 其中某张表没有该字段就报"未知变量 x" —— 用户看不出是哪张表
                    hint = ""
                    if invariant.table is None:
                        hint = (
                            f"（未指定 table 时会对所有表求值，"
                            f"表 {table.name} 里没有该字段 —— "
                            "若只想校验部分表，请显式写 table: <表名>）"
                        )
                    raise GenerateError(
                        f"invariants[{index}] 在表 {table.name} 上求值失败: {exc}{hint}",
                        f"invariants[{index}]",
                    ) from exc

                if not ok:
                    failures.append(
                        InvariantFailure(
                            index=index,
                            expr=invariant.expr,
                            table=table.name,
                            seq=row.seq,
                            row=row.values,
                        )
                    )

    return failures


# ---------------------------------------------------------------- 内部实现


def _eval_row_level(
    invariant: InvariantSpec, row, funcs: dict, index: int
) -> bool:
    from ..expr import Expression  # noqa: PLC0415

    path = f"invariants[{index}]"
    expression = Expression(invariant.expr, funcs, path)
    env: dict[str, Any] = dict(row.values)
    env["seq"] = row.seq
    env["row"] = row.values
    env["parent"] = row.values  # 行级上下文里 parent 即本行，命名空间保持一致
    return bool(expression(env))


def _eval_cross_table(
    invariant: InvariantSpec,
    row,
    child_index: dict[str, Any],
    funcs: dict,
    index: int,
) -> bool:
    path = f"invariants[{index}]"
    compiled = AggregateExpression(invariant.expr, funcs, path)

    env: dict[str, Any] = dict(row.values)
    env["parent"] = row.values
    env["row"] = row.values
    env["seq"] = row.seq

    key = tuple(row.values.get(f) for f in child_index["parent_fields"])
    child_rows = child_index["rows"].get(key, [])
    return bool(compiled.eval_over(child_rows, env))


def _index_children(
    config: SeedConfig,
    invariant: InvariantSpec,
    parent_name: str,
    tables: dict[str, TableData],
) -> dict[str, Any]:
    """按 join 锚点把子表行建成倒排索引：父行锚点值 → 匹配的子行列表。"""
    source_name = invariant.from_.split(".")[0]

    join = None
    for relation in config.relations:
        if relation.parent == parent_name and relation.child == source_name:
            join = relation.join
            break

    if not join:
        raise GenerateError(
            f"invariant 引用的源表 {source_name} 与 {parent_name} 之间"
            "没有声明带 join 的关系",
            f"invariants({invariant.expr})",
        )

    data = tables.get(source_name)
    if data is None:
        raise GenerateError(f"不变量校验时找不到子表 {source_name} 的数据", "invariants")

    # 父行按 parent_field 取键，子行按 child_field 建索引 —— 两边字段名不同
    parent_fields = [key.parent_field for key in join]
    child_fields = [key.child_field for key in join]
    index: dict[tuple, list[dict[str, Any]]] = {}
    for row in data.rows:
        key = tuple(row.values.get(f) for f in child_fields)
        index.setdefault(key, []).append(row.values)

    return {"parent_fields": parent_fields, "rows": index}
