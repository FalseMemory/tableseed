"""规模预演：不产出数据，先算清楚要造多少行。

`plan` 的价值在于「在做之前就知道规模」——
理论组合数、是否会触顶 max_rows、各表行数分别是多少。

M2 起关系感知：子表行数不再由自己的组合数单独决定，而是**挂在父表上**：

- ``1:1`` / ``1:0..1``：子表行数 = 父表行数（有限组改用分配，不放大）
- ``1:N``：子表行数 = 父表行数 × 子表组合数（笛卡尔积放大）
"""

from __future__ import annotations

from ..engine.coverage import pairwise_skeletons
from ..engine.group_expander import combo_count, finite_groups
from ..engine.topology import topo_order
from ..models import PlanResult, SeedConfig, TablePlan


def plan_tables(config: SeedConfig) -> PlanResult:
    plans: list[TablePlan] = []
    warnings: list[str] = []
    total = 0

    order = topo_order(config)
    parent_of = {r.child: r for r in config.relations}
    planned_rows: dict[str, int] = {}
    strategy = config.limits.strategy

    for name in order:
        table = config.table(name)
        finite = finite_groups(table)
        combos = combo_count(finite)
        relation = parent_of.get(name)
        note: str | None = None

        # ---- 策略影响的预估基数（根表 / 1:N 的每父行展开数）----
        if strategy == "sample":
            base = min(config.limits.sample_size or 10, combos) if combos else 0
            if combos > base:
                note = f"sample 策略：{combos} 个组合随机抽 {base} 行"
        elif strategy == "pairwise" and combos:
            estimate = sum(
                1 for _ in pairwise_skeletons(finite, _plan_rng(config), combos)
            )
            base = min(estimate, combos)
            if base < combos:
                note = (
                    f"pairwise 策略：{combos} 个组合预计精简为约 {base} 行"
                    "（两两配对全覆盖，实际行数以生成为准）"
                )
        else:
            base = combos

        if relation is None:
            # ---- 根表：组合数就是行数 ----
            planned = _cap(config, table, base, name, warnings)
        elif relation.cardinality in {"1:1", "1:0..1"}:
            # ---- 1:1：行数跟随父表，有限组只做分配 ----
            parent_rows = planned_rows.get(relation.parent, 0)
            planned = min(parent_rows, config.limits.max_rows)
            if combos > parent_rows:
                note = (
                    f"1:1 关系下有 {combos} 个组合但父表仅 {parent_rows} 行，"
                    f"只能覆盖其中 {min(combos, parent_rows)} 个（改用 1:N 可全覆盖）"
                )
                warnings.append(f"tables[{name}]: {note}")
            elif combos and combos < parent_rows:
                note = note or f"1:1 分配：{combos} 个组合在 {parent_rows} 行内轮转复用"
        else:
            # ---- 1:N：父表每行展开一遍 ----
            parent_rows = planned_rows.get(relation.parent, 0)
            planned = _cap(config, table, parent_rows * max(base, 1), name, warnings)
            if combos:
                note = note or f"1:N 展开：父表 {parent_rows} 行 × {combos} 组合"

        planned_rows[name] = planned
        plans.append(
            TablePlan(
                table=name,
                finite_groups=[g.name for g in finite],
                combo_count=combos,
                planned_rows=planned,
                note=note,
            )
        )
        total += planned

    within = total <= config.limits.max_rows and not warnings

    return PlanResult(
        tables=plans,
        total_rows=total,
        order=order,
        warnings=warnings,
        within_limits=within,
    )


def _plan_rng(config: SeedConfig):
    """plan 阶段 pairwise 预估用的随机源 —— 独立派生，不影响生成序列。"""
    from ..rng import SeededRandom  # noqa: PLC0415

    return SeededRandom(config.seed).fork(f"plan:{config.limits.strategy}")


def _cap(config: SeedConfig, table, planned: int, name: str, warnings: list[str]) -> int:
    """套用 max_rows / table.rows 上限，超限则记警告。"""
    limit = config.limits.max_rows
    if table.rows:
        limit = min(limit, table.rows)

    if planned > limit:
        warnings.append(
            f"tables[{name}]: 预估 {planned} 行超过上限 {limit}，将截断"
        )
        return limit
    return planned
