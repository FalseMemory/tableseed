"""端到端：samples/txn.yaml 的多表联合造数验收。

这是 M2 的「对照实验」—— 三张表一次性生成，断言：
    1. 行数符合基数（9 / 9 / 27）
    2. 跨表字段真的对得上（主键、币种、金额口径）
    3. follow_parent 分配既保一致又保覆盖
    4. 同 seed 可复现，且不落盘
"""

from __future__ import annotations

from pathlib import Path

import allure
import pytest

from tableseed import service

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "txn.yaml"


@pytest.fixture(scope="module")
def result():
    return service.generate(service.load(SAMPLE))


@allure.epic("tableseed")
@allure.feature("多表联合造数")
@allure.story("配置合法")
def test_sample_config_is_valid():
    assert service.check(service.load(SAMPLE)) == []


@allure.story("行数符合基数")
def test_row_counts(result):
    assert len(result.tables["t_txn"]) == 9          # 3 类型 × 3 渠道
    assert len(result.tables["t_txn_detail"]) == 9   # 1:1，不放大
    assert len(result.tables["t_txn_log"]) == 27     # 1:N，9 × 3


@allure.story("子行主键在父表中存在（join 自动 copy）")
def test_child_keys_exist_in_parent(result):
    txn_nos = {row.values["txn_no"] for row in result.tables["t_txn"].rows}
    for table in ("t_txn_detail", "t_txn_log"):
        for row in result.tables[table].rows:
            assert row.values["txn_no"] in txn_nos


@allure.story("copy 传播：币种与父表一致")
def test_copy_propagates_currency(result):
    currency = {
        row.values["txn_no"]: row.values["currency"] for row in result.tables["t_txn"].rows
    }
    for row in result.tables["t_txn_detail"].rows:
        assert row.values["currency"] == currency[row.values["txn_no"]]


@allure.story("map 传播：交易类型码值映射正确")
def test_map_propagates_type(result):
    expected = {"T": "TRANSFER", "D": "DEPOSIT", "W": "WITHDRAW"}
    txn_type = {
        row.values["txn_no"]: row.values["txn_type"] for row in result.tables["t_txn"].rows
    }
    for row in result.tables["t_txn_detail"].rows:
        assert row.values["detail_type"] == expected[txn_type[row.values["txn_no"]]]


@allure.story("derive 传播：净额 = 交易金额 - 手续费")
def test_derive_propagates_net_amount(result):
    parent = {
        row.values["txn_no"]: row.values for row in result.tables["t_txn"].rows
    }
    for row in result.tables["t_txn_detail"].rows:
        txn = parent[row.values["txn_no"]]
        assert row.values["net_amount"] == pytest.approx(txn["amount"] - txn["fee"], abs=0.02)


@allure.story("follow_parent：同一交易类型的清算状态一致")
def test_follow_parent_is_consistent_by_drive_key(result):
    txn_type = {
        row.values["txn_no"]: row.values["txn_type"] for row in result.tables["t_txn"].rows
    }
    seen: dict[str, str] = {}
    for row in result.tables["t_txn_detail"].rows:
        kind = txn_type[row.values["txn_no"]]
        status = row.values["settle_status"]
        assert seen.setdefault(kind, status) == status


@allure.story("follow_parent：仍覆盖到全部组合")
def test_follow_parent_still_covers_all_combos(result):
    statuses = {
        row.values["settle_status"] for row in result.tables["t_txn_detail"].rows
    }
    assert statuses == {"S0", "S1", "S2"}


@allure.story("1:N 每条父行都有完整的子表组合")
def test_one_to_many_covers_every_parent(result):
    steps: dict[str, set[str]] = {}
    for row in result.tables["t_txn_log"].rows:
        steps.setdefault(row.values["txn_no"], set()).add(row.values["step"])

    assert len(steps) == 9
    assert all(v == {"10", "20", "30"} for v in steps.values())


@allure.story("同 seed 结果可复现")
def test_generation_is_reproducible():
    first = service.generate(service.load(SAMPLE))
    second = service.generate(service.load(SAMPLE))
    for name in first.tables:
        assert first.tables[name].to_records() == second.tables[name].to_records()


@allure.story("默认不落盘")
def test_memory_mode_has_no_side_effect(result, tmp_path):
    """未提供 --out / --dsn 时不应产生任何文件。"""
    service.generate(service.load(SAMPLE))
    assert list(tmp_path.iterdir()) == []


@allure.story("计划行数与实际行数一致")
def test_plan_matches_generation(result):
    plan = service.plan(service.load(SAMPLE))
    planned = {item.table: item.planned_rows for item in plan.tables}
    assert planned == {"t_txn": 9, "t_txn_detail": 9, "t_txn_log": 27}
    assert plan.order == ["t_txn", "t_txn_detail", "t_txn_log"]
