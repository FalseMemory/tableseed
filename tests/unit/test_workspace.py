"""配置文件工作区测试：多文件清单、切换、另存为、越界防护。"""

from __future__ import annotations

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed.web.app import create_app

CONFIG = """
seed: 1
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_s, fields: [s], values: [["01"], ["02"]]}
"""


@allure.epic("tableseed")
@allure.feature("配置文件工作区")
@allure.story("另存为 → 清单 → 切换 全链路")
def test_workspace_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})

    # 初始清单为空（config.ini 未创建）
    ws = client.get("/api/workspace").json()
    assert ws["files"] == []

    # 另存为两个文件
    client.post("/api/workspace/save-as", json={"path": "samples/a.yaml"})
    client.post("/api/workspace/save-as",
                json={"path": "samples/b.yaml", "text": CONFIG.replace("seed: 1", "seed: 2")})
    # b.yaml 落盘且内容不同
    assert (tmp_path / "samples/b.yaml").exists()
    assert "seed: 2" in (tmp_path / "samples/b.yaml").read_text(encoding="utf-8")

    ws = client.get("/api/workspace").json()
    assert ws["active"] == "samples/b.yaml"
    assert {f["path"] for f in ws["files"]} == {"samples/a.yaml", "samples/b.yaml"}

    # 切回 a.yaml：服务载入其内容
    res = client.post("/api/workspace/switch", json={"path": "samples/a.yaml"}).json()
    assert "seed: 1" in res["text"]
    assert "seed: 1" in client.get("/api/config").json()["text"]


@allure.story("切换到清单外但不存在的文件被拒")
def test_switch_missing_file_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    res = client.post("/api/workspace/switch", json={"path": "nope/missing.yaml"})
    assert res.status_code == 400


@allure.story("越界路径被拒（.. 与绝对路径）")
@pytest.mark.parametrize("path", ["../evil.yaml", "C:/evil.yaml", "D:\\evil.yaml"])
def test_dangerous_paths_rejected(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    assert client.post("/api/workspace/save-as", json={"path": path}).status_code == 400


@allure.story("移出清单不删除磁盘文件")
def test_remove_keeps_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    client.post("/api/workspace/save-as", json={"path": "samples/a.yaml"})

    client.post("/api/workspace/remove", json={"path": "samples/a.yaml"})
    assert (tmp_path / "samples/a.yaml").exists()          # 文件还在
    ws = client.get("/api/workspace").json()
    assert "samples/a.yaml" not in [f["path"] for f in ws["files"]]


@allure.story("连接与工作区共存于同一 config.ini，互不覆盖")
def test_connections_and_workspace_coexist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/config", json={"text": CONFIG})
    client.put("/api/connections", json={
        "name": "demo", "type": "mysql", "host": "h", "user": "u", "database": "d"})
    client.post("/api/workspace/save-as", json={"path": "samples/a.yaml"})

    # 互相不受影响
    assert list(client.get("/api/connections").json()["connections"]) == ["demo"]
    assert client.get("/api/workspace").json()["active"] == "samples/a.yaml"
    # 再保存一次连接，workspace 仍完好
    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "h2", "user": "u", "database": "d2"})
    assert client.get("/api/workspace").json()["active"] == "samples/a.yaml"
