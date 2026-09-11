"""覆盖策略测试：full / sample / pairwise 三种骨架展开。"""

from __future__ import annotations

from itertools import combinations

import allure
import pytest

from tableseed import service
from tableseed.config import load_config_from_text
from tableseed.engine.coverage import (
    pairwise_pairs_total,
    pairwise_skeletons,
    sample_skeletons,
)
from tableseed.errors import ConfigError
from tableseed.models import GroupSpec
from tableseed.rng import SeededRandom

FIVE = [[f"V{i}"] for i in range(1, 6)]


def three_groups() -> list[GroupSpec]:
    return [
        GroupSpec(name="a", type="enum", fields=["x"], values=[[f"A{i}"] for i in range(1, 6)]),
        GroupSpec(name="b", type="enum", fields=["y"], values=[[f"B{i}"] for i in range(1, 6)]),
        GroupSpec(name="c", type="enum", fields=["z"], values=[[f"C{i}"] for i in range(1, 6)]),
    ]


def base_config(strategy: str, extra: str = "") -> str:
    return f"""
seed: 20260910
limits:
  max_rows: 100000
  strategy: {strategy}
{extra}
tables:
  - name: t_a
    groups:
      - {{type: enum, name: g_s, fields: [status], values: [["01"], ["02"]]}}
      - {{type: enum, name: g_c, fields: [currency], values: [["CNY"], ["USD"]]}}
      - {{type: enum, name: g_ch, fields: [channel],
         values: [["OTC"], ["EBANK"], ["MOBILE"]]}}
      - {{type: sequence, name: g_n, fields: [txn_no], format: "N{{seq:03d}}"}}
"""


# ---------------------------------------------------------------- pairwise 单元


@allure.epic("tableseed")
@allure.feature("覆盖策略")
@allure.story("pairwise 两两配对全覆盖")
def test_pairwise_covers_all_pairs():
    groups = three_groups()
    rows = list(pairwise_skeletons(groups, SeededRandom(1), max_rows=1000))

    covered = set()
    for r in rows:
        for i, j in combinations(range(3), 2):
            covered.add((r["xyz"[i]], r["xyz"[j]]))

    assert len(covered) == pairwise_pairs_total(groups) == 75


@allure.story("pairwise 行数远小于全组合")
def test_pairwise_is_compact():
    groups = three_groups()
    rows = list(pairwise_skeletons(groups, SeededRandom(1), max_rows=1000))
    assert len(rows) < 125  # 全组合 125
    assert len(rows) >= 25  # 至少要覆盖单因子 5×5 对的规模量级


@allure.story("pairwise 可复现")
def test_pairwise_is_reproducible():
    groups = three_groups()
    a = list(pairwise_skeletons(groups, SeededRandom(7), max_rows=1000))
    b = list(pairwise_skeletons(groups, SeededRandom(7), max_rows=1000))
    assert a == b


@allure.story("单因子逐值成行")
def test_pairwise_single_factor():
    groups = [GroupSpec(name="a", type="enum", fields=["x"], values=FIVE)]
    rows = list(pairwise_skeletons(groups, SeededRandom(1), max_rows=100))
    assert [r["x"] for r in rows] == ["V1", "V2", "V3", "V4", "V5"]


@allure.story("sample 随机抽取且可复现")
def test_sample_is_seeded():
    groups = three_groups()
    a = list(sample_skeletons(groups, 7, SeededRandom(3)))
    b = list(sample_skeletons(groups, 7, SeededRandom(3)))
    assert a == b and len(a) == 7


@allure.story("pairwise 配对总数计算")
def test_pairs_total():
    assert pairwise_pairs_total(three_groups()) == 75
    assert pairwise_pairs_total(three_groups()[:2]) == 25


# ---------------------------------------------------------------- 配置集成


@allure.feature("覆盖策略")
@allure.story("full 全组合覆盖")
def test_full_strategy_end_to_end():
    config = load_config_from_text(base_config("full"))
    result = service.generate(config)
    rows = result.tables["t_a"].records
    combos = {(r["status"], r["currency"], r["channel"]) for r in rows}
    assert len(combos) == 12


@allure.story("sample 行数受 sample_size 控制")
def test_sample_strategy_end_to_end():
    config = load_config_from_text(
        base_config("sample", "  sample_size: 5")
    )
    plan = service.plan(config)
    assert plan.tables[0].planned_rows == 5
    assert "随机抽" in (plan.tables[0].note or "")

    result = service.generate(config)
    assert len(result.tables["t_a"]) == 5


@allure.story("pairwise 行数精简且两两配对仍全覆盖")
def test_pairwise_strategy_end_to_end():
    config = load_config_from_text(base_config("pairwise"))
    plan = service.plan(config)
    assert plan.tables[0].planned_rows < 12

    result = service.generate(config)
    rows = result.tables["t_a"].records
    assert len(rows) < 12

    pairs = {
        (r[a], r[b])
        for a, b in combinations(["status", "currency", "channel"], 2)
        for r in rows
    }
    assert len(pairs) == 4 + 6 + 6  # status×currency(4) / status×channel(6) / currency×channel(6) —— 全配对覆盖


@allure.story("策略逐表生效（1:N 子表同样精简）")
def test_strategy_applies_to_child_tables():
    config = load_config_from_text(
        base_config("sample", "  sample_size: 3")
        + """
  - name: t_b
    groups:
      - {type: enum, name: g_x, fields: [x],
         values: [["X1"], ["X2"], ["X3"], ["X4"]]}
relations:
  - parent: t_a
    child: t_b
    cardinality: "1:N"
    join: [{parent_field: txn_no, child_field: pid}]
"""
    )
    result = service.generate(config)
    # t_a 5 行 × 每父行 3 条子行
    assert len(result.tables["t_b"]) == 9  # 3 父行 × 每父行抽 3


@allure.story("未知策略在 check 阶段报错")
def test_unknown_strategy_is_rejected():
    with pytest.raises((ConfigError, ValueError)):
        load_config_from_text(base_config("bogus"))
