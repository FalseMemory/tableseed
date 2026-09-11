"""表间关系测试（M2）：拓扑排序、分配策略、字段传播、1:1 / 1:N 行数。"""

from __future__ import annotations

import allure
import pytest

from tableseed.config import load_config_from_text
from tableseed.engine import (
    ComboAllocator,
    apply_propagate,
    drive_fields_of,
    effective_rules,
    generate_all,
    topo_order,
)
from tableseed.errors import GenerateError, PlanError
from tableseed.expr import build_functions
from tableseed.models import PropagateRule
from tableseed.rng import SeededRandom

PARENT = """
seed: 20260910
tables:
  - name: t_txn
    groups:
      - {type: enum, name: g_type, fields: [txn_type],
         values: [["T"], ["D"], ["W"]]}
      - {type: sequence, name: g_no, fields: [txn_no], format: "TXN{seq:04d}"}
      - {type: random, name: g_amt, fields: [amount], generator: int, range: [100, 200]}
"""


def build(text: str):
    return load_config_from_text(text)


# ---------------------------------------------------------------- 拓扑排序


@allure.epic("tableseed")
@allure.feature("表间关系")
@allure.story("拓扑排序")
def test_topo_order_parents_first():
    config = build(
        PARENT
        + """
  - name: t_detail
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["S0"]]}
relations:
  - {parent: t_txn, child: t_detail, join: [{parent_field: txn_no, child_field: txn_no}]}
"""
    )
    order = topo_order(config)
    assert order.index("t_txn") < order.index("t_detail")


@allure.story("环检测")
def test_topo_order_detects_cycle():
    config = build(
        """
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
    )
    with pytest.raises(PlanError, match="循环依赖"):
        topo_order(config)


# ---------------------------------------------------------------- 分配策略


@allure.feature("1:1 分配")
@allure.story("follow_parent：同父值复用同一组合")
def test_follow_parent_is_consistent():
    groups = build(
        """
seed: 1
tables:
  - name: t
    groups:
      - {type: enum, name: g, fields: [s], values: [["S0"], ["S1"], ["S2"]]}
"""
    ).tables[0].groups

    allocator = ComboAllocator(groups, "follow_parent", SeededRandom(1), drive_fields=["type"])
    first = allocator.assign({"type": "T"}, 0)
    again = allocator.assign({"type": "T"}, 1)
    assert first == again == {"s": "S0"}


@allure.story("follow_parent：不同父值轮转取不同组合（保覆盖）")
def test_follow_parent_rotates_for_new_keys():
    groups = build(
        """
seed: 1
tables:
  - name: t
    groups:
      - {type: enum, name: g, fields: [s], values: [["S0"], ["S1"], ["S2"]]}
"""
    ).tables[0].groups

    allocator = ComboAllocator(groups, "follow_parent", SeededRandom(1), drive_fields=["type"])
    picked = [
        allocator.assign({"type": key}, i)["s"]
        for i, key in enumerate(["T", "D", "W", "T", "D", "W"])
    ]
    assert picked == ["S0", "S1", "S2", "S0", "S1", "S2"]


@allure.story("round_robin 按序号轮转")
def test_round_robin():
    groups = build(
        """
seed: 1
tables:
  - name: t
    groups:
      - {type: enum, name: g, fields: [s], values: [["S0"], ["S1"]]}
"""
    ).tables[0].groups

    allocator = ComboAllocator(groups, "round_robin", SeededRandom(1))
    assert [allocator.assign({}, i)["s"] for i in range(4)] == [
        "S0",
        "S1",
        "S0",
        "S1",
    ]


@allure.story("drive_by 优先于 join 父字段")
def test_drive_fields_priority():
    from tableseed.models import JoinKey, RelationSpec

    join = [JoinKey(parent_field="txn_no", child_field="txn_no")]
    rules = [PropagateRule(mode="copy", to="currency", **{"from": "currency"})]

    assert drive_fields_of(RelationSpec(parent="a", child="b", join=join), rules) == ["currency"]
    assert drive_fields_of(
        RelationSpec(parent="a", child="b", join=join, drive_by=["txn_type"]), rules
    ) == ["txn_type"]
    assert drive_fields_of(RelationSpec(parent="a", child="b", join=join), []) == ["txn_no"]


# ---------------------------------------------------------------- 字段传播


def _rule(mode: str, to: str, **kwargs) -> PropagateRule:
    return PropagateRule(mode=mode, to=to, **kwargs)


@allure.feature("字段传播")
@allure.story("copy / map / derive / free")
def test_propagate_modes():
    funcs = build_functions(SeededRandom(1))
    child = {"amount": 100}
    parent = {"txn_type": "T", "amount": 100, "fee": 1}

    apply_propagate(
        [
            _rule("copy", "txn_type", **{"from": "txn_type"}),
            _rule("map", "detail_type", **{"from": "txn_type", "mapping": {"T": "TRANSFER"}}),
            _rule("derive", "net", expr="parent.amount - parent.fee"),
            _rule("free", "remark"),
        ],
        child,
        parent,
        funcs,
    )

    assert child["txn_type"] == "T"
    assert child["detail_type"] == "TRANSFER"
    assert child["net"] == 99
    assert "remark" not in child  # free 不传播


@allure.story("map 未命中且无 default 时报错")
def test_map_missing_entry_raises():
    funcs = build_functions(SeededRandom(1))
    with pytest.raises(GenerateError, match="未命中"):
        apply_propagate(
            [_rule("map", "d", **{"from": "t", "mapping": {"T": "X"}})],
            {},
            {"t": "W"},
            funcs,
        )


@allure.story("map 未命中但有 default 时兜底")
def test_map_default_fallback():
    funcs = build_functions(SeededRandom(1))
    child: dict = {}
    apply_propagate(
        [_rule("map", "d", **{"from": "t", "mapping": {"T": "X"}, "default": "OTHER"})],
        child,
        {"t": "W"},
        funcs,
    )
    assert child["d"] == "OTHER"


@allure.story("join 锚点自动补齐 copy")
def test_join_generates_copy_rule():
    from tableseed.models import JoinKey, RelationSpec

    relation = RelationSpec(
        parent="a",
        child="b",
        join=[JoinKey(parent_field="id", child_field="a_id")],
        propagate=[PropagateRule(mode="copy", to="currency", **{"from": "currency"})],
    )
    modes = {(r.mode, r.to, r.from_) for r in effective_rules(relation)}
    assert ("copy", "a_id", "id") in modes          # 自动补齐
    assert ("copy", "currency", "currency") in modes  # 保留用户规则


@allure.story("用户显式规则优先于 join 自动补齐")
def test_user_rule_wins_over_join():
    from tableseed.models import JoinKey, RelationSpec

    relation = RelationSpec(
        parent="a",
        child="b",
        join=[JoinKey(parent_field="id", child_field="a_id")],
        propagate=[PropagateRule(mode="derive", to="a_id", expr="parent.id + 1")],
    )
    rules = effective_rules(relation)
    assert len(rules) == 1
    assert rules[0].mode == "derive"


# ---------------------------------------------------------------- 行数


def _two_tables(cardinality: str, child_values: str = "[['S0'], ['S1'], ['S2']]") -> str:
    return (
        PARENT
        + f"""
  - name: t_child
    groups:
      - {{type: enum, name: g_s, fields: [status], values: {child_values}}}
relations:
  - parent: t_txn
    child: t_child
    cardinality: "{cardinality}"
    join: [{{parent_field: txn_no, child_field: txn_no}}]
"""
    )


@allure.feature("基数与行数")
@allure.story("1:1 不放大行数")
def test_one_to_one_keeps_row_count():
    config = build(_two_tables("1:1"))
    result = generate_all(config)
    assert len(result.tables["t_txn"]) == 3
    assert len(result.tables["t_child"]) == 3  # 3 组合但 1:1，不放大


@allure.story("1:N 按笛卡尔积放大")
def test_one_to_many_multiplies_rows():
    config = build(_two_tables("1:N"))
    result = generate_all(config)
    assert len(result.tables["t_txn"]) == 3
    assert len(result.tables["t_child"]) == 9  # 3 父行 × 3 组合


@allure.story("1:N 覆盖全部组合")
def test_one_to_many_covers_all_combos():
    config = build(_two_tables("1:N"))
    result = generate_all(config)
    statuses = {row.values["status"] for row in result.tables["t_child"].rows}
    assert statuses == {"S0", "S1", "S2"}


@allure.story("1:1 在固定行数内尽量覆盖")
def test_one_to_one_covers_within_fixed_rows():
    config = build(_two_tables("1:1"))
    result = generate_all(config)
    statuses = {row.values["status"] for row in result.tables["t_child"].rows}
    assert len(statuses) == 3  # 3 父行恰好覆盖 3 个组合


@allure.story("多父关系尚未支持")
def test_multi_parent_rejected():
    config = build(
        """
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
    )
    with pytest.raises(PlanError, match="多个父表"):
        generate_all(config)


# ---------------------------------------------------------------- 传播落库


@allure.feature("跨表一致性")
@allure.story("子行主键与业务字段与父行一致")
def test_child_row_matches_parent():
    config = build(
        PARENT
        + """
  - name: t_detail
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["S0"]]}
relations:
  - parent: t_txn
    child: t_detail
    cardinality: "1:1"
    join: [{parent_field: txn_no, child_field: txn_no}]
    propagate:
      - {mode: copy, to: amount, from: amount}
"""
    )
    result = generate_all(config)
    parents = {row.values["txn_no"]: row.values for row in result.tables["t_txn"].rows}

    for child in result.tables["t_detail"].rows:
        parent = parents[child.values["txn_no"]]
        assert child.values["amount"] == parent["amount"]


@allure.story("ref 组引用父表字段")
def test_ref_group_reads_parent_field():
    config = build(
        PARENT
        + """
  - name: t_detail
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["S0"]]}
      - {type: ref, name: g_ref, fields: [ref_no], from: "parent.txn_no"}
relations:
  - parent: t_txn
    child: t_detail
    cardinality: "1:1"
    join: [{parent_field: txn_no, child_field: txn_no}]
"""
    )
    result = generate_all(config)
    for child in result.tables["t_detail"].rows:
        assert child.values["ref_no"] == child.values["txn_no"]


@allure.story("ref 组引用不存在的父字段时报错")
def test_ref_group_unknown_field_raises():
    config = build(
        PARENT
        + """
  - name: t_detail
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["S0"]]}
      - {type: ref, name: g_ref, fields: [ref_no], from: "parent.nope"}
relations:
  - parent: t_txn
    child: t_detail
    cardinality: "1:1"
    join: [{parent_field: txn_no, child_field: txn_no}]
"""
    )
    with pytest.raises(GenerateError, match="父行中不存在"):
        generate_all(config)


@allure.story("conditional 存在性：不满足条件不生成子行")
def test_conditional_existence_skips_rows():
    config = build(
        PARENT
        + """
  - name: t_detail
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["S0"]]}
relations:
  - parent: t_txn
    child: t_detail
    cardinality: "1:0..1"
    existence: conditional
    condition: "txn_type = 'W'"
    join: [{parent_field: txn_no, child_field: txn_no}]
"""
    )
    result = generate_all(config)
    assert len(result.tables["t_txn"]) == 3
    assert len(result.tables["t_detail"]) == 1  # 只有 W 类型的那一行有详情
