"""业务入口（service 层）。

CLI 与 WebUI **都只调用这里** —— 不允许任何一方自己实现业务逻辑，
否则会出现「CLI 能跑、页面结果不一致」的分叉（tech-design 第 2 节）。
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import check_config, load_config, load_config_from_text
from .engine import generate_table
from .models import GenerateResult, PlanResult, SeedConfig
from .plan import plan_tables
from .rng import SeededRandom
from .sink import resolve_sink
from .sink.base import Sink

__all__ = [
    "check",
    "generate",
    "load",
    "load_text",
    "plan",
]


def load(path: str | Path) -> SeedConfig:
    """加载配置文件。"""
    return load_config(path)


def load_text(text: str, source: str = "<inline>") -> SeedConfig:
    """加载 YAML 文本（WebUI 在线编辑）。"""
    return load_config_from_text(text, source=source)


def check(config: SeedConfig) -> list[str]:
    """静态校验，返回问题清单。"""
    return check_config(config)


def plan(config: SeedConfig) -> PlanResult:
    """规模预演，不产出数据。"""
    return plan_tables(config)


def generate(
    config: SeedConfig,
    out_dir: str | Path | None = None,
    dsn: str | None = None,
    dialect: str | None = None,
    dry_run: bool = False,
    sink: Sink | None = None,
) -> GenerateResult:
    """生成数据并交给对应的 sink。

    - 未提供 ``out_dir`` 与 ``dsn`` → 内存模式，只生成不落盘（默认）
    - 提供 ``dsn`` → 直接执行入库
    - 提供 ``out_dir`` → 显式落盘 SQL / CSV
    """
    started = time.perf_counter()

    tables = {}
    base_rng = SeededRandom(config.seed)
    for table in config.tables:
        # 每张表用独立的子随机源，避免某表行数变化影响其他表的随机序列
        tables[table.name] = generate_table(config, table, base_rng.fork(table.name))

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = GenerateResult(tables=tables, elapsed_ms=elapsed_ms, seed=config.seed)

    target = sink or resolve_sink(
        config, out_dir=out_dir, dsn=dsn, dialect=dialect, dry_run=dry_run
    )
    target.write(result)
    return result
