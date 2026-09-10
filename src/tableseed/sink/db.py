"""数据库 sink —— 提供连接时直接执行入库（PRD FR-8.1）。

SQLAlchemy 为可选依赖，函数内惰性导入（既有项目约定）。
"""

from __future__ import annotations

from ..errors import SinkError
from ..models import GenerateResult, TableData
from .base import Sink


class DbSink(Sink):
    """批量写入目标数据库。"""

    name = "db"

    def __init__(
        self,
        url: str,
        dialect: str | None = None,
        batch_size: int = 1000,
        dry_run: bool = False,
    ) -> None:
        self.url = url
        self.dialect = dialect
        self.batch_size = batch_size
        self.dry_run = dry_run

    def write(self, result: GenerateResult) -> None:
        if self.dry_run:
            return

        try:
            from sqlalchemy import MetaData, Table, create_engine, insert  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise SinkError(
                "未安装 SQLAlchemy，无法直连数据库。请执行 pip install SQLAlchemy，"
                "或去掉连接参数改用内存模式 / --out 落盘。"
            ) from exc

        engine = create_engine(self.url, future=True)
        metadata = MetaData()
        try:
            with engine.begin() as conn:
                for data in result.tables.values():
                    self._write_table(conn, metadata, data, Table, insert)
        except SinkError:
            raise
        except Exception as exc:  # 驱动异常类型繁杂，统一包装
            raise SinkError(f"写入数据库失败: {exc}") from exc
        finally:
            engine.dispose()

    def _write_table(self, conn, metadata, data: TableData, Table, insert) -> None:
        table = Table(data.table, metadata, autoload_with=conn)

        missing = [c for c in data.columns if c not in table.c]
        if missing:
            raise SinkError(
                f"目标表 {data.table} 缺少字段: {', '.join(missing)}"
                "（请检查表结构或配置中的字段名）"
            )
        if not data.rows:
            return

        payload = [
            {col: row.values.get(col) for col in data.columns} for row in data.rows
        ]
        for start in range(0, len(payload), self.batch_size):
            conn.execute(insert(table), payload[start : start + self.batch_size])

    def describe(self) -> str:
        mode = "（dry-run：只生成不写库）" if self.dry_run else ""
        return f"直连数据库入库{mode}"
