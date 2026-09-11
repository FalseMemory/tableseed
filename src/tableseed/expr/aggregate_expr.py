"""聚合表达式：在**一组行**上求值，而非单行。

普通表达式 ``amount * 0.01`` 在单行上下文里求值；
聚合表达式 ``sum(amount)`` 必须先把 ``amount`` 在**每一行**上算出来，
得到一个值列表，再做聚合。这两者的求值顺序完全不同，
所以需要一个专门的求值器，而不是给函数表塞几个函数了事。

做法：覆盖 ``_eval_Call`` —— 遇到聚合函数时，不按"先算参数再调用"的常规顺序，
而是把参数 AST 拿到每一行上去求值，收成列表后再交给聚合函数。
嵌套（``sum(amount)`` / ``count()``）自然成立，因为除法仍走常规求值。
"""

from __future__ import annotations

import ast
from typing import Any

from ..errors import ExprError
from ..expr.evaluator import Evaluator
from ..expr.parser import compile_expr

#: 聚合函数名 → 计算函数（输入为逐行求值得到的值列表）
AGGREGATE_FUNCTIONS = (
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "count_distinct",
)


def _to_num(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ExprError(f"无法参与数值聚合: {value!r}") from exc


def _agg_count(values: list[Any]) -> int:
    return len([v for v in values if v is not None])


def _agg_sum(values: list[Any]) -> float:
    return sum(_to_num(v) for v in values)


def _agg_avg(values: list[Any]) -> float:
    if not values:
        return 0
    return _agg_sum(values) / len(values)


def _agg_min(values: list[Any]) -> Any:
    cleaned = [v for v in values if v is not None]
    return min(cleaned) if cleaned else None


def _agg_max(values: list[Any]) -> Any:
    cleaned = [v for v in values if v is not None]
    return max(cleaned) if cleaned else None


def _agg_count_distinct(values: list[Any]) -> int:
    return len({v for v in values if v is not None})


_AGG_IMPL = {
    "count": _agg_count,
    "sum": _agg_sum,
    "avg": _agg_avg,
    "min": _agg_min,
    "max": _agg_max,
    "count_distinct": _agg_count_distinct,
}


class _AggregateEvaluator(Evaluator):
    """在多行上下文里求值：聚合函数的参数逐行求值。"""

    def __init__(self, functions: dict, rows: list[dict[str, Any]], base_env: dict) -> None:
        super().__init__(functions)
        self.rows = rows
        self.base_env = base_env

    def _eval_Call(self, node: ast.Call, env: dict[str, Any]) -> Any:
        name = node.func.id if isinstance(node.func, ast.Name) else None

        if name not in AGGREGATE_FUNCTIONS:
            return super()._eval_Call(node, env)

        # count() 无参特判：行数
        if not node.args:
            if name != "count":
                raise ExprError(f"{name}() 需要参数，例如 {name}(amount)")
            return len(self.rows)

        values: list[Any] = []
        for row in self.rows:
            row_env = dict(self.base_env)
            row_env.update(row)
            row_env["row"] = row
            row_env["child"] = row  # 子行语义别名，读起来更清楚
            values.append(self._eval(node.args[0], row_env))

        return _AGG_IMPL[name](values)


class AggregateExpression:
    """编译一次，在多组行上反复求值。"""

    __slots__ = ("source", "_tree", "_functions", "_path")

    def __init__(self, source: str, functions: dict, path: str | None = None) -> None:
        self.source = source
        self._functions = functions
        self._path = path
        self._tree = compile_expr(source, path)
        self._validate()

    def _validate(self) -> None:
        """编译期校验函数名，早失败（NFR-6）。"""
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name is None or (
                    name not in self._functions and name not in AGGREGATE_FUNCTIONS
                ):
                    raise ExprError(f"未注册的函数: {name or '未知'}", self._path)

    def eval_over(self, rows: list[dict[str, Any]], base_env: dict[str, Any] | None = None) -> Any:
        """在 ``rows`` 这批行上求值。"""
        evaluator = _AggregateEvaluator(self._functions, rows, base_env or {})
        return evaluator.eval(self._tree, evaluator.base_env)

    @property
    def names(self) -> set[str]:
        return {node.id for node in ast.walk(self._tree) if isinstance(node, ast.Name)}

    def __call__(self, rows: list[dict[str, Any]], base_env: dict[str, Any] | None = None) -> Any:
        return self.eval_over(rows, base_env)
