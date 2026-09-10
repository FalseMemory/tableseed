"""表达式解析：MySQL 风格语法糖归一化 + Python AST 白名单校验。

设计要点（tech-design 第 6 节）：
- 不预编译、不用 ``eval``/``exec``，只做 AST 白名单求值，杜绝任意代码执行；
- 语法糖（``<>`` / ``=`` / ``LIKE`` / ``BETWEEN`` / ``IS NULL`` / ``IF(...)``）
  在解析前归一化为等价的 Python 表达式。
"""

from __future__ import annotations

import ast
import re

from ..errors import ExprError

#: 允许出现的 AST 节点
ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.And,
    ast.Or,
    ast.Not,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    # MySQL 风格语法糖解析后可能出现的函数节点（func 为 Name，无需额外类型）
)

#: 允许做属性访问的命名空间
NAMESPACES: frozenset[str] = frozenset({"row", "parent", "src"})

_STRING_RE = re.compile(r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')')
_PLACEHOLDER_RE = r"\x00\d+\x00"
_PLACEHOLDER_MATCH_RE = re.compile(r"\x00(\d+)\x00")
_IS_NOT_NULL_RE = re.compile(r"\b([A-Za-z_][\w.]*)\s+is\s+not\s+null\b", re.I)
_IS_NULL_RE = re.compile(r"\b([A-Za-z_][\w.]*)\s+is\s+null\b", re.I)
_BETWEEN_RE = re.compile(
    r"\b([A-Za-z_][\w.]*)\s+between\s+(\S+)\s+and\s+(\S+)", re.I
)
_LIKE_RE = re.compile(
    rf"\b([A-Za-z_][\w.]*)\s+like\s+({_PLACEHOLDER_RE})", re.I
)
_NEQ_RE = re.compile(r"<>")
_EQ_RE = re.compile(r"(?<![<>=!])=(?!=)")
_KEYWORD_RE = re.compile(r"\b(AND|OR|NOT|IN|IS|NULL|TRUE|FALSE)\b", re.I)
_IF_RE = re.compile(r"\bif\s*\(", re.I)


def normalize(src: str) -> str:
    """把 MySQL 风格的写法归一化为等价的 Python 表达式。

    字符串字面量先替换为占位符再做语法转换，最后还原 —— 否则
    ``name LIKE "T%"`` 这类「跨字符串」的语法糖无法被整体匹配。
    """
    if not src or not src.strip():
        raise ExprError("表达式为空")

    literals: list[str] = []

    def stash(match: re.Match) -> str:
        literals.append(match.group(0))
        return f"\x00{len(literals) - 1}\x00"

    text = _STRING_RE.sub(stash, src)

    text = _IS_NOT_NULL_RE.sub(r"\1 is not None", text)
    text = _IS_NULL_RE.sub(r"\1 is None", text)
    text = _BETWEEN_RE.sub(r"between(\1, \2, \3)", text)
    text = _LIKE_RE.sub(r"like(\1, \2)", text)
    text = _NEQ_RE.sub("!=", text)
    text = _EQ_RE.sub("==", text)
    text = _KEYWORD_RE.sub(lambda m: m.group(1).lower(), text)
    text = _IF_RE.sub("if_(", text)

    return _PLACEHOLDER_MATCH_RE.sub(lambda m: literals[int(m.group(1))], text)


def compile_expr(src: str, path: str | None = None) -> ast.Expression:
    """归一化 + 解析 + 白名单校验，返回可直接求值的 AST。"""
    normalized = normalize(src)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ExprError(
            f"表达式语法错误: {src!r} → {normalized!r}（{exc.msg}）", path
        ) from exc

    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ExprError(
                f"表达式中不允许使用 {type(node).__name__}: {src!r}", path
            )
        if isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id in NAMESPACES):
                raise ExprError(
                    f"只允许访问 {' / '.join(sorted(NAMESPACES))} 命名空间下的字段: {src!r}",
                    path,
                )
    return tree
