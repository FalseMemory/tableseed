"""表间依赖的拓扑排序。

生成顺序必须满足：**父表先于子表** —— 子表要引用父表已生成的主键与字段。

关于反向边（back edge）：
    ``aggregate`` 组是「子表汇总回填父表」（如交易表的 total_amount 由明细汇总），
    它构成一条**子 → 父**的反向边，与正向传播边天然成环。
    解法是**两阶段**：Phase 1 只按正向边排序生成，Phase 2 再沿反向边回填。
    因此本模块把反向边单独摘出来返回，不参与排序（Phase 2 属 M3）。
"""

from __future__ import annotations

from ..errors import PlanError
from ..models import SeedConfig

__all__ = ["back_edges", "topo_order"]


def topo_order(config: SeedConfig) -> list[str]:
    """返回父先于子的生成顺序；存在环时抛 :class:`PlanError`。

    只有**正向传播边**参与排序（即 ``relations`` 中的 parent → child）。
    """
    names = [t.name for t in config.tables]
    known = set(names)

    children: dict[str, list[str]] = {name: [] for name in names}
    indegree: dict[str, int] = {name: 0 for name in names}

    for relation in config.relations:
        if relation.parent not in known or relation.child not in known:
            continue  # 表不存在由 check_config 负责报错
        children[relation.parent].append(relation.child)
        indegree[relation.child] += 1

    # Kahn 算法；每轮取字典序最小者，保证同一配置永远得到同一顺序（可复现）
    ready = sorted(name for name in names if indegree[name] == 0)
    order: list[str] = []

    while ready:
        name = ready.pop(0)
        order.append(name)
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(names):
        stuck = sorted(set(names) - set(order))
        cycle = _render_cycle(config, stuck)
        raise PlanError(
            f"表之间存在循环依赖，无法确定生成顺序: {', '.join(stuck)}。{cycle}"
            "若是为了做汇总回填，请改用 aggregate 组（两阶段生成），不要用关系环。",
            "relations",
        )

    return order


def back_edges(config: SeedConfig) -> list[tuple[str, str]]:
    """返回汇总回填所需的反向边 ``(子表, 父表)`` 列表（M3 使用）。

    判定：父表中存在 ``aggregate`` 组，且两表之间已声明关系。
    """
    edges: list[tuple[str, str]] = []
    for relation in config.relations:
        parent = _find_table(config, relation.parent)
        if parent is None:
            continue
        if any(g.type == "aggregate" for g in parent.groups):
            edges.append((relation.child, relation.parent))
    return edges


# ---------------------------------------------------------------- 内部实现


def _find_table(config: SeedConfig, name: str):
    for table in config.tables:
        if table.name == name:
            return table
    return None


def _render_cycle(config: SeedConfig, stuck: list[str]) -> str:
    """尽量把环打印成人能读懂的样子，便于改配置。"""
    stuck_set = set(stuck)
    pairs = [
        f"{r.parent} → {r.child}"
        for r in config.relations
        if r.parent in stuck_set and r.child in stuck_set
    ]
    return f"可疑的环: {' → '.join(pairs)}。" if pairs else ""
