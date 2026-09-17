"""DDL/INSERT → YAML 草稿生成测试：类型推断规则。"""

from __future__ import annotations

import allure

from tableseed import service
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


# ---------------------------------------------------------------- 只有 INSERT（无 DDL）


ONLY_INSERT = (
    "INSERT INTO t_txn (txn_no, txn_type, channel, currency, amount, tenant_id, log_count) "
    "VALUES ('txn_no_001001', 'Q', 'OTC', 'HKD', 1491714.43, '0001', NULL);"
)


@allure.story("只贴 INSERT、不提供建表语句 —— 列清单从 INSERT 的列名推导")
def test_insert_only_generates_full_config():
    """回归：生产环境常常拿不到建表语句，只有几条真实数据。
    以前 DDL 解析不到列就直接返回占位模板，INSERT 被完全忽略 ——
    用户看到的是只含一个示例组 g_status 的残缺配置。"""
    out = generate_yaml("", ONLY_INSERT, table="t_txn")

    config = service.load_text(out)
    assert service.check(config) == [], f"生成的配置应可直接使用:\n{out}"

    groups = config.tables[0].groups
    covered = {f for g in groups for f in g.fields}
    assert covered == {"txn_no", "txn_type", "channel", "currency",
                       "amount", "tenant_id", "log_count"}, f"每列都该有组，实际 {covered}"


@allure.story("只有 INSERT 时：编号列用自增，业务码保持常量，NULL 列忠实生成")
def test_insert_only_inference_rules():
    out = generate_yaml("", ONLY_INSERT, table="t_txn").replace("rows: 1 ", "rows: 30 ")
    result = service.generate(service.load_text(out))
    rows = result.tables["t_txn"].rows

    ids = [r.values["txn_no"] for r in rows]
    assert len(ids) == 30 and len(set(ids)) == 30, "编号列必须各不相同，否则主键冲突"
    assert {r.values["tenant_id"] for r in rows} == {"0001"}, "带引号的业务码不该被当成自增"
    assert {r.values["log_count"] for r in rows} == {None}, "样例为 NULL 的列应生成 NULL"


@allure.story("有 DDL 时如实尊重声明：普通字符串列仍是枚举（不回归）")
def test_ddl_declaration_wins_over_name_guess():
    """acct_no 样例互不相同、名字也以 _no 结尾，但 DDL 里它只是普通 varchar ——
    有建表语句就该尊重声明，否则用户拿它做覆盖组合的设计会被破坏。"""
    ddl = """CREATE TABLE t_demo (
      id bigint NOT NULL AUTO_INCREMENT PRIMARY KEY,
      acct_no varchar(32),
      status char(2)
    );"""
    inserts = ("INSERT INTO t_demo (id, acct_no, status) VALUES (1, 'acct_no_000001', '01');\n"
               "INSERT INTO t_demo (id, acct_no, status) VALUES (2, 'acct_no_000002', '02');")
    out = generate_yaml(ddl, inserts)

    config = service.load_text(out)
    kinds = {g.fields[0]: g.type for g in config.tables[0].groups}
    assert kinds["acct_no"] == "enum", f"有 DDL 时不该把 acct_no 猜成自增: {kinds}"
    assert len(service.generate(config).tables["t_demo"]) == 4, "两个枚举组 → 笛卡尔积 4 行"


@allure.story("DDL 与 INSERT 都没给 → 提示二者至少填一个")
def test_nothing_given_hints_clearly():
    assert "至少填一个" in generate_yaml("", "", table="t_x")
    assert "检查" in generate_yaml("这不是建表语句", "", table="t_x")


@allure.story("表名解析：DDL 优先，其次用户填写，再次 INSERT 里的表名")
def test_table_name_resolution():
    assert "t_new_table" in generate_yaml("", "", table="")
    assert "name: t_txn" in generate_yaml("", ONLY_INSERT)
    assert "name: t_custom" in generate_yaml("", ONLY_INSERT, table="t_custom")


@allure.story("占位模板必须带可识别标记（前端据此拦截，不覆盖用户文件）")
def test_placeholder_is_marked():
    out = generate_yaml("", "", table="t_x")
    assert out.lstrip().startswith("# 无法生成配置"), "占位模板要有统一前缀供程序识别"
    assert "至少填一个" in out
    # 解析失败（给了 DDL 但格式不对）也算占位
    bad = generate_yaml("这不是建表语句", "", table="t_x")
    assert bad.lstrip().startswith("# 无法生成配置")

    # 接口层显式回传 placeholder 标志
    from fastapi.testclient import TestClient
    from tableseed.web.app import create_app
    client = TestClient(create_app())
    res = client.post("/api/import/yaml", json={"ddl": "", "inserts": "", "table": "t_x"}).json()
    assert res["placeholder"] is True
    ok = client.post("/api/import/yaml",
                     json={"ddl": "", "inserts": ONLY_INSERT, "table": "t_txn"}).json()
    assert ok["placeholder"] is False


# ---------------------------------------------------------------- 限定表名 / 行数语义


@allure.story("库名.表名：DDL 与 INSERT 都取最后一段作为表名")
def test_qualified_table_name():
    """回归：CREATE TABLE core_db.t_account 曾被解析成表名 `core_db`（`.` 截断）。"""
    ddl = """CREATE TABLE core_db.t_account (
      acct_no varchar(32) NOT NULL PRIMARY KEY,
      balance decimal(18,2)
    );"""
    inserts = ("INSERT INTO core_db.t_account (acct_no, balance) "
               "VALUES ('A0001', 100.50);")

    from tableseed.config.ddl_import import (generate_yaml, insert_table_name,
                                             parse_create_table)

    assert parse_create_table(ddl)[0] == "t_account"
    assert insert_table_name(inserts) == "t_account"

    out = generate_yaml(ddl, inserts)
    config = service.load_text(out)
    assert config.tables[0].name == "t_account"
    assert service.check(config) == []
    # 反引号限定名也要认
    ddl2 = "CREATE TABLE `mydb`.`t_x` (id bigint PRIMARY KEY);"
    assert parse_create_table(ddl2)[0] == "t_x"


@allure.story("rows 是「我要这么多行」不是「上限」：组合数 > rows 时取满组合数")
def test_rows_is_target_not_cap():
    """回归：rows 曾被当成截断上限，组合展开被截断（6 种组合只出 3 行），
    预演却按「取满组合数」算 —— 两边对不上，还报「预估 6 行超过上限 3，将截断」。"""
    text = """
seed: 1
limits: {max_rows: 100000, strategy: full}
tables:
  - name: t_a
    rows: 3
    groups:
      - {type: enum, name: g_s, fields: [status], values: [["01"], ["02"], ["03"]]}
      - {type: enum, name: g_t, fields: [t], values: [["A"], ["B"]]}
"""
    config = service.load_text(text)
    plan = service.plan(config)
    result = service.generate(config)
    # 组合数 6 > rows 3 → 取满 6 行，预演与生成一致，无截断警告
    assert plan.tables[0].planned_rows == 6
    assert not plan.warnings
    assert len(result.tables["t_a"]) == 6
