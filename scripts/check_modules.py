"""逐模块浏览器级检查：用 Node 跑真实前端代码，逐个驱动各功能模块。

与 test_web_js.py 的区别：
- 那个只验证"启动流程能跑起来"
- 这个把 **13 个功能模块**逐个驱动一遍（加载、渲染、交互入口），
  断言每个模块都产出了预期的 DOM 且没有抛错

用法：
    python scripts/check_modules.py            # 全模块
    python scripts/check_modules.py --json     # 机器可读输出
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "src" / "tableseed" / "web" / "static" / "index.html"

NODE_CANDIDATES = [
    r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
    r"C:\Program Files\nodejs\node.exe",
]

#: 运行时由 JS 动态插入的元素
DYNAMIC_IDS = {
    "btn-sql-insert", "btn-ins-to-imp", "btn-ddl-to-imp", "btn-imp-gen",
    "btn-imp-plan", "btn-confirm-insert", "btn-gen-insert-sql",
    "insert-out", "insert-hint", "prog", "sql-prev", "sql-next",
    "ed-table", "ed-add-group", "err-bar", "log-dialog", "log-dialog-content",
    "saveas-dialog", "saveas-path", "saveas-error", "btn-saveas-confirm",
    "help-dialog", "help-dialog-content",
}


def node() -> str:
    for candidate in NODE_CANDIDATES:
        if pathlib.Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 node")


def extract(html: str) -> tuple[str, set[str]]:
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    return html[start:end], set(re.findall(r'id="([\w-]+)"', html[:start]))


#: 各模块检查代码 —— 在页面脚本之后执行（此时所有函数都在作用域内）
MODULE_CHECKS = r"""
// ---------------- 分类桩数据（按 URL 特征返回不同响应）----------------
const RESP = {
  connections: { active: "demo", connections: { demo: { type: "mysql", host: "127.0.0.1",
      port: 3306, user: "root", database: "seedtest", charset: "utf8mb4" },
      stage: { type: "postgresql", host: "10.0.0.9", port: 5432, user: "u",
      database: "d", charset: "utf8" } },
      incomplete: [], active_missing: false, has_usable: true, path: "config.ini" },
  workspace: { active: "samples/txn.yaml", files: [
      { path: "samples/txn.yaml", exists: true }, { path: "test", exists: true }] },
  config: { text: "seed: 1\ntables:\n  - name: t_a\n    rows: 2\n    groups:\n"
      + "      - {type: enum, name: g_x, fields: [x], values: [[\"1\"], [\"2\"]]}\n",
      path: "samples/txn.yaml" },
  health: { ok: true, config_path: "samples/txn.yaml", started_at: "2026-09-16 10:00:00" },
  validate: { ok: true, problems: [] },
  plan: { tables: [{ table: "t_a", finite_groups: ["g_x"], combo_count: 2,
      planned_rows: 2, note: null }], total_rows: 2, order: ["t_a"],
      warnings: [], within_limits: true },
  graph: { order: ["t_a"], cyclic: false,
      nodes: [{ name: "t_a", rows: 2, groups: 1, parent: null }],
      edges: [] },
  structured: { seed: 1, max_rows: 100000, strategy: "full", sample_size: 10,
      tables: [{ name: "t_a", rows: 2, scope: "per_parent",
        groups: [{ type: "enum", name: "g_x", fields: ["x"], values: [["1"], ["2"]] }] }],
      relations: [], invariants: [] },
  generate: { seed: 1, elapsed_ms: 3, warnings: [], invariant_failures: [],
      tables: { t_a: { columns: ["x"], rows: [["1"], ["2"]], count: 2,
      truncated: false } } },
  insertsql: { ok: true, dialect: "mysql", batch_size: 1000, quote: true,
      sql: "INSERT INTO `t_a` (`x`) VALUES\n  ('1'),\n  ('2');",
      total_rows: 2, tables: { t_a: 2 }, bytes: 48 },
  verify: { total: 1, passed: true, failures: [] },
  logs: { logs: [{ time: "2026-09-16 10:00:00", kind: "生成", detail: "2 行", ok: true }],
      total: 1, page: 1, page_size: 25, pages: 1 },
  sql: { columns: ["x"], rows: [["1"], ["2"]], row_count: 2, truncated: false,
      elapsed_ms: 4 },
  importyaml: { yaml: "seed: 1\ntables:\n  - name: t_x\n    rows: 1\n    groups:\n"
      + "      - {type: const, name: g_a, fields: [a], value: [\"v\"]}\n",
      problems: [], placeholder: false },
  ddl: { ddl: "CREATE TABLE t_a (a varchar(10));" },
};
function pick(url) {
  if (url.includes("/api/connections")) return RESP.connections;
  if (url.includes("/api/workspace")) return RESP.workspace;
  if (url.includes("/api/config/structured")) return RESP.structured;
  if (url.includes("/api/config/validate")) return RESP.validate;
  if (url.includes("/api/config")) return RESP.config;
  if (url.includes("/api/health")) return RESP.health;
  if (url.includes("/api/plan")) return RESP.plan;
  if (url.includes("/api/graph")) return RESP.graph;
  if (url.includes("/api/insert/sql")) return RESP.insertsql;
  if (url.includes("/api/verify")) return RESP.verify;
  if (url.includes("/api/logs")) return RESP.logs;
  if (url.includes("/api/sql")) return RESP.sql;
  if (url.includes("/api/import/yaml")) return RESP.importyaml;
  if (url.includes("/api/ddl")) return RESP.ddl;
  if (url.includes("/api/generate")) return RESP.generate;
  if (url.includes("/api/result")) return RESP.generate;
  return { ok: true };
}
const fetched = [];
global.fetch = async (url) => {
  fetched.push(String(url));
  const payload = pick(String(url));
  return {
    ok: true, status: 200, headers: { get: () => null },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
    body: { getReader: () => ({ read: async () => ({ done: true }) }) },
  };
};
"""

HARNESS_TAIL = r"""
// ---------------- 逐模块驱动 ----------------
const report = [];
async function mod(name, fn, expect) {
  const before = fetched.length;
  try {
    await fn();
    await new Promise((r) => setTimeout(r, 30));
    const missing = (expect || []).filter((id) => {
      const el = store[id];
      const html = el && (el.innerHTML || el.value || el.textContent || "");
      return !html || String(html).trim() === "";
    });
    if (missing.length) {
      report.push({ module: name, ok: false,
        reason: "这些元素仍为空: " + missing.join(", ") });
    } else {
      report.push({ module: name, ok: true, calls: fetched.length - before });
    }
  } catch (e) {
    report.push({ module: name, ok: false,
      reason: (e && e.message) + " @ " + String((e && e.stack) || "").split("\n")[1] });
  }
}

await mod("1 配置加载与校验", async () => { await validate(true); }, ["yaml"]);
await mod("2 数据库连接区", async () => { await loadDatabase(); }, ["db-state"]);
await mod("3 配置文件工作区", async () => { await loadWorkspace(); }, ["cfg-file-select"]);
await mod("4 规模预演", async () => { await runPlan(); }, ["tab-plan"]);
await mod("5 关系图", async () => { await runGraph(); }, ["tab-graph"]);
await mod("6 结构化配置编辑器", async () => { await loadEditor(); }, ["editor-body"]);
await mod("7 生成配置（DDL/INSERT）", async () => { await runImport(); }, ["imp-out"]);
await mod("8 生成数据与结果预览", async () => {
  renderResult(RESP.generate);
  // 结果页的按钮是 innerHTML 动态生成的 —— 断言渲染出的 HTML 含关键标记
  const html = String(store["tab-result"].innerHTML || "");
  for (const token of ["btn-confirm-insert", "btn-gen-insert-sql"]) {
    if (!html.includes(token)) throw new Error("结果页缺少 " + token);
  }
}, []);
await mod("9 生成 INSERT 语句", async () => {
  $("sqlgen-dialect").value = "mysql";
  await loadInsertSql();
}, ["sqlgen-text"]);
await mod("10 不变量验证", async () => { await runVerify(); }, ["tab-result"]);
await mod("11 SQL 台", async () => {
  $("sql").value = "SELECT 1";
  await runSql();
}, ["sql-out"]);
await mod("12 操作日志", async () => { await loadLogs(); }, ["logs-body"]);
await mod("13 帮助弹窗", async () => { showEditorHelp(); }, []);

// 错误横幅检查（window error 监听会往这写）
const errBar = store["err-bar"];
if (errBar && String(errBar.textContent || "").includes("前端脚本错误")) {
  report.push({ module: "错误横幅", ok: false, reason: errBar.textContent });
}

console.log("__REPORT__" + JSON.stringify({ report, fetched: fetched.length }));
process.exit(0);
"""


def check_live(port: int = 8643) -> int:
    """对**真实运行的服务**逐个接口冒烟 —— 排除桩数据带来的假象。"""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"

    def call(method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data, {"Content-Type": "application/json"}, method=method
        )
        try:
            resp = opener.open(req, timeout=30)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except Exception:  # noqa: BLE001
                return exc.code, {}
        except Exception as exc:  # noqa: BLE001
            return 0, {"detail": str(exc)[:90]}

    status, health = call("GET", "/api/health")
    if status != 200:
        print(f"服务不可达（{base}）：{health}")
        return 2
    print(f"服务: {health}")

    status, cfg = call("GET", "/api/config")
    text = cfg.get("text", "")
    checks: list[tuple[str, bool, str]] = []

    def probe(name: str, method: str, path: str, body=None, key=None):
        code, payload = call(method, path, body)
        ok = code == 200 and (key is None or key in payload)
        detail = ""
        if not ok:
            detail = f"HTTP {code} {str(payload.get('detail') or payload)[:80]}"
        checks.append((name, ok, detail))

    probe("健康检查", "GET", "/api/health", key="ok")
    probe("读取配置", "GET", "/api/config", key="text")
    probe("配置校验", "POST", "/api/config/validate", {"text": text}, "ok")
    probe("结构化视图", "GET", "/api/config/structured", key="tables")
    probe("规模预演", "POST", "/api/plan", {"text": text}, "tables")
    probe("关系图", "POST", "/api/graph", {"text": text}, "nodes")
    probe("生成数据", "POST", "/api/generate", {"text": text}, "tables")
    probe("读取生成结果", "GET", "/api/result", key="tables")
    probe("生成 INSERT 语句", "POST", "/api/insert/sql", {"dialect": "mysql"}, "sql")
    probe("不变量验证", "POST", "/api/verify", {"text": text}, "passed")
    probe("连接列表", "GET", "/api/connections", key="connections")
    probe("配置文件清单", "GET", "/api/workspace", key="files")
    probe("操作日志", "GET", "/api/logs?page=1&page_size=10", key="logs")
    probe("DDL 导入生成配置", "POST", "/api/import/yaml",
          {"ddl": "", "inserts": "INSERT INTO t_z (a) VALUES (1);", "table": "t_z"},
          "yaml")
    probe("SQL 台（只读查询）", "POST", "/api/sql/execute", {"sql": "SELECT 1 AS a"},
          "row_count")

    # 特别检查：写操作必须被**拒绝**（200 才是问题）—— 与 probe 的成功语义相反
    for bad_sql, label in (("DROP TABLE t", "SQL 台拒绝 DROP"),
                           ("DELETE FROM t", "SQL 台拒绝 DELETE"),
                           ("SELECT 1; DROP TABLE t", "SQL 台拒绝多语句绕过")):
        code, _ = call("POST", "/api/sql/execute", {"sql": bad_sql})
        checks.append((label, code != 200, f"HTTP {code}"))

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {name.ljust(width)} {detail}")
    print(f"\n共 {len(checks)} 项，{len(checks) - failed} 项通过")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="对真实服务做逐接口冒烟（而非桩数据）")
    ap.add_argument("--port", type=int, default=8643)
    args = ap.parse_args()

    if args.live:
        return check_live(args.port)

    html = INDEX.read_text(encoding="utf-8")
    js, ids = extract(html)
    ids |= DYNAMIC_IDS

    harness = f"""
const IDS = {json.dumps(sorted(ids))};
const calls = [];
function makeEl(id) {{
  return {{
    id, value: "", textContent: "", innerHTML: "", className: "", hidden: false,
    style: {{}}, dataset: {{}}, tagName: "DIV", checked: false, disabled: false,
    _h: {{}},
    addEventListener(ev, fn) {{ this._h[ev] = fn; }},
    removeEventListener() {{}}, appendChild() {{}}, remove() {{}},
    focus() {{}}, blur() {{}}, select() {{}}, close() {{}}, showModal() {{}},
    closest() {{ return null; }}, querySelector() {{ return null; }},
    querySelectorAll() {{ return []; }}, dispatchEvent() {{}},
    setAttribute() {{}}, getAttribute() {{ return null; }}, scrollIntoView() {{}},
    classList: {{ add() {{}}, remove() {{}}, contains() {{ return false; }} }},
  }};
}}
const store = {{}};
global.document = {{
  getElementById: (id) => (store[id] ||= makeEl(id)),
  createElement: (tag) => makeEl("_" + tag),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener: () => {{}}, body: makeEl("body"), head: makeEl("head"),
}};
global.window = {{ addEventListener: () => {{}}, location: {{ href: "/" }},
  confirm: () => true, open: () => null }};
global.confirm = () => true;
global.alert = () => {{}};
global.Event = class {{ constructor(t) {{ this.type = t; }} }};
global.URL = {{ createObjectURL: () => "blob:x", revokeObjectURL: () => {{}} }};
global.Blob = class {{ constructor(parts) {{ this.parts = parts; }} }};
global.navigator = {{ clipboard: {{ writeText: async () => {{}} }} }};
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
global.TextDecoder = require("util").TextDecoder;

// 全部放在同一个作用域：桩 → 页面脚本 → 逐模块驱动。
// （页面脚本里的函数若被 try 块包住会形成块级作用域，外面就调不到了）
(async () => {{
  try {{
{MODULE_CHECKS}
{js}
{HARNESS_TAIL}
  }} catch (e) {{
    console.log("__REPORT__" + JSON.stringify({{ report: [], fatal: String(e.message),
      stack: String(e.stack || "").split("\\n").slice(0, 4) }}));
    process.exit(1);
  }}
}})();
"""

    with tempfile.TemporaryDirectory() as tmp:
        script = pathlib.Path(tmp) / "check.js"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node(), str(script)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, cwd=tmp,
        )

    payload = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("__REPORT__"):
            payload = json.loads(line[len("__REPORT__"):])
            break

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if payload.get("fatal"):
        print("驱动脚本自身出错:", payload["fatal"])
        print("\n".join(payload.get("stack", [])))
        return 2

    report = payload.get("report", [])
    if not report:
        print("没有拿到模块报告：")
        print((proc.stdout or "")[-600:])
        print((proc.stderr or "")[-600:])
        return 2

    width = max(len(r["module"]) for r in report)
    failed = 0
    for item in report:
        mark = "✓" if item["ok"] else "✗"
        failed += 0 if item["ok"] else 1
        detail = "" if item["ok"] else "  ← " + str(item.get("reason", ""))[:110]
        calls = f"（{item.get('calls', 0)} 次接口调用）" if item["ok"] else ""
        print(f"  {mark} {item['module'].ljust(width)} {calls}{detail}")

    print(f"\n共 {len(report)} 个模块，{len(report) - failed} 个正常"
          f"（该页面共发起 {payload.get('fetched', 0)} 次接口调用）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
