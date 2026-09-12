"""多数据库连接管理 —— 存于项目根的 ``config.ini``，与 YAML 配置彻底分离。

为什么挪出 YAML：连接信息（地址/账号/密码来源）属于**环境**而非**业务规则**，
造数配置要能随便分享、入库；连接信息不是。多个连接存在一个 ini 里，
页面上随时切换，YAML 里干干净净。

文件形态（configparser，段名即连接名）::

    [general]
    active = demo            # 当前使用的连接

    [demo]
    type = mysql
    host = 127.0.0.1
    port = 3306
    user = root
    password_env = TABLESEED_DB_PASSWORD   # 推荐走环境变量，不落明文
    database = tableseed_demo

    [prod]
    type = mysql
    host = 10.0.0.5
    ...

密码：支持 ``password``（明文，不推荐）与 ``password_env``（推荐）。
明文密码属于用户自己的选择，本模块原样存取，但接口回显一律脱敏。
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

from ..errors import TableSeedError
from ..models import DatabaseSpec

__all__ = [
    "ConnectionsStore",
    "connections_path",
]

#: 项目根的连接配置文件
connections_path = Path("config.ini")

#: 通用段（记录当前使用的连接名）
_GENERAL = "general"
_ACTIVE_KEY = "active"

#: 连接段允许的字段（与 DatabaseSpec 的结构化字段一致）
_SPEC_FIELDS = ("type", "host", "port", "user", "password", "password_env",
                "database", "charset", "url", "dialect")


class ConnectionsStore:
    """config.ini 的读写封装。所有写操作都先落临时文件再原子替换。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or connections_path

    # ---------------------------------------------------------------- 读

    def load(self) -> dict[str, Any]:
        """读取全部连接与当前激活名。文件不存在视为空。

        ``interpolation=None`` 必须显式关掉 —— 默认的 BasicInterpolation 会把
        值里的 ``%`` 当插值语法，密码带 ``%`` 时读写直接抛异常（这是
        "连接含特殊字符无法保存/加载"的根因）。
        """
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(self.path, encoding="utf-8")

        active = None
        connections: dict[str, dict[str, Any]] = {}

        for section in parser.sections():
            if section == _GENERAL:
                active = parser[section].get(_ACTIVE_KEY) or None
                continue
            fields: dict[str, Any] = {}
            for key in _SPEC_FIELDS:
                value = parser[section].get(key)
                if value is None or value == "":
                    continue
                fields[key] = int(value) if key == "port" else value
            if fields.get("host") or fields.get("url"):
                connections[section] = fields

        return {"active": active, "connections": connections}

    def get(self, name: str) -> dict[str, Any] | None:
        return self.load()["connections"].get(name)

    def active_name(self) -> str | None:
        return self.load()["active"]

    def active_spec(self) -> DatabaseSpec | None:
        """当前激活连接的 DatabaseSpec；没有激活连接返回 None。"""
        data = self.load()
        name = data["active"]
        if name and name in data["connections"]:
            return DatabaseSpec.model_validate(data["connections"][name])
        # 未显式激活时回落到第一个连接（保持"开箱可用"）
        for fields in data["connections"].values():
            return DatabaseSpec.model_validate(fields)
        return None

    # ---------------------------------------------------------------- 写

    def save(self, name: str, fields: dict[str, Any]) -> None:
        """新增或更新一个连接。非法连接名直接拒绝。"""
        name = (name or "").strip()
        if not name or name == _GENERAL or any(ch in name for ch in "[]=\n"):
            raise TableSeedError(f"连接名不合法: {name!r}（不能为空、general 或包含 [ ] = 及换行）")

        cleaned: dict[str, Any] = {}
        for k, v in fields.items():
            if v in (None, ""):
                continue
            if isinstance(v, str) and ("\n" in v or "\r" in v):
                raise TableSeedError(
                    f"字段 {k} 含换行符 —— 连接信息不能跨行，请检查是否粘贴了多余内容"
                )
            cleaned[k] = v
        if not cleaned.get("host") and not cleaned.get("url"):
            raise TableSeedError("至少要填写 host（或直接给 url）")

        data = self.load()
        data["connections"][name] = cleaned
        if not data["active"]:
            data["active"] = name  # 第一个连接自动成为当前连接
        self._write(data)

    def delete(self, name: str) -> None:
        data = self.load()
        if name not in data["connections"]:
            return
        del data["connections"][name]
        if data["active"] == name:
            data["active"] = next(iter(data["connections"]), None)
        self._write(data)

    def set_active(self, name: str) -> None:
        data = self.load()
        if name not in data["connections"]:
            raise TableSeedError(f"连接 {name!r} 不存在")
        data["active"] = name
        self._write(data)

    # ---------------------------------------------------------------- 内部

    def _write(self, data: dict[str, Any]) -> None:
        parser = configparser.ConfigParser(interpolation=None)  # 写侧同理：% 不能当插值语法
        parser[_GENERAL] = {_ACTIVE_KEY: data["active"] or ""}
        for name, fields in data["connections"].items():
            parser[name] = {k: str(v) for k, v in fields.items()}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            parser.write(f)
        tmp.replace(self.path)  # 原子替换，写一半被杀也不留坏文件
