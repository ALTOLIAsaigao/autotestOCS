#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
在任意一个 CDP 页面上跑 JS —— 调试用的万能口。

    # 默认打到 OCS 自己的渲染进程（9222）
    python ocs_eval.py "document.title"

    # 打到 OCS 拉起的那个浏览器（9223）里 URL 含 chaoxing 的页面
    python ocs_eval.py --port 9223 --match chaoxing "location.href"

    # 看 9223 上都有什么页面
    python ocs_eval.py --port 9223 --list

表达式里的最后一个值就是返回值（returnByValue），所以要么写成表达式，
要么用 IIFE 显式 return —— 顶层 return 在 evaluate 里是非法的。
"""
import argparse
import json
import sys

from ocs_click_play import WebSocket, WSError, find_ocs_page, http_json


def pick_target(port: int, match: str | None) -> dict:
    if not match:
        return find_ocs_page(port)

    targets = http_json(f"http://127.0.0.1:{port}/json")
    pages = [t for t in targets
             if t.get("type") == "page" and match in (t.get("url") or "")]
    if not pages:
        print(f"[x] {port} 上没有 URL 含 {match!r} 的页面。现有的：")
        for t in targets:
            if t.get("type") == "page":
                print(f"    {t.get('url')}")
        raise SystemExit(1)
    return pages[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="在任意 CDP 页面上跑 JS")
    ap.add_argument("js", nargs="?", help="要执行的表达式；不填就从 stdin 读")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--match", help="按 URL 子串挑页面（在浏览器端口上用时必需）")
    ap.add_argument("--list", action="store_true", help="只列出这个端口上的页面")
    args = ap.parse_args()

    if args.list:
        for t in http_json(f"http://127.0.0.1:{args.port}/json"):
            if t.get("type") == "page":
                print(f"[{t.get('type')}] {t.get('title')!r}\n      {t.get('url')}")
        return 0

    src = args.js or sys.stdin.read()
    if not src.strip():
        print(__doc__)
        return 2

    target = pick_target(args.port, args.match)
    ws = WebSocket(target["webSocketDebuggerUrl"])
    ws.connect()
    try:
        value = ws.eval_js(src)
    except WSError as e:
        print(f"[x] {e}")
        return 1
    finally:
        ws.close()

    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
