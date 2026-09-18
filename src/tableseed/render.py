"""SQL / CSV 文本渲染（纯函数，无副作用）。

与落盘解耦：CLI、WebUI、TableData 导出方法都复用这里的实现。
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .errors import ConfigError
from .models import TableData

_IDENT_QUOTE: dict[str, str] = {
    "postgresql": '"',
    "postgres": '"',
    "oracle": '"',
    "sqlite": '"',
    "mysql": "`",
    "mariadb": "`",
}

_DIALECT_ALIASES: dict[str, str] = {
    "postgres": "postgresql",
    "pg": "postgresql",
    "mariadb": "mysql",
}


#: 支持的方言（其他一律拒绝 —— 静默回退到双引号会让生成的 SQL 用错引用符，
#: 到目标库执行才报语法错，排查成本很高）
_DIALECTS = ("mysql", "postgresql", "oracle")


def normalize_dialect(dialect: str | None) -> str:
    """规范化方言名；未知方言抛可读错误。

    曾经是 ``_ALIASES.get(key, key)`` —— 未知值原样返回，渲染时静默用双引号兜底。
    用户把 ``postgresql`` 打成 ``postgresql8`` 之类，会拿到一份看似正常、
    实际用错标识符引用符的 SQL（MySQL 上 ``"col"`` 会被当字符串）。
    """
    key = (dialect or "postgresql").lower().strip()
    key = _DIALECT_ALIASES.get(key, key)
    if key not in _DIALECTS:
        raise ConfigError(
            f"不支持的方言: {dialect!r} —— 可用: {' / '.join(_DIALECTS)}"
            f"（也接受别名: {', '.join(sorted(_DIALECT_ALIASES))}）"
        )
    return key


def quote_ident(identifier: str, dialect: str) -> str:
    quote = _IDENT_QUOTE.get(normalize_dialect(dialect), '"')
    escaped = identifier.replace(quote, quote * 2)
    return f"{quote}{escaped}{quote}"


def render_literal(value: Any, dialect: str) -> str:
    """把 Python 值渲染成目标方言的 SQL 字面量。"""
    dialect = normalize_dialect(dialect)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if dialect == "postgresql":
            return "TRUE" if value else "FALSE"
        return "1" if value else "0"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    text = str(value).replace("'", "''")
    return f"'{text}'"


def render_sql(
    data: TableData,
    dialect: str = "postgresql",
    batch_size: int = 1000,
    quote: bool = True,
) -> str:
    """渲染批量 INSERT 语句。"""
    if not data.rows:
        return f"-- {data.table}: 无数据\n"

    dialect = normalize_dialect(dialect)
    table_name = quote_ident(data.table, dialect) if quote else data.table
    columns = (
        ", ".join(quote_ident(c, dialect) for c in data.columns)
        if quote
        else ", ".join(data.columns)
    )

    statements: list[str] = []
    for start in range(0, len(data.rows), batch_size):
        chunk = data.rows[start : start + batch_size]
        tuples = []
        for row in chunk:
            cells = ", ".join(
                render_literal(row.values.get(col), dialect) for col in data.columns
            )
            tuples.append(f"  ({cells})")
        statements.append(
            f"INSERT INTO {table_name} ({columns}) VALUES\n" + ",\n".join(tuples) + ";"
        )
    return "\n\n".join(statements) + "\n"


def render_csv(data: TableData, path: str | None = None) -> str:
    """渲染 CSV。``path`` 给定时写入文件并返回路径；否则返回文本内容。

    使用 ``utf-8-sig`` 编码，便于 Excel 直接打开而不乱码。
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=data.columns, extrasaction="ignore")
    writer.writeheader()
    for row in data.rows:
        writer.writerow({col: row.values.get(col) for col in data.columns})

    if path is None:
        return buffer.getvalue()

    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(buffer.getvalue())
    return path
