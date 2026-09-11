"""生成引擎。"""

from .allocator import ComboAllocator, drive_fields_of
from .group_expander import (
    attach_groups,
    combo_count,
    expand_skeleton,
    finite_groups,
)
from .invariants import verify_invariants
from .multi import generate_all
from .propagator import apply_propagate, build_env, effective_rules
from .table_gen import generate_child, generate_table
from .topology import back_edges, topo_order

__all__ = [
    "ComboAllocator",
    "apply_propagate",
    "attach_groups",
    "back_edges",
    "build_env",
    "combo_count",
    "drive_fields_of",
    "effective_rules",
    "expand_skeleton",
    "finite_groups",
    "generate_all",
    "generate_child",
    "generate_table",
    "topo_order",
    "verify_invariants",
]
