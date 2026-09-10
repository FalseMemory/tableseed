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
| 日志 | **标准库 logging + Rich（进度条）** | 零侵入，CLI 体验好 | print（无法分级） |
| 可选前端 | **React 19 + Vite + TypeScript + Tailwind**（M5） | 与既有项目栈一致 | — |

**明确不引入**：pandas（大行数内存压力）、numpy（除随机分布外无必要）、任何 ORM、任何代码生成。

## 2. 架构分层

```
┌─────────────────────────────────────────────┐
│  cli.py            Typer 子命令：plan/check/gen/verify  │
├─────────────────────────────────────────────┤
│  config/           加载 YAML → 校验 → IR 对象             │
│  metadata/         元数据扫描（DDL/反射/手工）与类型映射   │
├─────────────────────────────────────────────┤
│  plan/             依赖图 → 拓扑排序 → 规模预演            │
├─────────────────────────────────────────────┤
│  engine/           生成引擎（P1 正向 / P2 回填）           │
│   ├ group_expander  有限取值组笛卡尔积展开                  │
│   ├ allocator       1:1 分配策略                            │
│   ├ propagator      表间字段传播                            │
│   └ aggregator      P2 汇总回填                             │
├─────────────────────────────────────────────┤
│  expr/             表达式解析与沙箱求值（全局复用）        │
├─────────────────────────────────────────────┤
│  output/           SQL / CSV / 直连入库                    │
│  verify/           invariants 校验 + 覆盖报告              │
└─────────────────────────────────────────────┘
```

**依赖方向**：上层依赖下层，`expr` 与 `models` 被所有层引用但不反向依赖。`engine` 不碰 IO（流式由 `output` 拉取），保证 NFR-5 可单测。

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
│   ├── output/
│   │   ├── sql.py
│   │   ├── csv.py
│   │   └── db.py
│   └── verify/
│       ├── invariants.py
│       └── coverage.py
└── tests/
    ├── unit/
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


class SeedConfig(BaseModel):
    seed: int = 20260910
    limits: LimitsSpec = LimitsSpec()
    tables: list[TableSpec]
    relations: list[RelationSpec] = []
    invariants: list[str] = []
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

| 能力 | 语法 | 说明 |
| --- | --- | --- |
| 比较 | `==` `!=` `>` `>=` `<` `<=` | Python 风格，不用 `=` |
| 逻辑 | `and` `or` `not` | 不支持 `&` `|` |
| 算术 | `+ - * / // % **` | — |
| 成员 | `in` `not in` | 如 `channel in ("OTC", "EBANK")` |
| 变量 | `字段名` / `row.字段名` / `parent.字段名` / `src.字段名` / `seq` | 按上下文注入 |
| 函数 | `abs` `round` `len` `upper` `lower` `substr` `today` `days_ago` `rand_int` `dict` `coalesce` | 白名单注册 |
| 聚合 | `count(x) by k` / `sum(x) by k` / `avg` `min` `max` `count_distinct` | 仅 `aggregate` 与 `invariants` 可用 |

### 6.2 安全实现（NFR-3）

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

## 8. 输出层

| 目标 | 实现要点 |
| --- | --- |
| SQL | 按方言生成批量 INSERT，默认 1000 行/语句；字符串转义与日期字面量按方言处理 |
| CSV | 每表一文件，UTF-8 with BOM（兼容 Excel 打开），表头为字段名 |
| 直连入库 | 驱动 executemany + 分批 commit；**写入前校验目标表存在且字段匹配** |

**输出顺序**：严格按 `phase1_order`（父表先于子表），保证直接执行 SQL 不会触发外键错误（FR-8.4）。

## 9. 配置 Schema 完整字段表

### 顶层

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | :---: | --- | --- |
| `seed` | int | 否 | 20260910 | 随机种子，决定可复现性 |
| `limits` | object | 否 | 见下 | 规模治理 |
| `tables` | list | **是** | — | 表定义 |
| `relations` | list | 否 | `[]` | 表间关系 |
| `invariants` | list[str] | 否 | `[]` | 生成后校验断言 |

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

## 10. 测试策略

| 层次 | 范围 | 要点 |
| --- | --- | --- |
| 单元测试 | `expr` / `group_expander` / `allocator` / `topo` / `sizing` | 纯内存，无 IO；边界：空取值集、单字段组、环检测 |
| 安全测试 | `expr` 沙箱 | 注入 `__import__` / 属性逃逸 / 任意函数调用，必须全部拒绝 |
| 端到端 | `samples/account.yaml` | 对应 PRD 第 7 节 AC-1 ~ AC-9 逐条断言 |
| 黄金文件 | 同 seed 两次生成 | 字节级比对，保障 NFR-2 |
| 方言测试 | SQL 输出 | 以字符串快照对比（不依赖真实数据库） |

**覆盖率目标**：核心模块 ≥ 80%（NFR-5）。测试用例采用 allure 中文注解，便于评审。

## 11. 开发环境与命令

```bash
# 环境
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows

# 常用命令
pytest                                              # 全量测试
pytest --alluredir=reports/allure                   # 生成 allure 报告
tableseed plan  -c samples/account.yaml             # 预演
tableseed check -c samples/account.yaml             # 校验
tableseed gen   -c samples/account.yaml -o out/     # 生成
```

**requirements.txt（预计）**

```
typer>=0.12
pydantic>=2.7
PyYAML>=6.0
SQLAlchemy>=2.0          # 元数据反射（M1 可选，惰性导入）
rich>=13.0               # CLI 进度与表格输出
pytest>=8.0
allure-pytest>=2.13
```

> 遵循既有项目约定：**可选依赖在函数内惰性导入**，避免拖慢 CLI 启动与拖垮服务。

## 12. 实施顺序建议（M1 最短链路）

按"能跑通即验证"的顺序推进，每步都有可见产出：

1. `models.py` + `config/loader.py` → 能加载并校验 YAML
2. `expr/`（parser + evaluator + functions）→ 表达式可单测
3. `engine/group_expander.py` → 12 行示例能展开出全部组合
4. `engine/table_gen.py` → 叠加附着组，产出完整行
5. `output/sql.py` + `csv.py` → 落盘
6. `cli.py`（plan / check / gen）→ 端到端可用
7. `samples/account.yaml` + e2e 测试 → 对齐 PRD AC-1 ~ AC-9

前六步可独立测试，第 7 步做总验收。

---

*本文档与 PRD 共同构成实施依据。选型变更需在此文档记录理由与影响范围。*
