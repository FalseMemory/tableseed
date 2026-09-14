"""生成 INSERT 语句（离线场景）：不依赖数据库连接，产出可复制的 SQL。

场景：目标库不允许直连（生产库只走工单/第三方平台）时，
造数工具的产出必须能落成文本 SQL 交给用户自行执行。
"""

from __future__ import annotations

import allure
from fastapi.testclient import TestClient

from tableseed.web.app import create_app

CFG = """
seed: 20260910
limits:
  max_rows: 100000
  strategy: full
tables:
  - name: t_txn
    groups:
      - {type: enum, name: g_type, fields: [txn_type], values: [["D"], ["W"]]}
      - {type: const, name: g_cur, fields: [currency], value: ["CNY"]}
      - {type: sequence, name: g_no, fields: [txn_no], start: 1, format: "T{seq:04d}"}
"""


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return TestClient(create_app())


@allure.epic("tableseed")
@allure.feature("生成 INSERT 语句")
@allure.story("无数据库连接也能产出完整 SQL")
def test_render_insert_sql_without_connection(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.put("/api/config", json={"text": CFG})
    client.post("/api/generate", json={"text": CFG})

    res = client.post("/api/insert/sql", json={"dialect": "mysql"})
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["ok"] is True
    assert body["dialect"] == "mysql"
    assert body["total_rows"] == 2                      # 2 个枚举取值 → 2 行
    assert "INSERT INTO `t_txn`" in body["sql"]         # mysql 用反引号
    assert "'D'" in body["sql"] and "'W'" in body["sql"]
    assert "'CNY'" in body["sql"]
    assert "T0001" in body["sql"]                       # sequence 已渲染
    assert "seed=20260910" in body["sql"]               # 头部注释带可复现信息


@allure.story("方言切换：标识符引用与字面量风格不同")
def test_dialect_variants(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.put("/api/config", json={"text": CFG})
    client.post("/api/generate", json={"text": CFG})

    pg = client.post("/api/insert/sql", json={"dialect": "postgresql"}).json()
    assert 'INSERT INTO "t_txn"' in pg["sql"]           # pg 用双引号
    assert pg["batch_size"] == 500

    ora = client.post("/api/insert/sql", json={"dialect": "oracle"}).json()
    assert ora["batch_size"] == 100                     # oracle 默认更小批量


@allure.story("批量大小可控，且被限制在合理范围")
def test_batch_size(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.put("/api/config", json={"text": CFG})
    client.post("/api/generate", json={"text": CFG})

    one = client.post("/api/insert/sql", json={"dialect": "mysql", "batch_size": 1}).json()
    # 2 行 + 每行一条 INSERT → 出现两条 INSERT 语句
    assert one["sql"].count("INSERT INTO") == 2

    huge = client.post("/api/insert/sql", json={"dialect": "mysql", "batch_size": 999999}).json()
    assert huge["batch_size"] == 5000                   # 上限保护


@allure.story("没有生成结果时明确报错（而不是给空 SQL）")
def test_requires_generated_data(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.post("/api/insert/sql", json={})
    assert res.status_code == 400
    assert "生成" in res.json()["detail"]


@allure.story("多表按依赖顺序输出（父表在前）")
def test_multi_table_order(tmp_path, monkeypatch):
    cfg = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_parent
    groups:
      - {type: enum, name: g_s, fields: [s], values: [["01"], ["02"]]}
  - name: t_child
    groups:
      - {type: const, name: g_c, fields: [c], value: ["X"]}
relations:
  - parent: t_parent
    child: t_child
    cardinality: "1:N"
    join: [{parent_field: s, child_field: c}]
"""
    client = _client(tmp_path, monkeypatch)
    client.put("/api/config", json={"text": cfg})
    client.post("/api/generate", json={"text": cfg})

    sql = client.post("/api/insert/sql", json={"dialect": "mysql"}).json()["sql"]
    assert sql.index("t_parent") < sql.index("t_child"), "父表必须先于子表输出"
