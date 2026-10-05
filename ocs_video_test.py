#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCS 视频播放稳定性测试

做什么：
  1. 用 OCS 自带的那份 Chrome for Testing + 它那个已登录的 profile 目录起浏览器
  2. 按固定步骤点进目标视频页
  3. 找到 <video>，播放，持续观测 N 分钟
  4. 记录卡顿、缓冲、错误、媒体分片请求
  5. 出报告，关浏览器，退出码表示通过/不通过

为什么不用 OCS 本体：
  OCS 起浏览器用的就是 playwright 的 launchPersistentContext + 同一个 profile 目录
  （见 resources/app/lib/src/worker/index.js）。我们做同样的事，但能拿到 OCS 给不了的
  播放层数据（currentTime / buffered / readyState / waiting 事件 / 分片状态码）。

用法：
  python ocs_video_test.py --discover           # 第一次：手动点到位，脚本把 URL 和选择器抓回来
  python ocs_video_test.py --dry-run            # 只走点击，不进 40 分钟，用来校准选择器
  python ocs_video_test.py                      # 完整跑一轮
  python ocs_video_test.py --minutes 5          # 缩短观测时间

  参数不写就从同目录 config.json 读，config.json 不存在就用脚本里的默认值 + 命令行。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:  # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print()
    print("=" * 66)
    print("❌ 没找到 playwright —— 你用的多半不是装了它的那个 Python。")
    print()
    print(f"   当前解释器 : {sys.executable}")
    print()
    print("   同一台机器上经常有好几个 Python（系统版 / conda / PyCharm 自带），")
    print("   playwright 只装在其中一个里。用装了它的那个跑，比如：")
    print()
    print(f'   "{sys.executable}" ocs_video_test.py --discover')
    print()
    print("   或者给当前这个解释器直接装上：")
    print(f'   "{sys.executable}" -m pip install playwright')
    print("=" * 66)
    sys.exit(3)

HERE = Path(__file__).resolve().parent
OCS_CONFIG = Path(os.environ.get("APPDATA", "")) / "OCS Desktop" / "config.json"
LOCAL_CONFIG = HERE / "config.json"

# ---------------------------------------------------------------------------
# 点击步骤。selectors 里按顺序试，第一个能点到的算数（会遍历页面所有 frame）。
#
# 第 4 步不写死封面图的 hash —— 那玩意儿是 CDN 上的资源名，课程封面一换就失效。
# 改成按课程名定位：超星课程卡片是 <a class="color1" target="_blank">
# 里套 <span class="course-name">课程名</span>。
# ---------------------------------------------------------------------------
# 兜底默认值。实际跑的时候一律以 config.json 里的 course_name / video_name 为准，
# 这两个留空 —— 留空时 build_steps 拿到的空串会点不到任何东西，一眼就能看出是没配。
COURSE_NAME = ""
VIDEO_NAME = ""


def build_steps(course: str = COURSE_NAME, video: str = VIDEO_NAME) -> list[dict]:
    return [
        {
            "n": 3,
            "desc": "点击左侧【课程】入口",
            "selectors": [".icon-kc-s", "span.icon-space"],
        },
        {
            "n": 4,
            "desc": f"点击课程「{course}」（会跳到新标签页）",
            "selectors": [
                f'a.color1:has-text("{course}")',
                f'a.color1:has(.course-name:text-is("{course}"))',
            ],
            "new_page": True,
        },
        {
            "n": 5,
            "desc": "点击【全部知识点】",
            # 这个页面上 a[href*=knowledge-all] 有一大堆（每个分类一个），
            # 所以先用文本精确匹配到那一个，模糊的 href 匹配只作退路。
            "selectors": ['a:text-is("全部知识点")', 'a[href*="knowledge-all"]'],
        },
        {
            "n": 6,
            "desc": f"点击知识点「{video}」进入视频页（会跳到新标签页）",
            # 知识点卡片上的标题就是知识点名。列表视图里是
            # <a href="javascript:goKnowledge(...)"><p class="book-name …">名字</p></a>，
            # 封面视图里才是 <img alt="名字">。两种都留着，按顺序试。
            "selectors": [
                f'p.book-name:text-is("{video}")',
                f'p.book-name:has-text("{video}")',
                f'img[alt="{video}"]',
                "img.height-img",
            ],
            # goKnowledge() 最后是 window.open()，点了会另开一个标签页：
            # 非预览模式落到 mooc1-1.chaoxing.com/mycourse/studentstudy?chapterId=…&mooc2=1
            # （先经 mycourse/transfer 中转），预览模式落到 nodedetailcontroller/visitnodedetail。
            "new_page": True,
            "video_after": True,
        },
    ]


DEFAULT_STEPS = build_steps()

# 判定媒体请求用（HLS 分片走 xhr/fetch，所以不能只看 resource_type）
MEDIA_HINTS = (".m3u8", ".ts", ".m4s", ".mp4", ".flv", "mediatype=", "/media/")


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------
def load_json_config() -> dict:
    if LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_ocs_browser(name: str | None) -> dict:
    """从 OCS 自己的 config.json 里读出浏览器 profile 路径和可执行文件路径，不写死。"""
    if not OCS_CONFIG.exists():
        raise SystemExit(f"找不到 OCS 配置文件：{OCS_CONFIG}\n（如果这台机器不是装了 OCS 的那台，请用 --profile / --chrome 手工指定）")

    with open(OCS_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

    chrome = cfg["render"]["setting"]["launchOptions"]["executablePath"]
    exe = Path(chrome)
    if not exe.exists():
        raise SystemExit(f"OCS 配置里的浏览器不存在：{chrome}")

    children = cfg["render"]["browser"]["root"]["children"]
    browsers = [v for v in children.values() if v.get("type") == "browser"]
    if not browsers:
        raise SystemExit("OCS 里一个浏览器实例都没有")

    if name:
        hit = [b for b in browsers if b.get("name") == name]
        if not hit:
            names = ", ".join(repr(b.get("name")) for b in browsers)
            raise SystemExit(f"没有叫 {name!r} 的浏览器实例。现有的：{names}")
        chosen = hit[0]
    else:
        chosen = browsers[0]

    profile = Path(chosen["cachePath"])
    if not profile.is_dir():
        raise SystemExit(f"profile 目录不存在：{profile}")

    ext_dir = Path(cfg["paths"].get("extensionsFolder", "")) if cfg.get("paths") else Path()

    return {
        "name": chosen.get("name"),
        "uid": chosen.get("uid"),
        "profile": profile,
        "chrome": exe,
        "cookie_db": profile / "Default" / "Network" / "Cookies",
        "extensions_dir": ext_dir,
    }


def find_extensions(folder: Path) -> list[Path]:
    """列出扩展目录下所有可加载的扩展（跟 OCS 的 getExtensionPaths 一样的规则）。
    脚本猫就在这里，OCS 用户脚本装在脚本猫自己的存储里（跟着扩展 ID 走）。"""
    if not folder or not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.is_dir() and not p.name.endswith(".zip") and (p / "manifest.json").exists():
            out.append(p)
    return out


def notify(cfg: dict, title: str, body: str, log=None) -> bool:
    """推送到 Bark。失败绝不影响主流程。"""
    base = (cfg.get("bark_url") or "").strip().rstrip("/")
    if not base:
        if log:
            log("(没配 bark_url，跳过推送)")
        return False
    if not base.startswith("http"):
        base = "https://api.day.app/" + base  # 只填了 key 的情况
    url = f"{base}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
    params = {}
    if cfg.get("bark_level"):
        params["level"] = cfg["bark_level"]
    if cfg.get("bark_group"):
        params["group"] = cfg["bark_group"]
    if params:
        url += "?" + urllib.parse.urlencode(params)
    # 显式把系统代理带上，并在失败时把"到底走没走代理"记进日志。
    # 服务器上直连 api.day.app 的证书链验不过（CERTIFICATE_VERIFY_FAILED），
    # 走代理出去才是好的 —— 一出问题最先要看的就是这一条。
    proxies = urllib.request.getproxies()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    last = None
    for attempt in (1, 2):          # 代理那条路偶尔抖一下，给一次重试
        try:
            with opener.open(url, timeout=15) as r:
                status = r.status
            ok = status == 200
            if log:
                log(f"Bark 推送{'成功' if ok else '返回 ' + str(status)}：{title}")
            return ok
        except Exception as e:
            last = e
            if attempt == 1:
                time.sleep(3)
    if log:
        log(f"⚠️ Bark 推送失败（不影响测试）：{type(last).__name__}: {last}")
        log(f"   这次用的代理：{proxies or '（一个都没探测到 —— 是直连出去的）'}")
        if not proxies:
            log("   直连出口在这台机器上验不过证书 —— 确认 mihomo 的系统代理开着"
                "（ProxyEnable=1 / ProxyServer=127.0.0.1:<mixed-port>）。")
        else:
            log("   代理是有的但还是不通 —— 看下 mihomo 那边（节点可用？全局模式还在？）。")
    return False


def profile_in_use(profile: Path) -> list[str]:
    """查有没有别的 chrome 进程正占着这个 profile。Chrome 同一 user-data-dir 只允许一个进程。"""
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        ).stdout or ""
    except Exception:
        return []
    key = str(profile).lower()
    return [l for l in out.splitlines() if key in l.lower()]


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class Log:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.lines: list[str] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}" if msg else ""
        print(line, flush=True)
        self.lines.append(line)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


# ---------------------------------------------------------------------------
# 页面操作
# ---------------------------------------------------------------------------
def first_visible(page, selectors: list[str]):
    """按顺序试选择器，返回第一个可见的 locator。

    要把页面里所有 frame 都翻一遍：超星的课程列表在 #frame_content 这个 iframe 里
    （mooc2-ans.chaoxing.com），而 Playwright 的 locator **不会**自动穿透 iframe，
    只搜主 frame 的话永远看不到那些课程卡片。
    选择器优先级不变 —— 外层按 selectors 顺序，内层才是 frame。
    """
    frames = list(page.frames)
    for sel in selectors:
        for fr in frames:
            try:
                loc = fr.locator(sel)
                n = loc.count()
            except Exception:
                continue
            if n == 0:
                continue
            for i in range(min(n, 5)):
                cand = loc.nth(i)
                try:
                    if cand.is_visible():
                        return cand, sel, n
                except Exception:
                    continue
    return None, None, 0


# 这些页面不算"点出来的新标签页"
TAB_SKIP_PREFIX = ("chrome-extension://", "about:blank", "devtools://", "chrome://")


def safe_shot(page, path, log: Log | None = None, timeout: int = 12000):
    """截图是诊断用的，卡住或失败都不该把整轮测试带走。

    页面在忙的时候 captureScreenshot 能空转到 30s 超时；不包起来的话，
    一次截图超时就会把已经点成功的流程整个打断。
    """
    try:
        page.screenshot(path=str(path), timeout=timeout)
        return True
    except Exception as e:
        if log:
            log(f"    （截图失败，忽略：{str(e).splitlines()[0][:70]}）")
        return False


def _fresh_pages(ctx, before: set[str]) -> list:
    """这个点击窗口内新冒出来的页面（按 URL 差集，不看顺序）。"""
    return [p for p in ctx.pages
            if (p.url or "") not in before
            and not (p.url or "").startswith(TAB_SKIP_PREFIX)]


def click_step(ctx, page, step: dict, log: Log, shots: Path):
    log(f"--- 步骤 {step['n']}：{step['desc']}")
    loc, sel, total = first_visible(page, step["selectors"])
    if loc is None:
        shot = shots / f"step{step['n']}_NOTFOUND.png"
        safe_shot(page, shot, log)
        raise RuntimeError(
            f"步骤 {step['n']} 找不到可点击元素。试过 {step['selectors']}，"
            f"当前 URL={page.url}（截图 {shot}）"
        )
    if total > 1:
        log(f"    ⚠️ {sel} 匹配到 {total} 个，取第一个可见的")

    log(f"    命中 selector: {sel}")
    log(f"    点击前 URL: {page.url}")

    before = {p.url for p in ctx.pages}
    known = set(ctx.pages)
    new_page = None

    # 点之前先把 expect_page 挂上。超星的课程卡片是 <a target="_blank">，卡片自己的 JS
    # 可能又 window.open 一次 —— 点一下会连开两个一样的标签页，事件流比事后数数靠谱。
    if step.get("new_page"):
        try:
            with ctx.expect_page(timeout=45000) as pinfo:
                loc.click(timeout=30000)
            new_page = pinfo.value
            log("    → 捕获到新标签页事件")
        except Exception as e:
            log(f"    （没等到新标签页事件：{str(e).splitlines()[0][:60]}）")
            log("      改用 URL 差集继续找")
    else:
        loc.click(timeout=30000)

    # 兜底 / 补充：按 URL 差集找这个窗口里新出现的页面。
    # 不能按 len(ctx.pages) 判断 —— ctx.pages 的顺序不是创建顺序；也不能只等很短，
    # 浏览器忙的时候新 target 上报会拖到 20s 以上（实测踩过）。
    if new_page is None:
        deadline = time.time() + (60 if step.get("new_page") else 20)
        while time.time() < deadline:
            fresh = _fresh_pages(ctx, before)
            if fresh:
                new_page = fresh[0]
                break
            time.sleep(0.5)

    if new_page is not None:
        # 其余同批冒出来的（重复打开的）收掉
        for extra in ctx.pages:
            if extra is new_page or extra in known:
                continue
            if (extra.url or "").startswith(TAB_SKIP_PREFIX):
                continue
            try:
                extra.close()
                log("    （关掉一个重复打开的标签页）")
            except Exception:
                pass
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        page = new_page
        log("    → 切到新标签页")
    else:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

    time.sleep(2)
    log(f"    点击后 URL: {page.url}")
    safe_shot(page, shots / f"step{step['n']}_after.png", log)
    return page


VIDEO_INFO_JS = """() => {
  const vs = [...document.querySelectorAll('video')];
  const v = vs.sort((a,b) => (b.duration || 0) - (a.duration || 0))[0];
  if (!v) return null;
  let bufEnd = 0, bufRanges = [];
  try {
    for (let i = 0; i < v.buffered.length; i++) {
      bufRanges.push([v.buffered.start(i), v.buffered.end(i)]);
      bufEnd = Math.max(bufEnd, v.buffered.end(i));
    }
  } catch (e) {}
  return {
    src: v.currentSrc || v.src || '',
    currentTime: v.currentTime || 0,
    duration: v.duration || 0,
    paused: v.paused, ended: v.ended, muted: v.muted,
    readyState: v.readyState, networkState: v.networkState,
    playbackRate: v.playbackRate, volume: v.volume,
    bufferedEnd: bufEnd, bufRanges: bufRanges,
    errCode: v.error ? v.error.code : null,
    w: v.clientWidth, h: v.clientHeight, count: vs.length,
  };
}"""


def video_info(frame, min_duration: float = 0.0):
    """在单个 frame 上读 <video> 状态。frame 被导航换掉时 evaluate 会抛，返回 None。"""
    if frame is None:
        return None
    try:
        info = frame.evaluate(VIDEO_INFO_JS)
    except Exception:
        return None
    if not info or (info.get("duration") or 0) < min_duration:
        return None
    return info


def src_id(src: str) -> str:
    """src 去掉查询串当身份用 —— at_/ak_ 那些 token 每次会话都不一样。"""
    return (src or "").split("?")[0]


def all_videos(ctx, min_duration: float = 0.0) -> list:
    """所有标签页、所有 frame 里的 <video>，按时长从大到小。"""
    out = []
    for pg in ctx.pages:
        if (pg.url or "").startswith(TAB_SKIP_PREFIX):
            continue
        for fr in pg.frames:
            info = video_info(fr, min_duration)
            if info:
                out.append((fr, pg, info))
    out.sort(key=lambda c: (c[2].get("duration") or 0), reverse=True)
    return out


def locate_video(ctx, min_duration: float = 0.0):
    """重新全扫一遍找播放器，返回 (frame, page, info) 或 None。

    超星把播放器放在 ananas/modules/video/index.html 这个 iframe 里；视频播完
    自动跳下一节时整页会导航，手里那个 frame 立刻失效 —— 所以每次采样前都
    得能重新定位，不能一直抱着开工时拿到的那个句柄。
    """
    vs = all_videos(ctx, min_duration)
    return vs[0] if vs else None


def nudge_play(frame) -> bool:
    """试着让 video 动起来（新节可能停在暂停态等着人点）。返回是否已在播。"""
    try:
        r = frame.evaluate("""() => {
          const vs = [...document.querySelectorAll('video')];
          const v = vs.sort((a,b) => (b.duration||0) - (a.duration||0))[0];
          if (!v) return null;
          v.muted = false;
          if (!v.volume) v.volume = 1;
          const p = v.play();
          if (p && p.catch) p.catch(() => {});
          return { paused: v.paused, ct: v.currentTime, dur: v.duration };
        }""")
        return bool(r and not r["paused"])
    except Exception:
        return False


def find_video(ctx, page, log: Log):
    """主 frame + 所有 iframe + 所有标签页里找 <video>，返回 (frame, info)。"""
    candidates = all_videos(ctx)
    if not candidates:
        return None, None
    fr, pg, info = candidates[0]
    log(f"    找到 <video>（共 {len(candidates)} 处候选）：")
    log(f"      src      : {info['src'][:120]}")
    log(f"      duration : {info['duration']}  paused={info['paused']}  muted={info['muted']}")
    log(f"      分辨率    : {info['w']}x{info['h']}   playbackRate={info['playbackRate']}")
    log(f"      所在页面  : {pg.url}")
    return fr, info


# ---------------------------------------------------------------------------
# 观测
#
# 这里是个小状态机，两个状态：
#
#   playing    视频在放。每 poll 秒采样一次，ct 连续 stall_after 秒不前进算卡顿；
#              暂停（被弹窗之类打断）先尝试自动恢复，恢复不了才算卡顿。
#   wait_next  上一节播完 / 页面正在跳转。这段不计卡顿 —— 超星的课在视频播完时
#              会自己整页导航到下一节，手里那个 frame 立刻失效，看不出这一点就会
#              读到 ct=0/dur=0 然后一路误报（2026-10-05 那次就是这么误报的）。
#              等超过 grace 秒还没等到下一节，才记一次卡顿并推 Bark。
#
# follow_next=True 时跟超星自己跳：播完的第 stop_after_videos 个视频就收工。
# ---------------------------------------------------------------------------
def monitor(ctx, frame, log: Log, minutes: float, shots: Path,
            poll: int = 5, stall_after: int = 10, on_stall=None,
            auto_loop: bool = False, follow_next: bool = False,
            stop_after_videos: int = 1, grace: float = 600.0):
    t0 = time.time()
    deadline = t0 + minutes * 60
    samples, stalls, media, events = [], [], [], []
    cur_frame, cur_src, seg = frame, None, 0
    completed = 0
    awaiting, wait_since, wait_flagged = False, None, False
    last_ct, last_ct_at, nudged = None, time.time(), False

    def on_response(resp):
        u = resp.url
        if any(h in u.lower() for h in MEDIA_HINTS):
            try:
                media.append({
                    "t": round(time.time() - t0, 1),
                    "status": resp.status,
                    "url": u[:180],
                })
            except Exception:
                pass

    for pg in ctx.pages:
        pg.on("response", on_response)

    def wait_timeout(el: float, waited: float, ct: float = 0.0):
        """等下一节等太久 —— 只记一次，避免拖一堆重复卡顿。"""
        nonlocal wait_flagged
        if waited < grace or wait_flagged:
            return
        wait_flagged = True
        shot = shots / f"wait_{int(el)}s.png"
        for pg in ctx.pages:
            if safe_shot(pg, shot, log, timeout=8000):
                break
        entry = {
            "t": el, "seg": seg, "kind": "wait_next_timeout",
            "frozen_seconds": round(waited, 1), "currentTime": ct,
            "readyState": None, "networkState": None, "bufferedEnd": None,
            "screenshot": str(shot.name),
        }
        stalls.append(entry)
        log(f"  [{el:>7.1f}s] ⛔ 等下一节已经 {waited:.0f}s，还没出现")
        if on_stall:
            on_stall(entry)

    log(f"开始观测 {minutes} 分钟（每 {poll}s 采样一次，连续 {stall_after}s 不前进算卡顿"
        + (f"；跟超星自动跳转，播完 {stop_after_videos} 个视频收工" if follow_next else "") + "）")

    while time.time() < deadline:
        el = round(time.time() - t0, 1)
        now = time.time()

        # 采样。frame 被导航换掉时 evaluate 会抛 → 全扫一遍重新定位播放器。
        info = video_info(cur_frame)
        if info is None:
            hit = locate_video(ctx)
            if hit:
                cur_frame, _pg, info = hit

        sig = src_id(info["src"]) if info else None

        # ---------- 换片：新 src，或者 ct 明显回退（重播/下一节用了同一个源）----------
        if info is not None and cur_src is not None and (
                sig != cur_src or (last_ct is not None and info["currentTime"] < last_ct - 5)):
            gap = round(now - wait_since, 1) if wait_since else None
            seg += 1
            events.append({
                "t": el, "kind": "next_video", "seg": seg,
                "duration": round(info["duration"] or 0, 1),
                "src": sig[-80:], "gap_seconds": gap,
            })
            log(f"  [{el:>7.1f}s] ↪ 换到第 {seg} 个视频：dur={info['duration']:.0f}s"
                + (f"（上一节之后空了 {gap:.0f}s）" if gap else ""))
            cur_src, last_ct, last_ct_at = sig, None, now
            awaiting, wait_since, wait_flagged, nudged = False, None, False, False
            # 新节的视频有时候停在暂停态等人点一下
            if info["paused"] and nudge_play(cur_frame):
                log("        新视频原本是暂停的，已自动点播")
                events.append({"t": el, "kind": "autoplay_nudge", "seg": seg})
        elif info is not None and cur_src is None:
            seg += 1
            cur_src = sig
            log(f"  [{el:>7.1f}s] ▶ 第 {seg} 个视频：dur={info['duration']:.0f}s  src=…{sig[-44:]}")
            last_ct, last_ct_at = None, now
            events.append({"t": el, "kind": "segment_start", "seg": seg,
                           "duration": round(info["duration"] or 0, 1), "src": sig[-80:]})

        # ---------- 没有 <video>：跳转中 ----------
        if info is None:
            if not awaiting:
                awaiting, wait_since, wait_flagged = True, now, False
                log(f"  [{el:>7.1f}s] …这个 frame 上没有 <video> 了（页面在跳转），等下一节")
            waited = now - wait_since
            samples.append({"t": el, "seg": seg, "waiting": True, "waited": round(waited, 1)})
            wait_timeout(el, waited)
            time.sleep(poll)
            continue

        ct, dur = info["currentTime"], info["duration"]
        at_end = bool(info["ended"]) or bool(dur and ct >= dur - 0.5)

        s = {
            "t": el, "seg": seg,
            "currentTime": ct, "duration": dur, "paused": info["paused"],
            "ended": info["ended"], "readyState": info["readyState"],
            "networkState": info["networkState"], "bufferedEnd": info["bufferedEnd"],
            "bufRanges": info["bufRanges"], "errCode": info["errCode"],
            "src": info["src"],
        }
        samples.append(s)

        # ---------- 这一节刚播完 ----------
        if at_end and not awaiting:
            completed += 1
            events.append({"t": el, "kind": "segment_end", "seg": seg,
                           "duration": round(dur, 1), "played_to": round(ct, 1)})
            log(f"  [{el:>7.1f}s] ✔ 第 {seg} 个视频播完（{dur:.0f}s），累计完成 {completed} 个")

            if auto_loop and not follow_next:
                # 老的用法：视频比观测时长短，播完从头再来，否则后面全是 ended 等于没测。
                try:
                    cur_frame.evaluate("""() => {
                      const v = document.querySelector('video');
                      if (v) { v.currentTime = 0; const p = v.play(); if (p && p.catch) p.catch(() => {}); }
                    }""")
                    log("        ↻ 从头再来")
                    last_ct, last_ct_at = None, now
                except Exception as e:
                    log(f"        重播失败：{e}")
                time.sleep(poll)
                continue

            if follow_next and completed >= stop_after_videos:
                log(f"        目标就是跟完 {stop_after_videos} 个视频，收工")
                break

            awaiting, wait_since, wait_flagged = True, now, False
            log("        等超星自己跳下一节")
            time.sleep(poll)
            continue

        # ---------- 已经在等下一节 ----------
        if awaiting:
            waited = now - wait_since
            log(f"  [{el:>7.1f}s] …等下一节（已 {waited:.0f}s）  ct={ct:.1f}/{dur:.1f}")
            s["waiting"] = True
            s["waited"] = round(waited, 1)
            wait_timeout(el, waited, ct)
            time.sleep(poll)
            continue

        # ---------- 正常在播 ----------
        if last_ct is not None and abs(ct - last_ct) > 0.05:
            last_ct_at, nudged = now, False
        last_ct = ct

        frozen = now - last_ct_at
        flag = ""
        if frozen >= stall_after:
            line = f"  ⛔ 卡住 {frozen:.0f}s"
            if info["paused"] and not nudged:
                # 被什么东西暂停了 —— 先救一把，救回来就不算它卡顿
                nudged = True
                resumed = nudge_play(cur_frame)
                events.append({"t": el, "kind": "paused", "seg": seg,
                               "ct": round(ct, 1), "resumed": resumed})
                log(f"  [{el:>7.1f}s] ⏸ 视频是暂停状态（{frozen:.0f}s 没动），"
                    + ("已自动恢复播放" if resumed else "尝试自动恢复失败"))
                if resumed:
                    last_ct_at = now
                    line = "  ⏸ 已尝试恢复播放"

            flag = line
            if "⛔" in line:
                shot = shots / f"stall_{int(el)}s.png"
                for pg in ctx.pages:
                    if safe_shot(pg, shot, log, timeout=8000):
                        break
                if not stalls or el - stalls[-1]["t"] > stall_after:
                    entry = {
                        "t": el, "seg": seg, "kind": "stall",
                        "frozen_seconds": round(frozen, 1),
                        "currentTime": ct, "readyState": info["readyState"],
                        "networkState": info["networkState"],
                        "bufferedEnd": info["bufferedEnd"],
                        "screenshot": str(shot.name),
                    }
                    stalls.append(entry)
                    if on_stall:
                        on_stall(entry)  # 第一次卡顿立刻推，不等整轮跑完

        if info["errCode"]:
            flag += f"  ⛔ MediaError code={info['errCode']}"

        log(
            f"  [{el:>7.1f}s] ct={ct:8.2f}/{dur:8.2f} "
            f"buf={info['bufferedEnd'] or 0:8.2f} rs={info['readyState']} "
            f"paused={int(bool(info['paused']))} ended={int(bool(info['ended']))}{flag}"
        )
        time.sleep(poll)

    log(f"观测结束：采样 {len(samples)} 次，卡顿 {len(stalls)} 次，媒体请求 {len(media)} 条，"
        f"播完 {completed} 个视频（共经历 {seg} 个）")
    return samples, stalls, media, events


def summarize_segments(samples: list, events: list) -> list:
    """按片段归拢采样，出每个视频播了多久、等跳转等了多久。"""
    segs: dict = {}
    for e in events:
        d = segs.setdefault(e.get("seg") or 0, {})
        if e["kind"] == "segment_start":
            d["duration"] = e.get("duration")
            d["src"] = e.get("src")
            d["started_t"] = e["t"]
        elif e["kind"] == "next_video":
            d["duration"] = e.get("duration")
            d["src"] = e.get("src")
            d["started_t"] = e["t"]
            if e.get("gap_seconds") is not None:
                d["gap_seconds"] = e["gap_seconds"]
        elif e["kind"] == "segment_end":
            d["duration"] = e.get("duration")
            d["ended_t"] = e["t"]
            d["played_to"] = e.get("played_to")
        elif e["kind"] in ("paused", "autoplay_nudge"):
            d.setdefault(e["kind"], 0)
            d[e["kind"]] += 1

    for s in samples:
        d = segs.setdefault(s.get("seg") or 0, {})
        d["seg"] = s.get("seg") or 0
        d.setdefault("first_sample_t", s["t"])
        d["last_sample_t"] = s["t"]
        d["samples"] = d.get("samples", 0) + 1
        if s.get("waiting"):
            d["wait_samples"] = d.get("wait_samples", 0) + 1
        elif s.get("duration"):
            d["duration"] = round(s["duration"], 1)

    for i, d in sorted(segs.items()):
        d["seg"] = i
        d["completed"] = "ended_t" in d
    return [segs[i] for i in sorted(segs)]



# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(cfg) -> int:
    ap = argparse.ArgumentParser(description="OCS 视频播放稳定性测试")
    ap.add_argument("--browser", default=cfg.get("browser_name"),
                    help="OCS 里的浏览器实例名（默认取第一个）")
    ap.add_argument("--profile", default=cfg.get("profile_path"),
                    help="手工指定 profile 目录（覆盖自动探测）")
    ap.add_argument("--chrome", default=cfg.get("chrome_path"),
                    help="手工指定 chrome.exe（覆盖自动探测）")
    ap.add_argument("--url", default=cfg.get("start_url"),
                    help="起始 URL（登录后第一跳所在的那个页面）")
    ap.add_argument("--minutes", type=float, default=cfg.get("minutes", 40))
    ap.add_argument("--poll", type=int, default=cfg.get("poll_seconds", 5))
    ap.add_argument("--stall-after", type=int, default=cfg.get("stall_after_seconds", 10))
    ap.add_argument("--login-wait", type=int, default=cfg.get("login_wait_seconds", 60),
                    help="进页面后等待登录态就绪的秒数")
    ap.add_argument("--discover", action="store_true",
                    help="只起浏览器，手动点到目标页，按回车后抓 URL/选择器")
    ap.add_argument("--dry-run", action="store_true",
                    help="走完点击就停，不进观测")
    ap.add_argument("--keep-open", action="store_true", help="结束后不关浏览器")
    ap.add_argument("--extensions", default=cfg.get("extensions_path"),
                    help="扩展目录（默认自动取 OCS 的 extensionsFolder，即脚本猫所在处）")
    ap.add_argument("--no-extensions", action="store_true",
                    help="不加载任何扩展（纯裸浏览器，观测更干净）")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    outdir = HERE / "reports" / stamp
    shots = outdir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    log = Log(outdir / "run.log")

    # --- 定位浏览器 ---
    if args.profile and args.chrome:
        target = {"name": "手工指定", "uid": "-",
                  "profile": Path(args.profile), "chrome": Path(args.chrome),
                  "cookie_db": Path(args.profile) / "Default" / "Network" / "Cookies",
                  "extensions_dir": Path()}
    else:
        target = load_ocs_browser(args.browser)

    # --- 扩展 ---
    ext_dirs: list[Path] = []
    if not args.no_extensions:
        folder = Path(args.extensions) if args.extensions else target.get("extensions_dir")
        ext_dirs = find_extensions(folder)

    log(f"OCS 浏览器实例 : {target['name']}  (uid={target['uid']})")
    log(f"profile        : {target['profile']}")
    log(f"chrome         : {target['chrome']}")
    log(f"登录凭据库      : {'存在' if target['cookie_db'].exists() else '❌ 不存在，可能没登录过'}")
    if args.no_extensions:
        log("扩展           : 不加载（--no-extensions）")
    elif ext_dirs:
        log(f"扩展           : {len(ext_dirs)} 个")
        for e in ext_dirs:
            log(f"                 - {e.name}")
    else:
        log(f"扩展           : ⚠️ 一个都没找到（目录 {args.extensions or target.get('extensions_dir')}）")
    log(f"输出目录        : {outdir}")
    log()

    # --- 占用检查：同一个 profile 只能被一个 Chrome 进程用 ---
    holders = profile_in_use(target["profile"])
    if holders:
        log("❌ 这个 profile 正被别的 chrome 进程占用，先关掉它再跑：")
        for h in holders[:5]:
            log("   " + h[:200])
        raise SystemExit(1)
    log("profile 未被占用 ✅")

    with sync_playwright() as p:
        launch_args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            # 播放器自动播放，不需要用户手势。这是测试用的标准开关，
            # 和平台侧的检测无关。
            "--autoplay-policy=no-user-gesture-required",
        ]
        if ext_dirs:
            joined = ",".join(str(e) for e in ext_dirs)
            # 新版 Chrome 光给 --load-extension 不生效，必须同时给 --disable-extensions-except
            launch_args += [f"--load-extension={joined}",
                            f"--disable-extensions-except={joined}"]

        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(target["profile"]),
            executable_path=str(target["chrome"]),
            headless=False,
            viewport=None,
            args=launch_args,
            # Playwright 默认参数里带 --disable-extensions，会把上面加载的扩展全关掉，
            # 必须把它和 --enable-automation 一起剔掉。
            ignore_default_args=["--enable-automation", "--disable-extensions"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 复用 profile 时 Chrome 会把上次没关干净的标签页一起恢复出来，
        # 那些页会让后面的 ctx.pages[-1] 抓错对象。留一个，其余关掉。
        time.sleep(1.5)
        for extra in list(ctx.pages):
            if extra is not page:
                try:
                    extra.close()
                except Exception:
                    pass
        log("浏览器已启动 ✅\n")

        samples, stalls, media = [], [], []
        try:
            # ---------------- discover 模式 ----------------
            if args.discover:
                log("【discover 模式】")
                if args.url:
                    log(f"自动导航到：{args.url}")
                    page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                    if args.login_wait:
                        log(f"等待登录态就绪 {args.login_wait}s ...")
                        time.sleep(args.login_wait)
                    # 别再用 ctx.pages[-1]：跳转/弹窗都可能多出标签，认准我们自己这页
                    log(f"落地 URL：{page.url}")
                else:
                    log("没有 start_url，请在浏览器里手动点到位，然后回车。")
                    if sys.stdin and sys.stdin.isatty():
                        try:
                            input(">>> 按回车继续...")
                        except EOFError:
                            pass
                    page = ctx.pages[-1]

                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                time.sleep(3)

                DUMP = """() => ({
                  url: location.href,
                  title: document.title,
                  icon_kc_s_count: document.querySelectorAll('.icon-kc-s').length,
                  icon_kc_s: [...document.querySelectorAll('.icon-kc-s')].map(e => e.outerHTML),
                  h3: [...document.querySelectorAll('h3')].map(e => e.outerHTML).slice(0, 30),
                  anchors: [...document.querySelectorAll('a')].slice(0, 60).map(a => ({
                    href: (a.getAttribute('href') || '').slice(0, 160),
                    text: (a.innerText || '').trim().slice(0, 60),
                    cls: (a.className || '').slice(0, 80)
                  })),
                  icons: [...document.querySelectorAll('[class*="icon-"]')].slice(0, 40).map(e => ({
                    tag: e.tagName, cls: e.className, text: (e.innerText || '').trim().slice(0, 30)
                  })),
                  body_text: (document.body ? document.body.innerText : '').slice(0, 800),
                  body_html_head: (document.body ? document.body.innerHTML : '').slice(0, 1200),
                  videos: [...document.querySelectorAll('video')].map(v => ({
                    src: (v.currentSrc || v.src || '').slice(0, 200),
                    duration: v.duration, paused: v.paused,
                    w: v.clientWidth, h: v.clientHeight
                  })),
                  iframes: [...document.querySelectorAll('iframe')].map(f => ({
                    src: f.src, id: f.id, cls: f.className
                  })),
                })"""
                info = page.evaluate(DUMP)
                info["frames_with_video"] = []
                info["all_frames"] = []
                for fr in page.frames:
                    try:
                        d = fr.evaluate(DUMP)
                    except Exception:
                        continue
                    d["is_main"] = (fr == page.main_frame)
                    info["all_frames"].append(d)
                    if d["videos"]:
                        info["frames_with_video"].append(
                            {"frame_url": d["url"], "videos": len(d["videos"])})
                (outdir / "discover.json").write_text(
                    json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
                log("\n=== discover 结果（同时写入 discover.json）===")
                log("主框架：")
                log(json.dumps({k: v for k, v in info.items() if k != "all_frames"},
                               ensure_ascii=False, indent=1))
                for i, d in enumerate(info["all_frames"]):
                    if d["is_main"]:
                        continue
                    log(f"\n--- iframe #{i}  {d['url'][:120]}  "
                        f"icon_kc_s={d['icon_kc_s_count']} videos={len(d['videos'])}")
                return

            # ---------------- 正常流程 ----------------
            if not args.url:
                raise SystemExit(
                    "没有起始 URL。先用 --discover 抓一个，或者在 config.json 里写 start_url。")

            log(f"导航到起始页：{args.url}")
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            log(f"当前 URL：{page.url}")

            if args.login_wait:
                log(f"等待登录态就绪 {args.login_wait}s ...")
                time.sleep(args.login_wait)
                log(f"等待结束，当前 URL：{page.url}")
            safe_shot(page, shots / "step0_start.png", log)

            for step in DEFAULT_STEPS:
                page = click_step(ctx, page, step, log, shots)

            if args.dry_run:
                log("\n【dry-run】点击流程走完，不做观测。")
                log(f"最终 URL：{page.url}")
                (outdir / "dryrun.json").write_text(
                    json.dumps({"final_url": page.url, "pages": [p.url for p in ctx.pages]},
                               ensure_ascii=False, indent=1), encoding="utf-8")
                return

            log("\n=== 步骤走完，开始找视频 ===")
            frame, vinfo = find_video(ctx, page, log)
            if frame is None:
                for pg in ctx.pages:
                    safe_shot(pg, shots / "no_video.png", log)
                raise RuntimeError("所有 frame 里都没找到 <video>，截图见 no_video.png")

            # 确保在播
            try:
                frame.evaluate("""() => {
                  const v = document.querySelector('video');
                  if (v) { v.muted = false; v.play().catch(() => {}); }
                }""")
                time.sleep(2)
            except Exception:
                pass

            def _stall_alert(entry, _pushed=[False]):
                if _pushed[0]:
                    return  # 只即时推第一条，剩下的进最终报告
                _pushed[0] = True
                notify(cfg, "⛔ 视频卡顿",
                       f"{target['name']} 播放到 {entry['currentTime']:.0f}s 处卡住 "
                       f"{entry['frozen_seconds']:.0f}s（buffered={entry['bufferedEnd']}）",
                       log)

            samples, stalls, media, events = monitor(
                ctx, frame, log, args.minutes, shots,
                poll=args.poll, stall_after=args.stall_after,
                on_stall=_stall_alert,
                follow_next=bool(cfg.get("follow_next_video", True)),
                stop_after_videos=int(cfg.get("stop_after_videos", 2)),
                grace=float(cfg.get("transition_grace_seconds", 600)))

        finally:
            (outdir / "report.json").write_text(
                json.dumps({
                    "run_at": stamp,
                    "browser": target["name"],
                    "profile": str(target["profile"]),
                    "minutes": args.minutes,
                    "samples": samples,
                    "stalls": stalls,
                    "segments": summarize_segments(samples, events),
                    "events": events,
                    "media_requests": media,
                }, ensure_ascii=False, indent=1), encoding="utf-8")

            if not args.keep_open:
                log("关闭浏览器 ...")
                try:
                    ctx.close()
                except Exception:
                    pass

    # 结论
    errs = [s for s in samples if s.get("errCode")]
    # 跳转期间采不到视频是正常的（超星自己跳下一节），只统计不判负
    waiting = [s for s in samples if s.get("waiting")]
    segs = summarize_segments(samples, events)
    done = len([s for s in segs if s.get("completed")])

    log("\n================ 结论 ================")
    log(f"观测时长   : {args.minutes} 分钟")
    log(f"采样 / 卡顿 / 媒体请求 : {len(samples)} / {len(stalls)} / {len(media)}")
    log(f"MediaError : {len(errs)}")
    log(f"播完视频   : {done} 个（经历 {len(segs)} 个，跳转等待采样 {len(waiting)} 次）")
    log(f"报告       : {outdir}")
    ok = not stalls and not errs
    log("判定       : " + ("✅ 稳定" if ok else "❌ 不稳定"))

    if not ok or cfg.get("bark_on_success"):
        why = []
        if stalls:
            why.append(f"卡顿 {len(stalls)} 次")
        if errs:
            why.append(f"MediaError {len(errs)}")
        if waiting:
            why.append(f"跳转等待 {len(waiting)} 次采样")
        notify(cfg,
               ("✅ 视频稳定 " if ok else "❌ 视频不稳定 ") + f"{args.minutes:g}min",
               (f"{target['name']}：{('、'.join(why)) if why else '全程正常'}"
                f"，媒体请求 {len(media)} 条。报告 {outdir.name}"),
               log)

    return 0 if ok else 2


def main() -> int:
    """包一层：run() 内部没兜住的异常也推到 Bark，然后原样抛。"""
    cfg = load_json_config()
    try:
        return run(cfg)
    except KeyboardInterrupt:
        raise
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if code != 0:
            notify(cfg, "❌ 测试没能启动",
                   f"退出码 {code}，多数是 profile 被占用或路径不对，看 run.log")
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        notify(cfg, "❌ 测试异常", f"{type(e).__name__}: {str(e)[:180]}")
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(130)
