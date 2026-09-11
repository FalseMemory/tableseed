"""M3 两阶段生成：aggregate 组由子表汇总回填（Phase 2）。"""

from __future__ import annotations

import allure
import pytest

from tableseed.config import load_config_from_text
from tableseed.engine import generate_all
from tableseed.errors import GenerateError
from tableseed.expr import build_functions
from tableseed.expr.aggregate_expr import AggregateExpression
from tableseed.rng import SeededRandom

FUNCS = build_functions(SeededRandom(1))


def build(extra: str) -> dict:
    """构造 父(1 行) → 子 的配置骨架，extra 追加到父表 groups 后。"""
    return generate_all(
        load_config_from_text(
            f"""
seed: 1
tables:
  - name: t_p
    columns: [{{name: id}}, {{name: agg}}]
    groups:
      - {{type: enum, name: g_id, fields: [id], values: [["P1"]]}}
{extra}
  - name: t_c
    groups:
      - {{type: enum, name: g_amt, fields: [amount], values: [[10], [20], [30]]}}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{{parent_field: id, child_field: pid}}]
"""
        )
    )


def agg_value(extra: str) -> object:
    return build(extra).tables["t_p"].rows[0].values["agg"]


# ---------------------------------------------------------------- 聚合函数


@allure.epic("tableseed")
@allure.feature("两阶段生成")
@allure.story("sum / count / avg / min / max / count_distinct")
@pytest.mark.parametrize(
    "expr,expected",
    [
        ("sum(amount)", 60),
        ("count()", 3),
        ("count(amount)", 3),
        ("avg(amount)", 20),
        ("min(amount)", 10),
        ("max(amount)", 30),
        ("count_distinct(amount)", 3),
    ],
)
def test_aggregate_functions(expr, expected):
    assert agg_value(f'      - {{type: aggregate, name: g, fields: [agg], expr: "{expr}"}}') == expected


@allure.story("复合表达式：聚合之间可做运算")
def test_compound_aggregate_expression():
    """sum 与 count 分别逐行求值后再相除。"""
    assert agg_value(
        '      - {type: aggregate, name: g, fields: [agg], expr: "sum(amount) / count()"}'
    ) == pytest.approx(20)


@allure.story("子行字段可参与运算后再聚合")
def test_aggregate_over_expression():
    assert agg_value(
        '      - {type: aggregate, name: g, fields: [agg], expr: "sum(amount * 2)"}'
    ) == 120


@allure.story("聚合可引用父行字段")
def test_aggregate_can_reference_parent():
    """子行求值环境里 parent.* 指向父行。"""
    result = generate_all(
        load_config_from_text(
            """
seed: 1
tables:
  - name: t_p
    columns: [{name: id}, {name: agg}]
    groups:
      - {type: enum, name: g_id, fields: [id], values: [["P1"], ["P2"]]}
      - {type: aggregate, name: g, fields: [agg],
         expr: "sum(amount + length(parent.id))"}
  - name: t_c
    groups:
      - {type: enum, name: g_amt, fields: [amount], values: [[10], [20]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: id, child_field: pid}]
"""
        )
    )
    # 每个父行 2 条子行 (10,20)，parent.id 长度 2 → (10+2)+(20+2) = 34
    assert {row.values["agg"] for row in result.tables["t_p"].rows} == {34}


# ---------------------------------------------------------------- 源表解析


@allure.feature("两阶段生成")
@allure.story("唯一子表时可省略 from")
def test_source_table_can_be_omitted():
    assert agg_value('      - {type: aggregate, name: g, fields: [agg], expr: "count()"}') == 3


@allure.story("多子表时必须显式 from")
def test_multiple_children_require_from():
    from tableseed.config import check_config

    problems = check_config(
        load_config_from_text(
            """
seed: 1
tables:
  - name: p
    columns: [{name: id}, {name: agg}]
    groups:
      - {type: enum, name: g, fields: [id], values: [["1"]]}
      - {type: aggregate, name: ga, fields: [agg], expr: "count()"}
  - name: c1
    groups: [{type: enum, name: g, fields: [a], values: [["1"]]}]
  - name: c2
    groups: [{type: enum, name: g, fields: [b], values: [["1"]]}]
relations:
  - {parent: p, child: c1, join: [{parent_field: id, child_field: a}]}
  - {parent: p, child: c2, join: [{parent_field: id, child_field: b}]}
"""
        )
    )
    assert any("多个子表" in p for p in problems)


@allure.story("from 指向非子表时报错")
def test_from_must_be_a_child_table():
    from tableseed.config import check_config

    problems = check_config(
        load_config_from_text(
            """
seed: 1
tables:
  - name: p
    columns: [{name: id}, {name: agg}]
    groups:
      - {type: enum, name: g, fields: [id], values: [["1"]]}
      - {type: aggregate, name: ga, fields: [agg], from: nope, expr: "count()"}
  - name: c1
    groups: [{type: enum, name: g, fields: [a], values: [["1"]]}]
relations:
  - {parent: p, child: c1, join: [{parent_field: id, child_field: a}]}
"""
        )
    )
    assert any("不是 p 的子表" in p for p in problems)


@allure.story("聚合必须有 join 锚点")
def test_join_is_required_for_aggregation():
    from tableseed.config import check_config

    problems = check_config(
        load_config_from_text(
            """
seed: 1
tables:
  - name: p
    columns: [{name: id}, {name: agg}]
    groups:
      - {type: enum, name: g, fields: [id], values: [["1"]]}
      - {type: aggregate, name: ga, fields: [agg], expr: "count()"}
  - name: c1
    groups: [{type: enum, name: g, fields: [a], values: [["1"]]}]
relations:
  - parent: p
    child: c1
    propagate: [{mode: copy, to: a, from: id}]
"""
        )
    )
    assert any("未声明 join" in p for p in problems)


# ---------------------------------------------------------------- 边界


@allure.feature("两阶段生成")
@allure.story("父行无匹配子行时按空集处理")
def test_missing_child_rows_yield_empty_aggregate():
    """1:0..1 + conditional：没有子行的父行，聚合不能崩。"""
    result = generate_all(
        load_config_from_text(
            """
seed: 1
tables:
  - name: t_p
    columns: [{name: id}, {name: cnt}, {name: total}]
    groups:
      - {type: enum, name: g_id, fields: [id], values: [["P1"], ["P2"]]}
      - {type: aggregate, name: g_cnt, fields: [cnt], expr: "count()"}
      - {type: aggregate, name: g_sum, fields: [total], expr: "sum(amount)"}
  - name: t_c
    groups:
      - {type: enum, name: g_a, fields: [amount], values: [[10]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:0..1"
    existence: conditional
    condition: "id = 'P1'"
    join: [{parent_field: id, child_field: pid}]
"""
        )
    )
    by_id = {row.values["id"]: row.values for row in result.tables["t_p"].rows}
    assert by_id["P1"]["cnt"] == 1
    assert by_id["P1"]["total"] == 10
    assert by_id["P2"]["cnt"] == 0   # 无子行 → 0 而非崩溃
    assert by_id["P2"]["total"] == 0


@allure.story("空集时 min / max 为 None 而非抛错")
def test_min_max_on_empty_set_is_none():
    expr = AggregateExpression("min(amount)", FUNCS, "test")
    assert expr.eval_over([]) is None
    assert AggregateExpression("max(amount)", FUNCS, "test").eval_over([]) is None


@allure.story("聚合函数不给参数时报错")
def test_aggregate_without_argument_raises():
    expr = AggregateExpression("sum()", FUNCS, "test")
    with pytest.raises(Exception):
        expr.eval_over([{"amount": 1}])


@allure.story("未知函数在编译期就被拒")
def test_unknown_function_rejected_at_compile_time():
    from tableseed.errors import ExprError

    with pytest.raises(ExprError, match="未注册的函数"):
        AggregateExpression("nope(amount)", FUNCS, "test")


@allure.story("aggregate 组取值与子表实际数据一致")
def test_aggregate_matches_child_data():
    result = generate_all(
        load_config_from_text(
            """
seed: 20260910
tables:
  - name: t_p
    columns: [{name: id}, {name: total}]
    groups:
      - {type: enum, name: g_id, fields: [id], values: [["P1"], ["P2"]]}
      - {type: aggregate, name: g_t, fields: [total], expr: "sum(amount)"}
  - name: t_c
    groups:
      - {type: enum, name: g_a, fields: [amount], values: [[10], [20], [30]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: id, child_field: pid}]
"""
        )
    )
    children: dict[str, int] = {}
    for row in result.tables["t_c"].rows:
        children[row.values["pid"]] = children.get(row.values["pid"], 0) + row.values["amount"]

    for row in result.tables["t_p"].rows:
        assert row.values["total"] == children[row.values["id"]] == 60


@allure.story("aggregate 缺 expr 时运行期报错")
def test_aggregate_without_expr_raises():
    with pytest.raises(GenerateError, match="缺少 expr"):
        generate_all(
            load_config_from_text(
                """
seed: 1
tables:
  - name: p
    columns: [{name: id}, {name: agg}]
    groups:
      - {type: enum, name: g, fields: [id], values: [["1"]]}
      - {type: aggregate, name: ga, fields: [agg]}
  - name: c1
    groups: [{type: enum, name: g, fields: [a], values: [["1"]]}]
relations:
  - {parent: p, child: c1, join: [{parent_field: id, child_field: a}]}
"""
            )
        )
