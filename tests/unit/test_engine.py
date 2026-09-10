"""生成引擎测试：笛卡尔积展开、组内同步、附着组、可复现性。"""

from __future__ import annotations

import allure
import pytest

from tableseed.config import load_config_from_text
from tableseed.engine import combo_count, expand_skeleton, finite_groups, generate_table
from tableseed.rng import SeededRandom

BASE = """
seed: 20260910
tables:
  - name: t_account
    groups:
      - {type: enum, name: g_status, fields: [status_code, status_desc],
         values: [["01", "正常"], ["02", "冻结"]]}
      - {type: enum, name: g_currency, fields: [currency], values: [["CNY"], ["USD"]]}
      - {type: enum, name: g_channel, fields: [channel],
         values: [["OTC"], ["EBANK"], ["MOBILE"]]}
      - {type: sequence, name: g_acct_no, fields: [acct_no], format: "6222{seq:012d}"}
      - {type: random, name: g_balance, fields: [balance],
         generator: int, range: [0, 1000]}
      - {type: const, name: g_tenant, fields: [tenant_id, branch_code],
         value: ["0001", "001"]}
      - {type: derive, name: g_amount, fields: [amount], expr: "balance * 0.01"}
"""


def build(text: str = BASE):
    config = load_config_from_text(text)
    return config, config.tables[0]


@allure.epic("tableseed")
@allure.feature("字段分组")
@allure.story("笛卡尔积展开")
def test_cartesian_product_is_complete():
    config, table = build()
    finite = finite_groups(table)
    assert combo_count(finite) == 12

    combos = list(expand_skeleton(finite))
    assert len(combos) == 12

    signatures = {
        (c["status_code"], c["currency"], c["channel"]) for c in combos
    }
    assert len(signatures) == 12, "12 个组合必须互不相同"
    expected = {
        (s, cur, ch)
        for s in ("01", "02")
        for cur in ("CNY", "USD")
        for ch in ("OTC", "EBANK", "MOBILE")
    }
    assert signatures == expected, "组合集合必须与理论集合完全一致（无遗漏）"


@allure.epic("tableseed")
@allure.feature("字段分组")
@allure.story("组内字段同步")
def test_group_fields_stay_in_sync():
    config, table = build()
    data = generate_table(config, table, SeededRandom(config.seed))

    mapping = {"01": "正常", "02": "冻结"}
    for row in data.rows:
        assert mapping[row.values["status_code"]] == row.values["status_desc"]


@allure.epic("tableseed")
@allure.feature("生成引擎")
@allure.story("附着组不放大行数")
def test_attach_groups_do_not_multiply_rows():
    config, table = build()
    data = generate_table(config, table, SeededRandom(config.seed))
    assert len(data) == 12

    indices = [row.values["acct_no"] for row in data.rows]
    assert indices == [f"6222{seq:012d}" for seq in range(1, 13)]
    assert {row.values["tenant_id"] for row in data.rows} == {"0001"}

    for row in data.rows:
        assert 0 <= row.values["balance"] <= 1000
        assert row.values["amount"] == pytest.approx(row.values["balance"] * 0.01)


@allure.epic("tableseed")
@allure.feature("生成引擎")
@allure.story("可复现性")
def test_same_seed_same_data():
    config, table = build()
    first = generate_table(config, table, SeededRandom(config.seed))
    second = generate_table(config, table, SeededRandom(config.seed))
    assert first.records == second.records


@allure.epic("tableseed")
@allure.feature("生成引擎")
@allure.story("非法组合剔除")
def test_exclude_filters_combinations():
    text = BASE.replace(
        "seed: 20260910",
        'seed: 20260910\nlimits:\n  exclude:\n    - "status_code == \'02\' and channel == \'OTC\'"',
    )
    config, table = build(text)
    data = generate_table(config, table, SeededRandom(config.seed))

    # status_code=02 且 channel=OTC 的组合有 2 个（CNY / USD），12 - 2 = 10
    assert len(data) == 10
    assert not any(
        row.values["status_code"] == "02" and row.values["channel"] == "OTC"
        for row in data.rows
    )


@allure.epic("tableseed")
@allure.feature("生成引擎")
@allure.story("列顺序稳定")
def test_columns_follow_declaration_order():
    config, table = build()
    data = generate_table(config, table, SeededRandom(config.seed))
    assert data.columns == [
        "status_code",
        "status_desc",
        "currency",
        "channel",
        "acct_no",
        "balance",
        "tenant_id",
        "branch_code",
        "amount",
    ]
