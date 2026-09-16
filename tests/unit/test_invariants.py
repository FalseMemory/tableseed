"""不变量测试：行级断言、跨表聚合断言、静态校验、违例明细。"""

from __future__ import annotations

import allure
import pytest

from tableseed import service
from tableseed.config import load_config_from_text
from tableseed.errors import GenerateError
from tableseed.models import InvariantSpec

CONFIG = """
seed: 1
limits: {max_rows: 100}
tables:
  - name: t_p
    columns: [{name: id}, {name: amount}, {name: fee}]
    groups:
      - {type: sequence, name: g_id, fields: [id], start: 1, format: "P{seq:03d}"}
      - {type: enum, name: g_a, fields: [amount], values: [[100], [200]]}
      - {type: const, name: g_f, fields: [fee], value: [10]}
  - name: t_c
    columns: [{name: pid}, {name: detail_amt}]
    groups:
      - {type: enum, name: g_d, fields: [detail_amt], values: [[50], [100]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: id, child_field: pid}]
invariants:
"""


def build(extra: str):
    return load_config_from_text(CONFIG + extra)


# ---------------------------------------------------------------- 模型


@allure.epic("tableseed")
@allure.feature("不变量")
@allure.story("裸字符串兼容旧写法")
def test_plain_string_invariant():
    spec = InvariantSpec.model_validate("fee <= amount")
    assert spec.expr == "fee <= amount"
    assert spec.table is None
    assert spec.from_ is None


# ---------------------------------------------------------------- 行级断言


@allure.feature("不变量")
@allure.story("行级断言通过")
def test_row_level_invariant_passes():
    config = build('  - table: t_p\n    expr: "fee <= amount"\n')
    assert service.check(config) == []
    assert service.verify(config) == []


@allure.feature("不变量")
@allure.story("行级断言抓出违例行")
def test_row_level_invariant_catches_violation():
    """故意声明 fee > amount，必须逐行报违例。"""
    config = build('  - table: t_p\n    expr: "fee > amount"\n')
    failures = service.verify(config)
    assert len(failures) == 2  # 2 条父行，每行都违例
    assert all(f.table == "t_p" for f in failures)
    # 违例行 = 断言为假的行：fee 并不大于 amount
    assert all(f.row["fee"] <= f.row["amount"] for f in failures)


@allure.feature("不变量")
@allure.story("未指定 table 时对所有表求值")
def test_invariant_without_table_applies_everywhere():
    """全局行级断言作用在字段集不同的表上 → 求值报错（早失败）。"""
    config = build('  - expr: "amount >= 0"\n')
    with pytest.raises(GenerateError, match="求值失败"):
        service.verify(config)


# ---------------------------------------------------------------- 跨表断言


@allure.feature("不变量")
@allure.story("跨表聚合断言通过")
def test_cross_table_invariant_passes():
    config = build(
        '  - table: t_p\n    from: t_c\n    expr: "sum(detail_amt) = 150"\n'
    )  # 1:N 每条父行 2 条子行 (50+100)
    assert service.check(config) == []
    assert service.verify(config) == []


@allure.feature("不变量")
@allure.story("跨表断言抓出口径不符")
def test_cross_table_invariant_catches_mismatch():
    """明细合计是 150，断言 999 —— 必须全部违例。"""
    config = build('  - table: t_p\n    from: t_c\n    expr: "sum(detail_amt) = 999"\n')
    failures = service.verify(config)
    assert len(failures) == 2
    assert {f.row["id"] for f in failures} == {"P001", "P002"}


@allure.story("count 跨表断言")
def test_count_invariant():
    config = build('  - table: t_p\n    from: t_c\n    expr: "count() = 2"\n')
    assert service.verify(config) == []


# ---------------------------------------------------------------- 静态校验


@allure.feature("不变量")
@allure.story("表不存在")
def test_unknown_table_is_reported():
    config = build('  - table: nope\n    expr: "fee <= amount"\n')
    assert any("表不存在: nope" in p for p in service.check(config))


@allure.feature("不变量")
@allure.story("源表必须是子表")
def test_from_must_be_child():
    config = build('  - table: t_p\n    from: nope\n    expr: "1 = 1"\n')
    problems = service.check(config)
    assert any("nope" in p for p in problems)


@allure.feature("不变量")
@allure.story("跨表必须有 join 锚点")
def test_cross_table_requires_join():
    text = CONFIG.replace(
        'join: [{parent_field: id, child_field: pid}]',
        'propagate: [{mode: copy, to: pid, from: id}]',
    ) + '  - table: t_p\n    from: t_c\n    expr: "sum(detail_amt) = 150"\n'
    config = load_config_from_text(text)
    assert any("未声明 join" in p for p in service.check(config))


@allure.feature("不变量")
@allure.story("表达式语法错误")
def test_bad_expression_is_reported():
    config = build('  - table: t_p\n    expr: "__import__(\'os\')"\n')
    assert service.check(config)


# ---------------------------------------------------------------- verify 命令链路


@allure.feature("不变量")
@allure.story("无不变量时无事可验")
def test_no_invariants():
    config = load_config_from_text(CONFIG)
    assert service.verify(config) == []


@allure.feature("不变量")
@allure.story("verify 不落盘")
def test_verify_is_memory_only(tmp_path):
    config = build('  - table: t_p\n    expr: "fee <= amount"\n')
    service.verify(config)
    assert list(tmp_path.iterdir()) == []


@allure.story("引用不存在的关系时报错")
def test_missing_child_table_raises():
    config = build('  - table: t_p\n    from: t_c\n    expr: "sum(detail_amt) = 150"\n')
    assert service.verify(config) == []

    broken = build('  - table: t_p\n    from: t_c\n    expr: "sum(detail_amt) = 150"\n')
    broken.relations[0].child = "t_ghost"
    # 现在由 service.verify 的前置静态校验拦下，报错更具体（"子表不存在: t_ghost"）
    with pytest.raises(GenerateError, match="子表不存在|没有声明带 join 的关系|找不到子表"):
        service.verify(broken)
