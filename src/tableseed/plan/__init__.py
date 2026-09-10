"""规模预演与依赖分析。

M1 只做规模预演；表间依赖拓扑排序见 M2（graph.py / topo.py）。
"""

from .sizing import plan_tables

__all__ = ["plan_tables"]
