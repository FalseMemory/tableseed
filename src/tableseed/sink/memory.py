"""内存 sink —— 默认行为：只生成、不落盘、不写库。

PRD NFR-8 / AC-13 要求：未提供数据库连接时，生成过程不产生任何磁盘副作用。
"""

from __future__ import annotations

from ..models import GenerateResult
from .base import Sink


class MemorySink(Sink):
    """把结果留在内存里，供调用方直接读取或导出。"""

    name = "memory"

    def write(self, result: GenerateResult) -> None:  # noqa: ARG002  有意不做任何事
        return None

    def describe(self) -> str:
        return "内存模式：只生成不落盘（无任何磁盘副作用）"
