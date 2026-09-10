"""端到端验收 —— 对齐 docs/PRD.md 第 7 节的 AC-1 ~ AC-13。

基准配置：samples/account.yaml（12 行全组合）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import allure
import pytest

from tableseed import service
from tableseed.render import render_sql

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "account.yaml"


@pytest.fixture(scope="module")
def config():
    return service.load(SAMPLE)


@pytest.fixture(scope="module")
def result(config):
    return service.generate(config)


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-1 配置校验通过")
def test_ac1_check_passes(config):
    assert service.check(config) == []


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-2 预演组合数 12")
def test_ac2_plan_reports_twelve(config):
    plan = service.plan(config)
    assert plan.total_rows == 12
    assert plan.tables[0].combo_count == 12
    assert plan.within_limits is True


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-3 生成 12 行")
def test_ac3_generates_twelve_rows(result):
    assert len(result.tables["t_account"]) == 12


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-4 组合无重复无遗漏")
def test_ac4_combinations_match_theory(result):
    rows = result.tables["t_account"].rows
    actual = {(r.values["status_code"], r.values["currency"], r.values["channel"]) for r in rows}
    expected = {
        (s, c, ch)
        for s in ("01", "02")
        for c in ("CNY", "USD")
        for ch in ("OTC", "EBANK", "MOBILE")
    }
    assert len(actual) == 12, "组合不得重复"
    assert actual == expected, "组合不得遗漏"


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-5 组内字段严格配对")
def test_ac5_status_code_pairs_with_desc(result):
    pairs = {"01": "正常", "02": "冻结"}
    for row in result.tables["t_account"].rows:
        assert pairs[row.values["status_code"]] == row.values["status_desc"]


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-6 附着组规则正确")
def test_ac6_attach_group_values(result):
    rows = result.tables["t_account"].rows
    assert [r.values["acct_no"] for r in rows] == [
        f"6222{seq:012d}" for seq in range(1, 13)
    ]
    assert {r.values["tenant_id"] for r in rows} == {"0001"}
    assert {r.values["branch_code"] for r in rows} == {"001"}
    for row in rows:
        assert 0 <= row.values["balance"] <= 1_000_000
        assert row.values["amount"] == pytest.approx(row.values["balance"] * 0.01, rel=1e-9)


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-7 同 seed 输出字节级一致")
def test_ac7_reproducible_output(config):
    first = service.generate(config).tables["t_account"].to_sql("postgresql")
    second = service.generate(config).tables["t_account"].to_sql("postgresql")
    assert first == second


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-9 生成的 SQL 可执行")
def test_ac9_generated_sql_is_executable(result):
    data = result.tables["t_account"]
    statements = render_sql(data, dialect="postgresql")

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            'CREATE TABLE "t_account" ('
            '"acct_no" TEXT, "status_code" TEXT, "status_desc" TEXT, "currency" TEXT, '
            '"channel" TEXT, "balance" INTEGER, "amount" REAL, '
            '"tenant_id" TEXT, "branch_code" TEXT)'
        )
        conn.executescript(statements)
        count = conn.execute('SELECT COUNT(*) FROM "t_account"').fetchone()[0]
        assert count == 12
    finally:
        conn.close()


@allure.epic("tableseed")
@allure.feature("端到端验收")
@allure.story("AC-13 无数据库连接时不产生磁盘副作用")
def test_ac13_no_disk_side_effect(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())

    service.generate(config)  # 不带 out_dir / dsn → 内存模式

    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after, "内存模式下不得写入任何文件"
