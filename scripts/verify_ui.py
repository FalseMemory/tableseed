"""浏览器级 UI 验证：用系统 Chrome/Edge 无头模式真跑一遍页面 JS，检查关键元素。

为什么需要：接口级测试和语法检查**抓不到前端启动故障**（例如顶层绑定
抛错导致 boot() 从不执行、注释里出现脚本结束标签导致脚本被截断）。
本项目曾因此连续数轮"改了但页面没变化"。

用法：
    python scripts/verify_ui.py                     # 验证本机 8643
    python scripts/verify_ui.py --port 9000         # 指定端口
    python scripts/verify_ui.py --screenshot ui.png # 顺带截图
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str | None:
    for path in BROWSERS:
        if pathlib.Path(path).exists():
            return path
    return None


def dump_dom(browser: str, url: str, budget_ms: int = 6000) -> str:
    # 用固定文件而非 TemporaryDirectory：Chrome 子进程退出有延迟，
    # Windows 会锁住刚写完的文件，临时目录清理会抛 PermissionError
    out = pathlib.Path(".tmp") / "verify-dom.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-proxy-server",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=120,
        )
    time.sleep(0.3)  # 等 Chrome 完全释放文件句柄
    return out.read_text(encoding="utf-8", errors="replace")


def screenshot(browser: str, url: str, dest: str) -> None:
    pathlib.Path(dest).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-proxy-server",
         "--window-size=1440,900", "--virtual-time-budget=6000",
         f"--screenshot={dest}", url],
        capture_output=True, timeout=120,
    )


def text_of(dom: str, element_id: str) -> str | None:
    m = re.search(rf'id="{element_id}"[^>]*>([^<]*)<', dom)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8643)
    ap.add_argument("--screenshot", default=None)
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("找不到 Chrome / Edge，无法做浏览器级验证")
        return 2

    base = f"http://127.0.0.1:{args.port}"
    try:
        health = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        ).open(base + "/api/health", timeout=10).read().decode()
    except Exception as exc:  # noqa: BLE001
        print(f"服务不可达（{base}）：{exc}")
        return 2
    print("服务:", health)

    dom = dump_dom(browser, base + "/")
    print(f"浏览器: {pathlib.Path(browser).name} · DOM {len(dom)} 字符")

    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 前端脚本是否真的跑起来了
    add("无前端脚本错误横幅", '<div id="err-bar"' not in dom)
    add("页面已启动（版本行已填充）", bool(text_of(dom, "app-version")),
        text_of(dom, "app-version") or "")
    add("配置路径已回填", (text_of(dom, "cfg-path") or "未绑定") != "未绑定文件（仅内存）",
        text_of(dom, "cfg-path") or "")
    add("连接状态已加载", "当前：" in (text_of(dom, "db-state") or ""),
        text_of(dom, "db-state") or "")
    add("连接下拉含服务端连接",
        bool(re.search(r'id="db-conn-select".*?</select>', dom, re.S)
             and 'value=""' in re.search(r'id="db-conn-select".*?</select>', dom, re.S).group(0)
             and len(re.findall(r"<option", re.search(r'id="db-conn-select".*?</select>', dom, re.S).group(0))) > 1),
        "")

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {name.ljust(width)}  {detail[:70]}")
        failed += 0 if ok else 1

    if args.screenshot:
        screenshot(browser, base + "/", args.screenshot)
        print(f"截图已保存: {args.screenshot}")

    print(f"\n{'全部通过' if not failed else str(failed) + ' 项未通过'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
