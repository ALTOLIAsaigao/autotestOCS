"""把 OCS 那只浏览器里超星的 cookie 清掉，让它重新登录一次。

什么时候用
--------------------------------------------------------------------------
超星的登录态出现"半失效"时，`i.chaoxing.com` 和 `passport2.chaoxing.com` 会互相
踢皮球，一直重定向到 Chrome 自己的上限（20 跳），报：

    page.goto: net::ERR_TOO_MANY_REDIRECTS at http://i.chaoxing.com/

原因一般是 passport2 那边还留着"看起来已登录"的 cookie（`uf` / `_uid` 一类），而
i.chaoxing.com 那边的会话已经失效 —— 两边判断不一致就来回跳。清掉 cookie、
让自动登录脚本从零登一次，是最直接的解法。

用法
--------------------------------------------------------------------------
    python ocs_clear_cookies.py              # 清超星相关的 cookie
    python ocs_clear_cookies.py --dry-run    # 只列出来，不动
    python ocs_clear_cookies.py --all        # 清所有 cookie（含超星之外的）
    python ocs_clear_cookies.py --port 9223

清完把浏览器关掉，再让 OCS 重新点 ▶（自动登录脚本会拿存的账号密码重新登）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocs_click_play import WebSocket, http_json  # noqa: E402

# 超星那一串域名（含课程站、mooc、云盘 CDN 的登录相关 cookie）
MATCH = ("chaoxing.com", "chaoxing.cn", "cldisk.com")


def pick_page_ws(port: int) -> str:
    """Network 域要挂在 page target 上，拿一个页面的 ws。"""
    for t in http_json(f"http://127.0.0.1:{port}/json/list"):
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return t["webSocketDebuggerUrl"]
    raise SystemExit(f"[x] {port} 上一个 page target 都没有")


def main() -> int:
    ap = argparse.ArgumentParser(description="清掉 OCS 浏览器里超星的 cookie")
    ap.add_argument("--port", type=int, default=9223, help="OCS 浏览器的调试端口")
    ap.add_argument("--dry-run", action="store_true", help="只列出来，不删")
    ap.add_argument("--all", action="store_true", help="清所有 cookie，不只超星的")
    args = ap.parse_args()

    try:
        v = http_json(f"http://127.0.0.1:{args.port}/json/version")
    except Exception as e:
        raise SystemExit(f"[x] 连不上 {args.port}：{e}\n"
                         f"    浏览器没起？先在 OCS 里点实例上的 ▶。")
    print(f"[*] 连上 {v.get('Browser')}")

    ws = WebSocket(pick_page_ws(args.port))
    ws.connect()
    try:
        # Storage.getCookies 是 browser 级的，挂哪个 target 都能问全量
        cookies = (ws.call("Storage.getCookies", timeout=15) or {}).get("cookies", [])
        targets = [c for c in cookies
                   if args.all or any(m in (c.get("domain") or "") for m in MATCH)]
        print(f"    共 {len(cookies)} 条 cookie，要清 {len(targets)} 条")
        for c in targets:
            print(f"      {c.get('domain'):<28} {c.get('name')}")

        if args.dry_run:
            print("(--dry-run，没动)")
            return 0
        if not targets:
            print("[=] 没有要清的，跳过")
            return 0

        n = 0
        for c in targets:
            try:
                ws.call("Network.deleteCookies", {
                    "name": c["name"],
                    "domain": c.get("domain"),
                    "path": c.get("path", "/"),
                }, timeout=10)
                n += 1
            except Exception as e:
                print(f"      [x] {c.get('name')} 删不掉：{str(e)[:60]}")

        left = (ws.call("Storage.getCookies", timeout=15) or {}).get("cookies", [])
        left = [c for c in left if args.all or any(m in (c.get("domain") or "") for m in MATCH)]
        print(f"[+] 删了 {n} 条，还剩 {len(left)} 条超星 cookie")
        if left:
            for c in left:
                print(f"      残留 {c.get('domain')} {c.get('name')}")
    finally:
        try:
            ws.close()
        except Exception:
            pass

    print("\n下一步：关掉浏览器 → 回 OCS 点实例上的 ▶，让它重新登录一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
