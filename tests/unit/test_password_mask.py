"""密码掩码解析测试：掩码/空 → 用已存密码；新密码 → 用新密码。

回归背景：密码框回填掩码后，**测试连接 / 入库 / SQL 台 / 取建表语句**
原先直接拿表单值当密码，把 `********` 送进 MySQL → 报
"用户名或密码不正确（Access denied）"，用户完全找不到原因。
"""

from __future__ import annotations

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed.web.app import create_app

MASK = "********"
BASE = {
    "type": "mysql",
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "database": "demo",
}


def _client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/connections", json={**BASE, "name": "prod", "password": "realpwd"})
    return client


@allure.feature("密码掩码")
@allure.story("掩码被解析为该连接已保存的密码")
def test_resolve_mask_to_saved(tmp_path, monkeypatch):
    from tableseed.web.app import resolve_saved_password

    _client(tmp_path, monkeypatch)
    out = resolve_saved_password({**BASE, "password": MASK, "name": "prod"}, "prod")
    assert out["password"] == "realpwd"
    assert "name" not in out, "name 不能进 DatabaseSpec（会被判 extra_forbidden）"


@allure.story("空密码也解析为已保存的密码（用户没重输）")
def test_resolve_blank_to_saved(tmp_path, monkeypatch):
    from tableseed.web.app import resolve_saved_password

    _client(tmp_path, monkeypatch)
    assert resolve_saved_password({**BASE, "password": "", "name": "prod"}, "prod")["password"] == "realpwd"
    assert resolve_saved_password({**BASE, "name": "prod"}, "prod")["password"] == "realpwd"


@allure.story("显式给新密码 → 用新密码（不能被已存密码覆盖）")
def test_new_password_wins(tmp_path, monkeypatch):
    from tableseed.web.app import resolve_saved_password

    _client(tmp_path, monkeypatch)
    out = resolve_saved_password({**BASE, "password": "typed-by-user", "name": "prod"}, "prod")
    assert out["password"] == "typed-by-user"


@allure.story("没给 name 时回退到当前激活连接")
def test_resolve_falls_back_to_active(tmp_path, monkeypatch):
    from tableseed.web.app import resolve_saved_password

    _client(tmp_path, monkeypatch)
    assert resolve_saved_password({**BASE, "password": MASK}, None)["password"] == "realpwd"


@allure.story("测试连接接口收到掩码不会把它当密码发出去")
def test_test_endpoint_accepts_mask(tmp_path, monkeypatch):
    """回归：接口把 `********` 当密码 → MySQL 报 Access denied。"""
    client = _client(tmp_path, monkeypatch)
    res = client.post("/api/database/test", json={**BASE, "name": "prod", "password": MASK})
    assert res.status_code == 200
    body = res.json()
    # 没有真实数据库时也应该走到"连接失败：..."，而不是"密码不正确"以外的校验错
    assert "name" not in str(body.get("message", "")), (
        f"name 不该进模型校验: {body}"
    )
    assert "Extra inputs" not in str(body.get("message", "")), body


@allure.story("入库/SQL 台：database 里的掩码同样被解析")
def test_sql_endpoint_accepts_mask(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    res = client.post("/api/sql/execute",
                      json={"sql": "SELECT 1", "database": {**BASE, "password": MASK, "name": "prod"}})
    body = res.json() if res.status_code == 200 else {"detail": res.text}
    assert "Extra inputs" not in str(body), body
    assert "not permitted" not in str(body), body
