"""sink 包：结果的三种去向 + 分派逻辑。

分派优先级（tech-design 第 8 节）：``--out`` > ``--dsn`` > memory。
两者都给时同时生效（既入库又留档）。
"""

from __future__ import annotations

from pathlib import Path

from ..models import SeedConfig
from .base import CompositeSink, Sink
from .db import DbSink
from .files import FileSink
from .memory import MemorySink

__all__ = [
    "CompositeSink",
    "DbSink",
    "FileSink",
    "MemorySink",
    "Sink",
    "resolve_sink",
]


def resolve_sink(
    config: SeedConfig,
    out_dir: str | Path | None = None,
    dsn: str | None = None,
    dialect: str | None = None,
    dry_run: bool = False,
) -> Sink:
    """按「有无连接 / 有无 out」分派 sink。默认内存模式（不落盘）。"""
    database = config.database

    # 密码环境变量缺失时明确指出 —— 否则会拼出无密码连接串去撞数据库，
    # 报出来的是「Access denied ... using password: NO」，看不出真正原因
    if database and not dsn:
        problem = database.env_problem()
        if problem:
            from ..errors import SinkError  # noqa: PLC0415

            raise SinkError(problem)

    url = dsn or (database.resolved_url() if database else None)
    resolved_dialect = (
        dialect
        or (database.dialect if database else None)
        or _guess_dialect(url)
    )
    batch_size = database.batch_size if database else 1000
    effective_dry_run = dry_run or bool(database and database.dry_run)

    sinks: list[Sink] = []
    if url:
        sinks.append(
            DbSink(
                url=url,
                dialect=resolved_dialect,
                batch_size=batch_size,
                dry_run=effective_dry_run,
            )
        )
    if out_dir:
        sinks.append(
            FileSink(out_dir, dialect=resolved_dialect or "postgresql", batch_size=batch_size)
        )

    if not sinks:
        return MemorySink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)


def _guess_dialect(url: str | None) -> str | None:
    if not url:
        return None
    prefix = url.split("://", 1)[0].split("+", 1)[0].lower()
    return prefix or None
