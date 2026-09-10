"""WebUI API 测试 —— 重点覆盖 SQL 只读网关与生成闭环。"""

from __future__ import annotations

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed.web.app import create_app
from tableseed.web.security import SqlRejected, first_keyword, validate_readonly

CONFIG = """
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
"""


@pytest.fixture()
def client():
    return TestClient(create_app())


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("健康检查与配置读写")
def test_health_and_config_roundtrip(client):
    assert client.get("/api/health").json()["ok"] is True

    saved = client.put("/api/config", json={"text": CONFIG})
    assert saved.json()["saved"] is True
    assert client.get("/api/config").json()["text"] == CONFIG


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("配置校验")
def test_validate_endpoint(client):
    ok = client.post("/api/config/validate", json={"text": CONFIG}).json()
    assert ok["ok"] is True

    bad = client.post(
        "/api/config/validate",
        json={"text": CONFIG.replace("fields: [channel]", "fields: [currency]")},
    ).json()
    assert bad["ok"] is False


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("预演")
def test_plan_endpoint(client):
    plan = client.post("/api/plan", json={"text": CONFIG}).json()
    assert plan["total_rows"] == 12
    assert plan["tables"][0]["combo_count"] == 12


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("生成（SSE）")
def test_generate_stream_endpoint(client):
    response = client.post("/api/generate/stream", json={"text": CONFIG})
    assert response.status_code == 200
    body = response.text
    assert "event: progress" in body
    assert "event: done" in body

    result = client.get("/api/result").json()
    assert result["tables"]["t_account"]["count"] == 12


@allure.epic("tableseed")
@allure.feature("SQL 查询台")
@allure.story("只读网关")
@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM t_account",
        "DROP TABLE t_account",
        "UPDATE t_account SET currency = 'USD'",
        "TRUNCATE TABLE t_account",
        "ALTER TABLE t_account ADD COLUMN x INT",
        "INSERT INTO t_account VALUES (1)",
        "GRANT ALL ON t_account TO public",
    ],
)
def test_write_statements_are_rejected(statement):
    with allure.step(f"拒绝写语句: {statement}"):
        with pytest.raises(SqlRejected):
            validate_readonly(statement)


@allure.epic("tableseed")
@allure.feature("SQL 查询台")
@allure.story("只读网关")
@pytest.mark.parametrize(
    "statement",
    ["SELECT 1", "select * from t", "WITH x AS (SELECT 1) SELECT * FROM x",
     "SHOW TABLES", "EXPLAIN SELECT 1", "DESC t_account"],
)
def test_readonly_statements_are_allowed(statement):
    assert validate_readonly(statement) == statement


@allure.epic("tableseed")
@allure.feature("SQL 查询台")
@allure.story("只读网关")
def test_comment_and_multi_statement_bypass_is_blocked():
    """注释伪装与多语句拼接都必须被挡下。"""
    with pytest.raises(SqlRejected):
        validate_readonly("/* SELECT */ DROP TABLE t_account")
    with pytest.raises(SqlRejected):
        validate_readonly("SELECT 1; DROP TABLE t_account")
    assert first_keyword("/*x*/ select 1") == "select"


@allure.epic("tableseed")
@allure.feature("SQL 查询台")
@allure.story("写模式二次确认")
def test_write_mode_requires_confirmation(client):
    write_config = CONFIG + "\nsql:\n  allow_write: true\n"
    client.put("/api/config", json={"text": write_config})

    denied = client.post("/api/sql/execute", json={"sql": "DELETE FROM t_account"})
    assert denied.status_code == 403
    assert "二次确认" in denied.json()["detail"]


@allure.epic("tableseed")
@allure.feature("SQL 查询台")
@allure.story("缺少连接")
def test_sql_without_dsn_is_rejected(client):
    client.put("/api/config", json={"text": CONFIG})
    response = client.post("/api/sql/execute", json={"sql": "SELECT 1"})
    assert response.status_code == 400
    assert "未配置数据库连接" in response.json()["detail"]


# ---------------------------------------------------------------- M2 关系图


TWO_TABLES = """
seed: 20260910
tables:
  - name: t_txn
    groups:
      - {type: enum, name: g_type, fields: [txn_type], values: [["T"], ["D"]]}
      - {type: sequence, name: g_no, fields: [txn_no], format: "TXN{seq:04d}"}
  - name: t_detail
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["S0"], ["S1"], ["S2"]]}
relations:
  - parent: t_txn
    child: t_detail
    cardinality: "1:1"
    drive_by: [txn_type]
    join: [{parent_field: txn_no, child_field: txn_no}]
    propagate:
      - {mode: copy, to: status, from: txn_type}
"""


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("关系图数据")
def test_graph_endpoint_returns_nodes_and_edges(client):
    graph = client.post("/api/graph", json={"text": TWO_TABLES}).json()

    assert graph["order"] == ["t_txn", "t_detail"]
    assert [n["name"] for n in graph["nodes"]] == ["t_txn", "t_detail"]

    by_name = {n["name"]: n for n in graph["nodes"]}
    assert by_name["t_txn"]["layer"] == 0
    assert by_name["t_txn"]["is_root"] is True
    assert by_name["t_detail"]["layer"] == 1
    assert by_name["t_detail"]["is_root"] is False

    edge = graph["edges"][0]
    assert edge["cardinality"] == "1:1"
    assert edge["drive_by"] == ["txn_type"]
    # join 自动补齐的 copy 也要出现在图上
    assert any(r["to"] == "txn_no" for r in edge["propagate"])


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("关系图在成环时仍可渲染")
def test_graph_endpoint_survives_cycle(client):
    cyclic = """
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
    graph = client.post("/api/graph", json={"text": cyclic}).json()
    assert graph["cyclic"]          # 环要被报出来
    assert len(graph["nodes"]) == 2  # 但图仍要画得出来


@allure.epic("tableseed")
@allure.feature("WebUI API")
@allure.story("SSE 生成必须经过关系内核")
def test_generate_stream_applies_relations(client):
    """回归：SSE 曾绕过 generate_all，逐表生成导致子表关系全部丢失。"""
    response = client.post("/api/generate/stream", json={"text": TWO_TABLES})
    assert response.status_code == 200

    result = client.get("/api/result").json()
    assert result["tables"]["t_txn"]["count"] == 2
    assert result["tables"]["t_detail"]["count"] == 2  # 1:1 不放大

    # join 自动 copy：子行主键必须能在父表中找到
    parent_keys = {r["txn_no"] for r in result["tables"]["t_txn"]["rows"]}
    for row in result["tables"]["t_detail"]["rows"]:
        assert row["txn_no"] in parent_keys
