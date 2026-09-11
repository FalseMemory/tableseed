"""tableseed 命令行入口。

终端是脚本化与自动化的入口；日常验证走 ``tableseed ui``（WebUI）。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import service
from .errors import TableSeedError
from .sink import resolve_sink

app = typer.Typer(
    add_completion=False,
    help="tableseed —— 多表联合造数工具（用字段分组描述规则，生成跨表一致的测试数据）",
)
console = Console()

CONFIG_OPTION = typer.Option(..., "-c", "--config", help="配置文件路径（单个 YAML）")


def _plain(text: str) -> str:
    """转义 rich 标记。

    配置里的表名、字段名会被拼成 ``tables[t_txn]`` 这样的路径 ——
    其中的 ``[...]`` 会被 rich 当成样式标记吃掉，导致输出缺字。
    """
    return escape(str(text))


def _fail(message: str) -> None:
    console.print(f"[bold red]✗[/bold red] {_plain(message)}")
    raise typer.Exit(code=1)


def _coverage_text(config, result, name: str) -> str:
    """实际组合覆盖 / 理论组合数 —— 覆盖度一眼可见（NFR-5）。"""
    from .engine.group_expander import combo_count, finite_groups

    finite = finite_groups(config.table(name))
    if not finite or name not in result.tables:
        return "-"

    total = combo_count(finite)
    fields = [f for g in finite for f in g.fields]
    seen = {
        tuple(row.values.get(f) for f in fields)
        for row in result.tables[name].rows
    }
    ratio = f"{len(seen) / total:.0%}" if total else "-"
    return f"{len(seen)}/{total}（{ratio}）"


def _load(config: Path):
    try:
        return service.load(config)
    except TableSeedError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1) from exc


@app.command()
def plan(config: Path = CONFIG_OPTION) -> None:
    """预演：打印各表组合数、行数与依赖序，不产出任何数据。"""
    seed_config = _load(config)
    result = service.plan(seed_config)

    table = Table(title="生成计划（预演）", title_justify="left")
    table.add_column("表", style="cyan", no_wrap=True)
    table.add_column("有限取值组", style="dim")
    table.add_column("组合数", justify="right")
    table.add_column("计划行数", justify="right", style="green")
    table.add_column("备注", style="yellow")

    for item in result.tables:
        table.add_row(
            item.table,
            " × ".join(item.finite_groups) or "-",
            str(item.combo_count),
            str(item.planned_rows),
            item.note or "",
        )

    console.print(table)
    console.print(f"生成顺序: {' → '.join(result.order)}")
    console.print(f"总行数: [bold]{result.total_rows}[/bold]（max_rows={seed_config.limits.max_rows}）")

    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

    if result.within_limits:
        console.print("[green]✓ 规模在限制之内[/green]")
    else:
        console.print("[yellow]! 规模触顶，请检查策略[/yellow]")


@app.command()
def check(config: Path = CONFIG_OPTION) -> None:
    """校验配置：分组是否不重不漏、依赖有无环、字段引用是否有效。"""
    seed_config = _load(config)
    problems = service.check(seed_config)

    if not problems:
        console.print("[green]✓ 配置校验通过[/green]")
        return

    console.print(f"[bold red]✗ 发现 {len(problems)} 个问题：[/bold red]")
    for problem in problems:
        console.print(f"  - {_plain(problem)}")
    raise typer.Exit(code=1)


@app.command()
def gen(
    config: Path = CONFIG_OPTION,
    out: Path | None = typer.Option(None, "--out", "-o", help="显式落盘目录（SQL / CSV）"),
    dsn: str | None = typer.Option(None, "--dsn", help="数据库连接串，提供则直接执行入库"),
    dialect: str | None = typer.Option(None, "--dialect", help="方言: postgresql / mysql / oracle"),
    dry_run: bool = typer.Option(False, "--dry-run", help="有连接时也只生成不写库"),
    preview: int = typer.Option(5, "--preview", help="终端预览行数"),
) -> None:
    """生成数据。默认只生成不落盘；提供 --dsn 则直接入库。"""
    seed_config = _load(config)
    problems = service.check(seed_config)
    if problems:
        console.print(f"[bold red]✗ 配置存在问题，已中止（{len(problems)} 条）：[/bold red]")
        for problem in problems:
            console.print(f"  - {_plain(problem)}")
        raise typer.Exit(code=1)

    try:
        result = service.generate(
            seed_config, out_dir=out, dsn=dsn, dialect=dialect, dry_run=dry_run
        )
    except TableSeedError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1) from exc

    sink = resolve_sink(seed_config, out_dir=out, dsn=dsn, dialect=dialect, dry_run=dry_run)

    table = Table(title="生成结果", title_justify="left")
    table.add_column("表", style="cyan", no_wrap=True)
    table.add_column("行数", justify="right", style="green")
    table.add_column("字段数", justify="right")
    table.add_column("组合覆盖", justify="right")
    for name, data in result.tables.items():
        table.add_row(
            name, str(len(data)), str(len(data.columns)), _coverage_text(seed_config, result, name)
        )
    console.print(table)

    console.print(
        f"耗时 {result.elapsed_ms} ms　种子 {result.seed}　去向：{sink.describe()}"
    )

    # ---- 生成后自检：不变量违例醒目展示 ----
    if seed_config.invariants:
        failures = result.invariant_failures
        if failures:
            console.print(
                f"[bold red]✗ 自检: {len(seed_config.invariants)} 条不变量, "
                f"{len(failures)} 行违例[/bold red]"
            )
            for failure in failures[:10]:
                console.print(f"    {_plain(failure.describe())}")
            if len(failures) > 10:
                console.print(f"    ...（其余 {len(failures) - 10} 条省略）")
        else:
            console.print(
                f"[green]✓ 自检: {len(seed_config.invariants)} 条不变量全部通过[/green]"
            )

    for name, data in result.tables.items():
        if not data.rows or preview <= 0:
            continue
        preview_table = Table(
            title=f"{name} 预览（前 {min(preview, len(data.rows))} 行）", title_justify="left"
        )
        for column in data.columns:
            preview_table.add_column(_plain(column), overflow="fold")
        for row in data.rows[:preview]:
            preview_table.add_row(
                *(_plain(row.values.get(column, "")) for column in data.columns)
            )
        console.print(preview_table)


@app.command()
def verify(
    config: Path = CONFIG_OPTION,
    preview: int = typer.Option(5, "--preview", help="每个违例显示的字段预览行数"),
) -> None:
    """校验不变量：生成一份内存数据，逐条断言「对得上」。"""
    seed_config = _load(config)

    if not seed_config.invariants:
        console.print("[yellow]! 配置里没有声明 invariants，无事可验[/yellow]")
        return

    try:
        failures = service.verify(seed_config)
    except TableSeedError as exc:
        _fail(str(exc))
        raise typer.Exit(code=1) from exc

    console.print(
        f"不变量 {len(seed_config.invariants)} 条，"
        f"违例 [bold {'red' if failures else 'green'}]{len(failures)}[/bold {'red' if failures else 'green'}] 条"
    )

    if not failures:
        console.print("[green]✓ 全部通过[/green]")
        return

    for failure in failures[:50]:
        console.print(f"[red]✗[/red] {_plain(failure.describe())}")
        fields = list(failure.row.items())[:preview]
        rendered = ", ".join(f"{k}={_plain(v)}" for k, v in fields)
        console.print(f"    {rendered}")
    if len(failures) > 50:
        console.print(f"  ...（其余 {len(failures) - 50} 条省略）")

    raise typer.Exit(code=1)


@app.command()
def ui(
    config: Path | None = typer.Option(None, "-c", "--config", help="启动时载入的配置文件"),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（默认仅本机）"),
    port: int = typer.Option(8643, "--port", help="监听端口"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
) -> None:
    """启动 WebUI：配置、预演、生成、预览、查数据都在浏览器里完成。"""
    try:
        from .web.app import run_server  # noqa: PLC0415  惰性导入，避免拖慢 CLI
    except ImportError as exc:
        _fail(
            "未安装 WebUI 依赖。请执行：pip install fastapi uvicorn"
            f"（原始错误：{exc}）"
        )
        raise typer.Exit(code=1) from exc

    run_server(
        config_path=str(config) if config else None,
        host=host,
        port=port,
        open_browser=not no_browser,
    )


def main() -> None:
    """console_scripts 入口。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
