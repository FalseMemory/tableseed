"""业务入口（service 层）。

CLI 与 WebUI **都只调用这里** —— 不允许任何一方自己实现业务逻辑，
否则会出现「CLI 能跑、页面结果不一致」的分叉（tech-design 第 2 节）。
"""

from __future__ import annotations

import time
from pathlib import Path

from .config import check_config, load_config, load_config_from_text
from .errors import GenerateError
from .engine import generate_all, verify_invariants
from .models import GenerateResult, InvariantFailure, PlanResult, SeedConfig
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
    "verify",
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


def verify(
    config: SeedConfig,
    result: GenerateResult | None = None,
    dsn: str | None = None,
    out_dir: str | Path | None = None,
) -> list[InvariantFailure]:
    """对生成结果逐条求值不变量，返回违例清单（空 = 全部通过）。

    ``result`` 缺省时先在内存里生成一份（不落盘）——
    验证的是「这套配置会造出什么」，而不是「库里已有什么」。
    """
    # 先静态校验：不变量指向不存在的表、表达式写错之类的问题要在这里报出来，
    # 而不是等到跑到一半抛 KeyError（Web 层就成了 500）
    problems = [p for p in check(config) if not p.startswith("[提示]")]
    if problems:
        raise GenerateError("配置校验未通过：\n  - " + "\n  - ".join(problems))

    if result is None:
        result = generate_all(config, SeededRandom(config.seed))
    return verify_invariants(config, result.tables)


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

    多表时按拓扑序生成（父先于子），子表通过关系规则继承父表字段。
    """
    started = time.perf_counter()

    base_rng = SeededRandom(config.seed)
    result = generate_all(config, base_rng)

    target = sink or resolve_sink(
        config, out_dir=out_dir, dsn=dsn, dialect=dialect, dry_run=dry_run
    )
    target.write(result)

    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result
