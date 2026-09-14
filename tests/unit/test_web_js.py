"""前端冒烟测试：用 Node 桩 DOM 真跑一遍 index.html 里的脚本。

为什么需要它：前端 bug（元素不存在、null.addEventListener、boot 没执行）
接口级测试完全抓不到 —— 我们曾因此连续数轮"修了但没生效"：
script 里绑定了一个定义在 </script> 之后的按钮，抛 TypeError 导致
后面的 boot() 从未执行，整页数据加载不出来。

这个测试把页面里的 id 收集成一个桩 DOM，喂给 Node 执行脚本，然后断言：
启动流程真的跑到了（发起了 /api/health、/api/config 等请求）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import allure
import pytest

INDEX = Path(__file__).resolve().parents[2] / "src" / "tableseed" / "web" / "static" / "index.html"

_NODE_CANDIDATES = [
    shutil.which("node"),
    r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe",
    r"C:\Program Files\nodejs\node.exe",
]


def _node() -> str | None:
    for candidate in _NODE_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _extract(html: str) -> tuple[str, set[str], int]:
    """返回 (脚本, script 起始位置之前的全部 id, script 结束位置)。"""
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    return html[start:end], set(re.findall(r'id="([\w-]+)"', html[:start])), end


#: 运行时由 JS 动态插入的元素，不在静态 DOM 里
DYNAMIC_IDS = {
    "btn-sql-insert", "btn-ins-to-imp", "btn-ddl-to-imp", "btn-imp-gen",
    "btn-imp-plan", "btn-confirm-insert", "btn-gen-insert-sql",
    "insert-out", "insert-hint",
    "prog", "sql-prev", "sql-next", "ed-table", "ed-add-group",
    "err-bar", "log-dialog", "log-dialog-content", "saveas-dialog",
    "saveas-path", "saveas-error", "btn-saveas-confirm",
    "help-dialog", "help-dialog-content",
}


@allure.epic("tableseed")
@allure.feature("WebUI 前端")
@allure.story("script 里引用的静态元素必须定义在 script 之前")
def test_referenced_ids_exist_before_script():
    html = INDEX.read_text(encoding="utf-8")
    js, ids_before, _ = _extract(html)

    referenced = set(re.findall(r'\$\("([\w-]+)"\)', js)) | set(
        re.findall(r'\bon\("([\w-]+)"', js)
    )
    missing = sorted(referenced - ids_before - DYNAMIC_IDS)
    assert not missing, (
        "以下元素在 script 之前不存在，绑定时会抛错并中断启动：" + ", ".join(missing)
    )


@allure.story("顶层绑定必须走安全的 on()，不得裸 $().addEventListener")
def test_top_level_bindings_are_safe():
    html = INDEX.read_text(encoding="utf-8")
    js, _, _ = _extract(html)
    # 行首（顶层）的裸绑定一旦元素缺失就会中断整个脚本
    offenders = re.findall(r'^\$\("([\w-]+)"\)\.addEventListener', js, flags=re.M)
    assert not offenders, f"顶层裸绑定（应改用 on()）: {offenders}"


@allure.story("用桩 DOM 真跑脚本：启动流程必须执行到发请求")
def _run_harness(post_js: str = "") -> dict:
    """跑一次页面脚本（桩 DOM），返回 {ok, fetched, ...}。"""
    node = _node()
    if node is None:
        pytest.skip("未找到 node，跳过前端冒烟")

    html = INDEX.read_text(encoding="utf-8")
    js, ids, _ = _extract(html)
    ids |= DYNAMIC_IDS

    harness = """
// ---- 极简 DOM 桩：够跑通 init 流程 ----
const IDS = __IDS__;
const calls = [];
function makeEl(id) {
  const el = {
    id, value: "", textContent: "", innerHTML: "", className: "", hidden: false,
    style: {}, dataset: {}, tagName: "DIV",
    addEventListener(ev, fn) { (this._h ||= {})[ev] = fn; },
    removeEventListener() {},
    appendChild() {}, remove() {}, focus() {}, close() {},
    showModal() {}, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    dispatchEvent() {}, setAttribute() {}, getAttribute() { return null; },
    classList: { add() {}, remove() {}, contains() { return false; } },
  };
  return el;
}
const store = {};
global.document = {
  getElementById: (id) => (store[id] ||= makeEl(id)),
  createElement: (tag) => makeEl("_" + tag),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: makeEl("body"),
  head: makeEl("head"),
};
global.window = { addEventListener: () => {}, location: { href: "/" }, confirm: () => false };
global.confirm = () => false;
global.alert = () => {};
global.Event = class { constructor(t) { this.type = t; } };
global.TextDecoder = require("util").TextDecoder;
global.localStorage = { getItem: () => null, setItem: () => {} };

// ---- 记录网络调用（boot 一定会打 health / config）----
const fetched = [];
global.fetch = async (url, opts) => {
  fetched.push(String(url));
  return {
    ok: true, status: 200,
    headers: { get: () => null },
    text: async () => JSON.stringify({
      ok: true, text: "", path: "samples/txn.yaml", config_path: "samples/txn.yaml",
      started_at: "2026-09-13 00:00:00", active: null, connections: {},
      incomplete: [], active_missing: false, has_usable: false, files: [],
      columns: [], rows: [], row_count: 0, elapsed_ms: 0,
    }),
    json: async () => ({}),
    body: { getReader: () => ({ read: async () => ({ done: true }) }) },
  };
};

// ---- 执行页面脚本（捕获真实错误，别让它静默）----
try {
  __JS__
} catch (e) {
  console.log(JSON.stringify({
    ok: false, reason: "脚本抛出异常: " + e.message,
    stack: String(e.stack || "").split("\\n").slice(0, 5),
  }));
  process.exit(1);
}
process.on("uncaughtException", (e) => {
  console.log(JSON.stringify({
    ok: false, reason: "未捕获异常: " + e.message,
    stack: String(e.stack || "").split("\\n").slice(0, 5),
  }));
  process.exit(1);
});
process.on("unhandledRejection", (e) => {
  console.log(JSON.stringify({ ok: false, reason: "未处理的 Promise 拒绝: " + (e && e.message) }));
  process.exit(1);
});

// ---- 后置检查（可选：测试可传入一段代码在此执行）----
setTimeout(async () => {
  try {
    __POSTJS__
  } catch (e) {
    console.log(JSON.stringify({ ok: false, reason: "后置检查异常: " + e.message,
      stack: String(e.stack || "").split("\\n").slice(0, 4) }));
    process.exit(1);
  }
  const boot = fetched.filter((u) => u.includes("/api/") && !u.includes("_t="));
  if (fetched.length === 0) {
    console.log(JSON.stringify({ ok: false, reason: "脚本执行后没有任何请求 —— boot() 没有运行", fetched }));
    process.exit(1);
  }
  console.log(JSON.stringify({ ok: true, fetched: fetched.slice(0, 6), count: fetched.length }));
  process.exit(0);
}, 300);
"""

    harness = (
        harness.replace("__IDS__", json.dumps(sorted(ids)))
        .replace("__JS__", js)
        .replace("__POSTJS__", post_js)
    )

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "harness.js"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run(
            [node, str(script)], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, cwd=tmp,
        )

    out = (proc.stdout or "").strip().splitlines()
    payload = {}
    for line in reversed(out):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if payload.get("ok") is not True:
        payload["_raw_stdout"] = (proc.stdout or "")[-1500:]
        payload["_raw_stderr"] = (proc.stderr or "")[-1500:]
    return payload


@allure.story("用桩 DOM 真跑脚本：启动流程必须执行到发请求")
def test_script_boots_with_stub_dom():
    payload = _run_harness()
    assert payload.get("ok") is True, (
        "前端启动流程未跑通：\n"
        f"stdout={payload.get('_raw_stdout')}\nstderr={payload.get('_raw_stderr')}\n"
        f"reason={payload.get('reason')}"
    )


@allure.story("结果页：点击生成后出现「生成 INSERT 语句」按钮并可点击")
def test_insert_sql_button_rendered():
    """离线场景的入口必须在结果页真正渲染出来，且点击能触发渲染请求。

    桩 fetch 返回一份假结果 → 调 runGenerate() → 检查 tab-result 的 HTML
    里出现按钮，且按钮已绑定 click（桩 DOM 会记录 handler）。
    """
    post_js = """
    // 假结果：一张表两行
    global.fetch = async (url) => {
      fetched.push(String(url));
      const isGen = String(url).includes("/api/generate");
      return {
        ok: true, status: 200, headers: { get: () => null },
        text: async () => JSON.stringify(isGen ? {
          tables: { t_txn: { count: 2, columns: ["txn_no"], rows: [["T0001"], ["T0002"]],
                             truncated: false } },
          elapsed_ms: 3, seed: 1, sink: "内存模式", warnings: [], invariant_failures: [],
        } : { ok: true, text: "seed: 1\\ntables: []\\n", path: "x.yaml" }),
        json: async () => ({}),
        body: { getReader: () => ({ read: async () => ({ done: true }) }) },
      };
    };
    // 生成走的是 SSE 流，这里直接调渲染函数验证按钮落地
    renderResult({
      tables: { t_txn: { count: 2, columns: ["txn_no"], rows: [["T0001"], ["T0002"]],
                         truncated: false } },
      elapsed_ms: 3, seed: 1, sink: "内存模式", warnings: [], invariant_failures: [],
    });
    const html = store["tab-result"].innerHTML;
    if (!html.includes('id="btn-gen-insert-sql"')) {
      console.log(JSON.stringify({ ok: false, reason: "结果页没有渲染出「生成 INSERT 语句」按钮" }));
      process.exit(1);
    }
    const btn = store["btn-gen-insert-sql"];
    if (!btn || !btn._h || !btn._h.click) {
      console.log(JSON.stringify({ ok: false, reason: "「生成 INSERT 语句」按钮没有绑定 click" }));
      process.exit(1);
    }
    // 点击它应弹出 SQL 弹窗并发起渲染请求
    const before = fetched.length;
    btn._h.click();
    await new Promise((r) => setTimeout(r, 200));
    if (!store["sqlgen-dialog"] || store["sqlgen-dialog"].showModal === undefined) {
      console.log(JSON.stringify({ ok: false, reason: "SQL 弹窗不存在" }));
      process.exit(1);
    }
    if (fetched.length <= before) {
      console.log(JSON.stringify({ ok: false, reason: "点击按钮后没有发起 /api/insert/sql 请求" }));
      process.exit(1);
    }
    """
    payload = _run_harness(post_js)
    assert payload.get("ok") is True, (
        "结果页的「生成 INSERT 语句」按钮验证失败：\n"
        f"reason={payload.get('reason')}\nstdout={payload.get('_raw_stdout')}"
    )
