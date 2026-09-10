"""表达式引擎（沙箱）。

对外只暴露 Expression（编译好的表达式）与 build_functions（函数表）。
"""

from .evaluator import AttrDict, Evaluator, Expression, to_namespace
from .functions import FunctionMap, build_functions
from .parser import compile_expr, normalize

__all__ = [
    "AttrDict",
    "Evaluator",
    "Expression",
    "FunctionMap",
    "build_functions",
    "compile_expr",
    "normalize",
    "to_namespace",
]
