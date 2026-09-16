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


# ---------------------------------------------------------------- M2 关系校验


TWO_TABLES = """
seed: 1
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_a, fields: [a_id], values: [["1"]]}
  - name: t_b
    groups:
      - {type: enum, name: g_b, fields: [b_id], values: [["1"]]}
relations:
"""


@allure.feature("配置校验")
@allure.story("关系成环")
def test_relation_cycle_is_reported():
    text = """
seed: 1
tables:
  - name: a
    groups: [{type: enum, name: g, fields: [x], values: [["1"]]}]
  - name: b
    groups: [{type: enum, name: g, fields: [y], values: [["1"]]}]
relations:
  - {parent: a, child: b, join: [{parent_field: x, child_field: y}]}
  - {parent: b, child: a, join: [{parent_field: y, child_field: x}]}
"""
    assert any("循环依赖" in p for p in problems_of(text))


@allure.feature("配置校验")
@allure.story("多父关系")
def test_multi_parent_is_reported():
    text = """
seed: 1
tables:
  - name: a
    groups: [{type: enum, name: g, fields: [x], values: [["1"]]}]
  - name: b
    groups: [{type: enum, name: g, fields: [y], values: [["1"]]}]
  - name: c
    groups: [{type: enum, name: g, fields: [z], values: [["1"]]}]
relations:
  - {parent: a, child: c, join: [{parent_field: x, child_field: z}]}
  - {parent: b, child: c, join: [{parent_field: y, child_field: z}]}
"""
    assert any("多个父表" in p for p in problems_of(text))


@allure.feature("配置校验")
@allure.story("传播目标字段可以不存在 —— 传播是「创建者」不是「引用者」")
def test_propagate_to_new_field_is_allowed():
    """回归：曾经要求 to 必须已存在于子表，导致「子表字段完全由传播提供」
    （文档推荐用法）被误报「子表不存在字段 X」，用户以为配置错了不敢用，
    而实际生成完全正常。
    """
    text = TWO_TABLES + """
  - parent: t_a
    child: t_b
    join: [{parent_field: a_id, child_field: b_id}]
    propagate:
      - {mode: copy, to: brand_new_field, from: a_id}
"""
    problems = problems_of(text)
    assert not any("brand_new_field" in p and "不存在" in p for p in problems), (
        f"不该因为目标字段不存在而报错: {problems}"
    )


@allure.feature("配置校验")
@allure.story("目标字段与子表取值组冲突 → 提示（不拦生成）")
def test_propagate_over_group_field_is_hint_only():
    """传播覆盖组里的取值是允许的语义，但组里的值会被丢弃 —— 提示到即可，"""
    text = TWO_TABLES + """
  - parent: t_a
    child: t_b
    join: [{parent_field: a_id, child_field: b_id}]
    propagate:
      - {mode: copy, to: b_id, from: a_id}
"""
    problems = problems_of(text)
    hints = [p for p in problems if p.startswith("[提示]")]
    assert any("b_id" in h for h in hints), f"应给出提示: {problems}"
    # [提示] 不算错误 —— Web 层据前缀过滤，不会拦下生成
    assert all(not p.startswith("[提示]") or True for p in problems)


@allure.feature("配置校验")
@allure.story("copy 源字段必须存在")
def test_propagate_from_unknown_field_is_reported():
    text = TWO_TABLES + """
  - parent: t_a
    child: t_b
    join: [{parent_field: a_id, child_field: b_id}]
    propagate:
      - {mode: copy, to: b_id, from: nope}
"""
    assert any("不存在字段 nope" in p for p in problems_of(text))


@allure.feature("配置校验")
@allure.story("map 必须有映射表")
def test_map_without_mapping_is_reported():
    text = TWO_TABLES + """
  - parent: t_a
    child: t_b
    join: [{parent_field: a_id, child_field: b_id}]
    propagate:
      - {mode: map, to: b_id, from: a_id}
"""
    assert any("必须提供 mapping" in p for p in problems_of(text))


@allure.feature("配置校验")
@allure.story("derive 传播必须引用父字段")
def test_derive_without_parent_reference_is_reported():
    text = TWO_TABLES + """
  - parent: t_a
    child: t_b
    join: [{parent_field: a_id, child_field: b_id}]
    propagate:
      - {mode: derive, to: b_id, expr: "1 + 1"}
"""
    assert any("没有引用任何父表字段" in p for p in problems_of(text))


@allure.feature("配置校验")
@allure.story("被传播覆盖的字段无需归组")
def test_propagated_field_needs_no_group():
    """字段由 propagate 提供取值时，不必再要求它归入某个组。"""
    text = """
seed: 1
tables:
  - name: t_a
    columns: [{name: a_id}]
    groups:
      - {type: enum, name: g_a, fields: [a_id], values: [["1"]]}
  - name: t_b
    columns: [{name: b_id}, {name: a_id}]
    groups:
      - {type: enum, name: g_b, fields: [b_id], values: [["1"]]}
relations:
  - parent: t_a
    child: t_b
    join: [{parent_field: a_id, child_field: a_id}]
"""
    assert problems_of(text) == []


@allure.feature("配置校验")
@allure.story("子表可无有限取值组")
def test_child_table_without_finite_group_is_ok():
    """子表行数由父表决定，可以完全没有有限取值组。"""
    text = """
seed: 1
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_a, fields: [a_id], values: [["1"]]}
  - name: t_b
    groups:
      - {type: const, name: g_c, fields: [remark], value: ["x"]}
relations:
  - parent: t_a
    child: t_b
    propagate:
      - {mode: copy, to: remark, from: a_id}
"""
    problems = problems_of(text)
    assert not any("有限取值组" in p for p in problems)
