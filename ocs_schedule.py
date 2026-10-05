"""按 config.json 里的 instances 定时轮流拉起 OCS 浏览器实例。

服务器 4G + Windows，物理上同时只能跑一只 OCS 浏览器（一只 Chrome 差不多就吃掉
1G+），所以这里是**串行**的：一个实例到点 → 拉起它 → 走完整的播放+查分流程 →
浏览器关干净 → 再等下一个实例的点。两个实例的时间**至少隔 40 分钟**（34 分钟播放
加点击和收尾大约 36~38 分钟）。

配置（config.json）
--------------------------------------------------------------------------
    "instances": [
      { "name": "22", "start_at": "01:00", "enabled": true },
      { "name": "33", "start_at": "07:00", "enabled": true },
      { "name": "44", "start_at": "13:00", "enabled": false }
    ]

    name        OCS 里的浏览器实例名（就是界面上那个 22/33/44）
    start_at    每天的几点几分开始，本地时间，24 小时制 "HH:MM"
    enabled     false 就先留着不跑，想开的时候改成 true

实例里还可以单独覆盖：play_minutes / course_name / video_name / target_score —— 不写
就跟着 config 顶层的值走。

可选顶层配置：

    "schedule": { "poll_seconds": 20, "grace_minutes": 60 }

    poll_seconds   多久看一眼表（默认 20 秒）
    grace_minutes  到点了但没赶上（比如调度器刚被重启），还差多少分钟内可以补跑；
                   超过就当这个点错过了，跳过并推 Bark（默认 60 分钟）

用法
--------------------------------------------------------------------------
    python ocs_schedule.py                # 常驻，跨天自动接着跑
    python ocs_schedule.py --list         # 只看今天的排期表，不起任何东西
    python ocs_schedule.py --once         # 今天的都跑完就退出（配 Windows 计划任务）
    python ocs_schedule.py --force 33     # 不管几点，现在就跑 33
    python ocs_schedule.py --play-minutes 1   # 覆盖播放时长（测流程用）

跑过的实例记在 runs/schedule_YYYYMMDD.json 里，重启调度器不会重复跑同一天。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "ocs_drive.py"
STATE_DIR = HERE / "runs"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cfg() -> dict:
    p = HERE / "config.json"
    if not p.exists():
        raise SystemExit(f"[x] 没有 {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def parse_hhmm(s: str):
    """"7:5" / "07:05" 都收，返回 (时, 分)。"""
    try:
        h, _, m = str(s).strip().partition(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except Exception:
        raise SystemExit(f"[x] start_at 写错了吧：{s!r}（要写成 07:30 这种 24 小时制）")


def normalize(cfg: dict) -> list[dict]:
    """把 instances 整理成按时间排好、带默认值的列表。"""
    raw = cfg.get("instances") or []
    out = []
    for it in raw:
        if not it.get("name"):
            raise SystemExit(f"[x] instances 有一项没写 name：{it!r}")
        if not it.get("start_at"):
            raise SystemExit(f"[x] 实例 {it['name']} 没写 start_at")
        h, m = parse_hhmm(it["start_at"])
        out.append({
            "name": str(it["name"]),
            "at": (h, m),
            "enabled": bool(it.get("enabled", True)),
            "play_minutes": it.get("play_minutes"),
            "course_name": it.get("course_name"),
            "video_name": it.get("video_name"),
            "target_score": it.get("target_score"),
        })
    out.sort(key=lambda x: x["at"])
    return out


def state_path(day: str) -> Path:
    return STATE_DIR / f"schedule_{day}.json"


def load_state(day: str) -> dict:
    p = state_path(day)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": day, "done": [], "missed": []}


def save_state(st: dict):
    STATE_DIR.mkdir(exist_ok=True)
    state_path(st["date"]).write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                      encoding="utf-8")


# ---------------------------------------------------------------------------
# 起浏览器之前先把残留的收掉
# ---------------------------------------------------------------------------
def port_alive(port: int, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


def kill_port(port: int) -> bool:
    """上一个实例的浏览器要是没关干净，这里补一刀（CDP Browser.close）。"""
    if not port_alive(port):
        return True
    log(f"[!] {port} 上还有浏览器活着，发 CDP Browser.close 收掉")
    try:
        from ocs_click_play import WebSocket
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as r:
            v = json.loads(r.read().decode("utf-8"))
        ws = WebSocket(v["webSocketDebuggerUrl"])
        ws.connect()
        try:
            ws.call("Browser.close", timeout=8)
        except Exception:
            pass          # 关掉那一刻连接断掉，正常
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception as e:
        log(f"    （发 Browser.close 失败：{str(e).splitlines()[0][:70]}）")

    for _ in range(15):
        if not port_alive(port, timeout=2.0):
            log("[+] 收干净了")
            return True
        time.sleep(1)
    log(f"[!] {port} 还活着 —— 下一个实例可能会起不来")
    return False


# ---------------------------------------------------------------------------
def run_instance(it: dict, cfg: dict, args) -> int:
    name = it["name"]
    out = STATE_DIR / f"schedule_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}.log"
    cmd = [sys.executable, "-u", str(DRIVER), "--browser", name]

    pm = args.play_minutes if args.play_minutes else it["play_minutes"]
    if pm is None:
        pm = cfg.get("play_minutes", 34)
    cmd += ["--play-minutes", str(pm)]
    # 只有实例自己覆盖了才传，没写就让 ocs_drive 去读 config 顶层的
    for flag, key in (("--course", "course_name"), ("--video", "video_name")):
        if it[key]:
            cmd += [flag, it[key]]
    if args.no_bark:
        cmd.append("--no-bark")
    if args.keep_open:
        cmd.append("--keep-open")

    log(f"=== 实例 {name} 开跑（放 {pm:g} 分钟）→ {out.name}")
    log(f"    {' '.join(cmd[1:])}")
    rc = None
    with open(out, "w", encoding="utf-8", errors="replace") as f:
        p = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
        for line in p.stdout:
            line = line.rstrip("\n")
            f.write(line + "\n")
            print(f"    │ {line}", flush=True)
        rc = p.wait()
    log(f"=== 实例 {name} 结束，退出码 {rc}，日志 {out}" + ("（通过）" if rc == 0 else "（没通过）"))
    return rc


def push(cfg: dict, title: str, body: str, no_bark: bool):
    if no_bark:
        log(f"(--no-bark，跳过推送：{title} / {body})")
        return
    try:
        from ocs_video_test import notify
        notify(cfg, title, body, log)
    except Exception as e:
        log(f"（推送失败：{e}）")


def print_table(items: list[dict], st: dict, cfg: dict):
    log(f"今天排期（{st['date']}）：")
    if not items:
        log("    instances 是空的 —— 去 config.json 里加 {\"name\": \"22\", \"start_at\": \"01:00\"}")
        return
    for it in items:
        h, m = it["at"]
        mark = "已跑" if it["name"] in st["done"] else ("错过" if it["name"] in st["missed"] else "待跑")
        pm = it["play_minutes"] or cfg.get("play_minutes", 34)
        flag = "✓" if it["enabled"] else "✗(关着)"
        log(f"    {h:02d}:{m:02d}  实例 {it['name']:<6} {flag:<8} {mark}  "
            f"放 {pm:g} 分钟，目标 {it['target_score'] or cfg.get('target_score', 32)} 分")


# ---------------------------------------------------------------------------
def main() -> int:
    cfg = load_cfg()
    ap = argparse.ArgumentParser(description="定时轮流拉起 OCS 浏览器实例")
    ap.add_argument("--list", action="store_true", help="只打印今天的排期，不启动")
    ap.add_argument("--once", action="store_true", help="今天的都跑完就退出")
    ap.add_argument("--force", metavar="NAME", help="不管几点，现在就跑这个实例")
    ap.add_argument("--play-minutes", type=float, default=None,
                    help="覆盖所有实例的播放时长（测流程用，比如 1）")
    ap.add_argument("--no-bark", action="store_true", help="不推 Bark")
    ap.add_argument("--keep-open", action="store_true", help="跑完不关浏览器（别在服务器上用）")
    ap.add_argument("--browser-port", type=int, default=9223, help="OCS 浏览器的调试端口")
    args = ap.parse_args()

    items = normalize(cfg)
    sched = cfg.get("schedule") or {}
    poll = float(sched.get("poll_seconds", 20))
    grace = timedelta(minutes=float(sched.get("grace_minutes", 60)))

    state = load_state(datetime.now().strftime("%Y%m%d"))
    st = {"date": datetime.now().strftime("%Y%m%d"), "done": list(state["done"]),
          "missed": list(state["missed"])}

    if args.list:
        print_table(items, st, cfg)
        return 0

    if args.force:
        target = next((x for x in items if x["name"] == args.force), None)
        if target is None:
            raise SystemExit(f"[x] config 里没有实例 {args.force}")
        if not kill_port(args.browser_port):
            return 1
        rc = run_instance(target, cfg, args)
        st["done"].append(target["name"])
        save_state(st)
        return rc

    log(f"调度器起来了：{len([x for x in items if x['enabled']])} 个实例，"
        f"每 {poll:g}s 看一次表，错过 {grace.total_seconds() / 60:g} 分钟内可补跑")
    print_table(items, st, cfg)

    noted: dict[str, int] = {}       # 倒计时日志去重（实例名 -> 上次打过的 10 分钟档）
    while True:
        today = datetime.now().strftime("%Y%m%d")
        if st["date"] != today:      # 跨天：清空记录接着跑
            log(f"跨天到 {today}，重新计时")
            st = {"date": today, "done": [], "missed": []}
            save_state(st)
            print_table(items, st, cfg)

        now = datetime.now()
        pending = [x for x in items if x["enabled"] and x["name"] not in st["done"]
                   and x["name"] not in st["missed"]]

        if not pending:
            if args.once:
                log("今天的实例都处理完了，退出")
                return 0
            time.sleep(poll)
            continue

        # 到点的就按时间顺序跑（同一时刻只可能有一个在跑，串行）
        due = [x for x in pending
               if now >= now.replace(hour=x["at"][0], minute=x["at"][1],
                                     second=0, microsecond=0)]
        if not due:
            nxt = pending[0]
            wait = (now.replace(hour=nxt["at"][0], minute=nxt["at"][1], second=0, microsecond=0)
                    - now).total_seconds()
            if wait > poll:
                # 干等一整天会刷屏，倒计时只在跨过 10 分钟档的时候打一条
                bucket = int(wait // 600)
                if noted.get(nxt["name"]) != bucket:
                    noted[nxt["name"]] = bucket
                    log(f"    下一个：{nxt['at'][0]:02d}:{nxt['at'][1]:02d} 实例 {nxt['name']}，"
                        f"还有 {wait / 60:.1f} 分钟")
            time.sleep(max(1.0, min(poll, wait)))
            continue

        it = due[0]
        scheduled = now.replace(hour=it["at"][0], minute=it["at"][1], second=0, microsecond=0)
        late = now - scheduled
        if late > grace:
            log(f"[!] 实例 {it['name']} 的点（{it['at'][0]:02d}:{it['at'][1]:02d}）过了 "
                f"{late.total_seconds() / 60:.0f} 分钟，不补跑了")
            st["missed"].append(it["name"])
            save_state(st)
            push(cfg, "⏰ 错过了一个实例",
                 f"{it['name']} 计划 {it['at'][0]:02d}:{it['at'][1]:02d}，调度器晚了 "
                 f"{late.total_seconds() / 60:.0f} 分钟，没跑", args.no_bark)
            continue

        if late.total_seconds() > 60:
            log(f"[*] 实例 {it['name']} 晚点了 {late.total_seconds() / 60:.0f} 分钟，现在补跑")
        kill_port(args.browser_port)          # 上一个没收干净的话先收掉
        rc = run_instance(it, cfg, args)
        st["done"].append(it["name"])
        save_state(st)
        if rc != 0:
            log(f"[!] 实例 {it['name']} 退出码 {rc}，后面照常（自己的 Bark 已经推过了）")
        time.sleep(5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("收到 Ctrl-C，退出")
        raise SystemExit(130)
