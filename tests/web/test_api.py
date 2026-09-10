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
