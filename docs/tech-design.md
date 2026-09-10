# tableseed 技术方案与选型

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v0.1（草案） |
| 日期 | 2026-09-10 |
| 状态 | 待评审 |
| 关联文档 | [PRD.md](PRD.md)、[README.md](../README.md) |

---

## 1. 选型总表

| 关注点 | 决策 | 理由 | 备选与否决原因 |
| --- | --- | --- | --- |
| 主语言 | **Python 3.13** | 生态成熟（数据库驱动/数据处理），元数据反射与表达式处理便利；与既有测试平台同栈 | Go（元数据生态弱）、Node（数值与日期精度处理易踩坑） |
| CLI 框架 | **Typer** | 基于类型注解，自动生成 help，子命令组织清晰 | argparse（样板代码多）、Click（需装饰器与类型分离） |
| 配置格式 | **YAML + pydantic v2** | YAML 可注释、可 diff、适合人工维护；pydantic 提供强校验与友好报错 | TOML（不适合深层嵌套列表）、JSON（不可注释）、纯 Python（不安全且难评审） |
| 元数据读取 | **SQLAlchemy Core（Inspector）** | 一套 API 覆盖多方言，反射表结构成熟 | 各库原生驱动（工作量大）、sqlglot 解析 DDL（仅作 DDL 来源补充） |
| 表达式引擎 | **自研（Python `ast` + 白名单求值）** | 需要支持自定义聚合语法 `sum(x) by k`，通用求值库不支持；`ast` 解析天然安全可控、零额外依赖 | `simpleeval`（不支持聚合）、`asteval`（体积大且允许属性访问，需额外加固）、`eval`（禁止使用） |
| 随机数 | **`random.Random(seed)` 独立实例** | 与全局随机状态隔离，保证可复现（NFR-2） | `random` 全局函数（受其他代码调用影响，不可复现） |
| 错误提示 | **带路径与行号的 `ConfigError`** | 配置动辄数百行，需精确定位 | 裸异常栈（不可读） |
| 测试 | **pytest + allure** | 与既有项目栈一致；allure 中文用例便于评审 | unittest（样板多） |
| 依赖管理 | **venv + requirements.txt** | 与既有项目一致，Windows 环境无额外心智负担 | Poetry / uv（团队认知成本） |
| 输出 | **自研 SQL 生成 + 标准库 CSV + 驱动批量写入** | SQL 方言差异大，模板可控 | SQLAlchemy ORM（为造数引入 ORM 过重） |
| WebUI 后端 | **FastAPI + uvicorn** | 与既有项目栈一致；原生支持 SSE 流式进度；pydantic v2 与配置校验复用同一套模型 | Flask（异步与流式支持弱）、Streamlit（配置编辑与表格控制力不足） |
| WebUI 前端 | **React 19 + Vite + TypeScript + Tailwind v4** | 与既有项目同栈，可复用组件经验与设计规范 | Jinja + 原生 JS（编辑器与表格体验差）、Vue（与既有栈不一致） |
| 实时进度 | **SSE（Server-Sent Events）** | 生成进度是单向推送，SSE 比 WebSocket 简单且无握手协议 | WebSocket（双向能力用不上，复杂度更高） |
| DataFrame 导出 | **pandas 作可选依赖（惰性导入）** | 用户明确需要 DataFrame 形态；但不强制安装，核心返回自有内存对象 | 强制依赖（拖慢安装与启动）、仅返回 list[dict]（不便分析） |
| 只读 SQL 网关 | **M1 正则前缀判定 → M2 引入 sqlglot 解析语句类型** | 需要可靠判断语句类别以落实只读；M1 不引依赖，正则兜底，M2 加固 | 仅 `startswith`（易被注释绕过，仅作 M1 过渡） |
| 日志 | **标准库 logging + Rich（进度条）** | 零侵入，CLI 体验好 | print（无法分级） |

**明确不引入**：任何 ORM、任何代码生成、前端 UI 组件库（Tailwind 手写保持轻量）。pandas 与 sqlglot 按可选依赖处理，函数内惰性导入。

## 2. 架构分层

```
┌──────────────────────────────────────────────────────┐
│  cli.py        Typer 子命令：plan / check / gen / verify / ui │
├──────────────────────────────────────────────────────┤
│  web/          FastAPI：REST API + SSE 进度 + SQL 查询台   │
├──────────────────────────────────────────────────────┤
│  service.py    对 CLI 与 WebUI 统一暴露的业务入口          │
├──────────────────────────────────────────────────────┤
│  config/       加载 YAML → 校验 → IR 对象                 │
│  metadata/     元数据扫描（DDL/反射/手工）与类型映射       │
├──────────────────────────────────────────────────────┤
│  plan/         依赖图 → 拓扑排序 → 规模预演                │
├──────────────────────────────────────────────────────┤
│  engine/       生成引擎（P1 正向 / P2 回填）               │
│   ├ group_expander  有限取值组笛卡尔积展开                  │
│   ├ allocator       1:1 分配策略                            │
│   ├ propagator      表间字段传播                            │
│   └ aggregator      P2 汇总回填                             │
├──────────────────────────────────────────────────────┤
│  expr/         表达式解析与沙箱求值（全局复用）            │
├──────────────────────────────────────────────────────┤
│  sink/         执行策略分派（结果去向）                    │
│   ├ db_sink       提供连接 → 直接执行入库                   │
│   ├ memory_sink   无连接 → 只生成不落盘，返回内存对象        │
│   └ file_sink     显式 --out → 落盘 SQL / CSV               │
├──────────────────────────────────────────────────────┤
│  verify/       invariants 校验 + 覆盖报告                  │
└──────────────────────────────────────────────────────┘
```

**三个关键架构决策**：

1. **`service.py` 是唯一业务入口** —— CLI 与 WebUI 都只调用它，不各自实现逻辑。避免"CLI 能跑但页面结果不一致"这类分叉。
2. **`sink/` 抽象取代原来的 `output/`** —— 结果的去向是一个可替换的策略（入库 / 内存 / 落盘），生成引擎不关心数据最终去哪。这是 D-5「按连接有无分派」在架构上的落点。
3. **`engine` 不碰 IO** —— 引擎只产出 `TableData` 内存对象，由 `sink` 消费。既保证可单测（NFR-5），也让"不落盘"成为默认行为（NFR-8）。

## 3. 目录结构

```
tableseed/
├── README.md
├── docs/
│   ├── PRD.md
│   └── tech-design.md
├── requirements.txt
├── pyproject.toml              # 打包与工具配置（ruff / pytest）
├── samples/
│   └── account.yaml            # 12 行示例（端到端验收用）
├── src/tableseed/
│   ├── __init__.py
│   ├── cli.py                  # Typer 入口
│   ├── service.py              # CLI 与 WebUI 共用的业务入口（唯一逻辑出口）
│   ├── errors.py               # ConfigError / ExprError / PlanError
│   ├── models.py               # 全部 IR 数据结构（pydantic）
│   ├── rng.py                  # 种子化随机源
│   ├── config/
│   │   ├── loader.py           # YAML → dict → IR
│   │   └── checker.py          # check 子命令的静态校验
│   ├── metadata/
│   │   ├── base.py             # ColumnMeta / TableMeta 抽象与类型系统
│   │   ├── ddl.py              # DDL 文件解析
│   │   ├── reflect.py          # SQLAlchemy Inspector 反射
│   │   └── mapping.py          # 方言类型 → 内部类型
│   ├── expr/
│   │   ├── parser.py           # ast 白名单校验
│   │   ├── evaluator.py        # 求值器
│   │   └── functions.py        # 内置函数库
│   ├── plan/
│   │   ├── graph.py            # 依赖图构建（正向 FK + 反向 aggregate）
│   │   ├── topo.py             # 拓扑排序与环检测
│   │   └── sizing.py           # 组合数与行数预演
│   ├── engine/
│   │   ├── table_gen.py        # 单表生成主流程
│   │   ├── group_expander.py   # 笛卡尔积展开
│   │   ├── allocator.py        # 分配策略
│   │   ├── propagator.py       # 表间传播
│   │   └── aggregator.py       # P2 回填
│   ├── sink/
│   │   ├── base.py             # Sink 抽象（write(TableData)）
│   │   ├── db.py               # db_sink：直连批量入库
│   │   ├── memory.py           # memory_sink：内存对象 + to_dataframe() 等导出
│   │   └── files.py            # file_sink：SQL / CSV 落盘
│   ├── web/
│   │   ├── app.py              # FastAPI 实例、静态文件挂载、启动入口
│   │   ├── routes_config.py    # 配置读写与校验
│   │   ├── routes_run.py       # plan / gen + SSE 进度
│   │   ├── routes_sql.py       # SQL 查询台
│   │   └── security.py         # 只读白名单、绑定地址校验
│   └── verify/
│       ├── invariants.py
│       └── coverage.py
├── frontend/                   # React 19 + Vite + TS + Tailwind v4
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── App.tsx
│       ├── api/client.ts       # 后端接口封装
│       ├── components/
│       └── pages/
│           ├── ConfigEditor.tsx    # YAML 编辑 + 实时校验
│           ├── PlanView.tsx        # 预演结果
│           ├── ResultView.tsx      # 生成结果表格预览
│           └── SqlConsole.tsx      # SQL 查询台
└── tests/
    ├── unit/
    ├── web/                    # API 测试（FastAPI TestClient）
    └── e2e/
```

## 4. 核心数据结构（IR）

`models.py` 是全局契约，所有模块围绕它工作。

```python
from typing import Any, Literal
from pydantic import BaseModel, Field

GroupType = Literal[
    "enum", "boundary", "dict",              # 有限取值组（参与笛卡尔积）
    "sequence", "random", "const",            # 每行附着组
    "derive", "ref",                          # 每行附着组（依赖其他字段）
    "aggregate",                              # 跨表汇总组（P2）
]
Allocation = Literal["follow_parent", "round_robin", "random", "weighted"]
Cardinality = Literal["1:1", "1:0..1", "1:N", "N:M"]
Existence = Literal["required", "optional", "conditional"]
Strategy = Literal["full", "pairwise", "sample"]


class GroupSpec(BaseModel):
    name: str
    type: GroupType
    fields: list[str]
    # 有限取值组
    values: list[list[Any]] | None = None        # 元组列表，长度须等于 len(fields)
    allocation: Allocation = "follow_parent"
    when: str | None = None                      # 生效条件，可引用 parent.*
    # sequence
    start: int = 1
    step: int = 1
    format: str | None = None                    # 如 "T{seq:08d}"
    # random
    generator: str | None = None
    range: tuple[Any, Any] | None = None
    buckets: list[float] | None = None
    scale: int | None = None
    # const
    value: list[Any] | None = None
    # derive / aggregate
    expr: str | None = None
    # ref
    from_: str | None = Field(default=None, alias="from")


class TableSpec(BaseModel):
    name: str
    groups: list[GroupSpec]
    scope: Literal["per_parent", "global"] = "per_parent"
    rows: int | None = None                      # 显式指定行数（可选）


class JoinKey(BaseModel):
    parent_field: str
    child_field: str


class PropagateRule(BaseModel):
    mode: Literal["copy", "derive", "map", "split", "aggregate", "free"]
    to: str
    from_: str | None = Field(default=None, alias="from")
    expr: str | None = None
    table: str | None = None                     # map 模式的映射表名
    split_ratio: list[float] | None = None       # split 模式的分配比例


class RelationSpec(BaseModel):
    parent: str
    child: str
    cardinality: Cardinality = "1:1"
    existence: Existence = "required"
    condition: str | None = None                 # conditional 时的条件
    join: list[JoinKey]
    propagate: list[PropagateRule] = []


class LimitsSpec(BaseModel):
    max_rows: int = 100_000
    strategy: Strategy = "full"
    sample_size: int | None = None
    exclude: list[str] = []


class DatabaseSpec(BaseModel):
    url: str | None = None                       # None → 内存模式（只生成不落盘）
    dialect: str | None = None                   # 不填则从 url 推断
    batch_size: int = 1000
    dry_run: bool = False


class SqlSpec(BaseModel):
    allow_write: bool = False                    # 写模式默认关闭
    max_rows: int = 1000
    timeout_seconds: int = 30


class SeedConfig(BaseModel):
    seed: int = 20260910
    limits: LimitsSpec = LimitsSpec()
    tables: list[TableSpec]
    relations: list[RelationSpec] = []
    invariants: list[str] = []
    database: DatabaseSpec | None = None
    sql: SqlSpec = SqlSpec()
```

**运行时对象**（`engine` 内部，非配置）：

```python
class GeneratedRow(BaseModel):
    table: str
    seq: int                       # 该表内行序号，从 0 开始
    values: dict[str, Any]
    parent_seq: int | None = None  # 父表中对应行的 seq（用于回填索引）


class TableData(BaseModel):
    table: str
    rows: list[GeneratedRow]
    index: dict[tuple[Any, ...], list[int]]   # 锚点值 → 行下标，供 P2 聚合
```

## 5. 关键算法

### 5.1 依赖图与拓扑排序

```
节点 = 表
边   = 关系（parent → child，正向）
     + aggregate 组（child → parent，反向，标记为 back_edge）

1. 构建有向图
2. Kahn 拓扑排序；若存在环，检查是否为已知 back_edge：
   - 全部 back_edge → 允许，产出「分层顺序」
   - 出现未声明的环 → PlanError（FR-7.2）
3. 输出：phase1_order（正向拓扑序）、phase2_targets（含 aggregate 的表）
```

### 5.2 笛卡尔积展开

```python
# 有限取值组之间做 product，保持声明顺序 → 稳定展开（FR-3.4）
finite = [g for g in table.groups if g.type in ("enum", "boundary", "dict")]
combos = itertools.product(*(g.values for g in finite))
# 每个 combo 是一条「骨架行」，再叠加每行附着组的值
```

复杂度 `O(Π|values|)`，边生成边消费，不预先物化全部组合。

### 5.3 分配策略（1:1 关键路径）

1:1 关系下子表行数锁定为父行数，有限取值组改为**索引映射**而非展开：

```python
def allocate(group, parent_row, child_seq, rng) -> list[Any]:
    match group.allocation:
        case "follow_parent":
            key = lookup_key(group, parent_row)      # 由父字段决定
            return group.value_map[key]              # 祖先建好的映射表
        case "round_robin":
            return group.values[child_seq % len(group.values)]
        case "random":
            return rng.choice(group.values)
        case "weighted":
            return rng.choices(group.values, weights=group.buckets)[0]
```

`follow_parent` 的映射表在 Phase 1 起始时由 `when` 条件与父表取值域共同推导，推导不出时退化为 `round_robin` 并输出警告（避免静默失败）。

### 5.4 两阶段调度

```python
# Phase 1
for table in phase1_order:
    rows = generate_table(table, ctx)          # 展开 + 分配 + 传播
    ctx.store(table.name, rows)

# Phase 2（仅含 aggregate 的表）
for table in phase2_targets:
    for group in aggregate_groups(table):
        keyed = index_by(ctx.get(group.source_table), group.join_keys)
        for row in ctx.get(table.name):
            row.values[group.fields[0]] = aggregate(group.expr, keyed[row.key()])
```

### 5.5 pairwise 覆盖（M3）

采用**贪心近似**而非完整 IPOG：

```
1. 枚举所有字段对及其取值对（需覆盖目标集合）
2. 迭代：每次选择一个能覆盖最多未覆盖目标的取值组合，加入结果集
3. 直到所有目标覆盖完毕
判断标准：字段对覆盖率 100%（行数不保证最优，但可接受）
```

理由见 PRD R-2：完整 IPOG 复杂度高，贪心对本场景足够。

## 6. 表达式引擎

### 6.1 语法（统一一套，供 when / exclude / derive / aggregate / invariants 使用）

**风格取向（D-3）**：运算符与关键字**同时接受 Python 与 MySQL 两种写法**，大小写不敏感；函数提供双风格别名。目标用户写 SQL 多、写 Python 少，两套都认可以降低记忆负担。

| 能力 | 接受的写法 | 说明 |
| --- | --- | --- |
| 比较 | `==` / `=`（兼容）/ `!=` / `<>` / `>` `>=` `<` `<=` | `=` 与 `<>` 为 MySQL 习惯写法，内部归一化处理 |
| 逻辑 | `and` / `AND`、`or` / `OR`、`not` / `NOT` | 不支持 `&&` `||` `&` `|` |
| 算术 | `+ - * / // % **` | `div` 视为 `//` 别名 |
| 成员 | `in` / `not in` | 如 `channel in ("OTC", "EBANK")` |
| 空值 | `is null` / `is not null` | MySQL 习惯；同时提供 `isnone(x)` |
| 区间 | `between a and b` | 等价于 `a <= x <= b` |
| 模糊匹配 | `like` | 支持 `%` 与 `_` 通配，如 `remark like "TXN-%"` |
| 字符串拼接 | `+` 或 `concat(a, b)` | `||` 不作为拼接符（与逻辑 or 易混，明确拒绝） |
| 变量 | `字段名` / `row.字段名` / `parent.字段名` / `src.字段名` / `seq` / `seed` | 按上下文注入 |
| 聚合 | `count(x) by k` / `sum(x) by k` / `avg` `min` `max` `count_distinct` | 仅 `aggregate` 与 `invariants` 可用 |

### 6.2 函数库（Python 名 / MySQL 名双别名）

| 类别 | 函数 | MySQL 别名 |
| --- | --- | --- |
| 空值 | `coalesce` `nullif` | `ifnull` `nvl` |
| 数值 | `abs` `round` `ceil` `floor` `mod` `power` `greatest` `least` | 同名 |
| 字符串 | `length` `upper` `lower` `trim` `ltrim` `rtrim` `substr` `replace` `concat` | `char_length` `ucase` `lcase` |
| 日期 | `now` `today` `date_add` `date_sub` `datediff` `date_format` `year` `month` `day` | `curdate` `curtime` `date_add` `date_sub` `timestampdiff` |
| 条件 | `if_(cond, a, b)` | `if` |
| 造数专用 | `seq()` `rand_int(a,b)` `rand_choice([...])` `rand_decimal(a,b,s)` `dict_(name)` `uuid_()` | — |
| 聚合 | `count` `sum` `avg` `min` `max` `count_distinct` | 同名 |

**造数专用函数说明**：

| 函数 | 用途 |
| --- | --- |
| `seq()` | 当前行序号（等价于变量 `seq`） |
| `rand_int(a, b)` | 区间内随机整数，受全局 seed 控制 |
| `rand_choice([...])` | 从候选集中随机取一个（可带权重） |
| `rand_decimal(a, b, s)` | 区间内随机小数，保留 `s` 位 |
| `dict_(name)` | 从注册字典中取值 |
| `uuid_()` | 生成确定性 UUID（同 seed 同结果，保证可复现） |

> 命名用下划线后缀（`if_` / `dict_` / `uuid_`）避开 Python 关键字 —— 这类函数名会被解析为普通标识符，若与关键字冲突则在解析期报错而非静默出错。

### 6.3 安全实现（NFR-3）

```python
ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.Compare, ast.BinOp, ast.UnaryOp,
    ast.Constant, ast.Name, ast.Load, ast.Call, ast.Attribute, ast.Tuple,
    ast.List, ast.And, ast.Or, ast.Not, ast.In, ast.NotIn,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)

def compile_expr(src: str) -> ast.Expression:
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ExprError(f"不允许的语法: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr not in KNOWN_NAMESPACES:
            raise ExprError(f"不允许访问属性: {node.attr}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExprError("只允许调用内置函数")
    return tree
```

**三条硬规则**：不用 `eval`/`exec`；函数调用只认白名单；属性访问只认 `row` / `parent` / `src` 三个命名空间。

## 7. 方言适配与类型系统

| 内部类型 | PostgreSQL | MySQL | Oracle |
| --- | --- | --- | --- |
| `int` | integer / bigint | int / bigint | NUMBER(p,0) |
| `decimal` | numeric(p,s) | decimal(p,s) | NUMBER(p,s) |
| `string` | varchar / text | varchar / text | VARCHAR2 / CLOB |
| `date` | date | date | DATE |
| `datetime` | timestamp | datetime | TIMESTAMP |
| `bool` | boolean | tinyint(1) | NUMBER(1) |
| `binary` | bytea | blob | BLOB |

**DDL 来源解析**：优先用 SQLAlchemy 反射（连接可用时）；离线场景用 `sqlglot` 解析 DDL 语句（仅 M2 引入，M1 仅支持反射 + 手工声明，避免过早引入依赖）。

## 8. 执行与输出层（sink）

三种 sink，按 D-5 决策分派：

| sink | 触发条件 | 行为 | 磁盘副作用 |
| --- | --- | --- | --- |
| `db_sink` | 提供数据库连接（`--dsn` 或配置 `database.url`） | 批量 INSERT + 分批 commit；写入前校验目标表存在且字段匹配；`--dry-run` 时只生成不写 | 无 |
| `memory_sink` | **默认**（未提供连接） | 只生成，返回 `TableData` 内存对象 | **无** |
| `file_sink` | 显式 `--out <dir>` | 落盘 SQL / CSV | 有（显式触发） |

**分派优先级**：`--out` > `--dsn` > memory（二者都不给即内存模式）。`--out` 与 `--dsn` 可同时使用（既入库又留档）。

**内存对象的导出形态**（FR-8.3）：

```python
result = service.generate(config)              # 无连接 → memory_sink
result.tables["t_account"].to_dataframe()      # pandas（惰性导入）
result.tables["t_account"].to_records()        # list[dict]，零依赖
result.tables["t_account"].to_sql(dialect="mysql")
result.tables["t_account"].to_csv(path=None)   # path=None 时返回字符串
```

**落盘格式**：
- SQL：按方言生成批量 INSERT（默认 1000 行/语句）
- CSV：每表一文件，UTF-8 with BOM（兼容 Excel），表头为字段名
- 落盘与入库顺序严格按 `phase1_order`（父表先于子表），保证直接执行不触发外键错误（FR-8.5）

## 9. WebUI 设计

### 9.1 形态与启动

```bash
tableseed ui -c samples/account.yaml                     # 启动并载入配置
tableseed ui -c samples/account.yaml --port 8643 --no-browser
```

后端 FastAPI 监听 `127.0.0.1`（NFR-7），前端构建产物由同一进程托管为静态文件 —— 单端口、无跨域。开发期前端走 Vite dev server 代理到后端。

### 9.2 页面与接口

| 页面 | 主要能力 | 对应接口 |
| --- | --- | --- |
| 配置编辑器 | YAML 编辑、语法高亮、实时校验、错误定位到行 | `GET/PUT /api/config`、`POST /api/config/validate` |
| 预演视图 | 各表组合数 / 行数 / 依赖序 / 超限提示 | `POST /api/plan` |
| 生成与结果预览 | 触发生成、SSE 进度、结果表格分页筛选、一键导出 | `POST /api/generate`（SSE）、`GET /api/result/{table}`、`GET /api/export` |
| SQL 查询台 | 执行查询、表格渲染、快捷查看最新插入 | `POST /api/sql/execute`、`GET /api/sql/recent/{table}` |
| 关系图（M2） | 父子关系与锚点连线 | `GET /api/graph` |
| 覆盖度（M3） | 覆盖率与未覆盖清单 | `GET /api/coverage` |

### 9.3 生成进度（SSE）

```
event: progress
data: {"table": "t_account", "done": 6, "total": 12, "phase": "P1"}

event: done
data: {"tables": {"t_account": 12, "t_txn_detail": 3}, "elapsed_ms": 42}
```

前端用一个 `EventSource` 订阅，进度条与结果表格增量刷新。

### 9.4 结果预览的数据来源

- **无数据库连接**：直接读内存生成结果
- **有数据库连接**：默认仍读内存生成结果（不重复查库），另提供「从库中读回」按钮作交叉验证

> 这一点很重要 —— 预览默认展示**生成器产出的数据**，而不是查询结果。避免"页面看着对了、入库其实错了"的假象；入库是否成功由 SQL 查询台交叉验证。

## 10. SQL 查询台与安全

### 10.1 只读白名单（NFR-7 / FR-12.2）

| 语句类别 | M1 判定方式 | 结论 |
| --- | --- | --- |
| `SELECT` / `WITH` / `SHOW` / `EXPLAIN` / `DESC` / `DESCRIBE` | 去注释去空白后取首个关键字，比对白名单 | 放行 |
| 其余（`INSERT` / `UPDATE` / `DELETE` / `DROP` / `ALTER` / `TRUNCATE` / `GRANT` / `CREATE` …） | — | 拒绝并说明原因 |

**M1 实现**：正则前缀判定（零依赖）。
**M2 加固**：引入 `sqlglot` 解析语句 AST 判定类型，防住 `/* */` 注释绕过与 `;` 多语句拼接等手法。

### 10.2 保护措施

| 措施 | 参数 | 默认值 |
| --- | --- | --- |
| 返回行数上限 | `sql.max_rows` | 1000 |
| 查询超时 | `sql.timeout_seconds` | 30 |
| 写模式开关 | `sql.allow_write` | `false` |
| 写模式二次确认 | 页面弹窗需输入 `EXECUTE` 确认 | 强制 |
| 绑定地址 | 固定 `127.0.0.1`，不支持改为 `0.0.0.0` | 强制 |

### 10.3 写模式的定位

写模式是**逃生舱**，仅用于「造数后手工修正几条数据」这类场景，必须显式开启 + 逐次确认，且开启时页面顶部常驻警示条。默认关闭。

### 10.4 执行留痕（FR-12.7，M2）

每次执行记录：时间、语句摘要、耗时、返回或影响行数，落本地日志文件。

## 11. 配置 Schema 完整字段表

### 顶层

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | :---: | --- | --- |
| `seed` | int | 否 | 20260910 | 随机种子，决定可复现性 |
| `limits` | object | 否 | 见下 | 规模治理 |
| `tables` | list | **是** | — | 表定义 |
| `relations` | list | 否 | `[]` | 表间关系 |
| `invariants` | list[str] | 否 | `[]` | 生成后校验断言 |
| `database` | object | 否 | — | 数据库连接；**不填则走内存模式（只生成不落盘）** |
| `sql` | object | 否 | 见下 | SQL 查询台安全参数 |

> **单文件约定（D-4）**：配置始终是**一个** YAML 文件，不拆分、不支持 `include`，便于直接发给同事。体量变大时用 YAML anchor / alias 复用公共片段：
>
> ```yaml
> _common:
>   tenant: &tenant ["0001", "001"]
> tables:
>   - name: t_account
>     groups:
>       - {type: const, name: g_tenant, fields: [tenant_id, branch_code], value: *tenant}
> ```

### database

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `url` | str | — | 连接串，如 `postgresql://user:pwd@host:5432/db`；命令行 `--dsn` 优先于此 |
| `dialect` | enum | 从 url 推断 | `postgresql` / `mysql` / `oracle` |
| `batch_size` | int | 1000 | 批量写入条数 |
| `dry_run` | bool | `false` | 只生成不写库 |

### sql

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `allow_write` | bool | `false` | 写模式开关；开启后仍需页面二次确认 |
| `max_rows` | int | 1000 | 查询返回行数上限 |
| `timeout_seconds` | int | 30 | 查询超时 |

### limits

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_rows` | int | 100000 | 单表行数硬上限 |
| `strategy` | enum | `full` | `full` / `pairwise` / `sample` |
| `sample_size` | int | — | `sample` 策略的抽样数 |
| `exclude` | list[str] | `[]` | 非法组合表达式，剔除 |

### group（按类型区分必填项）

| 类型 | 必填字段 | 可选字段 |
| --- | --- | --- |
| `enum` / `boundary` | `name` `fields` `values` | `when` `allocation` |
| `dict` | `name` `fields` `values` 或 `from` | `when` `allocation` |
| `sequence` | `name` `fields` | `start` `step` `format` |
| `random` | `name` `fields` `generator` | `range` `buckets` `scale` |
| `const` | `name` `fields` `value` | — |
| `derive` | `name` `fields` `expr` | — |
| `ref` | `name` `fields` `from` | — |
| `aggregate` | `name` `fields` `expr` | — |

### relation

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `parent` / `child` | str | — | 表名 |
| `cardinality` | enum | `1:1` | `1:1` / `1:0..1` / `1:N` / `N:M` |
| `existence` | enum | `required` | `required` / `optional` / `conditional` |
| `condition` | str | — | `conditional` 时的条件表达式 |
| `join` | list | — | 锚点对，如 `[[txn_no, txn_no]]` |
| `propagate` | list | `[]` | 传播规则 |

## 12. 测试策略

| 层次 | 范围 | 要点 |
| --- | --- | --- |
| 单元测试 | `expr` / `group_expander` / `allocator` / `topo` / `sizing` | 纯内存，无 IO；边界：空取值集、单字段组、环检测 |
| 安全测试 | `expr` 沙箱 | 注入 `__import__` / 属性逃逸 / 任意函数调用，必须全部拒绝 |
| Web API 测试 | `web/` 路由 | FastAPI TestClient；**重点覆盖 SQL 白名单拒绝、`;` 多语句拼接、超限截断、写模式默认关闭** |
| 无副作用断言 | `memory_sink` 路径 | 生成前后对比工作目录文件清单，必须完全一致（NFR-8 / AC-13） |
| 端到端 | `samples/account.yaml` | 对应 PRD 第 7 节 AC-1 ~ AC-13 逐条断言 |
| 黄金文件 | 同 seed 两次生成 | 字节级比对，保障 NFR-2 |
| 方言测试 | SQL 输出 | 以字符串快照对比（不依赖真实数据库） |

**覆盖率目标**：核心模块 ≥ 80%（NFR-5）。测试用例采用 allure 中文注解，便于评审。

## 13. 开发环境与命令

```bash
# 后端环境
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows

# 前端环境
cd frontend && npm install

# 常用命令
pytest                                              # 全量测试
pytest --alluredir=reports/allure                   # 生成 allure 报告
tableseed plan -c samples/account.yaml              # 预演（终端）
tableseed check -c samples/account.yaml             # 配置校验
tableseed gen  -c samples/account.yaml              # 无连接 → 只生成不落盘
tableseed gen  -c samples/account.yaml --dsn postgresql://...   # 直连入库
tableseed gen  -c samples/account.yaml -o out/      # 显式落盘
tableseed ui   -c samples/account.yaml              # 启动 WebUI

# 前端开发（热更新）
cd frontend && npm run dev                          # Vite dev server，代理到后端
cd frontend && npm run build                        # 构建产物供 tableseed ui 托管
```

**requirements.txt（预计）**

```
typer>=0.12
pydantic>=2.7
PyYAML>=6.0
rich>=13.0               # CLI 进度与表格输出

fastapi>=0.115           # WebUI 后端
uvicorn>=0.30            # ASGI 服务器

SQLAlchemy>=2.0          # 元数据反射（可选，惰性导入）
pandas>=2.2              # DataFrame 导出（可选，惰性导入）
sqlglot>=25.0            # SQL 语句类型判定（M2 引入，可选）

pytest>=8.0
allure-pytest>=2.13
httpx>=0.27              # FastAPI TestClient 依赖
```

> 遵循既有项目约定：**可选依赖（SQLAlchemy / pandas / sqlglot）在函数内惰性导入**，避免拖慢 CLI 启动。
> 前端构建产物随包分发，使用者无需安装 Node —— `pip install` 后 `tableseed ui` 即可用。

## 14. 实施顺序建议（M1 最短链路）

按"能跑通即验证"的顺序推进，每步都有可见产出，且从第 7 步起可在浏览器里看到东西：

**后端骨架**

1. `models.py` + `config/loader.py` + `config/checker.py` → 能加载并校验 YAML
2. `expr/`（parser + evaluator + functions）→ 表达式可单测
3. `engine/group_expander.py` → 12 行示例展开出全部组合
4. `engine/table_gen.py` → 叠加附着组，产出完整行
5. `sink/memory.py` + `sink/files.py` → 内存对象与落盘，`to_dataframe()` 可用
6. `service.py` + `cli.py`（plan / check / gen）→ 终端端到端可用

**WebUI 闭环**（此段之后即可脱离终端验证）

7. `web/app.py` + `routes_config.py` + `routes_run.py`（plan 部分）→ 页面能编辑配置并跑预演
8. `frontend/` 配置编辑器 + 预演视图 → 浏览器可见
9. `routes_run.py`（SSE 生成）+ 结果预览页 → 页面点击生成、表格查看
10. `sink/db.py` + `routes_sql.py` + SQL 查询台页 → 入库后可直接查数据

**收口**

11. `samples/account.yaml` + e2e 测试 → 对齐 PRD AC-1 ~ AC-13

第 1~6 步纯后端可独立测试；第 7~10 步每步都产生浏览器可见的成果；第 11 步做总验收。

---

*本文档与 PRD 共同构成实施依据。选型变更需在此文档记录理由与影响范围。*
