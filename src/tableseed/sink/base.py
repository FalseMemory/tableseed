"""结果去向（sink）抽象。

设计意图（tech-design 第 8 节）：**结果的去向是一个可替换的策略**，
生成引擎不关心数据最终去哪。因此「不落盘」是默认行为，而不是特例。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import GenerateResult


class Sink(ABC):
    """生成结果的消费者。"""

    name: str = "sink"

    @abstractmethod
    def write(self, result: GenerateResult) -> None:
        """消费一次生成结果。"""

    @abstractmethod
    def describe(self) -> str:
        """给用户看的一行说明。"""


class CompositeSink(Sink):
    """把结果依次交给多个 sink（例如既入库又留档）。"""

    name = "composite"

    def __init__(self, sinks: list[Sink]) -> None:
        self.sinks = sinks

    def write(self, result: GenerateResult) -> None:
        for sink in self.sinks:
            sink.write(result)

    def describe(self) -> str:
        return " + ".join(s.describe() for s in self.sinks)
