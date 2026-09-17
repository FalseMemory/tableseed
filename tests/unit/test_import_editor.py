"""快速生成（DDL/INSERT → YAML）与结构化编辑往返测试。"""

from __future__ import annotations

import allure
import pytest

from tableseed import service
from tableseed.config.ddl_import import generate_yaml, parse_create_table, parse_inserts
from tableseed.errors import ConfigError
from tableseed.web.structured import from_edit_view, to_edit_view

DDL = """
CREATE TABLE t_demo (
  id bigint NOT NULL AUTO_INCREMENT,
  acct_no varchar(32) NOT NULL,
  status char(2) NOT NULL,
  balance decimal(18,2),
  open_date date,
  remark varchar(200),
  PRIMARY KEY (id)
);
"""

INSERTS = """
INSERT INTO t_demo (acct_no, status, balance, open_date) VALUES
('A1', '01', 10.5, '2026-01-15'),
('A2', '02', 20.5, '2026-03-20');
"""


def full_config() -> str:
    return generate_yaml(DDL, INSERTS)


# ---------------------------------------------------------------- DDL 解析


@allure.epic("tableseed")
@allure.feature("快速生成")
@allure.story("DDL 列解析")
def test_parse_create_table():
    table, columns = parse_create_table(DDL)
    assert table == "t_demo"
    assert [c.name for c in columns] == [
        "id", "acct_no", "status", "balance", "open_date", "remark",
    ]
    assert columns[0].primary_key is True       # AUTO_INCREMENT
    assert columns[1].primary_key is False      # 只在 PRIMARY KEY (...) 里的才是
    assert columns[1].nullable is False


@allure.story("INSERT 样例解析：字符串逗号不切错")
def test_parse_inserts_with_comma_in_string():
    samples = parse_inserts(
        "INSERT INTO t (a, b) VALUES ('x, y', 1), ('it''s', 2);"
    )
    assert samples["a"] == ["x, y", "it's"]
    assert samples["b"] == [1, 2]


@allure.story("坏 DDL 不抛异常，返回空")
def test_bad_ddl_is_tolerated():
    table, columns = parse_create_table("这是随手的文本")
    assert columns == []


# ---------------------------------------------------------------- YAML 生成


@allure.feature("快速生成")
@allure.story("启发式分组：类型优先于取值枚举")
def test_generated_yaml_heuristics():
    text = full_config()
    assert "type: sequence" in text            # 整型主键
    assert "type: enum" in text                # 低基数字符串
    assert "generator: decimal" in text        # 金额不因样例少而变 enum
    assert "generator: date" in text
    assert 'value: [""]' in text              # 无样例的 varchar → const 占位（不再是 random）
    assert "{{seq" not in text                 # 不残留转义大括号


@allure.story("生成的 YAML 可通过校验并生成数据")
def test_generated_yaml_is_loadable():
    config = service.load_text(full_config())
    assert service.check(config) == []
    result = service.generate(config)
    assert len(result.tables["t_demo"]) == 4  # status 2 值 × acct_no 2 值


@allure.feature("快速生成")
@allure.story("主键必须唯一：带样例也用 sequence")
def test_primary_key_always_sequence():
    """主键若按样例生成 enum，会造出重复主键 —— 必须走 sequence。"""
    from tableseed.config.ddl_import import Column, _infer_type

    pk_with_samples = Column(name="id", type_raw="bigint", primary_key=True, samples=[1, 2, 3])
    assert _infer_type(pk_with_samples) == "sequence"

    # 对照：非主键的低基数字符串列仍走 enum（样例就是天然候选）
    status = Column(name="status", type_raw="char(2)", samples=["01", "02"])
    assert _infer_type(status) == "enum"

    # 端到端：生成结果里主键不重复
    result = service.generate(service.load_text(full_config()))
    ids = [r.values["id"] for r in result.tables["t_demo"].rows]
    assert len(set(ids)) == len(ids)


@allure.story("无样例的纯 DDL 草稿自动声明 rows 行数")
def test_ddl_only_draft_declares_rows():
    """没有 INSERT 样例时字段全是逐行组 —— 必须补 rows，否则配置一拿就报错。
    默认 1 行（保守起点），要造更多行时用户自行调大。"""
    from tableseed.config.ddl_import import generate_yaml

    text = generate_yaml(
        "CREATE TABLE t_x (id bigint NOT NULL, acct_no varchar(32), "
        "balance decimal(18,2), PRIMARY KEY (id));"
    )
    assert "rows: 1" in text

    config = service.load_text(text)
    assert service.check(config) == []
    result = service.generate(config)
    assert len(result.tables["t_x"]) == 1  # rows: 1


# ---------------------------------------------------------------- 结构化编辑


@allure.feature("配置编辑器")
@allure.story("编辑视图往返一致（含关系/聚合/split 的复杂配置）")
def test_edit_view_roundtrip():
    from pathlib import Path

    from tableseed.sink import MemorySink

    sample = Path(__file__).resolve().parents[2] / "samples" / "txn.yaml"
    config = service.load(sample)
    view = to_edit_view(config)
    regenerated = service.load_text(from_edit_view(view))

    assert service.check(regenerated) == []
    # 显式内存 sink —— 示例配置可能带 database 段，默认 generate 会直连入库
    first = service.generate(config, sink=MemorySink())
    second = service.generate(regenerated, sink=MemorySink())
    for name in first.tables:
        assert first.tables[name].to_records() == second.tables[name].to_records()


@allure.story("enum 组不携带 sequence 的默认值")
def test_edit_view_has_no_cross_type_defaults():
    config = service.load_text(full_config())
    view = to_edit_view(config)
    status_group = next(g for g in view["tables"][0]["groups"] if g["name"] == "g_status")
    assert "start" not in status_group
    assert "step" not in status_group
    assert status_group["values"] == [["01"], ["02"]]


@allure.story("编辑后的非法配置当场报错（中文提示）")
def test_invalid_edit_is_rejected():
    view = to_edit_view(service.load_text(full_config()))
    view["tables"][0]["groups"][0]["fields"] = []          # 组没有字段
    with pytest.raises(ConfigError, match="缺少必填字段"):
        from_edit_view(view)


@allure.feature("配置编辑器")
@allure.story("WebUI 结构化接口")
def test_structured_endpoints():
    from fastapi.testclient import TestClient

    from tableseed.web.app import create_app

    client = TestClient(create_app())
    client.put("/api/config", json={"text": full_config()})

    view = client.get("/api/config/structured").json()
    assert view["tables"][0]["name"] == "t_demo"

    saved = client.put("/api/config/structured", json={"edit": view})
    assert saved.json()["ok"] is True

    imported = client.post(
        "/api/import/yaml", json={"ddl": DDL, "inserts": INSERTS}
    ).json()
    assert imported["problems"] == []
