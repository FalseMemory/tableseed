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


@allure.story("从磁盘重载：文件被外部改动后一键同步到服务")
def test_reload_from_disk(tmp_path, monkeypatch):
    """回归：验证脚本 / git / 外部编辑器改了文件，服务内存还是旧副本 ——
    页面就会拿旧配置报错（"文件明明是干净的，页面却还报旧问题"）。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "samples").mkdir()
    target = tmp_path / "samples" / "r.yaml"
    target.write_text(CONFIG, encoding="utf-8")

    client = TestClient(create_app(config_path="samples/r.yaml"))
    assert "t_a" in client.get("/api/config").json()["text"]

    # 绕过服务直接在磁盘上改（模拟 git checkout / 外部编辑器）
    target.write_text(CONFIG.replace("t_a", "t_renamed"), encoding="utf-8")
    assert "t_renamed" not in client.get("/api/config").json()["text"]   # 内存还是旧的

    res = client.post("/api/config/reload", json={})
    assert res.status_code == 200
    assert "t_renamed" in res.json()["text"]
    assert "t_renamed" in client.get("/api/config").json()["text"]       # 已同步


@allure.story("没有绑定文件时重载给出明确提示")
def test_reload_without_bound_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    res = client.post("/api/config/reload", json={})
    assert res.status_code == 400
    assert "没有绑定" in res.json()["detail"]


@allure.story("首次启动：-c 指定的配置自动纳入清单（清单不至于空）")
def test_first_run_adds_current_config(tmp_path, monkeypatch):
    """回归：服务用 -c samples/txn.yaml 启动，但清单是空的 ——
    用户看到的就是"我保存过的 YAML 文件都不见了"。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "txn.yaml").write_text(CONFIG, encoding="utf-8")

    client = TestClient(create_app(config_path="samples/txn.yaml"))
    ws = client.get("/api/workspace").json()
    assert [f["path"] for f in ws["files"]] == ["samples/txn.yaml"]
    assert ws["active"] == "samples/txn.yaml"
    # 落盘了：重启（新建 app 实例）后仍在
    client2 = TestClient(create_app(config_path="samples/txn.yaml"))
    assert [f["path"] for f in client2.get("/api/workspace").json()["files"]] == ["samples/txn.yaml"]


@allure.story("用户清空过清单后，不再自动加回（否则移除永远无效）")
def test_user_cleared_list_stays_cleared(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "a.yaml").write_text(CONFIG, encoding="utf-8")

    client = TestClient(create_app(config_path="samples/a.yaml"))
    assert len(client.get("/api/workspace").json()["files"]) == 1
    client.post("/api/workspace/remove", json={"path": "samples/a.yaml"})

    ws = client.get("/api/workspace").json()
    assert [f["path"] for f in ws["files"]] == []          # 移除生效，不被加回
    assert (tmp_path / "samples/a.yaml").exists()          # 磁盘文件保留


@allure.story("连接到不存在的文件：400")
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
