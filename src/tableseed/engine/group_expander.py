"""有限取值组的笛卡尔积展开。

模型一定义：``enum`` / ``boundary`` / ``dict`` 三类组有确定取值集合，
彼此之间做笛卡尔积，每个组合生成一条数据 —— 覆盖因此是完备且可计算的。
"""

from __future__ import annotations

from itertools import product
from typing import Any, Iterator

from ..models import FINITE_GROUP_TYPES, GroupSpec, TableSpec


def finite_groups(table: TableSpec) -> list[GroupSpec]:
    """取出参与笛卡尔积的组，保持声明顺序（顺序影响展开稳定性）。"""
    return [g for g in table.groups if g.type in FINITE_GROUP_TYPES]


def attach_groups(table: TableSpec) -> list[GroupSpec]:
    """取出逐行附着的组（含 derive，但排除 Phase 2 的 aggregate）。

    derive 排在最后 —— 它需要引用其他组已生成的值。
    """
    others = [
        g
        for g in table.groups
        if g.type not in FINITE_GROUP_TYPES and g.type != "aggregate"
    ]
    return sorted(others, key=lambda g: g.type == "derive")


def combo_count(groups: list[GroupSpec]) -> int:
    """理论组合数 = 各有限取值组取值集大小的乘积。"""
    total = 1
    for group in groups:
        total *= len(group.values or [])
    return total


def expand_skeleton(groups: list[GroupSpec]) -> Iterator[dict[str, Any]]:
    """展开骨架行。

    每个组合被摊平成 ``{字段名: 值}``；组内多字段因此天然同步 ——
    这是分组模型相对字段级造数最关键的收益。
    """
    if not groups:
        yield {}
        return

    value_sets: list[list[Any]] = []
    for group in groups:
        values = group.values or []
        if not values:
            return
        value_sets.append(values)

    for combo in product(*value_sets):
        row: dict[str, Any] = {}
        for group, tuple_value in zip(groups, combo):
            for field, value in zip(group.fields, tuple_value):
                row[field] = value
        yield row
