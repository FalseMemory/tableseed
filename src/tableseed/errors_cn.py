"""pydantic 校验错误的中文化。

pydantic 的 msg 是英文（"Input should be a valid ..."），直接抛给页面用户
等于没说 —— 这里按 error["type"] 映射成中文，字段路径保留（好定位），
枚举允许值等上下文原样附上。
"""

from __future__ import annotations

from pydantic import ValidationError

from .errors import ConfigError

#: pydantic error type → 中文描述
_CN: dict[str, str] = {
    "missing": "缺少必填字段",
    "string_type": "应为文本",
    "int_type": "应为整数",
    "int_parsing": "应为整数（当前内容无法解析成整数）",
    "int_from_float": "应为整数（不允许小数）",
    "float_type": "应为数字",
    "float_parsing": "应为数字（当前内容无法解析成数字）",
    "bool_type": "应为 true / false",
    "bool_parsing": "应为 true / false",
    "list_type": "应为数组 —— 支持这几种写法：[\"Q\",\"A\"] / ['Q','A'] / Q, A（编辑器里也会自动识别）",
    "dict_type": "应为映射 —— JSON 对象写法，形如 {\"k\": \"v\"}",
    "enum": "取值不在允许范围内",
    "extra_forbidden": "不认识的字段 —— 请检查拼写",
    "string_too_short": "内容太短",
    "string_too_long": "内容太长",
    "greater_than": "数值太小",
    "greater_than_equal": "数值小于允许的最小值",
    "less_than": "数值太大",
    "less_than_equal": "数值超过允许的最大值",
    "finite_number": "应为有限数字（不能是 NaN / Infinity）",
    "json_invalid": "JSON 写法有误",
    "literal_error": "取值不在允许范围内",
    "model_type": "结构写法不对 —— 应写成映射（key: value）而不是列表",
    "is_instance_of": "结构写法不对 —— 应为该字段要求的对象/数组形式",
    "is_type": "类型不对",
    "list_type_no_item": "应为数组",
}

#: yaml 常见报错 → 中文提示（模式匹配）
_YAML_CN: list[tuple[str, str]] = [
    ("found character '\\t'", "缩进用了 Tab —— YAML 只允许空格缩进"),
    ("mapping values are not allowed", "冒号后少了空格，或缩进层级不对"),
    ("could not find expected ':'", "少了冒号或冒号后缺空格"),
    ("did not find expected '-'", "列表项的 '-' 缩进不对"),
    ("unallowed character", "有 YAML 不允许的特殊字符"),
]


def explain_validation(exc: ValidationError, prefix: str = "配置校验失败") -> str:
    """把 ValidationError 转成一行一条的中文提示（带字段路径）。

    ``prefix`` 让同一套映射服务两种场景：配置校验（默认）与请求参数校验。
    """
    lines: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"]) or "<根>"
        etype = error["type"]
        desc = _CN.get(etype)
        if desc is None:
            desc = error["msg"]  # 未收录的类型退回原文，至少不失真
        else:
            ctx = error.get("ctx") or {}
            if etype == "enum" and "expected" in ctx:
                desc += f"：{ctx['expected']}"
            if etype in ("greater_than", "greater_than_equal") and "gt" in ctx:
                desc += f"（必须 > {ctx['gt']}）"
            if etype in ("less_than", "less_than_equal") and "lt" in ctx:
                desc += f"（必须 < {ctx['lt']}）"
            if etype == "string_too_short" and "min_length" in ctx:
                desc += f"（最少 {ctx['min_length']} 个字符）"
            if etype == "string_too_long" and "max_length" in ctx:
                desc += f"（最多 {ctx['max_length']} 个字符）"
            if etype in ("model_type", "is_instance_of") and "class_name" in ctx:
                desc += f"（期望结构：{ctx['class_name']}）"
        lines.append(f"{loc}: {desc}")
    return f"{prefix}：\n  - " + "\n  - ".join(lines)


def explain_yaml_error(exc: Exception) -> str:
    """YAML 解析错误 → 中文提示（命中已知模式时）。"""
    text = str(exc)
    for pattern, hint in _YAML_CN:
        if pattern in text:
            return f"YAML 解析失败：{hint}"
    return f"YAML 解析失败：{text}"


def config_error_from_validation(exc: ValidationError, source: str | None = None) -> ConfigError:
    return ConfigError(explain_validation(exc), source)
