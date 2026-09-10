"""文件 sink —— 仅在显式指定 ``--out`` 时启用。

输出顺序严格按父表先于子表，保证直接执行 SQL 不触发外键错误（FR-8.5）。
"""

from __future__ import annotations

from pathlib import Path

from ..errors import SinkError
from ..models import GenerateResult
from ..render import render_csv, render_sql
from .base import Sink


class FileSink(Sink):
    """把每张表写成一个 .sql 与一个 .csv 文件。"""

    name = "file"

    def __init__(
        self,
        out_dir: str | Path,
        dialect: str = "postgresql",
        batch_size: int = 1000,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.dialect = dialect
        self.batch_size = batch_size

    def write(self, result: GenerateResult) -> None:
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            for data in result.tables.values():
                sql_path = self.out_dir / f"{data.table}.sql"
                csv_path = self.out_dir / f"{data.table}.csv"
                sql_path.write_text(
                    render_sql(data, dialect=self.dialect, batch_size=self.batch_size),
                    encoding="utf-8",
                )
                render_csv(data, path=str(csv_path))
        except OSError as exc:
            raise SinkError(f"写入目录 {self.out_dir} 失败: {exc}") from exc

    def describe(self) -> str:
        return f"落盘到 {self.out_dir}（SQL + CSV，方言 {self.dialect}）"
