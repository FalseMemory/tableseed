"""多表编排 —— 按拓扑序把「生成」串成一条流水线。

核心约束：**父表先于子表**。子表要引用父表已生成的主键与业务字段，
所以先由 :func:`topology.topo_order` 排出顺序，再逐表生成，
把父表的 :class:`TableData` 喂给 :func:`table_gen.generate_child`。

两阶段生成
----------
Phase 1（本模块）：正向传播，父 → 子。
Phase 2（M3）：沿反向边回填 ``aggregate`` 组 —— 那时子表已生成完毕。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from ..errors import PlanError
from ..expr import build_functions
from ..models import GenerateResult, SeedConfig, TableData
from ..rng import SeededRandom
from .aggregator import backfill_aggregates
from .table_gen import generate_child, generate_table
from .topology import back_edges, topo_order

__all__ = ["generate_all"]

ProgressFn = Callable[[str, dict[str, Any]], None]


def generate_all(
    config: SeedConfig,
    rng: SeededRandom | None = None,
    progress: ProgressFn | None = None,
) -> GenerateResult:
    """按拓扑序生成全部表的数据。

    ``progress`` 用于 WebUI 的 SSE 推送，形如 ``progress("table_done", {...})``。
    """
    started = time.perf_counter()
    base_rng = rng or SeededRandom(config.seed)
    order = topo_order(config)
    _reject_multi_parent(config)

    tables: dict[str, TableData] = {}
    result_warnings: list[str] = []
    for name in order:
        table = config.table(name)
        # 每张表用独立子随机源 —— 某表行数变化不会污染其他表的随机序列
        table_rng = base_rng.fork(name)

        parents = [r for r in config.relations if r.child == name]
        if not parents:
            tables[name] = generate_table(config, table, table_rng)
        else:
            relation = parents[0]
            parent_data = tables.get(relation.parent)
            if parent_data is None:  # pragma: no cover - 拓扑序保证不会发生
                raise PlanError(
                    f"生成 {name} 时父表 {relation.parent} 尚未生成", "relations"
                )
            tables[name] = generate_child(config, table, relation, parent_data, table_rng)

        if progress:
            progress(
                "table_done",
                {
                    "table": name,
                    "rows": len(tables[name]),
                    "truncated": bool(getattr(tables[name], "truncated", False)),
                    "done": len(tables),
                    "total": len(order),
                },
            )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = GenerateResult(tables=tables, elapsed_ms=elapsed_ms, seed=config.seed)

    # ---- Phase 2：沿反向边把子表汇总回填到父表的 aggregate 组 ----
    if back_edges(config):
        warnings = backfill_aggregates(config, tables, build_functions(base_rng))
        for message in warnings:
            if progress:
                progress("warning", {"message": message})
            result_warnings.append(message)
        if progress:
            progress("phase2_done", {"tables": [p for _, p in back_edges(config)]})

    # ---- Phase 3：生成后自检 —— 配置里声明了 invariants 就逐条求值 ----
    # 违例不阻断结果（数据已生成），但会随结果返回、由 CLI / WebUI 醒目展示
    invariant_failures = []
    if config.invariants:
        from .invariants import verify_invariants  # noqa: PLC0415

        invariant_failures = verify_invariants(
            config, tables, build_functions(base_rng)
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return GenerateResult(
        tables=tables,
        elapsed_ms=elapsed_ms,
        seed=config.seed,
        warnings=result_warnings,
        invariant_failures=invariant_failures,
    )


def _reject_multi_parent(config: SeedConfig) -> None:
    """M2 只支持单父；多父（N:M 的雏形）留到 M4。"""
    counter: dict[str, list[str]] = {}
    for relation in config.relations:
        counter.setdefault(relation.child, []).append(relation.parent)

    for child, parents in counter.items():
        if len(parents) > 1:
            raise PlanError(
                f"表 {child} 有多个父表（{', '.join(parents)}）—— "
                "多父关系计划在 M4 支持，当前请用单条关系链表达",
                "relations",
            )
