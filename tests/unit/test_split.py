"""M4 split 拆分测试：割点法守恒、ratio 占比、行数语义、checker 校验。"""

from __future__ import annotations

import allure
import pytest

from tableseed import service
from tableseed.config import load_config_from_text
from tableseed.engine.splitter import split_value
from tableseed.errors import GenerateError
from tableseed.rng import SeededRandom

SPLIT_BASE = """
seed: 20260910
limits: {max_rows: 10000}
tables:
  - name: t_txn
    columns: [{name: txn_no}, {name: amount}, {name: total_paid}]
    groups:
      - {type: sequence, name: g_no, fields: [txn_no], start: 1, format: "T{seq:03d}"}
      - {type: enum, name: g_amt, fields: [amount], values: [[1000], [500]]}
      - {type: aggregate, name: g_paid, fields: [total_paid], from: t_detail,
         expr: "sum(paid)"}
  - name: t_detail
    columns: [{name: pid}, {name: paid}, {name: memo}]
    groups:
      - {type: const, name: g_c, fields: [memo], value: ["x"]}
relations:
  - parent: t_txn
    child: t_detail
    cardinality: "1:N"
    join: [{parent_field: txn_no, child_field: pid}]
    propagate:
      - mode: split
        to: paid
        from: amount
        parts: 3
"""

SPLIT_CONFIG = SPLIT_BASE + """invariants:
  - table: t_txn
    from: t_detail
    expr: "sum(paid) = amount"
"""

NO_INVARIANTS = SPLIT_BASE + "invariants: []\n"


def build(extra: str = ""):
    return load_config_from_text(SPLIT_CONFIG + extra)


# ---------------------------------------------------------------- splitter 单元


@allure.epic("tableseed")
@allure.feature("split 拆分")
@allure.story("割点法严格守恒")
@pytest.mark.parametrize(
    "total,parts", [(1000, 3), (100.0, 7), (0.99, 2), (1, 1), (999999.99, 50)]
)
def test_split_conserves_total(total, parts):
    pieces = split_value(total, parts, SeededRandom(1))
    assert len(pieces) == parts
    assert sum(pieces) == pytest.approx(total, abs=1e-9)
    assert all(p > 0 for p in pieces)


@allure.story("同 seed 可复现")
def test_split_is_reproducible():
    a = split_value(1000, 5, SeededRandom(9))
    b = split_value(1000, 5, SeededRandom(9))
    assert a == b


@allure.story("ratio 占比模式守恒")
def test_split_with_ratio():
    pieces = split_value(1000, 3, SeededRandom(1), ratio=[0.5, 0.3, 0.2])
    assert pieces == [500.0, 300.0, 200.0]


@allure.story("ratio 与 parts 不一致时报错")
def test_split_ratio_length_mismatch():
    with pytest.raises(GenerateError, match="不一致"):
        split_value(1000, 4, SeededRandom(1), ratio=[0.5, 0.3, 0.2])


@allure.story("拆不出正数份时报错")
def test_split_too_small_raises():
    with pytest.raises(GenerateError, match="不足最小单位"):
        split_value(0.01, 3, SeededRandom(1))


# ---------------------------------------------------------------- 端到端


@allure.feature("split 拆分")
@allure.story("每父行恰好 parts 行")
def test_split_row_count():
    config = build()
    assert service.check(config) == []
    result = service.generate(config)
    # 2 父行 × 3 份
    assert len(result.tables["t_detail"]) == 6


@allure.story("Σ子 = 父 严格守恒（含不变量验证）")
def test_split_conserved_end_to_end():
    config = build()
    result = service.generate(config)
    paid: dict[str, float] = {}
    for row in result.tables["t_detail"].rows:
        paid[row.values["pid"]] = paid.get(row.values["pid"], 0) + row.values["paid"]

    amounts = {r.values["txn_no"]: r.values["amount"] for r in result.tables["t_txn"].rows}
    for txn_no, total in paid.items():
        assert total == pytest.approx(amounts[txn_no], abs=1e-9)

    # 生成后自检：Σ守恒作为不变量必须通过
    assert service.verify(config) == []


@allure.story("split 金额与父值不同但守恒（非均分）")
def test_split_pieces_differ():
    config = build()
    result = service.generate(config)
    pieces = [r.values["paid"] for r in result.tables["t_detail"].rows]
    # 割点法是随机拆分 —— 3 份通常不全相等
    assert len(set(pieces)) > 1


# ---------------------------------------------------------------- checker


@allure.feature("split 拆分")
@allure.story("parts 与 ratio 都缺时报错")
def test_split_requires_parts_or_ratio():
    config = load_config_from_text(
        NO_INVARIANTS.replace("        parts: 3\n", "")
    )
    problems = service.check(config)
    assert any("parts（份数）或 ratio" in p for p in problems)


@allure.story("ratio 之和不为 1 报错")
def test_split_ratio_must_sum_to_one():
    config = load_config_from_text(
        SPLIT_CONFIG.replace("parts: 3", "ratio: [0.5, 0.3, 0.3]")
    )
    problems = service.check(config)
    assert any("ratio 之和应为 1" in p for p in problems)


@allure.story("子表有有限组时与 split 冲突")
def test_split_conflicts_with_finite_group():
    config = load_config_from_text(
        SPLIT_CONFIG.replace(
            '- {type: const, name: g_c, fields: [memo], value: ["x"]}',
            '- {type: enum, name: g_m, fields: [memo], values: [["x"], ["y"]]}',
        )
    )
    problems = service.check(config)
    assert any("行数由份数决定" in p for p in problems)


@allure.story("1:1 下拆多份报错")
def test_split_one_to_one_rejected():
    config = load_config_from_text(
        SPLIT_CONFIG.replace('cardinality: "1:N"', 'cardinality: "1:1"')
    )
    problems = service.check(config)
    assert any("无法拆成 3 份" in p for p in problems)
