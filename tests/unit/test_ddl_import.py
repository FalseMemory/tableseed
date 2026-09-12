"""DDL/INSERT → YAML 草稿生成测试：类型推断规则。"""

from __future__ import annotations

import allure

from tableseed.config.ddl_import import generate_yaml

DDL = """CREATE TABLE t_demo (
  txn_no varchar(20) NOT NULL PRIMARY KEY,
  status char(2),
  amount decimal(18,2),
  remark varchar(50)
);"""


@allure.epic("tableseed")
@allure.feature("快速生成")
@allure.story("单条 INSERT：非主键全部 const（单值无推断空间，不猜）")
def test_single_insert_all_const():
    yaml_out = generate_yaml(
        DDL,
        "INSERT INTO t_demo (txn_no, status, amount, remark) VALUES ('T0001', '01', 123.45, '正常');",
    )
    for name, kind in [("txn_no", "sequence"), ("status", "const"),
                       ("amount", "const"), ("remark", "const")]:
        assert f"type: {kind}" in yaml_out, f"{name} 应为 {kind}"


@allure.story("多条 INSERT：字符串多值 → enum，数值列 → random")
def test_multi_insert_infers():
    inserts = (
        "INSERT INTO t_demo (txn_no, status, amount, remark) VALUES ('T0001', '01', 123.45, '正常');\n"
        "INSERT INTO t_demo (txn_no, status, amount, remark) VALUES ('T0002', '02', 999.99, '冻结');"
    )
    yaml_out = generate_yaml(DDL, inserts)
    assert "type: enum" in yaml_out
    assert "type: random" in yaml_out


@allure.story("主键识别：列内联 PRIMARY KEY 与表级约束都要认")
def test_primary_key_variants():
    # 列内联
    out1 = generate_yaml(DDL, "INSERT INTO t_demo (txn_no, status, amount, remark) VALUES ('T0001', '01', 1, 'x');")
    assert "type: sequence" in out1
    # 表级约束
    ddl2 = "CREATE TABLE t2 (id bigint, code varchar(10), PRIMARY KEY (id));"
    out2 = generate_yaml(ddl2, "INSERT INTO t2 (id, code) VALUES (1, 'A');")
    assert "type: sequence" in out2
