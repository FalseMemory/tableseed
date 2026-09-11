"""tableseed 全部 IR（中间表示）数据结构。

这是全局契约：config / plan / engine / sink / web 各层都围绕它工作。
配置结构与 README「模型一 / 模型二」以及 docs/PRD.md 一一对应。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- 枚举类型

GroupType = Literal[
    # 有限取值组 —— 参与笛卡尔积展开
    "enum",
    "boundary",
    "dict",
    # 每行附着组 —— 逐行现算，不放大行数
    "sequence",
    "random",
    "const",
    "derive",
    "ref",
    # 跨表汇总组 —— Phase 2 回填
    "aggregate",
]

#: 参与笛卡尔积的组类型
FINITE_GROUP_TYPES: frozenset[str] = frozenset({"enum", "boundary", "dict"})

#: Phase 2 才求值的组类型
PHASE2_GROUP_TYPES: frozenset[str] = frozenset({"aggregate"})

Allocation = Literal["follow_parent", "round_robin", "random", "weighted"]
Cardinality = Literal["1:1", "1:0..1", "1:N", "N:M"]
Existence = Literal["required", "optional", "conditional"]
Strategy = Literal["full", "pairwise", "sample"]
PropagateMode = Literal["copy", "derive", "map", "split", "aggregate", "free"]
Scope = Literal["per_parent", "global"]


class _Model(BaseModel):
    """统一基类：允许 by-name 填充，拒绝未知字段（早暴露配置拼写错误）。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# ---------------------------------------------------------------- 配置模型


class ColumnSpec(_Model):
    """手工声明的列定义（未提供元数据来源时使用）。"""

    name: str
    type: str | None = None  # 内部类型，缺省时由生成值推断
    length: int | None = None
    scale: int | None = None
    nullable: bool = True
    primary_key: bool = False


class GroupSpec(_Model):
    """字段组 —— 造数的最小规则单元。

    一个组包含一个或多个字段；组内多字段一次生成一个**联合取值元组**，
    从而保证同组字段天然同步（如 status_code 与 status_desc）。
    """

    name: str
    type: GroupType
    fields: list[str]

    # 有限取值组（enum / boundary / dict）：取值元组列表，每项长度须等于 len(fields)
    values: list[list[Any]] | None = None

    # 分配策略 —— 1:1 关系下不放大行数，改用分配保覆盖
    allocation: Allocation = "follow_parent"

    # 生效条件：可引用本表字段与 parent.*
    when: str | None = None

    # sequence
    start: int = 1
    step: int = 1
    format_: str | None = Field(default=None, alias="format")

    # random
    generator: str | None = None
    range_: tuple[Any, Any] | None = Field(default=None, alias="range")
    buckets: list[float] | None = None
    scale: int | None = None

    # const
    value: list[Any] | None = None

    # derive / aggregate
    expr: str | None = None

    # ref
    from_: str | None = Field(default=None, alias="from")


class TableSpec(_Model):
    """一张表的造数规格。"""

    name: str
    groups: list[GroupSpec]
    columns: list[ColumnSpec] | None = None
    scope: Scope = "per_parent"
    rows: int | None = None


class JoinKey(_Model):
    """父子表的锚点字段对。"""

    parent_field: str
    child_field: str


class PropagateRule(_Model):
    """父子表之间的字段传播规则。

    六种模式对应 README「模型二」：
    ``copy`` / ``derive`` / ``map`` / ``split``（M4）/ ``aggregate``（M3）/ ``free``。
    """

    mode: PropagateMode
    to: str
    from_: str | None = Field(default=None, alias="from")
    expr: str | None = None
    table: str | None = None
    split_ratio: list[float] | None = None

    # map 模式：码值映射表（父子系统码表不一致时用），形如 {父值: 子值}
    mapping: dict[str, Any] | None = None

    # map 模式：父值未命中 mapping 时的兜底值；不提供则视为配置错误并报错
    default: Any = None


class RelationSpec(_Model):
    """表间关系四要素：基数 / 存在性 / 锚点 / 传播。"""

    parent: str
    child: str
    cardinality: Cardinality = "1:1"
    existence: Existence = "required"
    condition: str | None = None
    join: list[JoinKey] = Field(default_factory=list)
    propagate: list[PropagateRule] = Field(default_factory=list)

    # follow_parent 分配的驱动字段：父行这些字段的值相同 → 子行复用同一组合。
    # 不填时按启发式推断：优先用被 copy / map 的父字段，其次用 join 父字段。
    drive_by: list[str] = Field(default_factory=list)


class LimitsSpec(_Model):
    """规模治理。"""

    max_rows: int = 100_000
    strategy: Strategy = "full"
    sample_size: int | None = None
    exclude: list[str] = Field(default_factory=list)


class DatabaseSpec(_Model):
    """数据库连接。不提供即走内存模式（只生成不落盘）。"""

    url: str | None = None
    dialect: str | None = None
    batch_size: int = 1000
    dry_run: bool = False


class SqlSpec(_Model):
    """SQL 查询台安全参数。"""

    allow_write: bool = False
    max_rows: int = 1000
    timeout_seconds: int = 30


class SeedConfig(_Model):
    """一份完整的造数配置（对应一个 YAML 文件）。"""

    seed: int = 20260910
    limits: LimitsSpec = Field(default_factory=LimitsSpec)
    tables: list[TableSpec]
    relations: list[RelationSpec] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    database: DatabaseSpec | None = None
    sql: SqlSpec = Field(default_factory=SqlSpec)

    def table(self, name: str) -> TableSpec:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(name)


# ---------------------------------------------------------------- 运行时对象


class GeneratedRow(_Model):
    """一行已生成的数据。"""

    table: str
    seq: int  # 表内行序号，从 0 开始
    values: dict[str, Any]
    parent_seq: int | None = None


class TableData(_Model):
    """一张表的生成结果（内存对象，不落盘）。

    导出形态对应 PRD FR-8.3：``to_records`` / ``to_dataframe`` / ``to_sql`` / ``to_csv``。
    其中 pandas 为可选依赖，缺失时给出明确提示而非 ImportError 堆栈。
    """

    table: str
    columns: list[str]
    rows: list[GeneratedRow]
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def records(self) -> list[dict[str, Any]]:
        return [r.values for r in self.rows]

    def to_records(self) -> list[dict[str, Any]]:
        """零依赖的导出形态。"""
        return self.records

    def to_dataframe(self):
        """导出为 pandas DataFrame（pandas 为可选依赖）。"""
        try:
            import pandas as pd  # noqa: PLC0415  惰性导入
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise TableSeedError(
                "未安装 pandas，无法导出 DataFrame。请执行 pip install pandas，"
                "或改用 to_records() / to_csv()。"
            ) from exc
        return pd.DataFrame(self.records, columns=self.columns)

    def to_sql(self, dialect: str = "postgresql", batch_size: int = 1000) -> str:
        """导出为批量 INSERT 语句文本。"""
        from .render import render_sql  # noqa: PLC0415  避免循环导入

        return render_sql(self, dialect=dialect, batch_size=batch_size)

    def to_csv(self, path: str | None = None) -> str:
        """导出 CSV；``path=None`` 时返回字符串。"""
        from .render import render_csv  # noqa: PLC0415

        return render_csv(self, path=path)


class GenerateResult(_Model):
    """一次生成的完整结果。"""

    tables: dict[str, TableData]
    elapsed_ms: int
    seed: int

    #: 生成过程中的非致命提示（如「有 N 行父数据在子表中没有匹配行」）
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 预演结果


class TablePlan(_Model):
    """单表的规模预演。"""

    table: str
    finite_groups: list[str] = Field(default_factory=list)
    combo_count: int = 0
    planned_rows: int = 0
    note: str | None = None


class PlanResult(_Model):
    """规模预演结果（plan 子命令 / WebUI 预演视图）。"""

    tables: list[TablePlan]
    total_rows: int
    order: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    within_limits: bool = True
