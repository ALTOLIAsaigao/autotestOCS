#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
在 OCS 拉起的那个浏览器里，对目标页面的**每一个 frame** 跑同一段 JS。

超星把课程列表塞在 #frame_content 这个 iframe 里，只探主 frame 会什么都看不到，
所以这个工具是遍历 frame 的。

    python ocs_probe.py "document.title"
    python ocs_probe.py --match chaoxing "location.href"
    echo "<js>" | python ocs_probe.py

JS 请写成表达式或 IIFE（显式 return），返回非 JSON 可序列化值会被转成字符串。
"""
import argparse
import json
import sys

from playwright.sync_api import sync_playwright

DEFAULT_BROWSER_PORT = 9223


def main() -> int:
    ap = argparse.ArgumentParser(description="对目标页所有 frame 跑 JS")
    ap.add_argument("js", nargs="?", help="JS 表达式；不填从 stdin 读")
    ap.add_argument("--port", type=int, default=DEFAULT_BROWSER_PORT, help="浏览器调试端口")
    ap.add_argument("--match", default="chaoxing", help="按 URL 子串挑页面")
    args = ap.parse_args()

    src = args.js or sys.stdin.read()
    if not src.strip():
        print(__doc__)
        return 2

    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        pages = [pg for ctx in br.contexts for pg in ctx.pages
                 if args.match in (pg.url or "")]
        if not pages:
            print(f"[x] 没有 URL 含 {args.match!r} 的页面。现有的：")
            for ctx in br.contexts:
                for pg in ctx.pages:
                    print(f"    {pg.url}")
            return 1
        page = pages[0]
        print(f"[page] {page.title()!r}  {page.url}\n")

        for i, fr in enumerate(page.frames):
            try:
                val = fr.evaluate(src)
            except Exception as e:
                val = f"<frame error: {str(e)[:120]}>"
            tag = "主frame" if fr == page.main_frame else f"frame#{i}"
            print(f"--- {tag}  {fr.url[:100]}")
            print(json.dumps(val, ensure_ascii=False, indent=1) if not isinstance(val, str) else val)
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
