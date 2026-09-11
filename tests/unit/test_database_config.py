"""数据库连接结构化配置测试：URL 拼接、YAML 存取、段替换不伤其他内容。"""

from __future__ import annotations

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed.models import DatabaseSpec
from tableseed.web.app import _replace_yaml_section, create_app

CONFIG = """
# 顶部注释不该被写连接信息时丢掉
seed: 20260910

tables:
  - name: t_a
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["01"], ["02"]]}
"""


# ---------------------------------------------------------------- URL 拼接


@allure.epic("tableseed")
@allure.feature("数据库连接")
@allure.story("结构化字段拼出连接串")
def test_resolved_url_from_fields():
    spec = DatabaseSpec(
        type="mysql", host="10.0.0.5", port=3307, user="root", password="pw", database="db1"
    )
    assert spec.resolved_url() == "mysql+pymysql://root:pw@10.0.0.5:3307/db1?charset=utf8mb4"


@allure.story("密码特殊字符必须 URL 编码")
def test_password_is_url_encoded():
    spec = DatabaseSpec(
        type="mysql", host="h", user="u", password="p@ss:w/ord#1", database="d"
    )
    url = spec.resolved_url()
    assert "p%40ss%3A" in url
    assert "@h:3306" in url  # @ 没有破坏 host 段


@allure.story("各类型默认端口")
@pytest.mark.parametrize(
    "kind,port",
    [("mysql", 3306), ("postgresql", 5432), ("oracle", 1521)],
)
def test_default_ports(kind, port):
    spec = DatabaseSpec(type=kind, host="h", user="u", database="d")
    assert f":{port}" in spec.resolved_url()


@allure.story("url 优先于结构化字段")
def test_url_takes_precedence():
    spec = DatabaseSpec(
        url="mysql+pymysql://a:b@x:3306/y",
        type="mysql", host="ignored", user="u", database="d",
    )
    assert spec.resolved_url() == "mysql+pymysql://a:b@x:3306/y"


@allure.story("describe 脱敏")
def test_describe_masks_password():
    spec = DatabaseSpec(url="mysql+pymysql://root:secret@h:3306/d")
    assert "secret" not in spec.describe()
    assert "***" in spec.describe()


@allure.story("信息不全时拼不出连接串")
def test_incomplete_spec_returns_none():
    assert DatabaseSpec(host="h").resolved_url() is None
    assert DatabaseSpec().describe() == "未配置"


# ---------------------------------------------------------------- 密码走环境变量


@allure.feature("数据库连接")
@allure.story("密码取自环境变量，不进 YAML")
def test_password_from_env(monkeypatch):
    monkeypatch.setenv("TABLESEED_TEST_PWD", "s3cr3t")
    spec = DatabaseSpec(
        type="mysql", host="h", user="root",
        password_env="TABLESEED_TEST_PWD", database="d",
    )
    assert spec.resolved_password() == "s3cr3t"
    assert "s3cr3t" in spec.resolved_url()
    assert spec.password_source == "env:TABLESEED_TEST_PWD"
    assert spec.env_problem() is None


@allure.story("环境变量优先于配置里的明文密码")
def test_env_password_wins(monkeypatch):
    monkeypatch.setenv("TABLESEED_TEST_PWD", "from-env")
    spec = DatabaseSpec(
        type="mysql", host="h", user="root", password="from-yaml",
        password_env="TABLESEED_TEST_PWD", database="d",
    )
    assert spec.resolved_password() == "from-env"


@allure.story("环境变量名写了但变量不存在 → 明确报错")
def test_missing_env_is_reported(monkeypatch):
    monkeypatch.delenv("TABLESEED_ABSENT_PWD", raising=False)
    spec = DatabaseSpec(
        type="mysql", host="h", user="root",
        password_env="TABLESEED_ABSENT_PWD", database="d",
    )
    problem = spec.env_problem()
    assert problem and "TABLESEED_ABSENT_PWD" in problem
    assert "未设置" in problem


@allure.story("密码来源标注在脱敏串上")
def test_source_is_annotated(monkeypatch):
    monkeypatch.setenv("TABLESEED_TEST_PWD", "x")
    spec = DatabaseSpec(
        type="mysql", host="h", user="root",
        password_env="TABLESEED_TEST_PWD", database="d",
    )
    described = spec.describe()
    assert "***" in described and "x" not in described.replace("env:", "")
    assert "env:TABLESEED_TEST_PWD" in described


@allure.feature("数据库连接")
@allure.story("保存 password_env 到 YAML，不落明文")
def test_save_password_env_to_yaml():
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})

    saved = client.put(
        "/api/database",
        json={
            "type": "mysql", "host": "127.0.0.1", "port": 3306, "user": "root",
            "password_env": "TABLESEED_DB_PASSWORD", "database": "testdb",
        },
    ).json()
    assert "password_env: TABLESEED_DB_PASSWORD" in saved["text"]
    assert "password:" not in saved["text"]          # 不写明文键

    loaded = client.get("/api/database").json()
    assert loaded["password_env"] == "TABLESEED_DB_PASSWORD"


@allure.story("环境变量缺失时查询给出可操作提示")
def test_query_reports_missing_env(monkeypatch):
    monkeypatch.delenv("TABLESEED_MISSING_PWD", raising=False)
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    client.put(
        "/api/database",
        json={
            "type": "mysql", "host": "127.0.0.1", "port": 3306, "user": "root",
            "password_env": "TABLESEED_MISSING_PWD", "database": "testdb",
        },
    )

    res = client.post("/api/sql/execute", json={"sql": "SELECT 1"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "TABLESEED_MISSING_PWD" in detail
    assert "setx" in detail                          # 给出怎么设的指引


# ---------------------------------------------------------------- YAML 段替换


@allure.feature("数据库连接")
@allure.story("替换 database 段不伤其他内容")
def test_replace_section_keeps_rest_intact():
    text = "# 注释\nseed: 1\ndatabase:\n  url: old\ninvariants: []\n"
    result = _replace_yaml_section(text, "database", "database:\n  type: mysql")
    assert "# 注释" in result
    assert "url: old" not in result
    assert "type: mysql" in result
    assert "invariants: []" in result


@allure.story("没有该段时追加到末尾")
def test_replace_section_appends_when_absent():
    text = "seed: 1\ntables: []\n"
    result = _replace_yaml_section(text, "database", "database:\n  type: mysql")
    assert result.strip().endswith("type: mysql")
    assert result.index("tables") < result.index("database")


@allure.story("block 为 None 时删除该段")
def test_replace_section_removes():
    text = "seed: 1\ndatabase:\n  url: x\ninvariants: []\n"
    result = _replace_yaml_section(text, "database", None)
    assert "database" not in result
    assert "invariants" in result


# ---------------------------------------------------------------- 接口


@allure.feature("数据库连接")
@allure.story("保存与读取往返")
def test_database_endpoints_roundtrip():
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})

    assert client.get("/api/database").json()["configured"] is False

    saved = client.put(
        "/api/database",
        json={
            "type": "mysql",
            "host": "127.0.0.1",
            "port": 3307,
            "user": "root",
            "password": "pw",
            "database": "seedtest",
        },
    ).json()
    assert "database:" in saved["text"]
    assert "# 顶部注释不该被写连接信息时丢掉" in saved["text"]  # 注释保住

    loaded = client.get("/api/database").json()
    assert loaded["configured"] is True
    assert (loaded["host"], loaded["port"], loaded["database"]) == (
        "127.0.0.1", 3307, "seedtest",
    )
    assert "pw" not in loaded["masked"]  # 脱敏


@allure.story("保存后配置仍合法")
def test_saved_config_stays_valid():
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    client.put("/api/database", json={"type": "mysql", "host": "h", "user": "u", "database": "d"})

    text = client.get("/api/config").json()["text"]
    valid = client.post("/api/config/validate", json={"text": text}).json()
    assert valid["ok"] is True


@allure.story("未配置连接时查询给出明确提示")
def test_execute_without_connection_is_rejected():
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    res = client.post("/api/sql/execute", json={"sql": "SELECT 1"})
    assert res.status_code == 400
    assert "未配置数据库连接" in res.json()["detail"]


@allure.story("页面传的结构化连接优先于配置")
def test_payload_database_takes_precedence():
    """页面填了连接就用页面的（即使配置里也存了一份）。"""
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    client.put("/api/database", json={"type": "mysql", "host": "cfg-host", "user": "u", "database": "d"})

    # 页面传的连接指向不存在的端口 → 报连接错误而非使用配置里的
    res = client.post(
        "/api/sql/execute",
        json={
            "sql": "SELECT 1",
            "database": {"type": "mysql", "host": "127.0.0.1", "port": 59999,
                         "user": "u", "database": "d"},
        },
    )
    assert res.status_code == 400
    assert "cfg-host" not in str(res.json())
