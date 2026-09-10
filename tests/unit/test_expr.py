"""表达式引擎测试：语法归一化、求值、沙箱安全。"""

from __future__ import annotations

import allure
import pytest

from tableseed.errors import ExprError
from tableseed.expr import Expression, build_functions, normalize
from tableseed.rng import SeededRandom


@pytest.fixture()
def funcs():
    return build_functions(SeededRandom(20260910))


@allure.epic("tableseed")
@allure.feature("表达式引擎")
@allure.story("MySQL 风格语法归一化")
@pytest.mark.parametrize(
    "source, expected",
    [
        ("a = 1", "a == 1"),
        ("a <> 1", "a != 1"),
        ("a >= 1", "a >= 1"),
        ("a != 1", "a != 1"),
        ("A = 1 AND B = 2", "A == 1 and B == 2"),
        ("NOT a", "not a"),
        ("x IS NULL", "x is None"),
        ("x IS NOT NULL", "x is not None"),
        ('name LIKE "T%"', 'like(name, "T%")'),
        ('d BETWEEN "2026-01-01" and "2026-12-31"',
         'between(d, "2026-01-01", "2026-12-31")'),
        ('IF(a = 1, "y", "n")', 'if_(a == 1, "y", "n")'),
    ],
)
def test_normalize_mysql_style(source, expected):
    assert normalize(source) == expected


@allure.epic("tableseed")
@allure.feature("表达式引擎")
@allure.story("语法糖等价求值")
@pytest.mark.parametrize(
    "source, env, expected",
    [
        ("status = '01'", {"status": "01"}, True),
        ("status <> '01'", {"status": "01"}, False),
        ("a > 1 AND b < 5", {"a": 2, "b": 3}, True),
        ("a > 1 OR b > 5", {"a": 0, "b": 3}, False),
        ("NOT a", {"a": 0}, True),
        ("x IS NULL", {"x": None}, True),
        ("x IS NOT NULL", {"x": None}, False),
        ("name LIKE 'TXN-%'", {"name": "TXN-0001"}, True),
        ("name LIKE 'TXN-%'", {"name": "ABC-0001"}, False),
        ("amount BETWEEN 100 AND 500", {"amount": 300}, True),
        ("amount BETWEEN 100 AND 500", {"amount": 900}, False),
        ("channel IN ('OTC', 'EBANK')", {"channel": "OTC"}, True),
        ("IF(flag = 1, 10, 20)", {"flag": 1}, 10),
        ("coalesce(a, b, 'x')", {"a": None, "b": None}, "x"),
        ("upper(name)", {"name": "abc"}, "ABC"),
        ("substr(code, 2, 3)", {"code": "ABCDEFG"}, "BCD"),
        ("round(3.14159, 2)", {}, 3.14),
    ],
)
def test_evaluate_style_variants(funcs, source, env, expected):
    assert Expression(source, funcs)(env) == expected


@allure.epic("tableseed")
@allure.feature("表达式引擎")
@allure.story("命名空间属性访问")
def test_namespace_access(funcs):
    expression = Expression("parent.txn_type = 'WITHDRAW'", funcs)
    assert expression({"parent": {"txn_type": "WITHDRAW"}}) is True
    assert expression({"parent": {"txn_type": "DEPOSIT"}}) is False


@allure.epic("tableseed")
@allure.feature("表达式引擎")
@allure.story("安全沙箱")
@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('echo hi')",
        "open('/etc/passwd').read()",
        "().__class__.__bases__",
        "eval('1+1')",
        "lambda: 1",
        "[x for x in range(3)]",
        "a.__class__",
    ],
)
def test_sandbox_rejects_dangerous_expressions(funcs, source):
    with allure.step(f"尝试注入不安全表达式: {source}"):
        with pytest.raises(ExprError):
            Expression(source, funcs)


@allure.epic("tableseed")
@allure.feature("表达式引擎")
@allure.story("错误可读")
def test_unknown_function_and_variable(funcs):
    with pytest.raises(ExprError, match="未注册的函数"):
        Expression("nope(1)", funcs)({})
    with pytest.raises(ExprError, match="未知变量或字段"):
        Expression("missing_field > 1", funcs)({})
