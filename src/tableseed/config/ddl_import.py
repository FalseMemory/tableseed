"""从 DDL（建表语句）与 INSERT 样例快速生成 YAML 配置草稿。

场景：库里已有现成的表，想为它造数。把建表语句和几条真实 INSERT 贴进来，
得到一份可继续编辑的 tableseed 配置初稿 —— 字段、类型、分组、取值范围
都有了合理的默认值，剩下的交给「配置编辑器」精修。

解析是**有意的简化**（不引第三方 SQL 解析库）：
- DDL 只处理常见的列定义形态（MySQL / Oracle 风格的反引号、双引号、裸名）
- INSERT 用小型 tokenizer 切 VALUES，能正确处理字符串里的逗号与转义引号
- 解析失败的部分跳过而非报错 —— 这是「草稿生成器」，不是 SQL 校验器

分组启发式（一列一组，用户可在编辑器里再合并/调整）：

+--------------------------+----------------------------------+
| 列特征                   | 生成的组                          |
+--------------------------+----------------------------------+
| 主键 / _no / _id 后缀    | sequence（自增编号）              |
| 只有 1 条样例（非主键）  | const（单值无推断空间，不猜）     |
| 样例值全部相同           | const                             |
| 样例 distinct ≤ 8        | enum（取值来自样例）              |
| decimal(p,s)             | random decimal（范围按样例扩缩）  |
| 整型                     | random int                        |
| date / datetime          | random date（样例前后一年）       |
| 其余                     | enum（样例值）或 random string    |
+--------------------------+----------------------------------+
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Column", "parse_create_table", "parse_inserts", "generate_yaml"]


@dataclass
class Column:
    """一张表的一列：DDL 声明 + INSERT 样例值。"""

    name: str
    type_raw: str = ""            # 原始类型串，如 varchar(32)
    nullable: bool = True
    primary_key: bool = False
    comment: str = ""
    samples: list[Any] = field(default_factory=list)  # INSERT 里的样例值
    #: 类型信息来源 —— "ddl"（有建表语句，如实尊重声明）
    #: 或 "samples"（只有 INSERT，类型靠样例猜，需要更多的启发式兜底）
    type_source: str = "ddl"


# ---------------------------------------------------------------- DDL 解析

#: 标识符（可带反引号/双引号/中括号引号）
_IDENT = r"[`\"\[]?(\w+)[`\"\]]?"
#: 限定名（库名.表名 / 模式.表名）—— 生产库常用 `core_db.t_account` 这种命名，
#: 只按第一个标识符解析会把表名截成 `core_db`（用户报过）。
_QUALIFIED = r"[`\"\[]?(\w+)[`\"\]]?(?:\.[`\"\[]?(\w+)[`\"\]]?)?"
_TYPE_RE = re.compile(
    r"^(?P<base>[A-Za-z]+)\s*(?:\(\s*(?P<p1>\d+)\s*(?:,\s*(?P<p2>\d+))?\s*\))?",
    re.IGNORECASE,
)


def parse_create_table(ddl: str) -> tuple[str, list[Column]]:
    """解析 CREATE TABLE，返回 (表名, 列列表)。解析失败返回 ("", [])。"""
    if not ddl or not ddl.strip():
        return "", []

    # 表名：支持 `库名.表名` 限定写法（生产库常用）—— 取**最后一段**作为表名，
    # 库名只是限定符，不该混进表名（否则 YAML 里表名变成 `core_db`）。
    table_match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _QUALIFIED,
        ddl,
        re.IGNORECASE,
    )
    table_name = table_match.group(2) or table_match.group(1) if table_match else ""

    # 取括号体：第一处 ( 到与之配对的 )
    body_match = re.search(r"\((.*)\)", ddl, re.DOTALL)
    if not body_match:
        return table_name, []

    body = body_match.group(1)
    columns: list[Column] = []
    pk_columns: set[str] = set()

    # 按逗号切顶层（括号深度 0）——列定义 / 约束
    for chunk in _split_top_level(body):
        chunk = chunk.strip()
        if not chunk:
            continue
        upper = chunk.upper()

        if upper.startswith(("PRIMARY KEY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FOREIGN")):
            for name in re.findall(_IDENT + r"\s*\)", chunk):
                pass
            # PRIMARY KEY (`a`, `b`)
            if upper.startswith("PRIMARY"):
                pk_columns.update(re.findall(_IDENT, chunk)[1:])
            continue

        m = re.match(_IDENT + r"\s+(.+)", chunk, re.DOTALL)
        if not m:
            continue
        name = m.group(1)
        rest = m.group(2)

        type_match = _TYPE_RE.match(rest.strip())
        type_raw = type_match.group(0).strip() if type_match else rest.split()[0]

        column = Column(
            name=name,
            type_raw=type_raw,
            nullable=not re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE),
            primary_key=False,
        )
        comment = re.search(r"COMMENT\s+'((?:[^']|'')*)'", rest, re.IGNORECASE)
        if comment:
            column.comment = comment.group(1).replace("''", "'")
        if re.search(r"\bAUTO_INCREMENT\b", rest, re.IGNORECASE) or re.search(
            r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE
        ):
            column.primary_key = True  # AUTO_INCREMENT 或列内联 PRIMARY KEY
        columns.append(column)

    for column in columns:
        if column.name in pk_columns:
            column.primary_key = True
    return table_name, columns


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切块 —— 字符串字面量与括号内的逗号不切。"""
    parts: list[str] = []
    depth = 0
    in_string = False
    quote = ""
    current: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:  # '' 转义
                    current.append(text[i + 1])
                    i += 1
                else:
                    in_string = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = True
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current))
    return parts


# ---------------------------------------------------------------- INSERT 解析


def parse_inserts(sql: str, table: str | None = None) -> dict[str, list[Any]]:
    """解析 INSERT 语句，返回 列 → 样例值列表（按出现顺序）。

    支持一条语句多组 VALUES；字符串里的逗号、引号转义都能正确切开。
    与 DDL 无关 —— 列名以 INSERT 里声明的为准。
    """
    samples: dict[str, list[Any]] = {}
    if not sql or not sql.strip():
        return samples

    for statement in re.split(r";\s*(?:\n|$)", sql):
        m = re.search(
            r"INSERT\s+(?:IGNORE\s+)?INTO\s+" + _QUALIFIED + r"\s*\(([^)]*)\)\s*VALUES\s*(.*)",
            statement,
            re.IGNORECASE | re.DOTALL,
        )
        if not m:
            continue
        # 限定名取最后一段（`core_db.t_account` → `t_account`）
        insert_table = m.group(2) or m.group(1)
        if table and insert_table.lower() != table.lower():
            continue
        # _QUALIFIED 占 1、2 组，列清单与 VALUES 顺延到 3、4
        columns = [re.match(_IDENT, c.strip()).group(1) for c in m.group(3).split(",")]
        rows = _split_top_level(m.group(4).strip())
        for row in rows:
            row = row.strip()
            if row.startswith("(") and row.endswith(")"):
                row = row[1:-1]
            values = _parse_values(row)
            for col, value in zip(columns, values):
                samples.setdefault(col, []).append(value)
    return samples


def _parse_values(text: str) -> list[Any]:
    """切一行 VALUES 的值：数字转数值，NULL → None，其余保留字符串。"""
    values: list[Any] = []
    for raw in _split_top_level(text):
        raw = raw.strip()
        if raw.upper() == "NULL":
            values.append(None)
        elif re.fullmatch(r"-?\d+", raw):
            values.append(int(raw))
        elif re.fullmatch(r"-?\d+\.\d+", raw):
            values.append(float(raw))
        elif len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            values.append(raw[1:-1].replace("''", "'"))  # 还原 '' 转义
        else:
            values.append(raw)
    return values


# ---------------------------------------------------------------- YAML 生成


def generate_yaml(
    ddl: str,
    inserts: str = "",
    table: str | None = None,
    seed: int = 20260910,
) -> str:
    """从 DDL + INSERT 样例生成 YAML 配置草稿。

    **两者至少要有一个**，而且**只有 INSERT 也能出完整配置**：
    生产环境常常拿不到建表语句，只有几条真实数据 —— 那就用 INSERT 里
    声明的列名当列清单，样例值当类型推断依据（`txn_no` 这类 _no/_id 结尾
    的列自动当自增主键，否则单值样例会让主键全相同、必然冲突）。
    """
    t = table.strip() if table else ""
    ddl_table, columns = parse_create_table(ddl)
    ins_table = insert_table_name(inserts)
    table_name = ddl_table or t or ins_table or "t_new_table"

    # 用最终表名去取样例；表名对不上时退化为"不管表名，取所有 INSERT 的列"
    samples = parse_inserts(inserts, table_name) if inserts else {}
    if not samples and inserts:
        samples = parse_inserts(inserts)
    origin = "ddl"
    if not columns:
        if not samples:
            return _empty_yaml(table_name, ddl_given=bool(ddl and ddl.strip()))
        columns = [
            Column(
                name=name,
                type_raw=_guess_type_from_samples(values),
                samples=values,
                type_source="samples",     # 没有 DDL，主键只能靠名字猜
            )
            for name, values in samples.items()
        ]
        origin = "insert"
    else:
        for column in columns:
            column.samples = samples.get(column.name, [])

    if origin == "ddl":
        first_line = "# 由建表语句与 INSERT 样例自动生成 —— 字段分组为草稿, 请按需调整"
    else:
        first_line = "# 未提供建表语句 —— 列与类型由 INSERT 样例推断, 字段分组为草稿, 请按需调整"

    head = [
        first_line,
        f"# 表: {table_name}  生成时间标记见 seed",
        f"seed: {seed}",
        "",
        "limits:",
        "  max_rows: 100000",
        "  strategy: full",
        "",
        "tables:",
        f"  - name: {table_name}",
    ]

    used_names: set[str] = set()
    used_types: set[str] = set()
    body: list[str] = []
    for column in columns:
        body.extend(_group_lines(column, table_name, used_names, used_types))

    # 没有任何有限取值组（字段全是 random / sequence / const / derive）→
    # 笛卡尔积无从展开，行数必须显式声明。
    # 默认值取**样例条数**（1 条 INSERT 样例 → 1 行）——
    # 曾经硬编码 100，导致"我给了一条样例却造出 100 行"的落差。
    # 想造更多行时，用户把这个值调大即可。
    if not (used_types & {"enum", "boundary", "dict"}):
        sample_rows = max((len(v) for v in samples.values()), default=0)
        default_rows = max(sample_rows, 1)
        head.append(
            f"    rows: {default_rows}"
            f"   # 全为逐行组, 行数按此声明（当前 {default_rows} 行, 要更多改这个数）"
        )

    lines = [*head, "    groups:", *body]
    return "\n".join(lines) + "\n"


def _unique_group_name(column: Column, table_name: str, used: set[str]) -> str:
    base = f"g_{column.name}"
    if base not in used:
        return base
    return f"g_{table_name}_{column.name}"


def _infer_type(column: Column) -> str:
    """推断一列该用哪种组 —— 判断只有这一处，渲染与统计共用，避免漂移。

    返回 sequence / const / enum / random 之一。

    规则：**只有 1 条样例（非主键）时一律 const** —— 一个值没有任何推断空间，
    猜 enum/random 都是编造；只有多条 INSERT 时才值得自动判断类型。
    主键除外：单值 const 会让主键全部相同，必然冲突，维持 sequence。
    """
    base_type = (column.type_raw or "").lower()
    samples = [v for v in column.samples if v is not None]
    # 编号类列判定：_no / _seq 结尾（业务流水号，几乎总唯一）；
    # **_id 结尾只有数值类型才算** —— 字符串型 `tenant_id = '0001'` 更像业务码/外键，
    # 判成自增会把"租户号"变成递增数字，语义就错了。
    numeric = bool(re.match(
        r"^(int|bigint|smallint|tinyint|integer|number|decimal|numeric)", base_type
    ))
    is_id_like = bool(
        column.primary_key
        or re.search(r"(_no|_seq)$", column.name, re.IGNORECASE)
        or (re.search(r"_id$", column.name, re.IGNORECASE) and numeric)
    )

    # 全是 NULL 的列：忠实于样例 —— 生成 NULL，不猜值
    if column.samples and not samples:
        return "const"

    # 主键 —— 无论样例多少都用 sequence
    if column.primary_key:
        return "sequence"
    # **只有"没有 DDL"时才靠名字猜唯一键**：有建表语句就如实尊重声明，
    # 否则会打破用户既有的分组设计（例如 acct_no 本来要用作枚举做覆盖组合）。
    # 没有 DDL 时若无此启发式：1 条样例的 txn_no 会变成 const，
    # 生成多行主键全相同、插入必然冲突。
    if column.type_source == "samples" and is_id_like and (
        not samples or len(set(map(str, samples))) == len(samples)
    ):
        return "sequence"
    # 只有 1 条样例（或样例值完全一致）→ const，不猜
    if len(samples) == 1 or (samples and len(set(map(str, samples))) == 1):
        return "const"
    # 数值与日期：类型优先于取值枚举（金额不该被样例限死成 enum）
    if re.match(r"^(decimal|numeric|number|int|bigint|smallint|tinyint|integer)", base_type):
        return "random"
    if re.match(r"^(date|datetime|timestamp)", base_type):
        return "random"
    # 字符串：样例值就是天然的枚举候选
    if samples:
        return "enum"
    return "random"


def _group_lines(
    column: Column, table_name: str, used: set[str], used_types: set[str] | None = None
) -> list[str]:
    """一列 → 一个组的 YAML 片段（启发式见模块 docstring）。"""
    name = _unique_group_name(column, table_name, used)
    used.add(name)
    kind = _infer_type(column)
    if used_types is not None:
        used_types.add(kind)

    indent = "      "
    base = f"{indent}- type: {kind}\n{indent}  name: {name}\n{indent}  fields: [{column.name}]"
    samples = [v for v in column.samples if v is not None]
    base_type = (column.type_raw or "").lower()

    if kind == "sequence":
        if re.match(r"^(bigint|int|smallint|tinyint|integer)", base_type):
            return _render(base, name, indent, extra=["start: 1"])
        return _render(base, name, indent,
                       extra=["start: 1", f'format: "{column.name}_{{seq:06d}}"'])

    if kind == "const":
        # 空值优先用原始样例（全 NULL 列会被推断成 const，此时 samples 里没有值）
        if samples:
            literal = _y(samples[0])
        elif column.samples:
            literal = "null"          # 样例全是 NULL → 就生成 NULL
        else:
            literal = '""'            # 完全没有样例 → 空串占位，用户自行填
        return _render(base, name, indent, extra=[f"value: [{literal}]"])

    if kind == "enum":
        distinct = list(dict.fromkeys(samples))
        values = ", ".join(f"[{_y(v)}]" for v in distinct[:8])
        return _render(base, name, indent, extra=[f"values: [{values}]"])

    # random：按列类型选生成器
    if re.match(r"^(decimal|numeric|number)", base_type):
        scale = _scale_of_type(base_type)
        low, high = _numeric_range(samples, float)
        return _render(base, name, indent, extra=[
            "generator: decimal", f"range: [{low}, {high}]", f"scale: {scale}",
        ])
    if re.match(r"^(int|bigint|smallint|tinyint|integer)", base_type):
        low, high = _numeric_range(samples, int)
        return _render(base, name, indent, extra=[
            "generator: int", f"range: [{low}, {high}]",
        ])
    if re.match(r"^(date|datetime|timestamp)", base_type):
        anchor = samples[0] if samples else "2026-01-01"
        return _render(base, name, indent, extra=[
            "generator: date", f'range: ["{anchor}", "2026-12-31"]',
        ])
    return _render(base, name, indent, extra=["generator: string", "range: [6, 16]"])


def _render(template: str, name: str, indent: str, extra: list[str]) -> list[str]:
    lines = template.splitlines()
    for item in extra:
        lines.append(f"{indent}  {item}")
    return lines


def _y(value: Any) -> str:
    """YAML 字面量：字符串加引号，数字原样。"""
    if isinstance(value, str):
        return '"' + value.replace('"', '\\"') + '"'
    return str(value)


def _scale_of_type(type_raw: str) -> int:
    m = re.search(r",\s*(\d+)\s*\)", type_raw)
    return int(m.group(1)) if m else 2


def _numeric_range(samples: list[Any], cast) -> tuple[int, int]:
    nums = [cast(v) for v in samples if isinstance(v, (int, float))]
    if nums:
        low, high = min(nums), max(nums)
        return int(low * 0.5), int(high * 2) + 1
    return 0, 1000


def insert_table_name(sql: str) -> str:
    """从 INSERT 语句里取表名（没有则返回空串）。

    支持 `库名.表名` 限定写法 —— 取最后一段作为表名。
    """
    m = re.search(
        r"INSERT\s+(?:IGNORE\s+)?INTO\s+" + _QUALIFIED, sql or "", re.IGNORECASE
    )
    return (m.group(2) or m.group(1)) if m else ""


def _guess_type_from_samples(values: list[Any]) -> str:
    """没有 DDL 时，按样例值推断一个"伪类型串"，供 _infer_type 决策。

    **按 Python 类型判断，而不是拿字符串去匹配数字正则** ——
    `_parse_values` 已经把 INSERT 原文的引号信息转成了类型：
    裸数字 → int/float，带引号的 → str。所以 `'0001'`（业务码）
    不会被误判成数值列，`12345`（裸数字）才是。

    推断结果只影响生成器选择（整数/小数/日期/字符串），不参与建表 ——
    猜错也不致命，用户可以在配置里改。
    """
    real = [v for v in values if v is not None]
    if not real:
        return "varchar(255)"
    if all(isinstance(v, bool) is False and isinstance(v, int) for v in real):
        return "bigint" if max(len(str(abs(v))) for v in real) > 9 else "int"
    if all(isinstance(v, (int, float)) for v in real):
        return "decimal(18,2)"
    texts = [str(v) for v in real]
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) for s in texts):
        return "date"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", s) for s in texts):
        return "datetime"
    longest = max(len(s) for s in texts)
    return f"varchar({max(32, longest)})"


def _empty_yaml(table_name: str, ddl_given: bool = False) -> str:
    hint = (
        "# 无法生成配置：建表语句里没解析到列 —— 请检查格式（是否含 CREATE TABLE 与括号内的列定义）"
        if ddl_given
        else "# 无法生成配置：未填写建表语句也没贴 INSERT 样例 —— 两者至少填一个，才能推出列清单"
    )
    return f"""{hint}
seed: 20260910

limits:
  max_rows: 100000
  strategy: full

tables:
  - name: {table_name}
    groups:
      - {{type: enum, name: g_status, fields: [status], values: [["01"], ["02"]]}}
"""
