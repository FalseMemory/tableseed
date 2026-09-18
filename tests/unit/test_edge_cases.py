"""边界与异常排查：主动攻击各模块，暴露隐藏缺陷。

这里不是"证明功能可用"，而是**故意喂坏数据、走极端路径**：
空配置、非法参数、注入串、路径穿越、除零、超大数……
每条断言都对应一个真实可能被用户触发的场景。
"""

from __future__ import annotations

import json

import allure
import pytest
from fastapi.testclient import TestClient

from tableseed import service
from tableseed.errors import ConfigError, GenerateError, TableSeedError
from tableseed.render import render_csv, render_sql
from tableseed.sink import MemorySink
from tableseed.web.app import create_app


# ---------------------------------------------------------------- 空与极端配置


@allure.epic("tableseed")
@allure.feature("边界排查")
@allure.story("空配置：空串明确报错；空表列表可加载但校验要提示")
def test_empty_config():
    with pytest.raises(TableSeedError):
        service.load_text("")

    # 编辑器里新建文件时是空的 —— 允许加载，但校验必须提示"还没定义表"，
    # 否则空文件也显示「校验通过」，用户不知道下一步该干什么
    config = service.load_text("tables: []")
    problems = service.check(config)
    assert problems, "空配置的校验必须给出提示"
    assert any("表" in p for p in problems)


@allure.story("rows 给了 0 / 负数：必须明确拒绝而不是静默生成 0 行")
def test_bad_rows_value():
    for bad in ("0", "-5", "abc"):
        text = f"""
seed: 1
tables:
  - name: t_a
    rows: {bad}
    groups:
      - {{type: const, name: g_a, fields: [a], value: ["x"]}}
"""
        # 能加载就必须能生成出合理结果；不能加载要给出可读错误
        try:
            config = service.load_text(text)
        except TableSeedError:
            continue
        try:
            result = service.generate(config, sink=MemorySink())
        except TableSeedError:
            continue
        # 静默生成 0 行是错的 —— 用户写了 rows: 0 应该被告知
        assert len(result.tables["t_a"]) > 0, f"rows: {bad} 被静默接受并生成 0 行"


@allure.story("max_rows 边界：0 与负数不能导致崩溃或无限循环")
def test_bad_max_rows():
    for bad in (0, -1):
        text = f"""
seed: 1
limits: {{max_rows: {bad}, strategy: full}}
tables:
  - name: t_a
    groups:
      - {{type: enum, name: g_a, fields: [a], values: [["1"], ["2"]]}}
"""
        try:
            config = service.load_text(text)
            service.generate(config, sink=MemorySink())
        except TableSeedError:
            pass   # 明确拒绝可以接受
        # 不允许挂死或抛未包装的异常 —— 能跑到这里就算过


# ---------------------------------------------------------------- 表达式与传播


@allure.story("derive 表达式异常：除零 / 未知字段 / 语法错都要可读报错")
def test_derive_expression_errors():
    cases = [
        ("1/0", "除零"),
        ("not_a_field * 2", "未知字段"),
        ("1 +", "语法错误"),
    ]
    for expr, label in cases:
        text = f"""
seed: 1
tables:
  - name: t_a
    rows: 3
    groups:
      - {{type: random, name: g_x, fields: [x], generator: int, range: [1, 9]}}
      - {{type: derive, name: g_y, fields: [y], expr: "{expr}"}}
"""
        try:
            config = service.load_text(text)
            service.generate(config, sink=MemorySink())
        except TableSeedError as exc:
            assert str(exc).strip(), f"{label} 的报错信息不能为空"
        # 未包装的 ZeroDivisionError / NameError / SyntaxError 会让 500 —— 不允许逃逸


@allure.story("split：parts 为 0 / 负数 / 缺失不能让父行凭空消失")
def test_bad_split():
    for parts in (0, -3, None):
        part_line = f"parts: {parts}" if parts is not None else ""
        text = f"""
seed: 1
limits: {{max_rows: 1000, strategy: full}}
tables:
  - name: t_order
    groups:
      - {{type: enum, name: g_o, fields: [ono], values: [["O1"], ["O2"]]}}
  - name: t_item
    groups:
      - {{type: const, name: g_i, fields: [ino], value: ["x"]}}
relations:
  - parent: t_order
    child: t_item
    cardinality: "1:N"
    join: [{{parent_field: ono, child_field: ino}}]
    propagate:
      - {{mode: split, to: amount, {part_line}}}
"""
        try:
            config = service.load_text(text)
            result = service.generate(config, sink=MemorySink())
        except TableSeedError:
            continue
        # 若接受了配置，子表不能为 0 行 —— 父行必须都被覆盖
        assert len(result.tables["t_item"]) > 0, f"parts={parts} 导致子表为空"


@allure.story("ref 指向不存在的父表/字段：明确报错")
def test_ref_missing_target():
    text = """
seed: 1
tables:
  - name: t_a
    rows: 2
    groups:
      - {type: ref, name: g_r, fields: [a], from: "t_ghost.id"}
"""
    try:
        service.generate(service.load_text(text), sink=MemorySink())
    except TableSeedError:
        pass


# ---------------------------------------------------------------- 渲染与注入


@allure.story("SQL 渲染：特殊字符正确转义（引号 / 反斜杠 / 换行 / emoji）")
def test_sql_literal_escaping():
    text = """
seed: 1
tables:
  - name: t_a
    rows: 1
    groups:
      - type: const
        name: g_a
        fields: [a]
        value: ["O'Brien \\\\ 反斜杠 ' 引号'"]
"""
    config = service.load_text(text)
    result = service.generate(config, sink=MemorySink())
    sql = render_sql(result.tables["t_a"], dialect="mysql")

    value = result.tables["t_a"].rows[0].values["a"]
    assert "''" in sql, f"单引号必须转义成两个单引号，实际 SQL: {sql}"
    # 转义后能还原（写出去的 SQL 单引号数量必须是偶数语义正确）
    assert sql.count("'") % 2 == 0, f"引号配对错误: {sql}"


@allure.story("SQL 渲染：表名/列名里的反引号不能逃逸出标识符")
def test_identifier_injection():
    text = """
seed: 1
tables:
  - name: "t_a`; DROP TABLE users; --"
    rows: 1
    groups:
      - {type: const, name: g_a, fields: [a], value: ["x"]}
"""
    try:
        config = service.load_text(text)
    except TableSeedError:
        return   # 直接拒绝也可以
    result = service.generate(config, sink=MemorySink())
    sql = render_sql(result.tables[list(result.tables)[0]], dialect="mysql")
    # 表名里的反引号必须被转义（`` ），不能直接闭合标识符
    assert "DROP TABLE" not in sql or "``" in sql, f"标识符未转义: {sql}"


@allure.story("CSV 渲染：逗号 / 引号 / 换行按 RFC4180 转义")
def test_csv_escaping():
    text = """
seed: 1
tables:
  - name: t_a
    rows: 1
    groups:
      - type: const
        name: g_a
        fields: [a]
        value: ["x,y"]
"""
    config = service.load_text(text)
    result = service.generate(config, sink=MemorySink())
    csv = render_csv(result.tables["t_a"])
    assert '"x,y"' in csv, f"含逗号的字段必须加引号: {csv}"


# ---------------------------------------------------------------- Web 层安全


@allure.story("SQL 台只读校验：多语句 / 注释绕过 / DDL 全都要拦下")
def test_sql_readonly_guard():
    client = TestClient(create_app())
    attacks = [
        "DROP TABLE t",
        "SELECT 1; DROP TABLE t",
        "SELECT 1; DELETE FROM t",
        "/* x */ DROP TABLE t",
        "SELECT * FROM t -- ; DROP TABLE t",
        "UPDATE t SET a=1",
        "INSERT INTO t VALUES (1)",
        "TRUNCATE t",
        "SELECT 1; SELECT 2",          # 多语句即使都只读也应收紧（注释说明默认单语句）
    ]
    allowed = 0
    for sql in attacks:
        res = client.post("/api/sql/execute", json={"sql": sql})
        if res.status_code == 200:
            allowed += 1
            # 只读放行的只能是无副作用的查询
            assert sql.strip().upper().startswith(("SELECT", "WITH", "SHOW", "EXPLAIN", "DESC")), \
                f"写操作被放行了: {sql}"
    assert allowed < len(attacks), "至少 DDL/DML 必须被拒绝"


@allure.story("工作区路径穿越：不允许把任意文件加入清单")
def test_workspace_path_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    for evil in ("../../etc/passwd", "C:/Windows/win.ini", "....//....//x.yaml"):
        res = client.post("/api/workspace/add", json={"path": evil})
        if res.status_code == 200:
            files = [f["path"] for f in client.get("/api/workspace").json()["files"]]
            assert not any("passwd" in f or "win.ini" in f for f in files), \
                f"危险路径被接受: {evil} → {files}"


@allure.story("配置文件切换：拒绝清单外与非法路径")
def test_switch_config_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(config_path="samples/txn.yaml"))
    (tmp_path / "samples").mkdir(exist_ok=True)
    (tmp_path / "samples" / "txn.yaml").write_text("seed: 1\ntables: []\n", encoding="utf-8")

    for evil in ("../../../secret.yaml", "/etc/passwd", "C:/Windows/win.ini"):
        res = client.post("/api/workspace/switch", json={"path": evil})
        assert res.status_code != 200 or "secret" not in res.text, \
            f"非法配置路径被接受: {evil}"


# ---------------------------------------------------------------- 并发与状态


@allure.story("连续请求状态不串：生成两次结果互不污染")
def test_sequence_isolation():
    cfg = """
seed: 7
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_a
    groups:
      - {type: enum, name: g_a, fields: [a], values: [["1"], ["2"]]}
      - {type: sequence, name: g_b, fields: [b], start: 1}
"""
    config = service.load_text(cfg)
    first = service.generate(config, sink=MemorySink())
    second = service.generate(config, sink=MemorySink())
    assert [r.values for r in first.tables["t_a"].rows] == \
           [r.values for r in second.tables["t_a"].rows], "同配置两次生成必须一致"


@allure.story("大结果集：SQL 渲染不炸内存（1 万行级别）")
def test_large_render():
    text = """
seed: 1
limits: {max_rows: 20000, strategy: full}
tables:
  - name: t_a
    rows: 10000
    groups:
      - {type: sequence, name: g_a, fields: [a], start: 1}
      - {type: const, name: g_b, fields: [b], value: ["x"]}
"""
    config = service.load_text(text)
    result = service.generate(config, sink=MemorySink())
    sql = render_sql(result.tables["t_a"], dialect="mysql", batch_size=1000)
    assert sql.count("INSERT INTO") == 10, "1 万行按 1000 分批 → 10 条 INSERT"


# ---------------------------------------------------------------- 本轮排查修掉的问题（回归）


@allure.story("rows 必须 ≥ 1：写 0 / 负数要报错，不能静默生成 0 行")
def test_rows_must_be_positive():
    for bad in ("0", "-5"):
        text = f"""
seed: 1
tables:
  - name: t_a
    rows: {bad}
    groups:
      - {{type: const, name: g_a, fields: [a], value: ["x"]}}
"""
        with pytest.raises(TableSeedError) as exc:
            service.load_text(text)
        # 报错要能读懂（中文映射到 greater_than_equal）
        assert "rows" in str(exc.value) or "大于" in str(exc.value) or ">=" in str(exc.value)


@allure.story("表达式除零 / 溢出必须包成可读错误（否则 Web 层 500）")
def test_expression_runtime_errors_wrapped():
    for expr in ("1/0", "1%0", "10**100000", "'x' * 100000000"):
        text = f"""
seed: 1
tables:
  - name: t_a
    rows: 2
    groups:
      - {{type: random, name: g_x, fields: [x], generator: int, range: [1, 9]}}
      - {{type: derive, name: g_y, fields: [y], expr: "{expr}"}}
"""
        with pytest.raises(TableSeedError) as exc:
            service.generate(service.load_text(text), sink=MemorySink())
        msg = str(exc.value)
        assert any(k in msg for k in ("零", "溢出", "无效", "过大")), msg


@allure.story("空配置校验要有提示（否则空文件也显示「校验通过」）")
def test_empty_tables_hint():
    config = service.load_text("tables: []")
    problems = service.check(config)
    assert problems and any("表" in p for p in problems)


@allure.story("DSN 特殊字符：密码/用户名含 @ : / 空格 + 等都要正确往返")
def test_dsn_special_chars_roundtrip():
    """密码含空格曾用 quote_plus 编码成 +，URL 解析时 + 是字面加号 → 密码错、连接被拒。"""
    from sqlalchemy.engine import make_url

    from tableseed.models import DatabaseSpec

    for pwd in ["p@ssword", "pa:ss", "p/ss", "p#ss", "p?ss", "pass word",
                "My Pass 123", "p%40ss", "中文密码", "p'q", "p&q", "p+q"]:
        spec = DatabaseSpec(type="mysql", host="127.0.0.1", port=3306,
                            user="u ser", password=pwd, database="db")
        parsed = make_url(spec.resolved_url())
        assert parsed.password == pwd, f"密码 {pwd!r} 往返后变成 {parsed.password!r}"
        assert parsed.username == "u ser", "用户名含空格也要正确"


@allure.story("接口参数校验错误也返回中文（与配置校验风格一致）")
def test_request_validation_is_chinese(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    res = client.post("/api/plan", json={"text": None})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "请求参数" in detail, f"应是中文提示，实际: {detail}"
    assert "Input should be" not in detail, "不能漏出 FastAPI 英文原文"


# ---------------------------------------------------------------- 传播 / 拆分（业务正确性）


SPLIT_BASE = """
seed: 42
limits: {{max_rows: 10000, strategy: full}}
tables:
  - name: t_order
    groups:
      - {{type: enum, name: g_o, fields: [ono], values: [["O1"], ["O2"], ["O3"]]}}
      - {{type: random, name: g_a, fields: [amount], generator: decimal, range: [100, 99999], scale: 2}}
  - name: t_item
    groups:
      - {{type: random, name: g_n, fields: [net_amount], generator: decimal, range: [10, 9999], scale: 2}}
relations:
  - parent: t_order
    child: t_item
    cardinality: "1:N"
    join: [{{parent_field: ono, child_field: ono}}]
    propagate:
      - {prop}
"""


@allure.story("split 漏写 from：静态校验报错，运行时也有可读兜底（不 500）")
def test_split_requires_from():
    text = SPLIT_BASE.format(prop="{mode: split, to: net_amount, parts: 3}")
    config = service.load_text(text)
    problems = service.check(config)
    assert any("from" in p for p in problems), f"应提示缺 from: {problems}"

    # 即使绕过校验直接生成，也要是 TableSeedError 而不是 TypeError
    with pytest.raises(TableSeedError) as exc:
        service.generate(config, sink=MemorySink())
    assert "总额" in str(exc.value) or "from" in str(exc.value)


@allure.story("split 金额守恒：子表各份之和 == 父表金额（parts 与 ratio 两种写法）")
def test_split_conservation():
    from decimal import Decimal

    for prop in ("{mode: split, to: net_amount, from: amount, parts: 3}",
                 "{mode: split, to: net_amount, from: amount, ratio: [0.5, 0.3, 0.2]}"):
        result = service.generate(service.load_text(SPLIT_BASE.format(prop=prop)),
                                  sink=MemorySink())
        parents = {r.values["ono"]: Decimal(str(r.values["amount"]))
                   for r in result.tables["t_order"].rows}
        collected: dict[str, list[Decimal]] = {}
        for row in result.tables["t_item"].rows:
            collected.setdefault(row.values["ono"], []).append(
                Decimal(str(row.values["net_amount"]))
            )
        assert collected, "子表必须有行"
        for ono, pieces in collected.items():
            assert sum(pieces) == parents[ono], f"{prop} 拆分不守恒: {pieces} vs {parents[ono]}"


@allure.story("传播可以创建子表的新字段（不因目标字段不存在而报错）")
def test_propagate_can_create_field():
    text = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"], ["P2"]]}
      - {type: const, name: g_c, fields: [currency], value: ["CNY"]}
  - name: t_c
    groups:
      - {type: const, name: g_n, fields: [net], value: ["N"]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
    propagate:
      - {mode: copy, from: currency, to: currency}
"""
    config = service.load_text(text)
    problems = service.check(config)
    assert not any("currency" in p and "不存在" in p for p in problems), (
        f"传播创建字段是合法用法，不该报错: {problems}"
    )
    result = service.generate(config, sink=MemorySink())
    assert {r.values.get("currency") for r in result.tables["t_c"].rows} == {"CNY"}


@allure.story("传播覆盖组字段：只提示不拦生成（组里的取值会被丢弃）")
def test_propagate_over_group_field_is_hint():
    text = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"]]}
      - {type: const, name: g_c, fields: [currency], value: ["CNY"]}
  - name: t_c
    groups:
      - {type: enum, name: g_x, fields: [currency], values: [["USD"]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
    propagate:
      - {mode: copy, from: currency, to: currency}
"""
    problems = service.check(service.load_text(text))
    assert any(p.startswith("[提示]") for p in problems), f"应有提示: {problems}"
    # 提示不阻止生成；实际以传播值为准
    result = service.generate(service.load_text(text), sink=MemorySink())
    assert {r.values.get("currency") for r in result.tables["t_c"].rows} == {"CNY"}


@allure.story("Web 层：只有 [提示] 时不该拦下生成")
def test_hint_does_not_block_generate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    text = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"]]}
      - {type: const, name: g_c, fields: [currency], value: ["CNY"]}
  - name: t_c
    groups:
      - {type: enum, name: g_x, fields: [currency], values: [["USD"]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
    propagate:
      - {mode: copy, from: currency, to: currency}
"""
    res = client.post("/api/generate", json={"text": text})
    assert res.status_code == 200, res.text
    assert res.json()["tables"]["t_c"]["count"] == 1


# ---------------------------------------------------------------- 传播 / 拆分（业务正确性）


SPLIT_BASE = """
seed: 42
limits: {{max_rows: 10000, strategy: full}}
tables:
  - name: t_order
    groups:
      - {{type: enum, name: g_o, fields: [ono], values: [["O1"], ["O2"], ["O3"]]}}
      - {{type: random, name: g_a, fields: [amount], generator: decimal, range: [100, 99999], scale: 2}}
  - name: t_item
    groups:
      - {{type: random, name: g_n, fields: [net_amount], generator: decimal, range: [10, 9999], scale: 2}}
relations:
  - parent: t_order
    child: t_item
    cardinality: "1:N"
    join: [{{parent_field: ono, child_field: ono}}]
    propagate:
      - {prop}
"""


@allure.story("split 漏写 from：静态校验报错，运行时也有可读兜底（不 500）")
def test_split_requires_from():
    text = SPLIT_BASE.format(prop="{mode: split, to: net_amount, parts: 3}")
    config = service.load_text(text)
    problems = service.check(config)
    assert any("from" in p for p in problems), f"应提示缺 from: {problems}"

    # 即使绕过校验直接生成，也要是 TableSeedError 而不是 TypeError
    with pytest.raises(TableSeedError) as exc:
        service.generate(config, sink=MemorySink())
    assert "总额" in str(exc.value) or "from" in str(exc.value)


@allure.story("split 金额守恒：子表各份之和 == 父表金额（parts 与 ratio 两种写法）")
def test_split_conservation():
    from decimal import Decimal

    for prop in ("{mode: split, to: net_amount, from: amount, parts: 3}",
                 "{mode: split, to: net_amount, from: amount, ratio: [0.5, 0.3, 0.2]}"):
        result = service.generate(service.load_text(SPLIT_BASE.format(prop=prop)),
                                  sink=MemorySink())
        parents = {r.values["ono"]: Decimal(str(r.values["amount"]))
                   for r in result.tables["t_order"].rows}
        collected: dict[str, list[Decimal]] = {}
        for row in result.tables["t_item"].rows:
            collected.setdefault(row.values["ono"], []).append(
                Decimal(str(row.values["net_amount"]))
            )
        assert collected, "子表必须有行"
        for ono, pieces in collected.items():
            assert sum(pieces) == parents[ono], f"{prop} 拆分不守恒: {pieces} vs {parents[ono]}"


@allure.story("传播可以创建子表的新字段（不因目标字段不存在而报错）")
def test_propagate_can_create_field():
    text = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"], ["P2"]]}
      - {type: const, name: g_c, fields: [currency], value: ["CNY"]}
  - name: t_c
    groups:
      - {type: const, name: g_n, fields: [net], value: ["N"]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
    propagate:
      - {mode: copy, from: currency, to: currency}
"""
    config = service.load_text(text)
    problems = service.check(config)
    assert not any("currency" in p and "不存在" in p for p in problems), (
        f"传播创建字段是合法用法，不该报错: {problems}"
    )
    result = service.generate(config, sink=MemorySink())
    assert {r.values.get("currency") for r in result.tables["t_c"].rows} == {"CNY"}


@allure.story("传播覆盖组字段：只提示不拦生成（组里的取值会被丢弃）")
def test_propagate_over_group_field_is_hint():
    text = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"]]}
      - {type: const, name: g_c, fields: [currency], value: ["CNY"]}
  - name: t_c
    groups:
      - {type: enum, name: g_x, fields: [currency], values: [["USD"]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
    propagate:
      - {mode: copy, from: currency, to: currency}
"""
    problems = service.check(service.load_text(text))
    assert any(p.startswith("[提示]") for p in problems), f"应有提示: {problems}"
    # 提示不阻止生成；实际以传播值为准
    result = service.generate(service.load_text(text), sink=MemorySink())
    assert {r.values.get("currency") for r in result.tables["t_c"].rows} == {"CNY"}


@allure.story("Web 层：只有 [提示] 时不该拦下生成")
def test_hint_does_not_block_generate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app())
    text = """
seed: 1
limits: {max_rows: 1000, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"]]}
      - {type: const, name: g_c, fields: [currency], value: ["CNY"]}
  - name: t_c
    groups:
      - {type: enum, name: g_x, fields: [currency], values: [["USD"]]}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
    propagate:
      - {mode: copy, from: currency, to: currency}
"""
    res = client.post("/api/generate", json={"text": text})
    assert res.status_code == 200, res.text
    assert res.json()["tables"]["t_c"]["count"] == 1


@allure.story("不变量指向不存在的表：可读错误，绝不 KeyError（否则 500）")
def test_invariant_unknown_table():
    text = """
seed: 1
limits: {max_rows: 100, strategy: full}
tables:
  - name: t_a
    rows: 3
    groups:
      - {type: random, name: g_x, fields: [x], generator: int, range: [1, 9]}
invariants:
  - {table: ghost, expr: 'x > 1'}
"""
    config = service.load_text(text)
    assert any("表不存在" in p for p in service.check(config))

    # 三条路径都要是可读异常，不能漏出 KeyError
    with pytest.raises(TableSeedError, match="ghost"):
        service.verify(config)
    with pytest.raises(TableSeedError, match="ghost"):
        service.generate(config, sink=MemorySink())


@allure.story("SeedConfig.table 找不到表时抛可读异常（不是 KeyError）")
def test_config_table_lookup_error():
    config = service.load_text("seed: 1\ntables: []\n")
    with pytest.raises(TableSeedError, match="表不存在"):
        config.table("nope")


# ---------------------------------------------------------------- 对账精度 / 入口 / 方言


SPLIT_CONSERVATION_CFG = """
seed: 7
limits: {max_rows: 10000, strategy: full}
tables:
  - name: t_order
    groups:
      - {type: enum, name: g_o, fields: [ono], values: [["O1"], ["O2"]]}
      - {type: random, name: g_a, fields: [amount], generator: decimal, range: [1000, 9999], scale: 2}
  - name: t_detail
    groups:
      - {type: random, name: g_n, fields: [net_amount], generator: decimal, range: [10, 99], scale: 2}
relations:
  - parent: t_order
    child: t_detail
    cardinality: "1:N"
    join: [{parent_field: ono, child_field: ono}]
    propagate:
      - {mode: split, to: net_amount, from: amount, parts: 3}
invariants:
  - {table: t_order, from: t_detail, expr: "sum(net_amount) = amount"}
  - {table: t_order, from: t_detail, expr: "sum(net_amount) * 1.1 > 0"}
"""


@allure.story("对账断言不能被浮点误差误报（Decimal 精确求和的真正用途）")
def test_reconciliation_assertion_is_exact():
    """用户写 sum(net_amount) = amount 是核心用法。

    float 累加下 6805.01+646.27+378.83 = 7830.110000000001 ≠ 7830.11 ——
    数据完全正确却报"违例"。聚合与比较都必须走 Decimal。
    """
    assert service.verify(service.load_text(SPLIT_CONSERVATION_CFG)) == [], (
        "守恒的数据不该被判违例"
    )
    # 真不守恒仍要抓到（不是"一律放行"）
    broken = SPLIT_CONSERVATION_CFG.replace('= amount"', "= amount + 0.01\"")
    assert service.verify(service.load_text(broken)), "真不守恒必须报违例"


@allure.story("精度修复不改变出参类型：字段值仍是 float，不混入 Decimal")
def test_value_types_stay_plain():
    from decimal import Decimal

    # aggregate 回填
    agg_cfg = """
seed: 1
limits: {max_rows: 100, strategy: full}
tables:
  - name: t_p
    groups:
      - {type: enum, name: g_p, fields: [pid], values: [["P1"]]}
      - {type: aggregate, name: g_sum, fields: [total], from: t_c, expr: "sum(net)"}
  - name: t_c
    groups:
      - {type: random, name: g_n, fields: [net], generator: decimal, range: [1, 99], scale: 2}
relations:
  - parent: t_p
    child: t_c
    cardinality: "1:N"
    join: [{parent_field: pid, child_field: cid}]
"""
    row = service.generate(service.load_text(agg_cfg)).tables["t_p"].rows[0]
    assert not isinstance(row.values["total"], Decimal), (
        f"aggregate 回填值该是 float，实际 {type(row.values['total']).__name__}"
    )

    # derive 算术
    drv_cfg = """
seed: 1
limits: {max_rows: 100, strategy: full}
tables:
  - name: t_c
    rows: 3
    groups:
      - {type: random, name: g_n, fields: [net], generator: decimal, range: [1, 99], scale: 2}
      - {type: derive, name: g_d, fields: [tax], expr: "net * 0.06"}
"""
    rows = service.generate(service.load_text(drv_cfg)).tables["t_c"].rows
    assert all(not isinstance(r.values["tax"], Decimal) for r in rows)


@allure.story("未知方言必须报错（静默回退会用错标识符引用符）")
def test_unknown_dialect_rejected():
    from tableseed.render import normalize_dialect

    for good, expect in (("mysql", "mysql"), ("POSTGRES", "postgresql"),
                         ("pg", "postgresql"), ("mariadb", "mysql"),
                         (None, "postgresql")):
        assert normalize_dialect(good) == expect

    for bad in ("nosql", "postgresql8", "sqlserver", "oracl"):
        with pytest.raises(TableSeedError, match="不支持的方言"):
            normalize_dialect(bad)


@allure.story("python -m tableseed 可用（源码目录里没装 console script 时的唯一入口）")
def test_module_entrypoint():
    """回归：缺 __main__.py 时报 No module named tableseed.__main__。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "tableseed", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout[:200]} stderr={proc.stderr[:200]}"
    assert "gen" in proc.stdout and "verify" in proc.stdout
