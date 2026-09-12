"""limits.total_rows 总行数上限测试。"""

from __future__ import annotations

import allure
import pytest

from tableseed import service
from tableseed.errors import GenerateError

CONFIG = """
seed: 1
limits:
  max_rows: 100000
  total_rows: 50
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_s, fields: [s], values: [["01"], ["02"], ["03"]]}
      - {type: random, name: g_n, fields: [n], generator: int, range: [0, 10]}
"""


@allure.epic("tableseed")
@allure.feature("规模治理")
@allure.story("总行数超上限时拒绝生成")
def test_total_rows_limit_rejects():
    config = service.load_text(CONFIG)   # 3 组合 <= max_rows，但 total_rows=50 会拦
    # 3 行 < 50 —— 不触发
    result = service.generate(config)
    assert len(result.tables["t_a"]) == 3

    over = service.load_text(CONFIG.replace("total_rows: 50", "total_rows: 2"))
    with pytest.raises(GenerateError, match="总行数 3 超过上限 2"):
        service.generate(over)


@allure.story("默认上限 10 万行")
def test_default_total_rows():
    from tableseed.models import LimitsSpec
    assert LimitsSpec().total_rows == 100_000


@allure.story("错误信息里给出明细与建议")
def test_error_has_breakdown():
    over = service.load_text(CONFIG.replace("total_rows: 50", "total_rows: 2"))
    with pytest.raises(GenerateError) as exc:
        service.generate(over)
    assert "t_a 3" in str(exc.value)
    assert "limits.total_rows" in str(exc.value)
