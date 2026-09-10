"""1:1 关系下的组合分配策略。

为什么需要它
------------
1:N 关系里，子表可以对自己的有限取值组做**笛卡尔积** —— 父行少、子行多，
放大出来的行数正好换来覆盖率。

1:1 关系不行：``每条父行恰好一条子行`` 是硬约束，笛卡尔积会直接撑破基数
（12 组合 × 12 父行 = 144 行 vs 应该 12 行）。此时只能在**固定行数内**争取覆盖，
这就是分配策略的活儿。

一句话：**笛卡尔积 = 放大行数换覆盖（1:N 用）；分配 = 固定行数内保覆盖（1:1 用）。**

follow_parent 的语义（用户拍板为默认）
-------------------------------------
不是简单轮转，而是**由父行的驱动字段值决定子行取哪个组合**：

- 驱动值**首次**出现 → 从组合池里按轮转取下一个（不同父值 → 不同组合，**保覆盖**）
- 驱动值**再次**出现 → 复用上次那个组合（同父值 → 同子值，**保一致**）

于是「父表 currency=CNY 的那些行，子表跟着也是 CNY 那一套取值」，真实感最强；
同时父值有多少种，就能覆盖到多少组合。

驱动字段的优先级：``relation.join`` 的父字段 → 传播规则的 ``from`` 源字段
→ 父行全部字段 → 都没有则退化为 round_robin。
"""

from __future__ import annotations

from typing import Any

from ..errors import GenerateError
from ..models import Allocation, GroupSpec, PropagateRule, RelationSpec
from ..rng import SeededRandom
from .group_expander import expand_skeleton

__all__ = ["ComboAllocator", "drive_fields_of"]


class ComboAllocator:
    """把子表的有限取值组合按策略分配给每条父行。"""

    def __init__(
        self,
        groups: list[GroupSpec],
        strategy: Allocation = "follow_parent",
        rng: SeededRandom | None = None,
        drive_fields: list[str] | None = None,
        path: str = "",
    ) -> None:
        self.combos: list[dict[str, Any]] = [dict(c) for c in expand_skeleton(groups)]
        self.strategy = strategy
        self.rng = rng
        self.drive_fields = list(drive_fields or [])
        self.path = path
        self._slot_of_key: dict[tuple, int] = {}
        self._next_slot = 0
        self._weights = _resolve_weights(groups)

    def __len__(self) -> int:
        return len(self.combos)

    def assign(self, parent_row: dict[str, Any] | None, seq: int) -> dict[str, Any]:
        """为第 ``seq`` 条子行取一个组合骨架。"""
        if not self.combos:
            return {}
        if self.strategy == "follow_parent":
            return self._follow_parent(parent_row, seq)
        if self.strategy == "round_robin":
            return dict(self.combos[seq % len(self.combos)])
        if self.strategy == "random":
            return self._pick_random(None)
        if self.strategy == "weighted":
            return self._pick_random(self._weights)
        raise GenerateError(f"未知的分配策略: {self.strategy}", self.path)

    # ------------------------------------------------------------ 内部实现

    def _follow_parent(self, parent_row: dict[str, Any] | None, seq: int) -> dict[str, Any]:
        key = self._drive_key(parent_row)
        if key is None:
            # 没有可用驱动键 —— 退化成轮转，至少保证覆盖
            return dict(self.combos[seq % len(self.combos)])

        if key not in self._slot_of_key:
            self._slot_of_key[key] = self._next_slot % len(self.combos)
            self._next_slot += 1
        return dict(self.combos[self._slot_of_key[key]])

    def _drive_key(self, parent_row: dict[str, Any] | None) -> tuple | None:
        if not parent_row or not self.drive_fields:
            return None
        values = tuple(parent_row.get(f) for f in self.drive_fields)
        if all(v is None for v in values):
            return None
        return values

    def _pick_random(self, weights: list[float] | None) -> dict[str, Any]:
        if self.rng is None:
            raise GenerateError(
                f"分配策略 {self.strategy} 需要随机源，但未传入 rng", self.path
            )
        index = self.rng.rand_choice(list(range(len(self.combos))), weights=weights)
        return dict(self.combos[index])


def drive_fields_of(relation: RelationSpec, rules: list[PropagateRule]) -> list[str]:
    """确定 follow_parent 的驱动字段（父行这些字段值相同 → 子行复用同一组合）。

    优先级：

    1. ``relation.drive_by`` —— 用户显式声明，最可控
    2. 被 ``copy`` / ``map`` 的父字段 —— 用户已认定「要对得上」的业务字段
    3. ``relation.join`` 的父字段 —— 兜底（多为唯一主键，此时退化为轮转）
    """
    if relation.drive_by:
        return list(relation.drive_by)

    effective = rules or list(relation.propagate)
    sources: list[str] = []
    for rule in effective:
        if rule.from_ and rule.mode in {"copy", "map"} and rule.from_ not in sources:
            sources.append(rule.from_)
    if sources:
        return sources

    return [key.parent_field for key in relation.join]


def _resolve_weights(groups: list[GroupSpec]) -> list[float] | None:
    """取第一个带 buckets 的有限组的权重；没有则 None（等概率）。"""
    for group in groups:
        if group.buckets and len(group.buckets) == len(group.values or []):
            return list(group.buckets)
    return None
