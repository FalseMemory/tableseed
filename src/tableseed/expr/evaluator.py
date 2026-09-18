"""表达式求值器（沙箱内）。

只处理 parser 白名单内的节点；函数调用必须在传入的函数表里，
否则直接抛 ExprError —— 不依赖任何反射或动态导入。
"""

from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation
from typing import Any

from ..errors import ExprError
from .functions import FunctionMap
from .parser import compile_expr


def _is_number(value: Any) -> bool:
    """数值（排除 bool —— Python 里 bool 是 int 的子类，但业务上不是数值）。"""
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _as_decimal(value: Any) -> Decimal:
    """数值 → Decimal，走 ``str()`` 避开 float 的二进制误差。"""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExprError(f"无法解析为数值: {value!r}") from exc


class AttrDict(dict):
    """支持点访问的字典，用于 row / parent / src 命名空间。"""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise ExprError(f"字段不存在: {key}") from exc


def to_namespace(value: Any) -> AttrDict:
    if value is None:
        return AttrDict()
    if isinstance(value, AttrDict):
        return value
    if isinstance(value, dict):
        return AttrDict(value)
    raise ExprError(f"命名空间必须是字典，收到 {type(value).__name__}")


class Evaluator:
    """把 AST 求值成具体值。"""

    def __init__(self, functions: FunctionMap) -> None:
        self.functions = functions

    def eval(self, tree: ast.Expression, variables: dict[str, Any]) -> Any:
        return self._eval(tree.body, variables)

    # ---------------------------------------------------------------- 内部

    def _eval(self, node: ast.AST, env: dict[str, Any]) -> Any:
        method = getattr(self, f"_eval_{type(node).__name__}", None)
        if method is None:
            raise ExprError(f"不支持的表达式节点: {type(node).__name__}")
        return method(node, env)

    def _eval_Constant(self, node: ast.Constant, env: dict[str, Any]) -> Any:
        return node.value

    def _eval_Name(self, node: ast.Name, env: dict[str, Any]) -> Any:
        if node.id not in env:
            raise ExprError(f"未知变量或字段: {node.id}")
        return env[node.id]

    def _eval_Attribute(self, node: ast.Attribute, env: dict[str, Any]) -> Any:
        base = node.value
        if not isinstance(base, ast.Name):
            raise ExprError("属性访问只支持一级")
        if base.id not in env:
            raise ExprError(f"未知命名空间: {base.id}")
        return getattr(to_namespace(env[base.id]), node.attr)

    def _eval_Tuple(self, node: ast.Tuple, env: dict[str, Any]) -> tuple:
        return tuple(self._eval(e, env) for e in node.elts)

    def _eval_List(self, node: ast.List, env: dict[str, Any]) -> list:
        return [self._eval(e, env) for e in node.elts]

    def _eval_BoolOp(self, node: ast.BoolOp, env: dict[str, Any]) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self._eval(value, env)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for value in node.values:
                result = self._eval(value, env)
                if result:
                    return result
            return result
        raise ExprError("不支持的布尔运算")

    def _eval_UnaryOp(self, node: ast.UnaryOp, env: dict[str, Any]) -> Any:
        operand = self._eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ExprError("不支持的一元运算")

    def _eval_BinOp(self, node: ast.BinOp, env: dict[str, Any]) -> Any:
        left = self._eval(node.left, env)
        right = self._eval(node.right, env)
        op = node.op

        # 只有**其中一边已是 Decimal**时才统一为 Decimal（聚合结果参与算术的场景，
        # 例如 sum(x) * 1.1 —— 否则 Decimal × float 会抛 TypeError）。
        # 两边都是 float 时保持原样：不改变生成数据的值类型，
        # 用户拿到的字段值该是 float 就还是 float。
        if (_is_number(left) and _is_number(right)
                and (isinstance(left, Decimal) or isinstance(right, Decimal))):
            left, right = _as_decimal(left), _as_decimal(right)

        # ---- 规模防护：手滑写出的超大数/超长字符串会让进程卡死甚至吃爆内存 ----
        # 例：`10**10**10` 会让服务 CPU 打满、整个页面无响应（用户只能杀进程）。
        if isinstance(op, ast.Pow) and isinstance(right, (int, float, Decimal)) and abs(right) > 1000:
            raise ExprError(f"幂运算指数过大（{right}）—— 会产生超大数，请检查表达式")
        if isinstance(op, ast.Pow) and _is_number(left) and isinstance(right, (int, Decimal)):
            digits = len(str(abs(int(left)))) if left else 1
            if digits * int(right) > 10_000:
                raise ExprError("幂运算结果过大 —— 请检查表达式")
        if isinstance(op, ast.Mult):
            for text, times in ((left, right), (right, left)):
                if isinstance(text, str) and isinstance(times, int) and len(text) * times > 1_000_000:
                    raise ExprError(
                        f"字符串重复次数过大（{len(text)} × {times}）—— 请检查表达式"
                    )
        try:
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow):
                return left**right
        except TypeError as exc:
            # 给出**可操作**的提示：最常见的原因是字段值是字符串（enum/const
            # 组里的 "100"），而表达式拿它做算术。
            hint = ""
            for value in (left, right):
                if isinstance(value, str):
                    try:
                        Decimal(value)
                    except (InvalidOperation, ValueError):
                        continue
                    hint = (
                        f"（{value!r} 是字符串 —— 需要算术时请把该字段的取值改成数值，"
                        "或用 cast_num(字段名) 转换）"
                    )
                    break
            raise ExprError(f"类型不匹配: {left!r} 与 {right!r}{hint}") from exc
        except ZeroDivisionError as exc:
            # 除零 / 取模零：必须包成可读错误，
            # 否则 ZeroDivisionError 会一路逃到 Web 层变成 500 内部错误
            raise ExprError(f"除以零: {left!r} 与 {right!r}") from exc
        except OverflowError as exc:
            raise ExprError(f"数值溢出: {left!r} 与 {right!r}") from exc
        except ValueError as exc:
            raise ExprError(f"数值无效: {left!r} 与 {right!r}（{exc}）") from exc
        raise ExprError("不支持的二元运算")

    def _eval_Compare(self, node: ast.Compare, env: dict[str, Any]) -> bool:
        left = self._eval(node.left, env)
        for op, comparator in zip(node.ops, node.comparators):
            right = self._eval(comparator, env)
            if not self._compare(op, left, right):
                return False
            left = right
        return True

    @staticmethod
    def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
        if isinstance(op, ast.Is):
            return left is right
        if isinstance(op, ast.IsNot):
            return left is not right

        # 比较时若有一边是 Decimal（聚合结果），另一边转 Decimal 再比 ——
        # 否则 `sum(x) = amount` 会被 float 累加误差误判（6805.01+646.27+378.83
        # = 7830.110000000001 ≠ 7830.11），用户的业务对账断言全是假的"违例"。
        # 两边都是 float 时按原样比较，不改变既有语义。
        if isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            if (_is_number(left) and _is_number(right)
                    and (isinstance(left, Decimal) or isinstance(right, Decimal))):
                left, right = _as_decimal(left), _as_decimal(right)

        if isinstance(op, ast.Eq):
            return left == right
        if isinstance(op, ast.NotEq):
            return left != right
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.In):
            return left in right
        if isinstance(op, ast.NotIn):
            return left not in right
        raise ExprError("不支持的比较运算")

    def _eval_Call(self, node: ast.Call, env: dict[str, Any]) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ExprError("只允许调用内置函数")
        name = node.func.id
        if name not in self.functions:
            raise ExprError(f"未注册的函数: {name}")
        args = [self._eval(a, env) for a in node.args]
        kwargs = {kw.arg: self._eval(kw.value, env) for kw in node.keywords}
        try:
            return self.functions[name](*args, **kwargs)
        except ExprError:
            raise
        except Exception as exc:  # 函数内部错误统一包成 ExprError
            raise ExprError(f"函数 {name} 执行失败: {exc}") from exc


class Expression:
    """编译一次、多次求值的表达式对象。"""

    __slots__ = ("source", "_tree", "_evaluator", "_path")

    def __init__(self, source: str, functions: FunctionMap, path: str | None = None) -> None:
        self.source = source
        self._path = path
        self._tree = compile_expr(source, path)
        self._evaluator = Evaluator(functions)
        self._validate_calls(functions)

    def _validate_calls(self, functions: FunctionMap) -> None:
        """编译期就校验函数名 —— 早失败胜过运行时才发现（NFR-6）。"""
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name is None or name not in functions:
                    raise ExprError(f"未注册的函数: {name or '未知'}", self._path)

    def __call__(self, variables: dict[str, Any]) -> Any:
        return self._evaluator.eval(self._tree, variables)

    @property
    def names(self) -> set[str]:
        """表达式中引用的变量名（用于依赖分析与校验）。"""
        names: set[str] = set()
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
        return names
