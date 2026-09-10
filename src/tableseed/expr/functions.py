"""表达式函数库。

同时提供 Python 与 MySQL 两种命名风格（PRD D-3），大小写不敏感。
造数专用函数使用下划线后缀（``if_`` / ``dict_`` / ``uuid_``）以避开 Python 关键字。
"""

from __future__ import annotations

import builtins
import math
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..errors import ExprError
from ..rng import SeededRandom

#: 函数名 -> 实现
FunctionMap = dict[str, Callable[..., Any]]


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    raise ExprError(f"无法解析为日期: {value!r}")


def _to_num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ExprError(f"无法解析为数值: {value!r}") from exc


def _like(value: Any, pattern: str) -> bool:
    """SQL LIKE 语义：``%`` 匹配任意长度，``_`` 匹配单个字符。"""
    import re

    if value is None:
        return False
    # 逐字符转义，只把 % 与 _ 转成正则元字符（不能直接 re.escape 后替换）
    pieces: list[str] = []
    for char in str(pattern):
        if char == "%":
            pieces.append(".*")
        elif char == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(char))
    return re.fullmatch("".join(pieces), str(value), flags=re.S) is not None


def _between(value: Any, low: Any, high: Any) -> bool:
    return low <= value <= high


def _date_add(value: Any, days: Any) -> date:
    d = _to_date(value)
    if isinstance(days, timedelta):
        return d + days
    return d + timedelta(days=int(days))


def _date_sub(value: Any, days: Any) -> date:
    return _date_add(value, -int(days))


def build_functions(
    rng: SeededRandom | None = None,
    dictionaries: dict[str, list[Any]] | None = None,
) -> FunctionMap:
    """构建函数表。需要随机源或字典的函数在闭包中绑定上下文。"""

    dictionaries = dictionaries or {}
    rng = rng or SeededRandom(0)

    def _dict_lookup(name: str, index: Any = None) -> Any:
        key = str(name)
        if key not in dictionaries:
            raise ExprError(f"未注册的字典: {key}")
        pool = dictionaries[key]
        if not pool:
            raise ExprError(f"字典 {key} 为空")
        if index is None:
            return rng.rand_choice(pool)
        return pool[int(index) % len(pool)]

    funcs: FunctionMap = {
        # ---- 空值 ----
        "coalesce": lambda *args: next((a for a in args if a is not None), None),
        "nullif": lambda a, b: None if a == b else a,
        "isnull": lambda a: a is None,
        # ---- 数值 ----
        "abs": abs,
        "round": lambda x, n=0: builtins.round(_to_num(x), int(n)),
        "ceil": lambda x: int(math.ceil(_to_num(x))),
        "floor": lambda x: int(math.floor(_to_num(x))),
        "mod": lambda a, b: _to_num(a) % _to_num(b),
        "power": lambda a, b: _to_num(a) ** _to_num(b),
        "greatest": lambda *args: max(args),
        "least": lambda *args: min(args),
        # ---- 字符串 ----
        "length": lambda s: 0 if s is None else len(str(s)),
        "upper": lambda s: None if s is None else str(s).upper(),
        "lower": lambda s: None if s is None else str(s).lower(),
        "trim": lambda s: None if s is None else str(s).strip(),
        "ltrim": lambda s: None if s is None else str(s).lstrip(),
        "rtrim": lambda s: None if s is None else str(s).rstrip(),
        "substr": _substr,
        "replace": lambda s, old, new: None if s is None else str(s).replace(str(old), str(new)),
        "concat": lambda *args: "".join("" if a is None else str(a) for a in args),
        "like": _like,
        "between": _between,
        # ---- 条件 ----
        "if_": lambda cond, a, b=None: a if cond else b,
        # ---- 日期 ----
        "now": datetime.now,
        "today": date.today,
        "date_add": _date_add,
        "date_sub": _date_sub,
        "datediff": lambda a, b: (_to_date(a) - _to_date(b)).days,
        "date_format": lambda d, fmt: _to_date(d).strftime(fmt),
        "year": lambda d: _to_date(d).year,
        "month": lambda d: _to_date(d).month,
        "day": lambda d: _to_date(d).day,
        # ---- 造数专用 ----
        "seq_": lambda: None,  # 占位：真实 seq 由上下文注入
        "rand_int": rng.rand_int,
        "rand_decimal": rng.rand_decimal,
        "rand_choice": lambda pool, weights=None: rng.rand_choice(list(pool), weights),
        "uuid_": rng.rand_uuid,
        "dict_": _dict_lookup,
        # ---- 聚合（供 aggregate 组与 invariants 使用）----
        "count": lambda items: len(list(items)),
        "sum": lambda items: builtins.sum(_to_num(x) for x in items),
        "avg": lambda items: _avg(items),
        "min": lambda items: builtins.min(items),
        "max": lambda items: builtins.max(items),
        "count_distinct": lambda items: len({x for x in items}),
    }

    # ---- MySQL 风格别名 ----
    aliases: FunctionMap = {
        "ifnull": funcs["coalesce"],
        "nvl": funcs["coalesce"],
        "char_length": funcs["length"],
        "ucase": funcs["upper"],
        "lcase": funcs["lower"],
        "curdate": funcs["today"],
        "timestampdiff": lambda unit, a, b: _timestampdiff(unit, a, b),
        "if": funcs["if_"],
    }
    funcs.update(aliases)
    return funcs


def _substr(s: Any, start: int, length: int | None = None) -> str | None:
    """MySQL 语义：start 从 1 开始；start 为负表示从末尾倒数。"""
    if s is None:
        return None
    text = str(s)
    idx = int(start)
    begin = idx - 1 if idx > 0 else len(text) + idx
    if length is None:
        return text[begin:]
    return text[begin : begin + int(length)]


def _avg(items: Any) -> float:
    values = [_to_num(x) for x in items]
    if not values:
        raise ExprError("avg 的输入为空")
    return builtins.sum(values) / len(values)


def _timestampdiff(unit: Any, a: Any, b: Any) -> int:
    unit = str(unit).upper()
    if unit in {"DAY", "D"}:
        return (_to_date(b) - _to_date(a)).days
    if unit in {"MONTH", "M"}:
        da, db = _to_date(a), _to_date(b)
        return (db.year - da.year) * 12 + (db.month - da.month)
    if unit in {"YEAR", "Y"}:
        return _to_date(b).year - _to_date(a).year
    raise ExprError(f"不支持的 timestampdiff 单位: {unit}")
