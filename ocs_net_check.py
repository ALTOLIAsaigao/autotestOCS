"""在跑 OCS 的那台机器上查一遍"到超星这条网络路"通不通。

症状对应的用法
--------------------------------------------------------------------------
OCS 的自动登录脚本报：

    page.goto: net::ERR_TOO_MANY_REDIRECTS at http://i.chaoxing.com/

或者浏览器里连登录页都进不去、没有输账号密码的机会。这时候要分清是**哪一类**问题：

    A. 网络到不了超星 / 被中间人劫持  —— 本脚本的 URL 链路会露馅（落到别的域名、
       一直 302 到认证页、DNS 解析不出来）
    B. 网络是通的，但浏览器本地 cookie 半失效 —— 链路干净三跳结束，
       那就去清 cookie（见 ocs_clear_cookies.py）

只用到标准库，不需要管理员，不改任何东西，纯只读。

用法
--------------------------------------------------------------------------
    python ocs_net_check.py
    python ocs_net_check.py --json      # 输出机器可读的结果
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urljoin

HOSTS = ["i.chaoxing.com", "passport2.chaoxing.com", "tsjy.chaoxing.com", "mooc1-1.chaoxing.com"]
TRACE_START = "http://i.chaoxing.com/"
PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


# ---------------------------------------------------------------------------
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: A002
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def resolve(host: str, port: int = 443):
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        return {"ok": True, "ips": ips, "ms": round((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}", "ms": round((time.time() - t0) * 1000)}


def tcp(host: str, port: int, timeout: float = 6.0):
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "ms": round((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}", "ms": round((time.time() - t0) * 1000)}


def trace(url: str, max_hops: int = 20, timeout: float = 10.0):
    """手动跟重定向，每一跳都记下来 —— 死循环就是在这里露出来的。"""
    hops = []
    cur = url
    server_date = None
    for _ in range(max_hops):
        req = urllib.request.Request(cur, headers={"User-Agent": "Mozilla/5.0 (ocs-net-check)"})
        try:
            with OPENER.open(req, timeout=timeout) as r:
                code, headers = r.status, dict(r.headers)
        except urllib.error.HTTPError as e:           # 3xx/4xx/5xx 都从这儿出
            code, headers = e.code, dict(e.headers)
        except Exception as e:
            hops.append({"url": cur, "error": f"{type(e).__name__}: {e}"})
            break

        loc = headers.get("Location") or headers.get("location")
        hops.append({"url": cur, "status": code, "location": loc})
        if server_date is None:
            server_date = headers.get("Date")
        if code not in (301, 302, 303, 307, 308) or not loc:
            break
        cur = urljoin(cur, loc)
    return {"hops": hops, "server_date": server_date}


def check_proxy():
    env = {k: os.environ[k] for k in PROXY_KEYS if os.environ.get(k)}
    reg = {}
    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL"):
                try:
                    reg[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
        except Exception as e:
            reg["_err"] = str(e)
    return {"env": env, "registry": reg}


def main() -> int:
    ap = argparse.ArgumentParser(description="查这台机器到超星的网络链路")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    out: dict = {"when": datetime.now().isoformat(timespec="seconds"), "dns": {}, "tcp": {},
                 "trace": None, "proxy": check_proxy(), "clock": None, "verdict": []}

    for h in HOSTS:
        out["dns"][h] = resolve(h)
    for h in ("i.chaoxing.com", "passport2.chaoxing.com"):
        out["tcp"][h + ":443"] = tcp(h, 443)

    try:
        out["trace"] = trace(TRACE_START)
    except Exception as e:
        out["trace"] = {"hops": [{"url": TRACE_START, "error": f"{type(e).__name__}: {e}"}]}

    # 用响应头里的 Date 反推本机时钟偏了多少 —— 不用装 NTP 工具
    sd = (out["trace"] or {}).get("server_date")
    if sd:
        try:
            remote = parsedate_to_datetime(sd)
            if remote.tzinfo is None:
                remote = remote.replace(tzinfo=timezone.utc)
            skew = (datetime.now(timezone.utc) - remote).total_seconds()
            out["clock"] = {"server_date": sd, "skew_seconds": round(skew, 1)}
        except Exception:
            pass

    # ---- 判个话 ----
    v = out["verdict"]
    hops = (out["trace"] or {}).get("hops", [])
    hosts_landed = set()
    for h in hops:
        if h.get("url"):
            hosts_landed.add(h["url"].split("/")[2] if "//" in h["url"] else h["url"])
    bad_host = [x for x in hosts_landed if not any(x.endswith(d) for d in
                                                   ("chaoxing.com", "chaoxing.cn", "cldisk.com"))]

    if any(not d["ok"] for d in out["dns"].values()):
        v.append("DNS 解析失败：这台机器解析不出部分超星域名 —— 大概率是本机 DNS / 内网 DNS 的问题，换个 DNS（223.5.5.5 / 119.29.29.29）再试。")
    if any(not d["ok"] for d in out["tcp"].values()):
        v.append("TCP 443 连不上超星 —— 防火墙 / 出网限制，这台机器根本出不去。")
    if bad_host:
        v.append(f"链路里落到了**非超星域名** {bad_host} —— 中间有人插了一手（认证页 / 劫持 / 代理），这就是重定向死循环的源头。")
    if len(hops) >= 20:
        v.append("重定向撞到 20 跳上限还没落地 —— 和你看到的 ERR_TOO_MANY_REDIRECTS 一致。")
    if out["proxy"]["env"] or out["proxy"]["registry"].get("ProxyEnable"):
        v.append("这台机器配了代理（见下面 proxy）—— OCS 的浏览器会跟着走，代理不通或代理自己插重定向都会出这个错。")
    if out["clock"] and abs(out["clock"]["skew_seconds"]) > 120:
        v.append(f"本机时钟跟服务器差了 {out['clock']['skew_seconds'] / 60:.1f} 分钟 —— cookie 过期判定会飘，先把时间对一下。")
    if not v:
        last = hops[-1] if hops else {}
        if last.get("status") == 200 and len(hops) <= 4:
            v.append("链路干净（正常几跳就停在超星的登录页 200）—— 问题在浏览器本地的 cookie 状态，去清 cookie 重登（ocs_clear_cookies.py）。")

    out["verdict"] = v

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print("=" * 70)
    print(f" 超星链路体检   {out['when']}")
    print("=" * 70)
    for h, d in out["dns"].items():
        print(f"  DNS  {h:<26} " + (f"→ {', '.join(d['ips'])}  ({d['ms']}ms)" if d["ok"] else f"✗ {d['err']}"))
    for h, d in out["tcp"].items():
        print(f"  TCP  {h:<26} " + (f"通 ({d['ms']}ms)" if d["ok"] else f"✗ {d['err']}"))
    print("\n  重定向链路：")
    for i, h in enumerate(hops, 1):
        if "error" in h:
            print(f"    {i:>2}. [连不上] {h['url']}\n         {h['error']}")
        else:
            print(f"    {i:>2}. {h['status']}  {h['url'][:88]}")
            if h.get("location"):
                print(f"         → {h['location'][:88]}")
    p = out["proxy"]
    print(f"\n  代理：env={p['env'] or '无'}  注册表={p['registry'] or '无'}")
    if out["clock"]:
        print(f"  时钟：本机比服务器{'快' if out['clock']['skew_seconds'] > 0 else '慢'} "
              f"{abs(out['clock']['skew_seconds']):.0f} 秒")
    print("\n  结论：")
    for line in v:
        print(f"    · {line}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
