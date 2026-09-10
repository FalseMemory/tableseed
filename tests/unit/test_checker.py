"""配置校验测试 —— 重点是分组完备性（不重不漏）。"""

from __future__ import annotations

import allure
import pytest

from tableseed.config import check_config, load_config_from_text
from tableseed.errors import ConfigError


def problems_of(text: str) -> list[str]:
    return check_config(load_config_from_text(text))


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("分组完备性")
def test_ungrouped_field_is_reported():
    """AC-8：故意漏掉一个字段的分组，check 必须报错并指名该字段。"""
    text = """
seed: 1
tables:
  - name: t_account
    columns:
      - {name: status_code}
      - {name: currency}
      - {name: orphan_field}
    groups:
      - {type: enum, name: g_status, fields: [status_code], values: [["01"], ["02"]]}
      - {type: enum, name: g_currency, fields: [currency], values: [["CNY"]]}
"""
    problems = problems_of(text)
    assert problems
    assert any("orphan_field" in p and "未分组" in p for p in problems)


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("分组完备性")
def test_duplicated_field_in_two_groups_is_reported():
    text = """
seed: 1
tables:
  - name: t_account
    groups:
      - {type: enum, name: g_a, fields: [status_code], values: [["01"]]}
      - {type: enum, name: g_b, fields: [status_code], values: [["02"]]}
"""
    problems = problems_of(text)
    assert any("重复分组" in p and "status_code" in p for p in problems)


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("取值元组长度")
def test_value_tuple_length_mismatch_is_reported():
    text = """
seed: 1
tables:
  - name: t_account
    groups:
      - {type: enum, name: g_status, fields: [code, desc],
         values: [["01", "正常"], ["02"]]}
"""
    problems = problems_of(text)
    assert any("取值元组长度" in p for p in problems)


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("缺少有限取值组")
def test_table_without_finite_group_is_reported():
    text = """
seed: 1
tables:
  - name: t_account
    groups:
      - {type: const, name: g_t, fields: [tenant_id], value: ["0001"]}
"""
    problems = problems_of(text)
    assert any("没有任何有限取值组" in p for p in problems)


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("关系字段引用")
def test_relation_field_must_exist():
    text = """
seed: 1
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_a, fields: [a_id], values: [["1"]]}
  - name: t_b
    groups:
      - {type: enum, name: g_b, fields: [b_id], values: [["1"]]}
relations:
  - {parent: t_a, child: t_b, join: [{parent_field: nope, child_field: b_id}]}
"""
    problems = problems_of(text)
    assert any("不存在字段 nope" in p for p in problems)


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("规模上限")
def test_combo_count_over_max_rows_is_reported():
    text = """
seed: 1
limits: {max_rows: 4}
tables:
  - name: t_account
    groups:
      - {type: enum, name: g_a, fields: [a], values: [["1"], ["2"], ["3"]]}
      - {type: enum, name: g_b, fields: [b], values: [["1"], ["2"], ["3"]]}
"""
    problems = problems_of(text)
    assert any("超过 max_rows" in p for p in problems)


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("表达式校验")
def test_bad_expression_is_reported():
    text = """
seed: 1
tables:
  - name: t_account
    groups:
      - {type: enum, name: g_a, fields: [a], values: [["1"]]}
      - {type: derive, name: g_d, fields: [b], expr: "__import__('os')"}
"""
    problems = problems_of(text)
    assert problems


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("合法配置")
def test_valid_config_has_no_problems():
    text = """
seed: 1
tables:
  - name: t_account
    groups:
      - {type: enum, name: g_a, fields: [status_code], values: [["01"], ["02"]]}
      - {type: const, name: g_t, fields: [tenant_id], value: ["0001"]}
"""
    assert problems_of(text) == []


@allure.epic("tableseed")
@allure.feature("配置校验")
@allure.story("未知字段早失败")
def test_unknown_config_key_raises():
    with pytest.raises(ConfigError, match="配置校验失败"):
        load_config_from_text("seed: 1\ntables: []\nnope: 1")
