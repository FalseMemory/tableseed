"""配置文件工作区测试：多文件清单、切换、另存为、越界防护。"""

from __future__ import annotations

import json

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


@allure.story("连接接口绝不下发明文密码（页面/开发者工具/截图都会泄露）")
def test_connections_never_leak_password(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "127.0.0.1", "port": 3306,
        "user": "root", "password": "S3cret!Pass", "database": "db",
    })
    res = client.get("/api/connections")
    assert res.status_code == 200
    body = res.text
    assert "S3cret!Pass" not in body, "明文密码不能出现在响应里"
    entry = res.json()["connections"]["prod"]
    assert "password" not in entry, f"不该回传 password 字段: {entry}"
    assert entry["password_set"] is True, "要给出「已配置」标记供前端提示"


@allure.story("密码留空保存 = 不修改（前端不回填明文，留空不该清掉密码）")
def test_blank_password_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "127.0.0.1",
        "user": "root", "password": "origin", "database": "db",
    })
    # 编辑时只改 host、密码框留空（前端不发明文密码）
    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "10.0.0.9",
        "user": "root", "password": "", "database": "db",
    })

    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    parser.read("config.ini", encoding="utf-8")
    assert parser["prod"]["password"] == "origin", "留空不该覆盖已保存的密码"
    assert parser["prod"]["host"] == "10.0.0.9", "其他字段要正常更新"

    # 显式给新密码则更新
    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "10.0.0.9",
        "user": "root", "password": "newpwd", "database": "db",
    })
    parser.read("config.ini", encoding="utf-8")
    assert parser["prod"]["password"] == "newpwd"


@allure.story("密码掩码：看得出「已配置」，回传掩码仍然不改密码")
def test_password_mask_roundtrip(tmp_path, monkeypatch):
    """回归：只给一个布尔标记时密码框一片空白，用户以为没保存、
    每次重输甚至输错 —— 必须回填一个**定长掩码**。"""
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "127.0.0.1",
        "user": "root", "password": "realpwd", "database": "db",
    })

    entry = client.get("/api/connections").json()["connections"]["prod"]
    mask = entry["password_mask"]
    assert mask, "已配置密码时必须给出掩码"
    assert "realpwd" not in mask
    assert "realpwd" not in json.dumps(entry, ensure_ascii=False), "响应不能含明文密码"

    # 前端原样回传掩码 → 视为不修改
    client.put("/api/connections", json={
        "name": "prod", "type": "mysql", "host": "127.0.0.1",
        "user": "root", "password": mask, "database": "db",
    })
    import configparser
    parser = configparser.ConfigParser(interpolation=None)
    parser.read("config.ini", encoding="utf-8")
    assert parser["prod"]["password"] == "realpwd", "回传掩码不该覆盖真实密码"

    # 没有密码的连接 → 掩码为空串
    client.put("/api/connections", json={
        "name": "nopwd", "type": "mysql", "host": "127.0.0.1",
        "user": "root", "database": "db",
    })
    entry2 = client.get("/api/connections").json()["connections"]["nopwd"]
    assert entry2["password_mask"] == ""
    assert entry2["password_set"] is False


# ---------------------------------------------------------------- 导入外部 YAML


IMPORTED_YAML = (
    "seed: 4242\n"
    "limits: {max_rows: 100, strategy: full}\n"
    "tables:\n"
    "  - name: t_shared\n"
    "    rows: 2\n"
    "    groups:\n"
    "      - {type: const, name: g_a, fields: [a], value: ['来自同事']}\n"
)


@allure.story("导入别人发来的 YAML：浏览器选文件后把内容送进来")
def test_import_yaml_by_content(tmp_path, monkeypatch):
    """"别人分享给我一个 yaml" 是真实场景 —— 原来的加载只能从项目内的
    清单里选，外部文件根本进不来。"""
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    res = client.post("/api/workspace/import",
                      json={"name": "同事发的配置.yaml", "text": IMPORTED_YAML})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["path"].startswith("imports/"), body["path"]
    assert "t_shared" in body["text"]
    assert body["problems"] == [], f"导入后应立即校验: {body['problems']}"

    assert (tmp_path / body["path"]).exists()
    ws = client.get("/api/workspace").json()
    assert body["path"] in [f["path"] for f in ws["files"]]
    assert client.get("/api/config").json()["text"] == body["text"]


@allure.story("导入重名文件不覆盖已有文件（自动加序号）")
def test_import_same_name_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    cfg = "seed: 1\ntables: []\n"

    first = client.post("/api/workspace/import", json={"name": "a.yaml", "text": cfg}).json()
    second = client.post("/api/workspace/import", json={"name": "a.yaml", "text": cfg}).json()
    assert first["path"] != second["path"], "同名导入必须另存为不同文件"
    assert (tmp_path / first["path"]).exists()
    assert (tmp_path / second["path"]).exists()


@allure.story("按绝对路径导入（同事把文件放桌面/下载目录的场景）")
def test_import_yaml_by_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    outside = tmp_path / "outside"
    outside.mkdir()
    shared = outside / "shared.yaml"
    shared.write_text(IMPORTED_YAML.replace("4242", "7"), encoding="utf-8")

    res = client.post("/api/workspace/import-path", json={"path": str(shared)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["text"].strip().startswith("seed: 7")
    # 复制进项目而不是直接绑定外部路径（否则重载/另存会被路径规范拒绝）
    assert body["path"].startswith("imports/")
    assert (tmp_path / body["path"]).exists()
    assert shared.exists(), "原文件不能被移动或删除"


@allure.story("按路径导入也要支持 GBK 编码的文件")
def test_import_path_gbk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    shared = tmp_path / "gbk.yaml"
    shared.write_bytes(IMPORTED_YAML.replace("4242", "1").encode("gbk"))

    res = client.post("/api/workspace/import-path", json={"path": str(shared)})
    assert res.status_code == 200, res.text
    assert "来自同事" in res.json()["text"]


@allure.story("导入路径不存在 / 空内容 → 明确报错")
def test_import_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    res = client.post("/api/workspace/import-path",
                      json={"path": str(tmp_path / "nope.yaml")})
    assert res.status_code == 400
    assert "不存在" in res.json()["detail"]

    res = client.post("/api/workspace/import", json={"name": "x.yaml", "text": "   "})
    assert res.status_code == 400
    assert "为空" in res.json()["detail"]


@allure.story("导入时的路径穿越：../ 必须被剥离，不能写到项目外")
def test_import_path_traversal_is_stripped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    res = client.post("/api/workspace/import",
                      json={"name": "../../evil.yaml", "text": "seed: 1\ntables: []\n"})
    assert res.status_code == 200
    assert res.json()["path"].startswith("imports/")
    assert not (tmp_path.parent / "evil.yaml").exists()


