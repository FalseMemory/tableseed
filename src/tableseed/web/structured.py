"""配置的结构化（表格化）编辑：SeedConfig ⇄ 编辑视图 JSON。

「配置编辑器」页面不是让用户改文本，而是像表格一样改：
有哪些字段、每个字段属于哪个组、组的类型、字段的值。

实现方式：YAML 文本 → SeedConfig（Pydantic 校验）→ 扁平化的编辑视图 JSON；
编辑完成后反向 —— 编辑 JSON → SeedConfig（再次校验，配置错了当场报）→
YAML 文本（exclude_none，保持干净可读）。

 relations / invariants 等"关系型"结构仍留在 YAML 文本里编辑 ——
 它们是嵌套结构，表格化反而难用；表格编辑的价值在**组与字段的矩阵**上。
"""

from __future__ import annotations

from typing import Any

import yaml as _pyyaml

from pydantic import ValidationError
from ..errors import ConfigError
from ..errors_cn import explain_validation
from ..models import SeedConfig

__all__ = ["to_edit_view", "from_edit_view"]

#: 编辑视图里每个组暴露的字段 —— 覆盖全部组类型的可编辑项
_GROUP_FIELDS = (
    "type", "name", "fields", "values", "value", "generator", "range_",
    "scale", "start", "step", "format_", "expr", "when", "from_", "buckets",
    "allocation",
)

_TABLE_FIELDS = ("name", "columns", "groups", "rows")


#: 各组类型**相关**的可编辑字段 —— 避免 enum 组带出一堆 sequence 的默认值
_GROUP_KEYS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "enum": ("values", "allocation", "buckets", "when"),
    "boundary": ("values", "allocation", "buckets", "when"),
    "dict": ("from", "allocation", "buckets", "when"),
    "sequence": ("start", "step", "format", "when"),
    "random": ("generator", "range", "scale", "values", "buckets", "when"),
    "const": ("value", "when"),
    "derive": ("expr", "when"),
    "ref": ("from", "when"),
    "aggregate": ("from", "expr", "when"),
}


def to_edit_view(config: SeedConfig) -> dict[str, Any]:
    """SeedConfig → 编辑视图 JSON。"""
    tables = []
    for table in config.tables:
        groups = []
        for group in table.groups:
            item: dict[str, Any] = {"type": group.type, "name": group.name, "fields": list(group.fields)}
            raw = group.model_dump(by_alias=True, exclude_none=True)
            for key in _GROUP_KEYS_BY_TYPE.get(group.type, ()):
                value = raw.get(key)
                if value is None:
                    continue
                # tuple 会序列化成 !!python/tuple，统一转 list
                if isinstance(value, tuple):
                    value = list(value)
                if value == [] or value == ():
                    continue
                item[key] = [list(v) if isinstance(v, tuple) else v for v in value] \
                    if isinstance(value, list) else value
            groups.append(item)

        tables.append(
            {
                "name": table.name,
                "rows": table.rows,
                "columns": [c.name for c in (table.columns or [])],
                "groups": groups,
            }
        )

    return {
        "seed": config.seed,
        "max_rows": config.limits.max_rows,
        "strategy": config.limits.strategy,
        "sample_size": config.limits.sample_size,
        "tables": tables,
        "relations": [r.model_dump(by_alias=True, exclude_none=True) for r in config.relations],
        "invariants": [i.model_dump(by_alias=True, exclude_none=True) for i in config.invariants],
    }


def from_edit_view(data: dict[str, Any]) -> str:
    """编辑视图 JSON → 校验 → 干净的 YAML 文本。

    构造 SeedConfig 本身就是校验 —— 字段分组冲突、表达式错误等当场报出。
    """
    payload: dict[str, Any] = {
        "seed": data.get("seed", 20260910),
        "limits": {
            "max_rows": data.get("max_rows", 100000),
            "strategy": data.get("strategy", "full"),
        },
        "tables": [],
    }
    if data.get("sample_size") is not None:
        payload["limits"]["sample_size"] = data["sample_size"]

    for table in data.get("tables", []):
        table_payload: dict[str, Any] = {
            "name": table["name"],
            "groups": _clean_groups(table.get("groups", [])),
        }
        if table.get("rows"):
            table_payload["rows"] = table["rows"]
        if table.get("columns"):
            table_payload["columns"] = [{"name": c} for c in table["columns"]]
        payload["tables"].append(table_payload)

    if data.get("relations"):
        payload["relations"] = data["relations"]
    if data.get("invariants"):
        payload["invariants"] = data["invariants"]

    try:
        config = SeedConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(explain_validation(exc)) from exc
    except Exception as exc:
        raise ConfigError(f"编辑后的配置不合法: {exc}") from exc

    return dump_config_yaml(config)


def _clean_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把编辑视图的组映射回模型字段名（range→range_、format→format_、from→from_）。"""
    cleaned: list[dict[str, Any]] = []
    for group in groups:
        item: dict[str, Any] = {"type": group.get("type"), "name": group.get("name")}
        if group.get("fields"):
            item["fields"] = group["fields"]
        for key in ("values", "value", "generator", "scale", "start", "step",
                    "expr", "when", "allocation", "buckets"):
            if group.get(key) is not None:
                item[key] = group[key]
        if group.get("range") is not None:
            item["range"] = group["range"]
        if group.get("format") is not None:
            item["format"] = group["format"]
        if group.get("from") is not None:
            item["from"] = group["from"]
        cleaned.append(item)
    return cleaned


def dump_config_yaml(config: SeedConfig) -> str:
    """SeedConfig → 精简 YAML（去掉 None 与空值，保持字段顺序）。"""
    payload = config.model_dump(by_alias=True, exclude_none=True)

    # 清理空列表 / 空字典，避免 yaml 里一堆 []；tuple 转 list（否则成 !!python/tuple）
    def prune(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: prune(v) for k, v in node.items() if v not in (None, [], {})}
        if isinstance(node, list):
            return [prune(v) for v in node]
        if isinstance(node, tuple):
            return [prune(v) for v in node]
        return node

    payload = prune(payload)
    return _pyyaml.dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )
