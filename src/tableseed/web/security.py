"""SQL 查询台的安全网关（PRD FR-12.2 / NFR-7）。

默认只读：仅放行 SELECT / WITH / SHOW / EXPLAIN / DESC。
M1 用「去注释去字符串后取首个关键字」的判定方式（零依赖）；
M2 将引入 sqlglot 做 AST 级判定，防住注释绕过与多语句拼接。
"""

from __future__ import annotations

import re

#: 允许的只读语句首关键字
READONLY_KEYWORDS: frozenset[str] = frozenset(
    {"select", "with", "show", "explain", "desc", "describe"}
)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


class SqlRejected(Exception):
    """语句被安全网关拒绝。"""


def strip_noise(statement: str) -> str:
    """去掉注释与字符串字面量，只保留语句骨架，避免注释绕过。"""
    text = _BLOCK_COMMENT_RE.sub(" ", statement)
    text = _LINE_COMMENT_RE.sub(" ", text)
    text = _STRING_RE.sub("''", text)
    return text


def first_keyword(statement: str) -> str:
    skeleton = strip_noise(statement).strip()
    if not skeleton:
        return ""
    return re.split(r"[\s(]+", skeleton, maxsplit=1)[0].lower()


def validate_readonly(statement: str, allow_write: bool = False) -> str:
    """校验语句；通过则返回语句本身，否则抛 SqlRejected。

    ``allow_write=True`` 时放行写语句 —— 但 WebUI 仍需页面二次确认（FR-12.6）。
    """
    if not statement or not statement.strip():
        raise SqlRejected("SQL 语句为空")

    skeleton = strip_noise(statement).strip()
    # 去掉结尾分号后，若仍存在分号则说明是多语句拼接
    if ";" in skeleton.rstrip().rstrip(";"):
        raise SqlRejected("只允许执行单条语句（检测到多条语句拼接）")

    keyword = first_keyword(statement)
    if keyword in READONLY_KEYWORDS:
        return statement

    if allow_write:
        return statement

    raise SqlRejected(
        f"只读模式不允许执行 {keyword.upper() or '该'} 语句，"
        f"仅支持 {' / '.join(sorted(k.upper() for k in READONLY_KEYWORDS))}。"
        "如确需写操作，请在配置中开启 sql.allow_write 并在页面上二次确认。"
    )
