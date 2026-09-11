"""数据库连接测试：config.ini 多连接、URL 拼接、旧 YAML database 段兼容。"""

from __future__ import annotations

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed.config.connections import ConnectionsStore
from tableseed.models import DatabaseSpec
from tableseed.web.app import create_app

CONFIG = """
# 顶部注释不该被动到
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
    spec = DatabaseSpec(type="mysql", host="h", user="u", password="p@ss:w/ord#1", database="d")
    url = spec.resolved_url()
    assert "p%40ss%3A" in url and "@h:3306" in url


@allure.story("各类型默认端口")
@pytest.mark.parametrize("kind,port", [("mysql", 3306), ("postgresql", 5432), ("oracle", 1521)])
def test_default_ports(kind, port):
    spec = DatabaseSpec(type=kind, host="h", user="u", database="d")
    assert f":{port}" in spec.resolved_url()


@allure.story("url 优先于结构化字段")
def test_url_takes_precedence():
    spec = DatabaseSpec(url="mysql+pymysql://a:b@x:3306/y",
                        type="mysql", host="ignored", user="u", database="d")
    assert spec.resolved_url() == "mysql+pymysql://a:b@x:3306/y"


@allure.story("describe 脱敏")
def test_describe_masks_password():
    spec = DatabaseSpec(url="mysql+pymysql://root:secret@h:3306/d")
    assert "secret" not in spec.describe() and "***" in spec.describe()


@allure.story("信息不全时拼不出连接串")
def test_incomplete_spec_returns_none():
    assert DatabaseSpec(host="h").resolved_url() is None
    assert DatabaseSpec().describe() == "未配置"


# ---------------------------------------------------------------- 密码环境变量


@allure.feature("数据库连接")
@allure.story("密码取自环境变量")
def test_password_from_env(monkeypatch):
    monkeypatch.setenv("TABLESEED_TEST_PWD", "s3cr3t")
    spec = DatabaseSpec(type="mysql", host="h", user="root",
                        password_env="TABLESEED_TEST_PWD", database="d")
    assert spec.resolved_password() == "s3cr3t"
    assert spec.password_source == "env:TABLESEED_TEST_PWD"
    assert spec.env_problem() is None


@allure.story("环境变量优先于明文密码")
def test_env_password_wins(monkeypatch):
    monkeypatch.setenv("TABLESEED_TEST_PWD", "from-env")
    spec = DatabaseSpec(type="mysql", host="h", user="root", password="from-yaml",
                        password_env="TABLESEED_TEST_PWD", database="d")
    assert spec.resolved_password() == "from-env"


@allure.story("环境变量缺失时明确报出")
def test_missing_env_is_reported(monkeypatch):
    monkeypatch.delenv("TABLESEED_ABSENT_PWD", raising=False)
    spec = DatabaseSpec(type="mysql", host="h", user="root",
                        password_env="TABLESEED_ABSENT_PWD", database="d")
    problem = spec.env_problem()
    assert problem and "TABLESEED_ABSENT_PWD" in problem and "setx" in problem


# ---------------------------------------------------------------- config.ini 多连接


@allure.feature("数据库连接")
@allure.story("多连接保存 / 切换 / 删除")
def test_connections_store(tmp_path):
    store = ConnectionsStore(tmp_path / "config.ini")
    store.save("demo", {"type": "mysql", "host": "127.0.0.1", "user": "root",
                        "password_env": "TABLESEED_DB_PASSWORD", "database": "tableseed_demo"})
    store.save("prod", {"type": "mysql", "host": "10.0.0.5", "user": "root", "database": "prod"})
    store.set_active("prod")

    data = store.load()
    assert data["active"] == "prod"
    assert set(data["connections"]) == {"demo", "prod"}
    assert store.active_spec().host == "10.0.0.5"

    store.delete("prod")
    assert store.load()["active"] == "demo"   # 删除激活连接后回落
    assert store.active_spec().host == "127.0.0.1"


@allure.story("非法连接名被拒")
def test_invalid_name_rejected(tmp_path):
    from tableseed.errors import TableSeedError

    store = ConnectionsStore(tmp_path / "config.ini")
    with pytest.raises(TableSeedError, match="不合法"):
        store.save("bad[name]", {"host": "h", "user": "u", "database": "d"})


@allure.story("第一个保存的连接自动成为当前连接")
def test_first_connection_becomes_active(tmp_path):
    store = ConnectionsStore(tmp_path / "config.ini")
    store.save("only", {"type": "mysql", "host": "h", "user": "u", "database": "d"})
    assert store.active_name() == "only"


# ---------------------------------------------------------------- 接口


@allure.feature("数据库连接")
@allure.story("连接接口：保存 / 列出 / 切换 / 删除")
def test_connections_endpoints(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)  # config.ini 落在临时目录
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})

    assert client.get("/api/connections").json()["connections"] == {}

    client.put("/api/connections", json={
        "name": "demo", "type": "mysql", "host": "127.0.0.1", "port": 3306,
        "user": "root", "password_env": "TABLESEED_DB_PASSWORD", "database": "tableseed_demo",
    })
    client.post("/api/connections/active", json={"name": "demo"})

    data = client.get("/api/connections").json()
    assert data["active"] == "demo"
    assert data["connections"]["demo"]["database"] == "tableseed_demo"
    assert "TABLESEED_DB_PASSWORD" in str(data)          # 变量名可见
    # 回显里不该有明文密码（这个测试没写明文，故验证脱敏键存在）
    assert "masked" in data["connections"]["demo"]

    client.post("/api/connections/delete", json={"name": "demo"})
    assert client.get("/api/connections").json()["connections"] == {}


@allure.story("切换到不存在的连接报 400")
def test_activate_unknown_connection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    res = client.post("/api/connections/active", json={"name": "nope"})
    assert res.status_code == 400


@allure.story("SQL 查询回落到 config.ini 的激活连接")
def test_query_falls_back_to_ini_connection(tmp_path, monkeypatch):
    """页面不传连接、YAML 也没有 database 段时，用 ini 的激活连接。"""
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    client.put("/api/connections", json={
        "name": "demo", "type": "mysql", "host": "127.0.0.1", "port": 59999,
        "user": "root", "database": "d",
    })
    client.post("/api/connections/active", json={"name": "demo"})

    res = client.post("/api/sql/execute", json={"sql": "SELECT 1"})
    assert res.status_code == 400
    # 端口 59999 不会有 MySQL —— 报「连不上服务器」证明走了 ini 的连接
    assert "Can't connect" in res.json()["detail"]  # 走的是 ini 的连接（59999 无人监听）


@allure.story("旧 YAML database 段仍兼容")
def test_legacy_yaml_database_still_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG + """
database:
  type: mysql
  host: 127.0.0.1
  port: 59998
  user: root
  database: legacy
"""})
    res = client.post("/api/sql/execute", json={"sql": "SELECT 1"})
    assert res.status_code == 400
    assert "Can't connect" in res.json()["detail"]  # 走的是 YAML 里 59998 端口
