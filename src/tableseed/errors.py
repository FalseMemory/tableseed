"""tableseed 异常体系。

所有对外抛出的异常都带**配置路径**（如 ``tables[0].groups[2].values``），
便于在数百行的 YAML 里精确定位问题（PRD NFR-6）。
"""

from __future__ import annotations


class TableSeedError(Exception):
    """所有 tableseed 异常的基类。"""

    def __init__(self, message: str, path: str | None = None) -> None:
        self.path = path
        self.message = message
        super().__init__(self._render())

    def _render(self) -> str:
        if self.path:
            return f"[{self.path}] {self.message}"
        return self.message


class ConfigError(TableSeedError):
    """配置加载或校验失败。"""


class ExprError(TableSeedError):
    """表达式解析或求值失败。"""


class PlanError(TableSeedError):
    """依赖分析、拓扑排序或规模预演失败。"""


class GenerateError(TableSeedError):
    """生成阶段失败。"""


class SinkError(TableSeedError):
    """输出阶段失败（落盘 / 入库）。"""
