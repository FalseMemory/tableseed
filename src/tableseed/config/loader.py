"""配置加载：YAML → IR，并把错误定位到具体字段路径（PRD NFR-6）。"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..errors import ConfigError
from ..errors_cn import explain_validation, explain_yaml_error
from ..models import SeedConfig

log = logging.getLogger("tableseed.config")


def load_config_text_from_file(path: str | Path) -> str:
    """读配置文件文本，带编码回退。

    **编码自动回退**：中文 Windows 上"记事本 → 另存为 ANSI"存出来的 YAML
    是 GBK 编码，直接按 UTF-8 读会抛 UnicodeDecodeError —— 曾经它是**未包装**
    的异常，CLI 打一串堆栈、Web 层直接 500，用户只看到"崩了"。
    现在 UTF-8 优先，失败回退 GBK（并记一条 warning 提醒转成 UTF-8）。
    """
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"配置文件不存在: {file_path}")

    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"读取配置文件失败: {exc}") from exc

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("gbk")
        except UnicodeDecodeError as exc:
            raise ConfigError(
                f"配置文件编码无法识别（既不是 UTF-8 也不是 GBK）: {file_path}"
                " —— 请用编辑器另存为 UTF-8",
                str(file_path),
            ) from exc
        log.warning("%s 不是 UTF-8 编码，已按 GBK 读取 —— 建议另存为 UTF-8", file_path)
        return text


def load_config(path: str | Path) -> SeedConfig:
    """从 YAML 文件加载配置。"""
    text = load_config_text_from_file(path)
    return load_config_from_text(text, source=str(Path(path)))


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
