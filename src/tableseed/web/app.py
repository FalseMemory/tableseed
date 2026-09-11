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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import service
from ..errors import TableSeedError
from ..models import SeedConfig
from .security import SqlRejected, validate_readonly

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


    # ---------------------------------------------------------------- 页面

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "config_path": state.config_path}

    # ---------------------------------------------------------------- 配置

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {"text": state.text, "path": state.config_path}

    @app.put("/api/config")
    def put_config(payload: ConfigPayload) -> dict[str, Any]:
        state.text = payload.text
        if state.config_path:
            Path(state.config_path).write_text(state.text, encoding="utf-8")
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
        return {"yaml": text, "problems": problems}

    # ---------------------------------------------------------- 配置编辑器

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
        if state.config_path:
            Path(state.config_path).write_text(text, encoding="utf-8")
        return {
            "text": text,
            "saved": bool(state.config_path),
            "path": state.config_path,
            "problems": problems,
            "ok": not problems,
        }

    # ---------------------------------------------------------------- 数据库连接

    @app.get("/api/database")
    def get_database() -> dict[str, Any]:
        """读取配置里的 database 段（供页面回填连接表单）。"""
        config = _current_config(state)
        if config is None or config.database is None:
            return {"configured": False}
        spec = config.database
        return {
            "configured": True,
            "type": spec.type or "mysql",
            "host": spec.host,
            "port": spec.port,
            "user": spec.user,
            "password": spec.password,
            "password_env": spec.password_env,
            "database": spec.database,
            "charset": spec.charset,
            "url": spec.url,
            "has_structured": spec.is_structured,
            "masked": spec.describe(),
            "password_source": spec.password_source,
            "env_problem": spec.env_problem(),
        }

    @app.put("/api/database")
    def put_database(payload: DatabasePayload) -> dict[str, Any]:
        """把连接信息写回 YAML 的 database 段。

        **只替换 database 段**，其余文本（含注释、字段顺序）原样保留 ——
        用 yaml.dump 重写整个文件会把用户写的注释全丢掉。
        """
        section = {
            k: v
            for k, v in payload.model_dump().items()
            if v not in (None, "") and not (k == "charset" and v == "utf8mb4")
        }
        block = None
        if section:
            section.setdefault("type", "mysql")
            block = "database:\n" + "\n".join(
                f"  {k}: {_yaml_scalar(v)}" for k, v in section.items()
            )

        text = _replace_yaml_section(state.text, "database", block)
        try:
            config = state.parse(text)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=f"写回后配置不合法: {exc}") from exc

        state.text = text
        if state.config_path:
            Path(state.config_path).write_text(text, encoding="utf-8")
        return {
            "text": text,
            "saved": bool(state.config_path),
            "path": state.config_path,
            "masked": config.database.describe() if config.database else "未配置",
        }

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
        if problems:
            raise HTTPException(status_code=400, detail={"problems": problems})

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
            if problems:
                yield _sse("error", {"message": "配置校验未通过", "problems": problems})
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

            from ..sink import resolve_sink  # noqa: PLC0415

            sink = resolve_sink(
                config, out_dir=payload.out_dir, dsn=payload.dsn, dialect=payload.dialect
            )
            sink.write(result)
            state.last_result = result

            yield _sse("done", {**state.snapshot(), "sink": sink.describe()})

        return StreamingResponse(event_stream(), media_type="text/event-stream")

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
            return _run_sql(url, statement, max_rows=max_rows, timeout=timeout)
        except TableSeedError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    """按优先级取连接串：显式 dsn → 页面传的结构化连接 → 配置里的 database 段。

    连接信息来自三处，优先级必须明确 —— 页面当前填的 > 配置里存的。
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


def _safe_table_names(conn) -> list[str] | None:
    """尽力取表名列表；取不到不算错（某些账号无权限看元数据）。"""
    try:
        from sqlalchemy import inspect  # noqa: PLC0415

        return sorted(inspect(conn).get_table_names())
    except Exception:  # pragma: no cover
        return None


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


def _yaml_scalar(value: Any) -> str:
    """把一个标量渲染成 YAML 字面量（字符串加引号，数字/布尔原样）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # 含特殊字符或形如数字的字符串要引起来，避免被 YAML 当成别的类型
    if re.search(r"[:#\[\]{}&*!|>'\"%@`,\n]|^\s|\s$|^\d", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _replace_yaml_section(text: str, key: str, block: str | None) -> str:
    """替换顶层 YAML 段的文本，**其余内容一字不动**（保留注释与格式）。

    定位 ``^key:`` 行后，吃掉其后续的缩进行（该段的子内容），再插入新段。
    段不存在时追加到文件末尾。
    """
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    replaced = False

    while index < len(lines):
        line = lines[index]
        if re.match(rf"^{re.escape(key)}\s*:", line):
            index += 1
            # 吃掉该段的子行（缩进行）；段尾的空行若后面仍是缩进行也算入
            while index < len(lines):
                current = lines[index]
                if current.startswith((" ", "\t")):
                    index += 1
                    continue
                if not current.strip() and index + 1 < len(lines) and lines[index + 1].startswith((" ", "\t")):
                    index += 1
                    continue
                break
            if block:
                out.extend(block.splitlines())
            replaced = True
            continue
        out.append(line)
        index += 1

    if not replaced and block:
        if out and out[-1].strip():
            out.append("")
        out.extend(block.splitlines())

    return "\n".join(out).rstrip("\n") + "\n"


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
