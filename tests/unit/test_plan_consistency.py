"""预演与生成的一致性：plan 说多少行，生成就必须是多少行。

这个不变量曾被破坏：无有限取值组的表，空笛卡尔积按数学算是 1，
plan 就用 1 当行数，而生成侧对这类"全为逐行组"的表**用 rows 声明当行数** ——
于是用户看到"预演 1 行、结果 100 行"。本文件把这条不变量钉死。
"""

from __future__ import annotations

from pathlib import Path

import allure
import pytest

from tableseed import service
from tableseed.errors import GenerateError
from tableseed.sink import MemorySink

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = [
    ROOT / "samples" / "account.yaml",
    ROOT / "samples" / "txn.yaml",
    ROOT / "tests" / "fixtures" / "txn.yaml",
]

ROWS_ONLY = """
seed: 20260910
limits:
  max_rows: 100000
  strategy: full
tables:
  - name: t_txn
    rows: 100
    groups:
      - {type: sequence, name: g_no, fields: [txn_no], start: 1, format: "txn_no_{seq:06d}"}
      - {type: const, name: g_type, fields: [txn_type], value: ["Q"]}
      - {type: random, name: g_amt, fields: [amount], generator: decimal, range: [1, 999999], scale: 2}
"""


def _generated_counts(config) -> dict[str, int]:
    result = service.generate(config, sink=MemorySink())
    return {name: len(data) for name, data in result.tables.items()}


@allure.epic("tableseed")
@allure.feature("预演一致性")
@allure.story("全为逐行组的表：行数取 rows 声明（预演 100 = 生成 100）")
def test_rows_only_table_uses_declared_rows():
    config = service.load_text(ROWS_ONLY)
    plan = service.plan(config)
    counts = _generated_counts(config)

    assert plan.tables[0].planned_rows == 100
    assert counts["t_txn"] == 100
    assert plan.total_rows == sum(counts.values())
    assert "rows: 100" in (plan.tables[0].note or "")


@allure.story("全为逐行组且没声明 rows：预演给警告，生成明确报错")
def test_rows_only_without_declaration():
    config = service.load_text(ROWS_ONLY.replace("    rows: 100\n", ""))
    plan = service.plan(config)
    assert plan.tables[0].planned_rows == 0
    assert any("rows" in w for w in plan.warnings)

    with pytest.raises(GenerateError, match="没有声明 rows"):
        service.generate(config, sink=MemorySink())


@allure.story("示例配置：预演行数 == 生成行数（通用不变量）")
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name if hasattr(p, "name") else str(p))
def test_plan_matches_generation_for_samples(path):
    if not Path(path).exists():
        pytest.skip(f"缺少示例配置: {path}")
    config = service.load(path)
    plan = service.plan(config)
    counts = _generated_counts(config)

    assert set(plan.order) == set(counts), "生成顺序与生成的表集合应一致"
    for name, actual in counts.items():
        planned = next(t.planned_rows for t in plan.tables if t.table == name)
        assert planned == actual, f"{name}: 预演 {planned} 行，实际生成 {actual} 行"
    assert plan.total_rows == sum(counts.values())


@allure.story("子表无有限组时每父行 1 行（与生成一致）")
def test_child_without_finite_groups():
    text = """
seed: 1
limits: {max_rows: 100000, strategy: full}
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_s, fields: [s], values: [["01"], ["02"], ["03"]]}
  - name: t_b
    groups:
      - {type: const, name: g_c, fields: [c], value: ["X"]}
relations:
  - parent: t_a
    child: t_b
    cardinality: "1:N"
    join: [{parent_field: s, child_field: s}]
"""
    config = service.load_text(text)
    plan = service.plan(config)
    counts = _generated_counts(config)

    assert counts["t_b"] == 3, "1:N 下子表无有限组 → 每父行 1 行"
    planned_b = next(t.planned_rows for t in plan.tables if t.table == "t_b")
    assert planned_b == counts["t_b"]
