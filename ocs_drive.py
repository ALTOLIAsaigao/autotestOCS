#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外部驱动 OCS，完整跑一次视频播放稳定性测试。

和 ocs_video_test.py 的分工区别
--------------------------------------------------------------------------
ocs_video_test.py   自己起一个 Playwright、直接借 OCS 的 profile 目录。
                    跑之前必须先关掉 OCS（profile 被占），而且它自己重来一遍加载扩展、
                    登录的事。OCS 在整条链路里只是个"profile 提供方"。

ocs_drive.py        OCS 自己启动它配置好的那个浏览器实例 —— 脚本猫、自动登录脚本、
                    profile、书签页，全都由 OCS 按它自己的逻辑装好。我们不碰浏览器的
                    启动过程，只在浏览器起来之后，从外部用 CDP 接管那只页面。
                    OCS 负责"把带登录态的浏览器拉起来"，点击/找视频/观测全在我们这边。

这样 OCS 的浏览器监控（右下角那个）完全用不上，服务器内存压力等同于开一个普通 Chrome。

链路
--------------------------------------------------------------------------
    [1] CDP 到 OCS 渲染进程(9222) -> 点实例行上的 play_circle
    [2] OCS fork script.js -> Playwright launchPersistentContext -> Chrome
        （worker 补丁会让这只 Chrome 额外带 --remote-debugging-port=9223）
    [3] 等超星自动登录脚本把页面带到 i.chaoxing.com
    [4] Playwright connect_over_cdp(9223) 接管那个 context
    [5] 按 build_steps 点进视频页 -> 确认在播 -> 之后**不碰这个页面**
    [6] 放够 play_minutes 分钟 -> 关掉视频页 -> 回课程首页刷新
    [7] 读 p#dayScore：到 32 就算通过，没到推 Bark 让人手动处理 -> 关浏览器

为什么不再监视播放过程
--------------------------------------------------------------------------
视频页只要被外部反复读（采样、evaluate、截图），超星那边会把登录页拉起来，
播放就断了。所以判定标准改成看结果：今日积分 p#dayScore 有没有到 32
（每天归零，看视频就是为了把它刷满）。播放期间脚本一句 JS 都不发给那个页面。

起点页为什么要校正
--------------------------------------------------------------------------
OCS 在当天没刷满积分时会自己把浏览器拉起来放视频，接管时经常已经开着上一轮的
tsjy/studentstudy 页签，只按 wait_login_page 交出来的那一页往下点必然错位，
所以接管后先认 i.chaoxing.com 的页签、顺手把残留页签收掉。

用法
--------------------------------------------------------------------------
    python ocs_drive.py                    # 单个实例跑一轮（默认 34 分钟）
    python ocs_drive.py --dry-run          # 只走到点击流程结束，不看积分
    python ocs_drive.py --skip-launch      # 浏览器已经在跑，直接接管
    python ocs_drive.py --play-minutes 1   # 短测
    python ocs_drive.py --browser 33       # 换一个 OCS 实例跑
    python ocs_drive.py --list             # 看 OCS 里实例的当前状态

多实例、按时刻轮流开的话用 ocs_schedule.py（4G 机器同时只能跑一只浏览器，
所以那边是串行的）：到点拉起一个 → 跑完关干净 → 再等下一个的点。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "ocs_click_play.py"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("=" * 66)
    print("缺 playwright。装一下：")
    print(f'   "{sys.executable}" -m pip install playwright')
    print("=" * 66)
    sys.exit(3)

# 点击路径 / 找视频 / 观测 / 日志 / 推送 —— 全部复用已经写好的那套
from ocs_video_test import (  # noqa: E402
    COURSE_NAME,
    VIDEO_NAME,
    Log,
    build_steps,
    click_step,
    first_visible,
    load_json_config,
    locate_video,
    notify,
    safe_shot,
    settle_landing,
)
from ocs_click_play import urlopen_local  # noqa: E402  # 打本机的请求直连，绕开系统代理

# 这些页面不是我们要操作的目标页
SKIP_URL_PREFIX = ("chrome-extension://", "about:blank", "devtools://", "chrome://")


# ---------------------------------------------------------------------------
# 调试端口
# ---------------------------------------------------------------------------
def port_version(port: int, timeout: float = 2.0):
    try:
        with urlopen_local(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def close_browser(browser, port: int, log: Log) -> bool:
    """真的把浏览器关掉。

    connect_over_cdp 拿到的 browser 调 close() 只是断了连接 —— Chrome 进程还在，
    下一轮接管时它还在那儿开着上一轮的页签（步骤 3 就栽在这上面）。所以补一刀：
    发 CDP Browser.close，然后盯着端口直到它不再响应。
    """
    try:
        browser.close()
    except Exception as e:
        log(f"    （browser.close() 报错：{str(e).splitlines()[0][:60]}）")
    if port_version(port, timeout=3.0) is None:
        log("[+] 浏览器已关闭")
        return True

    log("[*] Playwright 的 close() 没把进程关掉，改发 CDP Browser.close")
    try:
        from ocs_click_play import WebSocket  # 纯标准库的 CDP 客户端，用到才导
        v = port_version(port, timeout=5.0)
        ws = WebSocket(v["webSocketDebuggerUrl"])
        ws.connect()
        try:
            ws.call("Browser.close", timeout=8)
        except Exception:
            pass  # 浏览器关掉那一刻连接会断，正常
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception as e:
        log(f"    （CDP Browser.close 失败：{str(e).splitlines()[0][:70]}）")

    for _ in range(10):
        if port_version(port, timeout=2.0) is None:
            log("[+] 浏览器已关闭（走的是 CDP Browser.close）")
            return True
        time.sleep(1)
    log(f"[!] 浏览器还活着，{port} 端口仍在响应 —— 得手动收")
    return False


def wait_port(port: int, seconds: float):
    t0 = time.time()
    while time.time() - t0 < seconds:
        v = port_version(port)
        if v:
            return v
        time.sleep(2)
    return None


def http_targets(port: int) -> list[dict]:
    """列目标列表。注意这是纯 HTTP，不会附着到任何页面上。"""
    try:
        with urlopen_local(f"http://127.0.0.1:{port}/json", timeout=4) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return []


def wait_login_page(port: int, log: Log, seconds: float = 180):
    """等 OCS 内嵌的自动登录脚本把页面跳到位 —— 全程只用 /json，不 connect_over_cdp。

    为什么必须先等、不能一上来就接管：
    登录脚本是 page.goto('http://i.chaoxing.com/') 之后再填表登录的。如果正赶上它跳转
    到一半我们就把 Playwright 附加上去，那个页面的导航会**卡死** —— 页面停在
    about:blank，title 也是 about:blank，之后连 CDP 的 Page.navigate 都不响应，
    整个渲染进程处于僵死状态，只能关掉重来。
    /json 是普通 HTTP 接口，照着看不影响页面，拿它当门铃最合适。
    """
    log(f"[*] 等超星页面自己跳到位（只读 /json，不接管；最多 {seconds:.0f}s）...")
    t0 = time.time()
    last = ""
    while time.time() - t0 < seconds:
        for t in http_targets(port):
            if t.get("type") != "page":
                continue
            u = t.get("url") or ""
            title = (t.get("title") or "").strip()
            if any(u.startswith(p) for p in SKIP_URL_PREFIX) or "localhost:" in u:
                continue
            if "chaoxing.com" in u and "passport2" not in u and title and title != "about:blank":
                log(f"[+] 页面已到位：{title!r}")
                log(f"    {u}")
                return u
            if u and u != last:
                last = u
                log(f"    ...还在跳：title={title!r} url={u[:90]}")
        time.sleep(3)
    log("[x] 等登录超时 —— 登录脚本可能报错了")
    for t in http_targets(port):
        if t.get("type") == "page":
            log(f"    {t.get('title')!r}  {t.get('url')}")
    return None


# ---------------------------------------------------------------------------
# 让 OCS 把浏览器拉起来
# ---------------------------------------------------------------------------
def ensure_ocs_browser(log: Log, ocs_port: int, browser_port: int,
                       name: str | None, skip_launch: bool) -> bool:
    if port_version(browser_port):
        log(f"[=] 浏览器已经在跑（{browser_port} 上 CDP 可连），跳过启动这一步")
        return True

    if skip_launch:
        log(f"[x] --skip-launch 指定不启动，但 {browser_port} 连不上")
        return False

    log(f"[*] 让 OCS 启动实例 {name or '(默认)'!r} —— 点它那行上的播放键")
    cmd = [sys.executable, str(LAUNCHER), "--port", str(ocs_port), "--no-wait"]
    if name:
        cmd += ["--name", name]
    # 它的中文按 UTF-8 编出来 —— 中文 Windows 上子进程默认拿 cp936，跟父进程这边
    # （ocs_video_test 里把 stdout 设成 UTF-8 了）对不上，日志里就是"目标"变"Ŀ��"。
    rc = subprocess.run(cmd, cwd=str(HERE), env=dict(os.environ, PYTHONIOENCODING="utf-8")).returncode
    if rc != 0:
        log(f"[x] 点启动失败（exit={rc}）。OCS 是不是没带 --remote-debugging-port 启动？")
        return False

    log(f"[*] 等这只浏览器在 {browser_port} 上开 CDP（最多 180s）...")
    v = wait_port(browser_port, 180)
    if not v:
        log(f"[x] {browser_port} 一直不开。三件事按顺序查：")
        log(f"    1) 补丁在不在：resources/app/lib/src/worker/index.js 里搜 OCS_BROWSER_DEBUG_PORT")
        log(f"    2) 端口文件在不在：%APPDATA%\\OCS Desktop\\browser-debug-port.txt")
        log(f"    3) 是不是从别的 OCS（没打补丁的那个）启动的")
        return False
    log(f"[+] 浏览器 CDP 就绪：{v.get('Browser')}")
    return True


# ---------------------------------------------------------------------------
# 找目标页面
# ---------------------------------------------------------------------------
def pick_target_page(ctx, log: Log, expect_url: str, seconds: float = 30):
    """接管之后，在 context 里把刚才那个页面找出来。"""
    base = (expect_url or "").split("?")[0]
    t0 = time.time()
    while time.time() - t0 < seconds:
        for pg in ctx.pages:
            if base and (pg.url == expect_url or (pg.url or "").split("?")[0] == base):
                log(f"[+] 目标页面: {pg.url}")
                return pg
        time.sleep(1)

    for pg in ctx.pages:  # 退路：不是 OCS 自己的页面就行
        u = pg.url or ""
        if not any(u.startswith(p) for p in SKIP_URL_PREFIX) and "localhost:" not in u:
            log(f"[!] 没精确匹配上，退而取: {u}")
            return pg
    log("[x] 接管后找不到目标页面。当前页面：")
    for pg in ctx.pages:
        log(f"    {pg.url}")
    return None


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------
PLAY_JS = """() => {
  const vs = [...document.querySelectorAll('video')].sort((a,b) => (b.duration||0) - (a.duration||0));
  const v = vs[0];
  if (!v) return { ok: false, why: 'no-video' };
  v.muted = false;
  if (!v.volume) v.volume = 1;
  const p = v.play();
  if (p && p.catch) p.catch(() => {});
  return { ok: true, paused: v.paused, muted: v.muted, duration: v.duration, src: (v.currentSrc||v.src||'').slice(0,120) };
}"""

STATE_JS = """() => {
  const v = [...document.querySelectorAll('video')].sort((a,b) => (b.duration||0) - (a.duration||0))[0];
  if (!v) return null;
  return { currentTime: v.currentTime, duration: v.duration, paused: v.paused, muted: v.muted,
           readyState: v.readyState, playbackRate: v.playbackRate,
           errCode: v.error ? v.error.code : null };
}"""


LANDING_URL = "https://i.chaoxing.com/base"


def pick_landing(ctx, log: Log, page):
    """把页面认到个人空间首页 —— 步骤 3 的起点。

    不能只信 wait_login_page 挑出来的那一页：OCS 自己会在没刷满积分时把浏览器
    拉起来放视频，接管时往往已经开着上一轮的 tsjy/studentstudy 页签，挑错了后面
    每一步都对不上。
    """
    for pg in ctx.pages:
        u = pg.url or ""
        if "i.chaoxing.com" in u and "passport2" not in u:
            page = pg
            break
    else:
        log(f"[*] 没有 i.chaoxing.com 的页签，把当前页导航过去：{LANDING_URL}")
        try:
            page.goto(LANDING_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
        except Exception as e:
            log(f"    （导航失败：{str(e).splitlines()[0][:60]}）")

    if "passport2" in (page.url or ""):
        log("[x] 落在登录页上了 —— 登录态可能已经失效")
        return None
    log(f"[+] 起点页：{page.url[:100]}")
    try:
        page.bring_to_front()
    except Exception:
        pass
    return page


def close_stale_tabs(ctx, keep, log: Log):
    """收掉上一轮留下的超星页签，只留这一轮要用的那一页。"""
    for pg in list(ctx.pages):
        u = pg.url or ""
        if pg is keep or "chaoxing.com" not in u:
            continue
        try:
            pg.close()
            log(f"    （收掉上一轮留下的页签：{u[:70]}）")
        except Exception:
            pass


def read_day_score(page, log: Log | None = None, wait_seconds: float = 0.0):
    """读课程首页的今日积分：<p class="txt-p1" id="dayScore">22</p>。

    刚刷新出来时这块面板是三个 0 的占位（数据还在往回拉），读到 0 不算结果 ——
    wait_seconds > 0 就一直等到读到非 0 的数，实在等不到才把最后读到的值交出去。
    """
    t0, last = time.time(), None
    while True:
        v = None
        try:
            v = page.evaluate("""() => {
              const el = document.querySelector('#dayScore');
              return el ? el.textContent.trim() : null;
            }""")
        except Exception as e:
            if log:
                log(f"    （读 #dayScore 失败：{str(e).splitlines()[0][:60]}）")
        m = re.search(r"\d+", v) if v else None
        if m:
            last = int(m.group())
            if last > 0 or wait_seconds <= 0:
                return last
        if time.time() - t0 >= wait_seconds:
            if log:
                log(f"    （等了 {wait_seconds:.0f}s，积分还是 {last}，面板可能没加载完）")
            return last
        time.sleep(2)


def wait_for_video(ctx, log: Log, seconds: float = 300.0, every: float = 3.0):
    """点进视频页之后等播放器把 <video> 建出来，返回 (frame, info)。

    播放器是异步建的，而且不一定是"秒到"：知识点页打开后它还要自己去拉播放信息，
    实测有等到几分钟的情况。只 find_video 一次就判"没有视频"会误杀。
    """
    log(f"[*] 等播放器把 <video> 建出来（最多 {seconds:.0f}s）...")
    t0 = time.time()
    n = 0
    while time.time() - t0 < seconds:
        hit = locate_video(ctx)
        if hit:
            fr, pg, info = hit
            log(f"[+] 播放器就绪（等了 {time.time() - t0:.0f}s）："
                f"duration={info['duration']:.0f}s paused={info['paused']} "
                f"readyState={info['readyState']}")
            log(f"    在 {pg.url}")
            return fr, info
        n += 1
        if n % 10 == 0:
            log(f"    ...还没有 <video>（已等 {time.time() - t0:.0f}s）")
        time.sleep(every)
    log(f"[x] {seconds:.0f}s 内没等到 <video>")
    return None, None


def ensure_playing(page, frame, log: Log):
    """把视频真的放起来。autoplay 被拦时依次降级重试。

    起得来就返回那一刻的播放器状态（顺手当报告里的证据），起不来返回 None。
    """
    steps = [
        ("直接 play()", None),
        ("静音后再 play()", "() => { const v=document.querySelector('video'); if(v){ v.muted=true; v.play().catch(()=>{}); } }"),
    ]
    for label, pre in steps:
        if pre:
            log(f"    重试：{label}")
            try:
                frame.evaluate(pre)
            except Exception as e:
                log(f"      {e}")
        else:
            log(f"    尝试：{label}")
            try:
                r = frame.evaluate(PLAY_JS)
                log(f"      -> {json.dumps(r, ensure_ascii=False)}")
            except Exception as e:
                log(f"      {e}")
        time.sleep(4)
        try:
            st = frame.evaluate(STATE_JS)
        except Exception:
            st = None
        if st and not st["paused"]:
            log(f"[+] 已经在播：currentTime={st['currentTime']:.1f}s / duration={st['duration']:.0f}s "
                f"muted={st['muted']} readyState={st['readyState']}")
            return st
        log(f"    还是 paused：{json.dumps(st, ensure_ascii=False) if st else '视频没了'}")

    # 最后一招：点一下 video 本体，拿用户手势
    log("    重试：点击 video 本体（补一个用户手势）")
    try:
        page.mouse.click(400, 400)
        time.sleep(2)
        frame.evaluate(PLAY_JS)
        time.sleep(4)
        st = frame.evaluate(STATE_JS)
        if st and not st["paused"]:
            log(f"[+] 已经在播：currentTime={st['currentTime']:.1f}s")
            return st
    except Exception as e:
        log(f"      {e}")

    log("[x] 视频起不来。看截图确认页面是不是停在某个弹窗/确认框上。")
    return None


# ---------------------------------------------------------------------------
def main() -> int:
    cfg = load_json_config()

    ap = argparse.ArgumentParser(description="外部驱动 OCS 跑视频稳定性测试")
    ap.add_argument("--browser", default=cfg.get("browser_name"), help="OCS 里的浏览器实例名")
    ap.add_argument("--ocs-port", type=int, default=9222, help="OCS 自身的调试端口")
    ap.add_argument("--browser-port", type=int, default=9223, help="OCS 拉起的浏览器的调试端口")
    ap.add_argument("--play-minutes", type=float, default=cfg.get("play_minutes", 34),
                    help="确认视频在播之后，放着不管多少分钟（从确认那一刻起算）")
    ap.add_argument("--course", default=None, help="覆盖 config 里的课程名（调度器给多实例用）")
    ap.add_argument("--video", default=None, help="覆盖 config 里的视频名（调度器给多实例用）")
    ap.add_argument("--skip-launch", action="store_true", help="浏览器已经在跑，直接接管")
    ap.add_argument("--page-wait", type=float, default=max(cfg.get("login_wait_seconds", 60), 180),
                    help="等登录脚本把页面跳到位的最长秒数（全程只读 /json，到位就走）")
    ap.add_argument("--dry-run", action="store_true", help="走完点击就停，不看积分")
    ap.add_argument("--list", action="store_true", help="只看 OCS 里实例的状态，不启动")
    ap.add_argument("--keep-open", action="store_true", help="结束后不关浏览器（默认关）")
    ap.add_argument("--no-bark", action="store_true", help="这一轮不推 Bark（短测验证用）")
    ap.add_argument("--test-bark", action="store_true", help="只推一条 Bark 试试通路，不跑浏览器")
    ap.add_argument("--video-wait", type=float, default=cfg.get("video_wait_seconds", 300),
                    help="进视频页后等播放器把 <video> 建出来的最长秒数")
    args = ap.parse_args()

    if args.test_bark:
        print("[*] 只测 Bark 通路，不跑浏览器")
        ok = notify(cfg, "OCS 测试", "Bark 通路测试，收到这条就可以忽略", print)
        print(f"[{'✓' if ok else '✗'}] Bark {'通了' if ok else '不通 —— 看上面那几行'}")
        return 0 if ok else 1

    if args.list:
        rc = subprocess.run([sys.executable, str(LAUNCHER), "--port", str(args.ocs_port), "--dump"],
                            cwd=str(HERE)).returncode
        v = port_version(args.browser_port)
        print(f"\n浏览器调试端口 {args.browser_port}: " + (f"通 ({v.get('Browser')})" if v else "不通"))
        return rc

    def push(title: str, body: str):
        if args.no_bark:
            print(f"(--no-bark，跳过推送：{title} / {body})")
            return False
        return notify(cfg, title, body, log)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = HERE / "runs" / stamp
    shots = outdir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    log = Log(outdir / "run.log")

    log("=" * 66)
    log(f"OCS 外部驱动测试   {stamp}")
    log(f"目标实例: {args.browser}   OCS@{args.ocs_port}   浏览器@{args.browser_port}")
    log("=" * 66)

    if not ensure_ocs_browser(log, args.ocs_port, args.browser_port, args.browser, args.skip_launch):
        return 2

    # 顺序很重要：先把页面等到位，再接管。反过来会把登录那次跳转卡死。
    land_url = wait_login_page(args.browser_port, log, args.page_wait)
    if land_url is None:
        return 1

    rc = 0
    with sync_playwright() as p:
        log(f"[*] 接管 http://127.0.0.1:{args.browser_port} ...")
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.browser_port}")
        log(f"[+] 已接管: {browser.browser_type.name} {browser.version}，"
            f"{len(browser.contexts)} 个 context")

        if not browser.contexts:
            log("[x] 没有 context")
            push("❌ OCS 测试异常中断", f"{args.browser}: 接管的浏览器一个 context 都没有")
            return 1
        ctx = browser.contexts[0]

        # 接管阶段的失败也要推 —— 半夜没人看着，没推送到手机上就等于没发生。
        page = pick_target_page(ctx, log, land_url)
        if page is None:
            push("❌ OCS 测试异常中断", f"{args.browser}: 接管后没找到超星页面（报告 {outdir.name}）")
            return 1
        page = pick_landing(ctx, log, page)
        if page is None:
            push("❌ OCS 测试异常中断", f"{args.browser}: 落到 i.chaoxing.com 上的那页认不出来")
            return 1
        close_stale_tabs(ctx, page, log)
        safe_shot(page, shots / "step0_landing.png", log)
        log(f"    落地标题: {page.title()}")
        # 冷启动那一路（OCS 刚把浏览器拉起来）落地即点，多半点了个空 —— 等它加载完再动
        settle_landing(page, log)

        try:
            course = args.course or cfg.get("course_name") or COURSE_NAME
            video = args.video or cfg.get("video_name") or VIDEO_NAME
            target_score = int(cfg.get("target_score", 32))
            steps = build_steps(course, video)
            log(f"[*] 目标课程：{course}")
            log(f"[*] 目标视频：{video}")
            score_before, plaza_url = None, None
            for step in steps:
                page = click_step(ctx, page, step, log, shots)
                if step["n"] == 4:
                    # 第 4 步落地在课程首页，顺手记一下今天的基线积分
                    plaza_url = page.url
                    score_before = read_day_score(page, log, wait_seconds=40)
                    log(f"[*] 开跑前 #dayScore = {score_before}（目标 {target_score}）")

            if args.dry_run:
                log("")
                log("【dry-run】点击流程走完，不进观测。")
                log(f"最终 URL：{page.url}")
                (outdir / "dryrun.json").write_text(
                    json.dumps({"final_url": page.url, "pages": [x.url for x in ctx.pages]},
                               ensure_ascii=False, indent=1), encoding="utf-8")
                return 0

            log("")
            log("=== 点击流程走完，开始找视频 ===")
            try:
                page.bring_to_front()   # 后台标签页会被 Chrome 限流，播放器也会磨蹭
            except Exception:
                pass
            frame, vinfo = wait_for_video(ctx, log, seconds=args.video_wait)
            if frame is None:
                for pg in ctx.pages:
                    if safe_shot(pg, shots / "no_video.png", log):
                        break
                log("[x] 所有 frame 里都没有 <video>，截图见 no_video.png")
                return 1

            vstate = ensure_playing(page, frame, log)
            if not vstate:
                safe_shot(page, shots / "not_playing.png", log)
                push("❌ OCS 测试没能开始播放",
                     f"{args.browser}: 视频起不来，报告 {outdir.name}")
                return 1
            # wait_for_video 抓的时候元数据还没到（duration 是 0），用开播那一刻的状态覆盖
            if isinstance(vinfo, dict):
                vinfo.update({k: vstate[k] for k in vstate if vstate[k] is not None})
                vinfo.setdefault("card_duration", vstate.get("duration"))

            # 计时从"确认在播"这一刻开始。之后这个页面就完全不碰了 —— 不采样、
            # 不 evaluate、不截图。上一版就是一直在读页面，超星那边会把登录页拉起来。
            video_page = page
            t0 = time.time()
            play_secs = args.play_minutes * 60
            log("")
            log(f"=== 从此刻起放 {args.play_minutes:g} 分钟，期间不碰这个页面 ===")
            while True:
                left = play_secs - (time.time() - t0)
                if left <= 0:
                    break
                time.sleep(min(300.0, left))
                if left > 60:
                    log(f"    ...还剩 {left / 60:.1f} 分钟")
            log(f"[+] {args.play_minutes:g} 分钟到了")

            # 关掉视频页（不管是哪个标签页上开着的播放器页）
            log("[*] 关掉视频标签页")
            closed = []
            for pg in list(ctx.pages):
                u = pg.url or ""
                if pg is video_page or "studentstudy" in u or "nodedetail" in u:
                    closed.append(u[:80])
                    try:
                        pg.close()
                    except Exception as e:
                        log(f"    （关不掉 {u[:50]}：{str(e).splitlines()[0][:40]}）")
            for u in closed:
                log(f"    关掉 {u}")

            # 回课程首页看积分
            def pick(pred):
                for pg in ctx.pages:
                    u = pg.url or ""
                    if not u.startswith(SKIP_URL_PREFIX) and "chaoxing.com" in u and pred(u):
                        return pg
                return None

            plaza = (pick(lambda u: "knowledge-all" in u) or pick(lambda u: "tsjy.chaoxing.com" in u)
                     or pick(lambda u: True))
            if plaza is None:
                log("[x] 一个超星标签页都不剩了，没法回去看积分")
                return 1
            plaza.bring_to_front()
            log(f"[*] 回首页：{plaza.url[:90]}")

            # 点页面上的「首页」，再刷新，然后读 #dayScore
            try:
                loc, sel, _n = first_visible(
                    plaza, ['a:text-is("首页")', 'a[href^="/plaza/"]:has-text("首页")'])
                if loc is not None:
                    log(f"    命中 selector: {sel}")
                    loc.click(timeout=20000)
                else:
                    log("    没找到「首页」链接，直接导航过去")
                    plaza.goto(plaza_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"    （点首页失败：{str(e).splitlines()[0][:60]}，改用导航）")
                try:
                    plaza.goto(plaza_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            time.sleep(2)
            try:
                plaza.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"    （刷新失败：{str(e).splitlines()[0][:60]}）")
            score_after = read_day_score(plaza, log, wait_seconds=90)
            safe_shot(plaza, shots / "day_score.png", log)
            log(f"[*] 关视频页、回首页刷新之后 #dayScore = {score_after}")

            # 32 是每日上限，正常只会等于；写成 >= 只是防"刚好读到 33"这种脏数据时误报失败
            ok = score_after is not None and score_after >= target_score
            report = {
                "run_at": stamp,
                "browser": args.browser,
                "play_minutes": args.play_minutes,
                "video": vinfo,
                "day_score_before": score_before,
                "day_score_after": score_after,
                "target_score": target_score,
                "closed_tabs": closed,
                "pages": [x.url for x in ctx.pages],
                "ok": ok,
            }
            (outdir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                encoding="utf-8")

            log("")
            log("=" * 66)
            if ok:
                log(f"[+] 通过：今日积分 {score_before} → {score_after}，已经到 {target_score} 了")
            elif score_after is None:
                log(f"[x] 没通过：首页上读不到 #dayScore（开跑前是 {score_before}）")
            else:
                log(f"[x] 没通过：今日积分 {score_before} → {score_after}，"
                    f"放了 {args.play_minutes:g} 分钟视频还是没到 {target_score}")
            log(f"报告：{outdir / 'report.json'}")
            log("=" * 66)

            if not ok:
                push("⚠️ 积分没刷满",
                     f"{args.browser}: {score_before} → {score_after}，目标 {target_score}，"
                     f"放完 {args.play_minutes:g} 分钟视频仍没到，需手动处理")
            elif cfg.get("bark_on_success"):
                push("✅ 积分已刷满", f"{args.browser}: {score_before} → {score_after}")

            rc = 0 if ok else 1
        except Exception as e:
            # 这一段以前是裸的 try/finally：步骤里抛出来的异常直接穿到最外层，
            # 只留一个 traceback，Bark 一条都不推 —— 2026-10-07 01:42 那轮就是这么
            # 静悄悄挂掉的（调度器还按"自己的 Bark 已经推过了"处理）。
            # 点击流程里任何一步挂掉，都得让人在手机上知道，并且知道挂在哪。
            log("")
            log("[x] 测试中断，异常如下（这一步之后的事都没做）：")
            for line in traceback.format_exc().splitlines():
                log(f"    {line}")
            for pg in ctx.pages:
                if safe_shot(pg, shots / "exception.png", log):
                    break
            rc = 1
            push("❌ OCS 测试异常中断",
                 f"{args.browser}: {type(e).__name__}: {str(e)[:140]}（报告 {outdir.name}）")
        finally:
            if not args.keep_open:
                log("[*] 关掉浏览器")
                if not close_browser(browser, args.browser_port, log):
                    push("⚠️ 浏览器没关掉",
                         f"{args.browser}: 端口 {args.browser_port} 还活着，得手动收一下")
            log(f"全部输出在：{outdir}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
