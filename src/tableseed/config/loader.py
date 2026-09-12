"""配置加载：YAML → IR，并把错误定位到具体字段路径（PRD NFR-6）。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from ..errors_cn import explain_validation, explain_yaml_error
from ..models import SeedConfig


def load_config(path: str | Path) -> SeedConfig:
    """从 YAML 文件加载配置。"""
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"配置文件不存在: {file_path}")
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc
    return load_config_from_text(text, source=str(file_path))


def load_config_from_text(text: str, source: str = "<inline>") -> SeedConfig:
    """从 YAML 文本加载配置（WebUI 在线编辑走这条路）。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(explain_yaml_error(exc), source) from exc

    if raw is None:
        raise ConfigError("配置内容为空", source)
    if not isinstance(raw, dict):
        raise ConfigError("配置顶层必须是映射（mapping）", source)

    try:
        return SeedConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(explain_validation(exc), source) from exc
