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
def _normalize_config_path(path: str) -> str:
    """规范配置文件路径：只允许项目根下的相对路径，拒绝绝对路径与越界。"""
    from pathlib import Path as _P

    clean = (path or "").strip().replace("\\", "/")
    if not clean:
        raise TableSeedError("文件路径不能为空")
    if _P(clean).is_absolute() or ":" in clean:
        raise TableSeedError(f"只支持项目根目录下的相对路径: {clean!r}")
    p = _P(clean)
    if ".." in p.parts:
        raise TableSeedError(f"路径不能包含 .. 越出项目根: {clean!r}")
    return str(p).replace("\\", "/")


connections_path = Path("config.ini")

#: 通用段（记录当前使用的连接名）
_GENERAL = "general"
_ACTIVE_KEY = "active"

#: 工作区段（配置文件清单与当前文件）
_WORKSPACE = "workspace"
_ACTIVE_FILE_KEY = "active_file"
_FILES_KEY = "files"

#: 连接段允许的字段（与 DatabaseSpec 的结构化字段一致）
_SPEC_FIELDS = ("type", "host", "port", "user", "password", "password_env",
                "database", "charset", "url", "dialect")


class ConnectionsStore:
    """config.ini 的读写封装。

    写操作一律「读原始文件 → 只改目标段 → 原子替换」：
    曾经用「load() 的解析结果整份重写」，而 load() 会过滤掉不完整的连接段，
    于是**任何一次保存都会把不完整连接段和其他段静默删掉**。
    现在保留文件里的所有段，只动该动的那一段。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or connections_path

    # ---------------------------------------------------------------- 读

    def _parser(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(self.path, encoding="utf-8")
        return parser

    def load(self) -> dict[str, Any]:
        """读取全部连接与当前激活名。文件不存在视为空。

        ``interpolation=None`` 必须显式关掉 —— 默认的 BasicInterpolation 会把
        值里的 ``%`` 当插值语法，密码带 ``%`` 时读写直接抛异常（这是
        "连接含特殊字符无法保存/加载"的根因）。

        **不完整连接也会返回**（``complete: False``）—— 静默丢弃会让用户在
        页面上"找不到自己刚存的连接"，且下次写回时真的删掉它。
        """
        parser = self._parser()

        active = None
        connections: dict[str, dict[str, Any]] = {}

        for section in parser.sections():
            if section == _GENERAL:
                active = parser[section].get(_ACTIVE_KEY) or None
                continue
            if section == _WORKSPACE:
                continue  # 工作区段不是连接
            fields: dict[str, Any] = {}
            for key in _SPEC_FIELDS:
                value = parser[section].get(key)
                if value is None or value == "":
                    continue
                fields[key] = int(value) if key == "port" else value
            # 字段 dict 保持纯净（可直接喂 DatabaseSpec），
            # 「信息是否完整」另用 names 列表表达
            connections[section] = fields

        #: 缺 host / url 的连接名 —— 连不上库，页面上要标出来
        incomplete = [
            n for n, f in connections.items() if not (f.get("host") or f.get("url"))
        ]
        usable = [n for n in connections if n not in incomplete]
        return {
            "active": active,
            "connections": connections,
            "incomplete": incomplete,
            #: active 指向一个不存在（已删）的连接 —— 前端要提示
            "active_missing": bool(active) and active not in connections,
            "has_usable": bool(usable),
        }

    def get(self, name: str) -> dict[str, Any] | None:
        return self.load()["connections"].get(name)

    def active_name(self) -> str | None:
        return self.load()["active"]

    def active_spec(self) -> DatabaseSpec | None:
        """当前激活连接的 DatabaseSpec；没有可用连接返回 None。

        active 指向已删除的连接时回落到第一个**信息完整**的连接 ——
        页面不至于因为一条坏数据整个连不上库。
        """
        data = self.load()
        incomplete = set(data["incomplete"])
        name = data["active"]
        if name and name in data["connections"] and name not in incomplete:
            return DatabaseSpec.model_validate(data["connections"][name])
        for candidate_name, fields in data["connections"].items():
            if candidate_name not in incomplete:
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

        parser = self._parser()
        if parser.has_section(name):
            parser.remove_section(name)   # 整体替换该段，避免残留旧键
        parser[name] = {k: str(v) for k, v in cleaned.items()}

        if not parser.has_section(_GENERAL):
            parser[_GENERAL] = {}
        if not (parser[_GENERAL].get(_ACTIVE_KEY) or "").strip():
            parser[_GENERAL][_ACTIVE_KEY] = name   # 第一个连接自动成为当前连接

        self._atomic_write(parser)

    def delete(self, name: str) -> None:
        parser = self._parser()
        if not parser.has_section(name):
            return
        parser.remove_section(name)
        current = parser[_GENERAL].get(_ACTIVE_KEY) if parser.has_section(_GENERAL) else None
        if (current or "") == name:
            remaining = [s for s in parser.sections() if s not in (_GENERAL, _WORKSPACE)]
            parser[_GENERAL][_ACTIVE_KEY] = remaining[0] if remaining else ""
        self._atomic_write(parser)

    def set_active(self, name: str) -> None:
        parser = self._parser()
        if not parser.has_section(name):
            raise TableSeedError(f"连接 {name!r} 不存在（可能已被删除）")
        if not parser.has_section(_GENERAL):
            parser[_GENERAL] = {}
        parser[_GENERAL][_ACTIVE_KEY] = name
        self._atomic_write(parser)

    def _atomic_write(self, parser: configparser.ConfigParser) -> None:
        """写临时文件再替换 —— 写一半被杀也不会留下坏文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            parser.write(f)
        tmp.replace(self.path)

    # ---------------------------------------------------------------- 工作区（配置文件清单）

    def get_workspace(self) -> dict[str, Any]:
        """配置文件清单与当前文件。active 文件始终在清单首位。"""
        parser = self._parser()
        files: list[str] = []
        if parser.has_section(_WORKSPACE):
            raw = parser[_WORKSPACE].get(_FILES_KEY, "")
            files = [f.strip() for f in raw.splitlines() if f.strip()]
        active = None
        if parser.has_section(_WORKSPACE):
            active = parser[_WORKSPACE].get(_ACTIVE_FILE_KEY) or None
        if active and active not in files:
            files.insert(0, active)
        return {"active": active, "files": files}

    def workspace_initialized(self) -> bool:
        """清单是否被初始化过 —— 用来区分「从未管理」与「用户主动清空」。"""
        return self._parser().has_section(_WORKSPACE)

    def add_file(self, path: str) -> None:
        """把一个配置文件加入清单（已存在则忽略），不改变当前文件。"""
        clean = _normalize_config_path(path)
        data = self.get_workspace()
        if clean not in data["files"]:
            data["files"].append(clean)
        self._write_workspace(data)

    def set_active_file(self, path: str) -> None:
        clean = _normalize_config_path(path)
        data = self.get_workspace()
        if clean not in data["files"]:
            data["files"].append(clean)
        data["active"] = clean
        self._write_workspace(data)

    def remove_file(self, path: str) -> None:
        clean = _normalize_config_path(path)
        data = self.get_workspace()
        data["files"] = [f for f in data["files"] if f != clean]
        if data["active"] == clean:
            data["active"] = data["files"][0] if data["files"] else None
        self._write_workspace(data)

    def _write_workspace(self, data: dict[str, Any]) -> None:
        parser = self._parser()
        if not parser.has_section(_GENERAL):
            parser[_GENERAL] = {}
        if data.get("active") and parser.has_section(_GENERAL):
            pass  # 连接的 active 由 ConnectionsStore 管理，这里不动
        parser[_WORKSPACE] = {
            _ACTIVE_FILE_KEY: data.get("active") or "",
            _FILES_KEY: "\n".join(data.get("files") or []),
        }
        self._atomic_write(parser)

