"""规模预演：不产出数据，先算清楚要造多少行。

`plan` 的价值在于「在做之前就知道规模」——
理论组合数、是否会触顶 max_rows、各表行数分别是多少。
"""

from __future__ import annotations

from ..models import PlanResult, SeedConfig, TablePlan
from ..engine.group_expander import combo_count, finite_groups


def plan_tables(config: SeedConfig) -> PlanResult:
    plans: list[TablePlan] = []
    warnings: list[str] = []
    total = 0

    for table in config.tables:
        finite = finite_groups(table)
        combos = combo_count(finite)
        cap = min(
            value
            for value in [config.limits.max_rows, table.rows]
            if value is not None
        )
        planned = combos if combos else 0
        note: str | None = None

        if planned > cap:
            note = f"组合数超过上限 {cap}，将截断为 {cap} 行"
            warnings.append(f"tables[{table.name}]: {note}")
            planned = cap

        if table.rows and combos > table.rows:
            note = note or f"配置指定 rows={table.rows}，将截断"

        plans.append(
            TablePlan(
                table=table.name,
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
        order=[t.name for t in config.tables],
        warnings=warnings,
        within_limits=within,
    )
