"""字段传播 —— 解决「父子表字段要对得上」。

README 模型二定义了六种传播模式，M2 实现其中四种：

=========  ============================================================
copy       原值复制，子字段 = 父字段（最常见的主键/业务键传播）
derive     表达式变换，可引用 ``parent.*`` 与本行字段
map        码值映射，父子系统码表不一致时查表转换
free       不传播，子字段由子表自己的组生成
=========  ============================================================

``split``（父子金额拆分，Σ子=父）留到 M4；
``aggregate``（子汇总回填父）是反向边，属 Phase 2，留到 M3。

关于 join 键
------------
``relation.join`` 声明的锚点字段对，本质上就是 copy 传播。
因此这里会自动为每条 join 键补一条 copy 规则 —— 用户不必重复声明；
若已显式声明了该子字段的传播规则，则以用户的为准。
"""

from __future__ import annotations

from typing import Any

from ..errors import GenerateError
from ..expr import Expression
from ..models import PropagateRule

__all__ = ["apply_propagate", "build_env", "effective_rules"]


def effective_rules(relation) -> list[PropagateRule]:
    """用户声明的规则 + join 键自动补的 copy 规则（去重，用户优先）。

    join 声明的锚点字段对本质上就是 copy，自动补齐可省掉大量样板。
    """
    declared = {rule.to for rule in relation.propagate}
    rules = list(relation.propagate)

    for key in relation.join:
        if key.child_field in declared:
            continue
        rules.append(PropagateRule(mode="copy", to=key.child_field, from_=key.parent_field))
    return rules


def apply_propagate(
    rules: list[PropagateRule],
    child_values: dict[str, Any],
    parent_values: dict[str, Any] | None,
    funcs: dict,
    path: str = "",
    seq: int = 0,
    split_pieces: dict[str, list] | None = None,
    part_index: int = 0,
) -> None:
    """把父行的值按规则写入子行（原地修改 ``child_values``）。

    ``split_pieces`` / ``part_index``：split 拆分由调用方先算好各份金额
    （见 :func:`table_gen.generate_child`），这里只负责按份取值写入 ——
    拆分算法与传播解耦。
    """
    parent_values = dict(parent_values or {})
    split_pieces = split_pieces or {}

    for index, rule in enumerate(rules):
        rule_path = f"{path}.propagate[{index}]({rule.mode}→{rule.to})"

        if rule.mode == "free":
            continue

        if rule.mode == "copy":
            _require_from(rule, rule_path)
            child_values[rule.to] = _parent_value(rule, parent_values, rule_path)
            continue

        if rule.mode == "map":
            _require_from(rule, rule_path)
            if not rule.mapping:
                raise GenerateError(
                    f"map 传播缺少 mapping 映射表", rule_path
                )
            source = _parent_value(rule, parent_values, rule_path)
            key = str(source)
            if key in rule.mapping:
                child_values[rule.to] = rule.mapping[key]
            elif rule.default is not None:
                child_values[rule.to] = rule.default
            else:
                raise GenerateError(
                    f"map 传播未命中: 父字段 {rule.from_}={source!r} 不在 mapping "
                    f"{sorted(rule.mapping)} 中（请补齐映射或设置 default）",
                    rule_path,
                )
            continue

        if rule.mode == "derive":
            if not rule.expr:
                raise GenerateError("derive 传播缺少 expr", rule_path)
            expression = Expression(rule.expr, funcs, rule_path)
            child_values[rule.to] = expression(
                build_env(child_values, parent_values, seq)
            )
            continue

        if rule.mode == "split":
            if not rule.from_:
                raise GenerateError(f"split 传播缺少 from（父字段名）", rule_path)
            if rule.to not in split_pieces:
                raise GenerateError(
                    f"split 传播缺少拆分结果（调用方未提供 {rule.to} 的各份金额）",
                    rule_path,
                )
            pieces = split_pieces[rule.to]
            if part_index >= len(pieces):
                raise GenerateError(
                    f"split 取第 {part_index} 份越界（共 {len(pieces)} 份）",
                    rule_path,
                )
            child_values[rule.to] = pieces[part_index]
            continue

        if rule.mode == "aggregate":
            continue  # Phase 2 回填，M3

        raise GenerateError(f"未知传播模式: {rule.mode}", rule_path)


def build_env(
    child_values: dict[str, Any],
    parent_values: dict[str, Any],
    seq: int = 0,
) -> dict[str, Any]:
    """构造表达式求值环境。

    名字解析规则（先子后父，避免歧义）：

    - 子行字段：直接写名字（``amount``）
    - 父行字段：写 ``parent.amount``；若子表**没有**同名字段，也可直接写名字
    - ``row`` / ``parent`` / ``seq`` 为保留名
    """
    env: dict[str, Any] = {}
    for key, value in parent_values.items():
        if key not in child_values:
            env[key] = value  # 父字段扁平别名，仅在无歧义时提供
    env.update(child_values)
    env["seq"] = seq
    env["row"] = child_values
    env["parent"] = parent_values
    return env


# ---------------------------------------------------------------- 内部实现


def _require_from(rule, path: str) -> None:
    if not rule.from_:
        raise GenerateError(f"{rule.mode} 传播缺少 from（父字段名）", path)


def _parent_value(rule, parent_values: dict[str, Any], path: str) -> Any:
    if rule.from_ not in parent_values:
        raise GenerateError(
            f"父表中不存在字段 {rule.from_}"
            f"（可用字段: {', '.join(sorted(parent_values)) or '无'}）",
            path,
        )
    return parent_values[rule.from_]
