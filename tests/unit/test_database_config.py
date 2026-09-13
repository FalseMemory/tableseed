"""数据库连接测试：config.ini 多连接、URL 拼接、旧 YAML database 段兼容。"""

from __future__ import annotations

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed.config.connections import ConnectionsStore
from tableseed.models import DatabaseSpec
from tableseed import service
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


@allure.feature("数据库连接")
@allure.story("特殊字符密码的保存与加载往返（% 曾炸掉 configparser）")
def test_special_chars_roundtrip(tmp_path):
    """密码带 % @ : / # # 空格 —— ini 读写必须无损（% 是 BasicInterpolation 的雷）。"""
    store = ConnectionsStore(tmp_path / "config.ini")
    fields = {
        "type": "mysql", "host": "127.0.0.1", "port": 3306, "user": "root",
        "password": "p%40ss:w/o rd#@1%2", "database": "db#1",
        "charset": "utf8mb4",
    }
    store.save("dev-1_local", fields)

    loaded = store.load()["connections"]["dev-1_local"]
    assert loaded["password"] == "p%40ss:w/o rd#@1%2"   # % 没被插值、没丢
    assert loaded["database"] == "db#1"

    # 密码里的 % 在拼连接串时必须被编码成 %25，否则 URL 解析会错位
    spec = DatabaseSpec.model_validate(loaded)
    url = spec.resolved_url()
    assert "p%2540ss" in url                            # % -> %25


@allure.story("换行值被拒（ini 不支持跨行值）")
def test_newline_value_rejected(tmp_path):
    from tableseed.errors import TableSeedError

    store = ConnectionsStore(tmp_path / "config.ini")
    with pytest.raises(TableSeedError, match="换行"):
        store.save("demo", {"host": "h", "user": "u", "database": "d", "password": "a\nb"})


@allure.story("中文连接名可保存")
def test_chinese_name(tmp_path):
    store = ConnectionsStore(tmp_path / "config.ini")
    store.save("本地演示", {"type": "mysql", "host": "h", "user": "u", "database": "d"})
    assert "本地演示" in store.load()["connections"]


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
    # masked 必须是正常脱敏串 —— 曾经这里 NameError 被宽 except 吞成
    # 「(配置有误: ...)」，而旧断言只检查键存在，放过了 bug
    masked = data["connections"]["demo"]["masked"]
    assert "配置有误" not in masked and "name '" not in masked
    assert masked.startswith("mysql") and ":***@" in masked          # 密码位已脱敏
    assert data["connections"]["demo"]["password_source"] == "env:TABLESEED_DB_PASSWORD"

    client.post("/api/connections/delete", json={"name": "demo"})
    assert client.get("/api/connections").json()["connections"] == {}


@allure.story("切换到不存在的连接报 400")
def test_activate_unknown_connection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    res = client.post("/api/connections/active", json={"name": "nope"})
    assert res.status_code == 400


# ---------------------------------------------------------------- 确认插入的防重复


@allure.feature("数据库连接")
@allure.story("指纹 = 数据内容哈希（改了主键/取值必须变）")
def test_fingerprint_is_content_hash():
    from tableseed.sink import MemorySink
    from tableseed.web.app import _result_fingerprint

    config_a = """
seed: 1
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_s, fields: [s], values: [["01"], ["02"]]}
"""
    res_a = service.generate(service.load_text(config_a), sink=MemorySink())
    res_a2 = service.generate(service.load_text(config_a), sink=MemorySink())
    assert _result_fingerprint(res_a) == _result_fingerprint(res_a2)   # 同配置同 seed → 同指纹

    changed = config_a.replace('values: [["01"], ["02"]]', 'values: [["X1"], ["Y2"]]')
    res_b = service.generate(service.load_text(changed), sink=MemorySink())
    assert _result_fingerprint(res_b) != _result_fingerprint(res_a)    # 数据变了 → 指纹变


@allure.feature("数据库连接")
@allure.story("确认插入：改了配置（数据内容变化）后必须放行")
def test_insert_allows_changed_data(tmp_path, monkeypatch):
    """回归：旧指纹是 (seed, 表名, 行数) —— 改主键后行数不变被误判重复。
    指纹必须是数据内容哈希：任何一行数据变了都要放行。"""
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    config_text = """
seed: 1
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_s, fields: [s], values: [["01"], ["02"]]}
"""
    # 第一次生成 + 插入（内存 sink，不真连库 —— 指纹拦截在连库之前）
    gen1 = client.post("/api/generate/stream", json={"text": config_text})
    assert gen1.status_code == 200

    # 改主键/取值规则 → 数据内容变化（行数不变），重新生成
    changed = config_text.replace('values: [["01"], ["02"]]', 'values: [["X1"], ["Y2"], ["Z3"]]')
    gen2 = client.post("/api/generate/stream", json={"text": changed})
    assert gen2.status_code == 200

    # 插入接口在未配置连接时 400，但**不能**是 duplicate 拦截
    res = client.post("/api/insert", json={})
    assert res.status_code == 400
    assert not res.json()["detail"].startswith("本次预览的数据")


# ---------------------------------------------------------------- 连接切换的健壮性（回归）


@allure.feature("数据库连接")
@allure.story("不完整连接不被静默丢弃，且不会因保存别的连接被删掉")
def test_incomplete_connection_is_kept(tmp_path):
    """回归：load() 曾过滤掉无 host 的段，而 save() 用 load() 的结果整份重写
    —— 于是任何一次保存都会把不完整连接段从 config.ini 里静默删掉。"""
    store = ConnectionsStore(tmp_path / "config.ini")
    store.save("ok", {"type": "mysql", "host": "127.0.0.1", "user": "root", "database": "d"})

    # 手工写一个缺 host 的连接段（模拟用户改了一半 / 老数据）
    text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    (tmp_path / "config.ini").write_text(
        text + "\n[half]\ntype = mysql\nuser = root\ndatabase = d2\n", encoding="utf-8"
    )

    data = store.load()
    assert "half" in data["connections"], "不完整连接必须能读出来"
    assert "half" in data["incomplete"]
    assert "ok" not in data["incomplete"]

    # 再保存一个连接 —— half 段必须还在
    store.save("other", {"type": "mysql", "host": "h2", "user": "root", "database": "d3"})
    reloaded = store.load()
    assert {"ok", "half", "other"} <= set(reloaded["connections"])
    assert "half" in (tmp_path / "config.ini").read_text(encoding="utf-8")


@allure.story("保存连接不会破坏 workspace 段")
def test_save_connection_keeps_workspace(tmp_path):
    store = ConnectionsStore(tmp_path / "config.ini")
    store.save("ok", {"type": "mysql", "host": "h", "user": "u", "database": "d"})
    store.set_active_file("samples/txn.yaml")

    store.save("ok2", {"type": "mysql", "host": "h2", "user": "u", "database": "d2"})
    assert store.get_workspace()["active"] == "samples/txn.yaml"


@allure.story("active 指向已删除的连接：明确报出而不是静默回落")
def test_active_missing_is_reported(tmp_path):
    store = ConnectionsStore(tmp_path / "config.ini")
    store.save("a", {"type": "mysql", "host": "127.0.0.1", "user": "u", "database": "da"})
    store.save("b", {"type": "mysql", "host": "127.0.0.2", "user": "u", "database": "db"})

    # 手动把 active 指向一个不存在的连接（模拟用户删了段但 active 没更新）
    text = (tmp_path / "config.ini").read_text(encoding="utf-8").replace("active = a", "active = ghost")
    (tmp_path / "config.ini").write_text(text, encoding="utf-8")

    data = store.load()
    assert data["active_missing"] is True
    # 但仍要能回落到可用连接，不至于整个连不上库
    assert store.active_spec() is not None


@allure.story("切换到不存在的连接：400 且文案说清")
def test_switch_unknown_connection_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    res = client.post("/api/connections/active", json={"name": "ghost"})
    assert res.status_code == 400
    assert "不存在" in res.json()["detail"]


@allure.story("切换到信息不完整的连接：拒绝并说明原因")
def test_switch_incomplete_connection_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/connections", json={"name": "ok", "type": "mysql", "host": "h",
                                         "user": "u", "database": "d"})
    # 手工塞一个缺 host 的连接段
    ini = tmp_path / "config.ini"
    ini.write_text(ini.read_text(encoding="utf-8")
                   + "\n[half]\ntype = mysql\nuser = root\ndatabase = d2\n", encoding="utf-8")

    res = client.post("/api/connections/active", json={"name": "half"})
    assert res.status_code == 400
    assert "信息不完整" in res.json()["detail"]
    # 没有真的切过去
    assert client.get("/api/connections").json()["active"] == "ok"


@allure.story("页面本身也不能被缓存（否则修好的前端到不了浏览器）")
def test_html_is_not_cacheable(tmp_path, monkeypatch):
    """回归：只给 /api 加 no-store 不够 —— index.html 被缓存时，
    浏览器刷新拿到的还是旧前端（旧 api() 没禁缓存），修复看起来"没生效"。
    """
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    res = client.get("/")
    cache = res.headers.get("cache-control", "")
    assert "no-store" in cache, f"HTML 缺少 no-store: {cache}"
    # HTML 里也要有防缓存 meta（双保险）
    assert 'http-equiv="Cache-Control"' in res.text

    # 根路径重定向到带版本号的地址 —— 绕开浏览器里残留的旧页面缓存
    raw = client.get("/", follow_redirects=False)
    assert raw.status_code in (307, 302)
    assert "v=" in raw.headers["location"]
    assert client.get(raw.headers["location"]).status_code == 200

    health = client.get("/api/health").json()
    assert health.get("started_at"), "health 应返回服务启动时间（用于确认前端新旧）"


@allure.story("数据接口禁止浏览器缓存（曾导致刷新后连接列表变空）")
def test_api_responses_are_not_cacheable(tmp_path, monkeypatch):
    """回归：浏览器缓存了「服务刚启动还没存连接」的空响应，刷新后一直拿到空列表，
    看起来像"保存的东西没了"。所有 /api 响应必须带 no-store。
    """
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())

    res = client.get("/api/connections")
    cache = res.headers.get("cache-control", "")
    assert "no-store" in cache, f"缺少 no-store: {cache}"

    # 连续两次请求（同一 URL）必须都回源，不能被缓存成同一份
    first = client.get("/api/connections")
    client.put("/api/connections", json={"name": "x", "type": "mysql", "host": "h",
                                         "user": "u", "database": "d"})
    second = client.get("/api/connections")
    assert first.json()["connections"] == {}
    assert "x" in second.json()["connections"]


@allure.story("接口回显带 complete / incomplete / active_missing")
def test_connections_response_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    client.put("/api/connections", json={"name": "ok", "type": "mysql", "host": "h",
                                         "user": "u", "database": "d"})
    body = client.get("/api/connections").json()
    assert body["connections"]["ok"]["complete"] is True
    assert body["incomplete"] == []
    assert body["active_missing"] is False
    assert body["has_usable"] is True


# ---------------------------------------------------------------- 保存的健壮性


@allure.feature("配置保存")
@allure.story("清理旧备份失败绝不能拖垮保存（曾把服务进程杀掉）")
def test_persist_survives_unlink_failure(tmp_path, monkeypatch):
    """回归：persist() 清理旧备份时 unlink 抛 SystemExit（安全策略拦删除），
    SystemExit 继承 BaseException，普通 except Exception 抓不到 → 逃逸到
    uvicorn → 整个服务进程退出。用户点一下"保存配置"服务就没了。
    """
    monkeypatch.chdir(tmp_path)
    from pathlib import Path

    (tmp_path / "samples").mkdir()
    cfg = tmp_path / "samples" / "t.yaml"
    cfg.write_text("seed: 1\ntables: []\n", encoding="utf-8")

    client = TestClient(create_app(config_path="samples/t.yaml"))

    # 造出超过 10 份的旧备份，触发清理分支
    backup_dir = tmp_path / ".tmp" / "config-backup"
    backup_dir.mkdir(parents=True)
    for i in range(15):
        (backup_dir / f"t-20260101-0000{i:02d}.yaml").write_text("x", encoding="utf-8")

    # 让删除动作像沙箱那样抛 SystemExit
    real_unlink = Path.unlink

    def boom(self, *args, **kwargs):  # noqa: ANN001
        if self.parent == backup_dir:
            raise SystemExit(1)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", boom)

    res = client.put("/api/config", json={"text": "seed: 2\ntables: []\n"})
    assert res.status_code == 200, "删除失败不能影响保存"
    assert "seed: 2" in cfg.read_text(encoding="utf-8")


@allure.story("SQL 查询回落到 config.ini 的激活连接")
def test_query_falls_back_to_ini_connection(tmp_path, monkeypatch):
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
