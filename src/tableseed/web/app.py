"""WebUI 后端（FastAPI）。

定位（PRD FR-11）：验证阶段的主要交互面 —— 配置、预演、生成、预览、查数据
全部在浏览器里完成。默认仅监听 127.0.0.1（NFR-7）。
"""

from __future__ import annotations

import json
import re
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from .. import service
from ..errors import TableSeedError
from ..errors_cn import explain_validation
from ..models import DatabaseSpec, SeedConfig
from .security import SqlRejected, validate_readonly

#: 服务启动时刻（页面显示，用于确认浏览器拿到的是新前端）
_APP_STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")

STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_CONFIG = """\
# tableseed —— 造数配置
# 每个字段必须归属一个组（不重不漏）；有限取值组之间做笛卡尔积。

seed: 20260910

limits:
  max_rows: 100000
  strategy: full

tables:
  - name: t_account
    groups:
      - type: enum
        name: g_status
        fields: [status_code, status_desc]
        values:
          - ["01", "正常"]
          - ["02", "冻结"]

      - type: enum
        name: g_currency
        fields: [currency]
        values:
          - ["CNY"]
          - ["USD"]

      - type: enum
        name: g_channel
        fields: [channel]
        values:
          - ["OTC"]
          - ["EBANK"]
          - ["MOBILE"]

      - type: sequence
        name: g_acct_no
        fields: [acct_no]
        format: "6222{seq:012d}"

      - type: const
        name: g_tenant
        fields: [tenant_id]
        value: ["0001"]
"""


class ConfigPayload(BaseModel):
    text: str


class GeneratePayload(BaseModel):
    text: str
    dsn: str | None = None
    out_dir: str | None = None
    dialect: str | None = None


class SqlPayload(BaseModel):
    sql: str
    dsn: str | None = None
    #: 结构化连接信息（页面分行填写）；与 dsn 二选一，dsn 优先
    database: dict[str, Any] | None = None
    confirm: str | None = None


class DdlPayload(BaseModel):
    table: str
    dsn: str | None = None
    database: dict[str, Any] | None = None


class DatabasePayload(BaseModel):
    """保存到配置的数据库连接（结构化字段）。"""

    type: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    password_env: str | None = None
    database: str | None = None
    charset: str | None = None
    url: str | None = None


class AppState:
    """WebUI 的单用户内存状态。"""

    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path
        self.text = DEFAULT_CONFIG
        if config_path and Path(config_path).exists():
            self.text = Path(config_path).read_text(encoding="utf-8")
        self.last_result: Any = None
        #: 操作日志（内存最近 500 条 + JSONL 落盘）—— 插入等关键动作可追溯
        self.operation_logs: list[dict[str, Any]] = []
        #: 最近一次「确认插入」的数据内容哈希 —— 防止同一批数据被重复插入
        self.inserted_hash: str | None = None
        self.logs_file = Path("logs") / "operations.log"
        self._restore_logs()

    def _restore_logs(self) -> None:
        """重启后从 JSONL 恢复历史日志（解析失败的行跳过）。"""
        if not self.logs_file.exists():
            return
        try:
            for line in self.logs_file.read_text(encoding="utf-8").splitlines():
                try:
                    self.operation_logs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self.operation_logs = self.operation_logs[-500:]
        except OSError:
            pass

    def log_operation(self, kind: str, detail: str, ok: bool = True) -> None:
        """记录一条操作日志：内存保留最近 500 条，同时追加到 logs/operations.log。"""
        import time as _time  # noqa: PLC0415

        entry = {
            "time": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,
            "detail": detail,
            "ok": ok,
        }
        self.operation_logs.append(entry)
        self.operation_logs = self.operation_logs[-500:]
        try:
            self.logs_file.parent.mkdir(parents=True, exist_ok=True)
            with self.logs_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 日志落盘失败不阻塞主流程

    def persist(self, text: str) -> None:
        """把配置文本写回绑定的文件。

        写前先备份到 ``.tmp/config-backup/``（保留最近 10 份）——
        保存类操作是覆盖写，一旦写坏没有备份就无法恢复。

        注意：**清理旧备份必须吃掉一切异常（含 BaseException）**。
        删除动作在某些环境里会被安全策略拦下（抛出的是 SystemExit，
        继承自 BaseException —— 普通 except Exception 抓不到），
        一旦逃逸就会把整个服务进程杀掉：用户点一下"保存配置"，
        服务就没了，页面所有请求失败，看起来像"数据全丢了"。
        备份多留几份只是占空间，崩溃是完全不可接受的。
        """
        import shutil
        import time as _time  # noqa: PLC0415

        if not self.config_path:
            return
        source = Path(self.config_path)
        if source.exists():
            try:
                backup_dir = Path(".tmp/config-backup")
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = _time.strftime("%Y%m%d-%H%M%S")
                shutil.copy2(source, backup_dir / f"{source.stem}-{stamp}{source.suffix}")
                backups = sorted(backup_dir.glob(f"{source.stem}-*{source.suffix}"))
                for stale in backups[:-10]:
                    try:
                        stale.unlink(missing_ok=True)
                    except BaseException:  # noqa: BLE001 - 删不掉就留着，绝不中断保存
                        break
            except BaseException:  # noqa: BLE001 - 备份是尽力而为，不能影响保存
                pass
        source.write_text(text, encoding="utf-8")

    def switch_to(self, path: str) -> str:
        """切换到另一个配置文件（路径经 config.ini 的清单规范校验）。"""
        from ..config.connections import _normalize_config_path  # noqa: PLC0415

        clean = _normalize_config_path(path)
        source = Path(clean)
        if not source.exists():
            raise TableSeedError(f"配置文件不存在: {clean}")
        self.config_path = clean
        self.text = source.read_text(encoding="utf-8")
        self.inserted_fingerprint = None  # 换了文件，插入指纹随之失效
        return clean

    def save_as(self, path: str, text: str) -> str:
        """另存为：当前配置写入新文件并切换绑定。"""
        from ..config.connections import _normalize_config_path  # noqa: PLC0415

        clean = _normalize_config_path(path)
        target = Path(clean)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.config_path = clean
        self.text = text
        self.inserted_fingerprint = None
        return clean

    def parse(self, text: str) -> SeedConfig:
        return service.load_text(text, source=self.config_path or "<webui>")

    def snapshot(self) -> dict[str, Any]:
        if self.last_result is None:
            return {"tables": {}, "elapsed_ms": 0, "seed": None}
        result = self.last_result
        return {
            "seed": result.seed,
            "elapsed_ms": result.elapsed_ms,
            "warnings": list(getattr(result, "warnings", []) or []),
            "invariant_failures": [
                f.model_dump() for f in getattr(result, "invariant_failures", []) or []
            ],
            "tables": {
                name: {
                    "columns": data.columns,
                    "rows": data.records,
                    "count": len(data),
                    "truncated": data.truncated,
                }
                for name, data in result.tables.items()
            },
        }


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="tableseed", docs_url=None, redoc_url=None)
    state = AppState(config_path)
    app.state.tableseed = state

    # 统一把领域异常转成 400 —— 漏掉一处就是 500，前端只看到 Internal Server Error，
    # 完全看不出「表不存在」这类真正原因
    @app.exception_handler(TableSeedError)
    async def _domain_error_handler(_request, exc: TableSeedError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # 请求参数校验（422）也走中文 —— 否则用户看到的是 FastAPI 默认的英文结构
    # （[{'type': 'string_type', 'loc': ['body','text'], 'msg': 'Input should be...'}]），
    # 与配置校验的中文提示风格完全不一致
    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(_request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": explain_validation(exc, prefix="请求参数")},
        )


    # ---------------------------------------------------------------- 页面

    @app.get("/", response_class=HTMLResponse)
    def index(v: str | None = None) -> Response:
        """WebUI 单页。

        根路径会**重定向到带版本号的地址**（``/?v=<服务启动时刻>``）：
        浏览器缓存里若留着旧版 index.html，普通刷新可能继续用旧的 ——
        换个 URL 就是换一个缓存条目，必定回源拿到新前端。
        """
        if not v:
            stamp = _APP_STARTED_AT.replace(" ", "").replace(":", "").replace("-", "")
            return RedirectResponse(url=f"/?v={stamp}", status_code=307)
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "config_path": state.config_path,
            #: 服务启动时刻 —— 页面右下角显示，用来确认"浏览器拿到的是不是新前端"
            "started_at": _APP_STARTED_AT,
        }

    # ---------------------------------------------------------------- 配置

    @app.middleware("http")
    async def _no_store(request, call_next):
        """**所有**响应都禁止缓存 —— 不只是 /api。

        两个坑，缺一不可：
        1. /api 的空响应被缓存 → 刷新后连接列表变空（数据其实还在文件里）；
        2. **index.html 本身被缓存** → 修好的前端代码根本到不了浏览器，
           用户刷新拿到的还是旧的 api()（没有 no-store），于是修复"无效"。
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {"text": state.text, "path": state.config_path}

    @app.put("/api/config")
    def put_config(payload: ConfigPayload) -> dict[str, Any]:
        state.text = payload.text
        state.persist(state.text)
        return {"saved": True, "path": state.config_path}

    @app.post("/api/config/validate")
    def validate_config(payload: ConfigPayload) -> dict[str, Any]:
        try:
            config = state.parse(payload.text)
        except TableSeedError as exc:
            return {"ok": False, "error": str(exc), "problems": []}
        problems = service.check(config)
        return {"ok": not problems, "problems": problems, "error": None}

    # ---------------------------------------------------------------- 预演

    @app.post("/api/plan")
    def plan(payload: ConfigPayload) -> dict[str, Any]:
        try:
            config = state.parse(payload.text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = service.plan(config)
        return result.model_dump()

    # ---------------------------------------------------------------- 关系图

    @app.post("/api/graph")
    def graph(payload: ConfigPayload) -> dict[str, Any]:
        """表间关系图数据：分层节点 + 带基数/传播标签的边。"""
        try:
            config = state.parse(payload.text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            from ..engine.topology import topo_order  # noqa: PLC0415

            order = topo_order(config)
        except TableSeedError as exc:
            # 成环时也要能把图画出来 —— 环本身就是最需要被看见的问题
            order = [t.name for t in config.tables]
            cyclic = str(exc)
        else:
            cyclic = None

        return {
            "order": order,
            "nodes": _graph_nodes(config, order),
            "edges": _graph_edges(config),
            "cyclic": cyclic,
        }

    @app.post("/api/verify")
    def verify_invariants(payload: ConfigPayload) -> dict[str, Any]:
        """生成一份内存数据，逐条求值不变量，返回违例清单。"""
        try:
            config = state.parse(payload.text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not config.invariants:
            return {"total": 0, "failures": [], "passed": True}

        try:
            failures = service.verify(config)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "total": len(config.invariants),
            "passed": not failures,
            "failures": [f.model_dump() for f in failures],
        }

    # ---------------------------------------------------------- 快速生成

    @app.post("/api/import/yaml")
    def import_yaml(payload: dict[str, Any]) -> dict[str, Any]:
        """从建表语句 + INSERT 样例生成 YAML 配置草稿。"""
        from ..config.ddl_import import generate_yaml  # noqa: PLC0415

        text = generate_yaml(
            payload.get("ddl") or "",
            payload.get("inserts") or "",
            table=payload.get("table"),
            seed=payload.get("seed") or 20260910,
        )
        problems: list[str] = []
        try:
            config = state.parse(text)
            problems = service.check(config)
        except TableSeedError as exc:
            problems = [str(exc)]
        # 占位模板不是真配置 —— 显式告知前端，避免它被当成草稿保存、覆盖用户文件
        placeholder = text.lstrip().startswith("# 无法生成配置")
        return {"yaml": text, "problems": problems, "placeholder": placeholder}

    @app.post("/api/insert/sql")
    def render_insert_sql(payload: dict[str, Any]) -> dict[str, Any]:
        """把最近一次生成结果渲染成 INSERT 语句 —— **不需要数据库连接**。

        为什么需要它：目标库不允许直连（生产库只走工单/第三方平台）时，
        造数工具不能因此变成废物。生成 SQL 交给用户去对方平台执行即可。
        多表按**生成顺序（拓扑序）** 拼接，保证外键依赖在前。
        """
        from ..render import render_sql  # noqa: PLC0415

        if state.last_result is None:
            raise HTTPException(status_code=400, detail="还没有生成过数据 —— 请先点「生成数据」")

        dialect = (payload.get("dialect") or "").strip()
        if not dialect:
            # 没指定就跟当前连接走；连不上库时给最通用的 mysql
            try:
                from ..config.connections import ConnectionsStore  # noqa: PLC0415
                spec = ConnectionsStore().active_spec()
                dialect = (spec.dialect or spec.type) if spec else "mysql"
            except Exception:  # noqa: BLE001 - 推断失败不影响渲染
                dialect = "mysql"

        default_batch = {"oracle": 100, "postgresql": 500}.get(dialect, 1000)
        try:
            batch_size = int(payload.get("batch_size") or default_batch)
        except (TypeError, ValueError):
            batch_size = default_batch
        batch_size = max(1, min(batch_size, 5000))
        quote = payload.get("quote", True)

        result = state.last_result
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"-- tableseed 生成 · 共 {sum(len(d) for d in result.tables.values())} 行"
            f" · seed={result.seed} · 方言={dialect}",
            f"-- 生成时间 {stamp}（同 seed + 同配置可复现同一批数据）",
            f"-- 建议执行顺序：按下方表的先后顺序（已按依赖排序）",
            "",
        ]
        parts: list[str] = []
        for name, data in result.tables.items():   # dict 顺序 = 生成顺序 = 拓扑序
            parts.append(f"-- ===== 表 {name}（{len(data)} 行）=====")
            parts.append(render_sql(data, dialect=dialect, batch_size=batch_size, quote=quote))
            parts.append("")
        sql = "\n".join(lines) + "\n".join(parts)

        tables = {n: len(d) for n, d in result.tables.items()}
        size = len(sql.encode("utf-8"))
        state.log_operation(
            "生成 INSERT", f"{sum(tables.values())} 行 → {dialect} SQL（{size // 1024} KB）"
        )
        return {
            "ok": True,
            "dialect": dialect,
            "batch_size": batch_size,
            "quote": bool(quote),
            "sql": sql,
            "total_rows": sum(tables.values()),
            "tables": tables,
            "bytes": size,
        }

    # ---------------------------------------------------------- 配置文件工作区（多文件）

    @app.get("/api/workspace")
    def get_workspace() -> dict[str, Any]:
        """配置文件清单与当前文件（清单存在 config.ini 的 workspace 段）。

        服务启动时 `-c` 指定的配置文件会**自动纳入清单** ——
        否则清单可能是空的，用户看到的就是"我保存过的 YAML 文件都不见了"。
        """
        from ..config.connections import ConnectionsStore, _normalize_config_path  # noqa: PLC0415

        store = ConnectionsStore()
        # 从未初始化过清单（config.ini 里没有 workspace 段）→ 把服务启动时
        # -c 指定的配置自动纳入，避免用户看到"我保存过的 YAML 都不见了"。
        # 用户主动清空过清单则尊重其意图，不自动加回（否则"移除"永远无效）。
        if not store.workspace_initialized() and state.config_path:
            try:
                store.add_file(_normalize_config_path(state.config_path))
            except TableSeedError:
                pass
        data = store.get_workspace()
        files = [{"path": f, "exists": Path(f).exists()} for f in data["files"]]
        active = data["active"] or state.config_path
        return {"active": active, "files": files}

    @app.post("/api/workspace/switch")
    def switch_workspace(payload: dict[str, Any]) -> dict[str, Any]:
        """切换到清单里的另一个配置文件（服务随之载入该文件）。"""
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        path = payload.get("path") or ""
        try:
            clean = state.switch_to(path)
            ConnectionsStore().set_active_file(clean)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.log_operation("切换配置", f"当前配置文件 → {clean}")
        return {"ok": True, "path": clean, "text": state.text}

    @app.post("/api/workspace/save-as")
    def save_as_workspace(payload: dict[str, Any]) -> dict[str, Any]:
        """另存为：当前左栏配置写入新文件，加入清单并切换绑定。"""
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        path = payload.get("path") or ""
        text = payload.get("text")
        if text is None:
            text = state.text
        try:
            clean = state.save_as(path, text)
            ConnectionsStore().set_active_file(clean)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.log_operation("另存为", f"配置另存为 {clean}")
        return {"ok": True, "path": clean, "text": state.text}

    @app.post("/api/workspace/remove")
    def remove_workspace_file(payload: dict[str, Any]) -> dict[str, Any]:
        """把文件移出清单（不删除磁盘文件）。"""
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        path = payload.get("path") or ""
        ConnectionsStore().remove_file(path)
        state.log_operation("移除配置", f"{path} 移出清单（文件保留）")
        return {"ok": True}

    # ---------------------------------------------------------- 配置编辑器

    @app.post("/api/config/reload")
    def reload_config() -> dict[str, Any]:
        """从磁盘重新载入当前配置文件。

        为什么需要：配置文件可能被服务之外的东西改动（git checkout、外部编辑器、
        自动化脚本）。服务内存里保有一份副本，不同步就会出现"文件明明是干净的、
        页面却还报旧错误"的错位。点一下这个按钮即回到磁盘真实内容。
        """
        if not state.config_path:
            raise HTTPException(status_code=400, detail="当前没有绑定配置文件（仅内存模式）")
        source = Path(state.config_path)
        if not source.exists():
            raise HTTPException(status_code=400, detail=f"配置文件不存在: {state.config_path}")
        try:
            clean = state.switch_to(state.config_path)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.log_operation("重载配置", f"从磁盘重新载入 {clean}")
        return {"ok": True, "path": clean, "text": state.text}

    @app.get("/api/config/structured")
    def get_structured() -> dict[str, Any]:
        """当前配置的编辑视图（供表格化编辑）。"""
        from .structured import to_edit_view  # noqa: PLC0415

        try:
            config = state.parse(state.text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return to_edit_view(config)

    @app.put("/api/config/structured")
    def put_structured(payload: dict[str, Any]) -> dict[str, Any]:
        """保存编辑视图 —— 校验 + 生成 YAML 写回状态（绑定了文件则落盘）。"""
        from .structured import from_edit_view  # noqa: PLC0415

        try:
            text = from_edit_view(payload.get("edit") or {})
            config = state.parse(text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        problems = service.check(config)
        state.text = text
        state.persist(text)
        return {
            "text": text,
            "saved": bool(state.config_path),
            "path": state.config_path,
            "problems": problems,
            "ok": not problems,
        }

    # ---------------------------------------------------------------- 数据库连接（config.ini 多连接）

    @app.get("/api/connections")
    def get_connections() -> dict[str, Any]:
        """全部连接 + 当前激活名。

        每条连接附脱敏串（masked）与密码来源（password_source），供页面回显；
        `incomplete` 列出缺 host/url 的连接（页面要标出来，不能静默丢弃）；
        `active_missing` 表示当前连接指向一个已不存在的连接。
        **只捕获校验异常**：曾经这里用宽 except Exception，把 DatabaseSpec
        未导入的 NameError 一起吞成「(配置有误: ...)」—— 页面看着像数据坏了，
        实际是代码 bug，藏了很久。
        """
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        data = ConnectionsStore().load()
        incomplete = set(data["incomplete"])
        connections = {}
        for name, fields in data["connections"].items():
            # **绝不下发明文密码**：原来 `{**fields}` 把 password 一起回传，
            # 页面源码 / 开发者工具 / 网络面板 / 截图里都能看到用户真实密码。
            # 改成只给"是否已配置"标记 —— 前端编辑时密码框留空即表示不修改。
            safe = {k: v for k, v in fields.items() if k != "password"}
            entry = {
                **safe,
                "password_set": bool(fields.get("password")) or bool(fields.get("password_env")),
                "password_env_set": bool(fields.get("password_env")),
                "complete": name not in incomplete,
            }
            try:
                spec = DatabaseSpec.model_validate(fields)
            except ValidationError as exc:
                entry.update(
                    masked=f"(连接信息不完整: {explain_validation(exc)})",
                    password_source="invalid",
                )
            except Exception as exc:  # 代码 bug 必须炸出来，不吞
                raise HTTPException(
                    status_code=500, detail=f"连接回显失败（内部错误）: {exc}"
                ) from exc
            else:
                entry.update(masked=spec.describe(), password_source=spec.password_source)
            connections[name] = entry
        return {
            "active": data["active"],
            "connections": connections,
            "incomplete": data["incomplete"],
            "active_missing": data["active_missing"],
            "has_usable": data["has_usable"],
            "path": str(Path(ConnectionsStore().path).resolve()),
        }

    @app.put("/api/connections")
    def put_connection(payload: dict[str, Any]) -> dict[str, Any]:
        """新增/更新一个连接。name 为连接名，其余为连接字段。

        **密码留空 = 不修改**：GET /api/connections 不再下发明文密码，
        所以前端编辑已有连接时密码框是空的。这里把"空密码"解释成
        "沿用原密码"，而不是把用户已保存的密码清成空串。
        """
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        name = payload.get("name")
        fields = {k: v for k, v in payload.items() if k != "name" and v is not None}

        store = ConnectionsStore()
        if not str(fields.get("password") or "").strip():
            fields.pop("password", None)
            existing = (store.load()["connections"].get(name or "") or {})
            if existing.get("password"):
                fields["password"] = existing["password"]

        try:
            store.save(name or "", fields)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.log_operation("保存连接", f"连接 {name}（{spec_mask(fields)}）")
        return {"ok": True, "masked": spec_mask(fields)}

    @app.post("/api/connections/delete")
    def delete_connection(payload: dict[str, Any]) -> dict[str, Any]:
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        name = payload.get("name") or ""
        ConnectionsStore().delete(name)
        state.log_operation("删除连接", f"连接 {name}")
        return {"ok": True}

    @app.post("/api/connections/active")
    def set_active_connection(payload: dict[str, Any]) -> dict[str, Any]:
        from ..config.connections import ConnectionsStore  # noqa: PLC0415

        name = payload.get("name") or ""
        store = ConnectionsStore()
        data = store.load()
        if name not in data["connections"]:
            raise HTTPException(
                status_code=400, detail=f"连接 {name!r} 不存在（可能已被删除）"
            )
        # 切到信息不全的连接等于把自己切成不可用 —— 先拦下，别切了再报错
        if name in data["incomplete"]:
            raise HTTPException(
                status_code=400,
                detail=f"连接 {name!r} 信息不完整（缺数据库地址），无法切换 —— 请补全 host 后再切换",
            )
        try:
            store.set_active(name)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        state.log_operation("切换连接", f"当前连接 → {name}")
        return {"ok": True, "active": name}

    @app.post("/api/database/test")
    def test_database(payload: DatabasePayload) -> dict[str, Any]:
        """试连一次，让用户知道连接信息填得对不对。

        失败也返回 200 + ``ok=false``：这是**检查结果**而非请求错误，
        前端按结果渲染成红/绿提示，不该走异常分支。
        """
        import time as _time  # noqa: PLC0415

        from ..models import DatabaseSpec  # noqa: PLC0415

        try:
            spec = DatabaseSpec.model_validate(payload.model_dump(exclude_none=True))
        except Exception as exc:
            return {"ok": False, "message": f"连接信息不完整或有误：{exc}"}

        problem = spec.env_problem()
        if problem:
            return {"ok": False, "message": problem}

        url = spec.resolved_url()
        if not url:
            return {"ok": False, "message": "连接信息不完整：地址、用户名、数据库名为必填"}

        try:
            from sqlalchemy import create_engine, text  # noqa: PLC0415
        except ImportError:
            return {"ok": False, "message": "未安装 SQLAlchemy/pymysql，无法测试连接"}

        engine = create_engine(url, future=True)
        started = _time.perf_counter()
        try:
            with engine.connect() as conn:
                version = conn.execute(text("SELECT VERSION()")).scalar()
                tables = _safe_table_names(conn)
            return {
                "ok": True,
                "message": f"连接成功 · {version}"
                + (f" · 库中 {len(tables)} 张表" if tables is not None else ""),
                "version": str(version),
                "tables": tables or [],
                "masked": spec.describe(),
                "elapsed_ms": int((_time.perf_counter() - started) * 1000),
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"连接失败：{_short_db_error(exc)}",
                "masked": spec.describe(),
                "elapsed_ms": int((_time.perf_counter() - started) * 1000),
            }
        finally:
            engine.dispose()

    # ---------------------------------------------------------------- 生成

    @app.post("/api/generate")
    def generate(payload: GeneratePayload) -> dict[str, Any]:
        try:
            config = state.parse(payload.text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        problems = service.check(config)
        # [提示] 不算错误（例如"传播会覆盖组里的取值"是允许的覆盖语义），不拦生成
        errors = [p for p in problems if not p.startswith("[提示]")]
        if errors:
            raise HTTPException(status_code=400, detail={"problems": errors})

        try:
            result = service.generate(
                config,
                out_dir=payload.out_dir,
                dsn=payload.dsn,
                dialect=payload.dialect,
            )
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        state.last_result = result
        return state.snapshot()

    @app.post("/api/generate/stream")
    def generate_stream(payload: GeneratePayload) -> StreamingResponse:
        """SSE 版本：逐表推送进度，便于大行数时观察进展。"""

        def event_stream() -> Iterator[str]:
            try:
                config = state.parse(payload.text)
            except TableSeedError as exc:
                yield _sse("error", {"message": str(exc)})
                return

            problems = service.check(config)
            errors = [p for p in problems if not p.startswith("[提示]")]
            if errors:
                yield _sse("error", {"message": "配置校验未通过", "problems": errors})
                return

            total = len(config.tables)
            yield _sse("progress", {"done": 0, "total": total, "table": None})

            events: list[dict[str, Any]] = []

            def on_progress(event: str, payload_: dict[str, Any]) -> None:
                events.append({"event": event, "data": payload_})

            try:
                # 必须走 generate_all —— 只有它按拓扑序生成并应用关系传播。
                # 逐表调用 generate_table 会绕过父子关系，造出对不上的数据。
                from ..engine import generate_all  # noqa: PLC0415

                result = generate_all(config, progress=on_progress)
            except TableSeedError as exc:
                yield _sse("error", {"message": str(exc)})
                return

            for item in events:
                if item["event"] == "table_done":
                    yield _sse(
                        "progress",
                        {
                            "done": item["data"]["done"],
                            "total": item["data"]["total"],
                            "table": item["data"]["table"],
                            "rows": item["data"]["rows"],
                        },
                    )
                elif item["event"] == "phase2_done":
                    yield _sse("phase2", {"tables": item["data"]["tables"]})
                else:
                    yield _sse("warning", {"message": item["data"]["message"]})
            events.clear()

            from ..sink import MemorySink  # noqa: PLC0415

            # WebUI 生成一律走内存 —— 入库是独立的「确认插入」动作，
            # 预览确认之后再写库（两段式），避免手一滑直接污染数据库
            sink = MemorySink()
            sink.write(result)
            state.last_result = result

            yield _sse("done", {**state.snapshot(), "sink": "内存模式（预览后可确认插入）"})
            state.log_operation(
                "生成",
                f"seed={config.seed}，"
                + ", ".join(f"{n} {len(t)} 行" for n, t in result.tables.items()),
            )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/logs")
    def get_logs(
        page: int = 1,
        page_size: int = 50,
        kind: str | None = None,
        q: str | None = None,
        ok: str | None = None,
    ) -> dict[str, Any]:
        """操作日志：分页 + 按操作类型 / 关键字 / 结果筛选（最近的在前）。"""
        logs = list(reversed(state.operation_logs))
        if kind:
            logs = [l for l in logs if l["kind"] == kind]
        if q:
            logs = [l for l in logs if q.lower() in l["detail"].lower()]
        if ok is not None and ok != "":
            want = ok.lower() == "true"
            logs = [l for l in logs if l["ok"] == want]

        total = len(logs)
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        start = (page - 1) * page_size
        return {
            "logs": logs[start : start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, -(-total // page_size)),
        }

    @app.post("/api/logs/clear")
    def clear_logs() -> dict[str, Any]:
        """清空操作日志（内存与落盘文件）。"""
        state.operation_logs.clear()
        try:
            state.logs_file.write_text("", encoding="utf-8")
        except OSError:
            pass
        state.log_operation("清理日志", "操作日志已清空")
        return {"ok": True}

    @app.post("/api/insert")
    def insert_result(payload: dict[str, Any]) -> dict[str, Any]:
        """把最近一次生成的结果插入数据库（生成与入库两段式的第二段）。"""
        import time as _time  # noqa: PLC0415

        if state.last_result is None:
            raise HTTPException(status_code=400, detail="还没有生成过数据 —— 请先点「生成数据」")

        result = state.last_result
        fingerprint = _result_fingerprint(result)
        if state.inserted_hash == fingerprint:
            state.log_operation("插入", "重复插入被拦截（数据内容与上次完全相同）", ok=False)
            return {
                "ok": False,
                "duplicate": True,
                "message": "本次预览的数据与上一次插入的数据完全相同，直接插入会因主键冲突失败。"
                           "请修改配置（取值、行数、主键规则等）后重新生成，再点确认插入。",
            }

        url = _resolve_url(state, payload.get("dsn"), payload.get("database"))
        if not url:
            detail = "未配置数据库连接 —— 请在左栏顶部填写并保存连接信息"
            state.log_operation("插入", detail, ok=False)
            raise HTTPException(status_code=400, detail=detail)

        from ..sink import DbSink  # noqa: PLC0415
        from ..models import DatabaseSpec  # noqa: PLC0415

        fields = payload.get("database") or {}
        spec = DatabaseSpec.model_validate(fields) if fields else (
            _current_config(state).database if _current_config(state) and _current_config(state).database else DatabaseSpec()
        )

        started = _time.perf_counter()
        try:
            sink = DbSink(
                url=url,
                dialect=spec.dialect or (spec.type or "mysql"),
                batch_size=spec.batch_size,
            )
            sink.write(result)
        except TableSeedError as exc:
            state.log_operation("插入", f"入库失败: {exc}", ok=False)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        elapsed = int((_time.perf_counter() - started) * 1000)
        state.inserted_hash = fingerprint
        summary = ", ".join(f"{n} {len(d)} 行" for n, d in result.tables.items())
        state.log_operation("插入", f"入库成功（{summary}，{elapsed} ms）")
        return {
            "ok": True,
            "elapsed_ms": elapsed,
            "tables": {n: len(d) for n, d in result.tables.items()},
            "masked": _mask_url(url),
            "message": f"插入成功：{summary}",
        }

    @app.get("/api/result")
    def result() -> dict[str, Any]:
        return state.snapshot()

    # ---------------------------------------------------------------- SQL 查询台

    @app.post("/api/sql/ddl")
    def get_ddl(payload: DdlPayload) -> dict[str, Any]:
        """按表名反射建表语句（SQLAlchemy Inspector + CreateTable）。"""
        url = _resolve_url(state, payload.dsn, payload.database)
        if not url:
            raise HTTPException(
                status_code=400,
                detail="未配置数据库连接 —— 在页面上填写连接信息，或保存到配置的 database 段",
            )
        state.log_operation("建表语句", f"反射表 {payload.table} 的建表语句")
        return {"ddl": _reflect_ddl(url, payload.table)}

    @app.post("/api/sql/execute")
    def execute_sql(payload: SqlPayload) -> dict[str, Any]:
        config = None
        try:
            config = state.parse(state.text)
        except TableSeedError:
            config = None

        allow_write = bool(config.sql.allow_write) if config else False
        # 写模式必须页面二次确认（FR-12.6）
        if allow_write and (payload.confirm or "").strip().upper() != "EXECUTE":
            raise HTTPException(
                status_code=403,
                detail="写模式需要二次确认：请在确认框中输入 EXECUTE",
            )

        try:
            statement = validate_readonly(payload.sql, allow_write=allow_write)
        except SqlRejected as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        url = _resolve_url(state, payload.dsn, payload.database)
        if not url and config and config.database:
            url = config.database.resolved_url()
        if not url:
            raise HTTPException(
                status_code=400,
                detail="未配置数据库连接 —— 在页面上填写连接信息，"
                "或保存到配置的 database 段。",
            )

        max_rows = config.sql.max_rows if config else 1000
        timeout = config.sql.timeout_seconds if config else 30

        try:
            result = _run_sql(url, statement, max_rows=max_rows, timeout=timeout)
        except TableSeedError as exc:
            state.log_operation("SQL 查询", f"{_first_line(statement)} → 失败: {exc}", ok=False)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        state.log_operation(
            "SQL 查询", f"{_first_line(statement)} → {result['row_count']} 行"
        )
        return result

    return app


# ---------------------------------------------------------------- 内部实现


def _graph_nodes(config: SeedConfig, order: list[str]) -> list[dict[str, Any]]:
    """图的节点 = 表。layer 用于分层布局（父在左、子在右）。"""
    from ..engine.group_expander import finite_groups  # noqa: PLC0415

    parents = {r.child for r in config.relations}
    layer: dict[str, int] = {}
    for name in order:
        incoming = [r.parent for r in config.relations if r.child == name]
        layer[name] = 0 if not incoming else max(layer.get(p, 0) + 1 for p in incoming)

    nodes = []
    for table in config.tables:
        fields: list[str] = []
        for group in table.groups:
            for field in group.fields:
                if field not in fields:
                    fields.append(field)
        if table.columns:  # 由 propagate 提供的字段也要显示
            for column in table.columns:
                if column.name not in fields:
                    fields.append(column.name)

        aggregates = [g.name for g in table.groups if g.type == "aggregate"]
        nodes.append(
            {
                "name": table.name,
                "layer": layer.get(table.name, 0),
                "is_root": table.name not in parents,
                "field_count": len(fields),
                "finite_count": len(finite_groups(table)),
                "aggregates": aggregates,
                "fields": fields,
            }
        )
    return nodes


def _graph_edges(config: SeedConfig) -> list[dict[str, Any]]:
    """图的边 = 关系，标签带上基数、锚点与传播模式。"""
    from ..engine.propagator import effective_rules  # noqa: PLC0415

    edges = []
    for relation in config.relations:
        rules = effective_rules(relation)
        modes: list[str] = []
        for rule in rules:
            if rule.mode not in modes:
                modes.append(rule.mode)
        edges.append(
            {
                "parent": relation.parent,
                "child": relation.child,
                "cardinality": relation.cardinality,
                "existence": relation.existence,
                "join": [k.model_dump() for k in relation.join],
                "propagate": [
                    {
                        "mode": r.mode,
                        "to": r.to,
                        "from": r.from_,
                        "expr": r.expr,
                    }
                    for r in rules
                ],
                "modes": modes,
                "drive_by": list(relation.drive_by),
            }
        )

    # aggregate 组构成「子 → 父」的反向边 —— 单独画成虚线，
    # 让用户一眼看见哪些字段是 Phase 2 回填出来的
    for table in config.tables:
        children = [r.child for r in config.relations if r.parent == table.name]
        for group in table.groups:
            if group.type != "aggregate":
                continue
            source = group.from_.split(".")[0] if group.from_ else (
                children[0] if len(children) == 1 else ""
            )
            if not source:
                continue
            edges.append(
                {
                    "parent": source,
                    "child": table.name,
                    "cardinality": "aggregate",
                    "existence": "required",
                    "join": [],
                    "propagate": [{"mode": "aggregate", "to": ",".join(group.fields),
                                   "from": None, "expr": group.expr}],
                    "modes": ["aggregate"],
                    "drive_by": [],
                }
            )
    return edges


def _resolve_url(
    state: AppState, dsn: str | None, database: dict[str, Any] | None
) -> str | None:
    """按优先级取连接串：显式 dsn → 页面表单 → config.ini 激活连接 → YAML database 段。

    连接信息已迁移到 config.ini（与业务规则分离，配置可随便分享）；
    YAML 的 database 段仅为兼容旧配置保留，不再推荐。
    """
    if dsn:
        return dsn
    if database:
        from ..models import DatabaseSpec  # noqa: PLC0415

        try:
            spec = DatabaseSpec.model_validate(database)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"连接信息不完整或有误: {exc}"
            ) from exc
        problem = spec.env_problem()
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        url = spec.resolved_url()
        if url:
            return url

    # config.ini 里激活的连接
    from ..config.connections import ConnectionsStore  # noqa: PLC0415

    ini_spec = ConnectionsStore().active_spec()
    if ini_spec is not None:
        problem = ini_spec.env_problem()
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        url = ini_spec.resolved_url()
        if url:
            return url

    # 旧配置兼容：YAML 的 database 段
    config = _current_config(state)
    if config and config.database:
        problem = config.database.env_problem()
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        return config.database.resolved_url()
    return None


def _current_config(state: AppState) -> SeedConfig | None:
    """解析当前文本配置；解析失败返回 None（各接口自行降级）。"""
    try:
        return state.parse(state.text)
    except TableSeedError:
        return None


def _first_line(text: str) -> str:
    """SQL 语句摘要：取第一行、截断到 60 字符，用于日志展示。"""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:60] + ("…" if len(line) > 60 else "")


def _safe_table_names(conn) -> list[str] | None:
    """尽力取表名列表；取不到不算错（某些账号无权限看元数据）。"""
    try:
        from sqlalchemy import inspect  # noqa: PLC0415

        return sorted(inspect(conn).get_table_names())
    except Exception:  # pragma: no cover
        return None


def spec_mask(fields: dict[str, Any]) -> str:
    """连接字段的脱敏摘要（用于日志与回显）。"""
    from ..models import DatabaseSpec  # noqa: PLC0415

    try:
        return DatabaseSpec.model_validate(fields).describe()
    except Exception:
        return f"{fields.get('type', 'mysql')}://{fields.get('user', '?')}@{fields.get('host', '?')}/{fields.get('database', '?')}"


def _short_db_error(exc: Exception) -> str:
    """把驱动抛的长错误压成一句人话 —— 用户要的是「密码错」而不是堆栈。"""
    text = str(exc)
    if "Access denied" in text:
        return "用户名或密码不正确（Access denied）"
    if "Unknown database" in text or "1049" in text:
        return "数据库不存在（Unknown database）"
    if "Can't connect" in text or "2003" in text:
        return "连不上服务器 —— 检查地址、端口，以及数据库服务是否已启动"
    if "timed out" in text.lower():
        return "连接超时 —— 检查地址与网络"
    if "No module named" in text:
        return text
    return text.splitlines()[0][:200]


def _reflect_ddl(url: str, table: str) -> str:
    """反射表结构并生成 CREATE TABLE 语句。

    生成的是 SQLAlchemy 方言 DDL —— 类型写法可能与原库略有出入
    （如 VARCHAR2 → VARCHAR），作为造数配置的输入足够了。
    """
    try:
        from sqlalchemy import MetaData, create_engine  # noqa: PLC0415
        from sqlalchemy.schema import CreateTable  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise TableSeedError("未安装 SQLAlchemy，无法反射建表语句") from exc

    from ..errors import SinkError  # noqa: PLC0415

    engine = create_engine(url, future=True)
    try:
        metadata = MetaData()
        try:
            metadata.reflect(bind=engine, only=[table])
        except Exception as exc:
            # reflect 失败 ≠ 连接失败 —— 大概率是表不存在，先查清楚再定性
            from sqlalchemy import inspect  # noqa: PLC0415

            try:
                available = inspect(engine).get_table_names()
            except Exception:
                raise SinkError(f"连接数据库失败: {exc}") from exc

            if table in available:
                raise SinkError(f"表 {table} 存在但反射失败: {exc}") from exc
            hint = f"库中现有表: {', '.join(available[:20])}" if available else "库中没有任何表"
            raise SinkError(f"数据库中不存在表 {table}（{hint}）") from exc

        if table not in metadata.tables:
            from sqlalchemy import inspect  # noqa: PLC0415

            available = inspect(engine).get_table_names()
            hint = f"库中现有表: {', '.join(available[:20])}" if available else "库中没有任何表"
            raise SinkError(f"数据库中不存在表 {table}（{hint}）")

        return str(CreateTable(metadata.tables[table]).compile(engine)).strip() + ";"
    finally:
        engine.dispose()


def _result_fingerprint(result) -> str:
    """生成结果的**内容哈希**：任何一行数据变了指纹就变。

    旧实现用 (seed, 表名, 行数) 做指纹 —— 用户改了主键规则后行数不变，
    会被误判成"同一批数据"而拒绝插入。防重复的唯一可靠依据是数据本身。
    """
    import hashlib
    import json

    digest = hashlib.sha256()
    for name in sorted(result.tables):
        data = result.tables[name]
        digest.update(name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(json.dumps(
            {"columns": data.columns, "rows": data.to_records()},
            ensure_ascii=False, sort_keys=True, default=str,
        ).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _mask_url(url: str) -> str:
    """连接串脱敏（密码替换成 ***）。"""
    import re  # noqa: PLC0415

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _run_sql(url: str, statement: str, max_rows: int, timeout: int) -> dict[str, Any]:
    try:
        from sqlalchemy import create_engine, text  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise TableSeedError("未安装 SQLAlchemy，无法执行查询") from exc

    from ..errors import SinkError

    engine = create_engine(url, future=True)
    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            result = conn.execute(text(statement))
            columns = list(result.keys())
            fetched = result.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [dict(zip(columns, row)) for row in fetched[:max_rows]]
        return {
            "columns": columns,
            "rows": _jsonable(rows),
            "row_count": len(rows),
            "truncated": truncated,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        raise SinkError(f"查询失败: {exc}") from exc
    finally:
        engine.dispose()


def _jsonable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把日期/Decimal 等类型转成可 JSON 序列化的值。"""
    from datetime import date, datetime
    from decimal import Decimal

    def convert(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    return [{k: convert(v) for k, v in row.items()} for row in rows]


def run_server(
    config_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8643,
    open_browser: bool = True,
) -> None:
    """启动 WebUI 服务。"""
    import uvicorn  # noqa: PLC0415

    safe_host = host if host in {"127.0.0.1", "localhost"} else "127.0.0.1"
    if safe_host != host:
        print(f"[tableseed] 出于安全考虑，监听地址已强制为 {safe_host}（NFR-7）")

    url = f"http://{safe_host}:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"[tableseed] WebUI: {url}")
    uvicorn.run(create_app(config_path), host=safe_host, port=port, log_level="warning")
