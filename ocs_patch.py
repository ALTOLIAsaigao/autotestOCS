"""给 OCS Desktop 打上"浏览器额外开一个调试端口"的补丁。

为什么需要它
--------------------------------------------------------------------------
OCS 拉起的浏览器是它自己用 Playwright `launchPersistentContext` 起的，走的是
`--remote-debugging-pipe`（匿名管道），**没有 TCP 端口** —— 外部任何 CDP 客户端都
连不上去。改 OCS 的 `resources/app/lib/src/worker/index.js`，让它给启动参数多加一个
`--remote-debugging-port=<端口>`，外部才接管得了。

补丁对原始行为是零影响的：端口来源先读环境变量 `OCS_BROWSER_DEBUG_PORT`，再读
`%APPDATA%\\OCS Desktop\\browser-debug-port.txt`，两个都没有就一个字都不改。

另一个关键点：这个文件是**每次点实例上的 ▶ 时**由 OCS fork 出来的 `script.js` 重新
加载的，所以打完补丁**不用重启 OCS**，下次点 ▶ 就生效。

用法
--------------------------------------------------------------------------
    python ocs_patch.py                       # 自动找 OCS 安装目录，打补丁
    python ocs_patch.py --ocs-dir "D:\\OCS Desktop"
    python ocs_patch.py --check               # 只看打没打过，不动文件
    python ocs_patch.py --port 9223 --write-port-file   # 顺手把端口写进
                                              # %APPDATA%\\OCS Desktop\\browser-debug-port.txt

原文件第一次打补丁时会被备份成 `index.js.orig`，重复运行会识别出来不重复打。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# 插在 "options.args = formatExtensionArguments(...)" 之后
ANCHOR = "options.args = formatExtensionArguments(this.extensionPaths);"

PATCH = """
            /**
             * 可选：给 OCS 拉起的浏览器额外开一个调试端口，外部 CDP 客户端就能接管这个页面。
             * 端口来源先环境变量、后文件；两者都没有时行为与原生完全一致。
             * script.js 是每次点启动时 fork 出来的新进程，每次都会重新读，所以改这里不需要重启 OCS。
             */
            const _debugPort = (() => {
                if (process.env.OCS_BROWSER_DEBUG_PORT) {
                    return process.env.OCS_BROWSER_DEBUG_PORT.trim();
                }
                try {
                    return fs_1.default.readFileSync(path_1.default.join(process.env.APPDATA || '', 'OCS Desktop', 'browser-debug-port.txt'), 'utf-8').trim();
                }
                catch (_e) {
                    return '';
                }
            })();
            if (/^\\d+$/.test(_debugPort)) {
                options.args.push(`--remote-debugging-port=${_debugPort}`, '--remote-allow-origins=*');
            }"""

MARK = "OCS_BROWSER_DEBUG_PORT"      # 判断打没打过的标志


def candidate_dirs() -> list[Path]:
    """常见装法挨个儿找。"""
    out = []

    # 本目录下的 ocs_path.txt（和 ocs_debug_start.bat 共用一份，一行完整路径）
    hint = Path(__file__).resolve().parent / "ocs_path.txt"
    if hint.exists():
        try:
            line = hint.read_text(encoding="utf-8-sig").strip().strip('"')
            if line:
                out.append(Path(line).parent)
        except Exception:
            pass

    for env in ("OCS_EXE", "LOCALAPPDATA", "PROGRAMFILES", "USERPROFILE"):
        v = os.environ.get(env)
        if not v:
            continue
        p = Path(v)
        if env == "OCS_EXE":
            out.append(p.parent)
        elif env == "USERPROFILE":
            out.append(p / "Desktop" / "OCS Desktop")
        elif env == "PROGRAMFILES":
            out.append(p / "OCS Desktop")
        else:
            out.append(p / "Programs" / "OCS Desktop")
    for drive in ("C:", "D:", "E:", "F:"):
        out.append(Path(f"{drive}/OCS Desktop"))
    # 本目录旁边放一份也算（绿色版）
    out.append(Path(__file__).resolve().parent / "OCS Desktop")

    seen, res = set(), []
    for p in out:
        if str(p).lower() not in seen:
            seen.add(str(p).lower())
            if (p / "resources" / "app" / "lib" / "src" / "worker" / "index.js").exists():
                res.append(p)
    return res


def find_worker(ocs_dir: Path) -> Path:
    return ocs_dir / "resources" / "app" / "lib" / "src" / "worker" / "index.js"


def patch_dir(ocs_dir: Path, check_only: bool, log=print) -> int:
    f = find_worker(ocs_dir)
    if not f.exists():
        log(f"[x] 这里不是 OCS 安装目录（找不到 {f}）：{ocs_dir}")
        return 1

    src = f.read_text(encoding="utf-8")
    if MARK in src:
        log(f"[=] 已经打过了，不用重复打：{f}")
        return 0
    if check_only:
        log(f"[*] 没打过补丁：{f}")
        return 1

    if ANCHOR not in src:
        # 版本不同，锚点可能换行了或者写法变了
        m = re.search(r"options\.args\s*=\s*formatExtensionArguments\([^)]*\);", src)
        if not m:
            log(f"[x] 找不到插入点，OCS 版本可能变了。要手改的话，往这行后面插：\n    {ANCHOR}")
            return 2
        anchor = m.group(0)
        log(f"[!] 用正则找到的插入点：{anchor}")
    else:
        anchor = ANCHOR

    backup = f.with_suffix(".js.orig")
    if not backup.exists():
        shutil.copy2(f, backup)
        log(f"[+] 原文件备份到：{backup.name}")
    else:
        log(f"[=] 备份已存在，不覆盖：{backup.name}")

    f.write_text(src.replace(anchor, anchor + PATCH, 1), encoding="utf-8")
    log(f"[+] 补丁打好了：{f}")
    log("    不用重启 OCS —— 下次点实例上的 ▶ 就会带上调试端口。")
    return 0


def write_port_file(port: int, log=print):
    d = Path(os.environ.get("APPDATA", "")) / "OCS Desktop"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "browser-debug-port.txt"
    p.write_text(str(port), encoding="utf-8")
    log(f"[+] 端口写进 {p}（OCS 拉起的浏览器会听这个端口）")


def main() -> int:
    ap = argparse.ArgumentParser(description="给 OCS Desktop 打调试端口补丁")
    ap.add_argument("--ocs-dir", default=None, help="OCS 安装目录（不带这个参数就自动找）")
    ap.add_argument("--check", action="store_true", help="只看打没打过，不动文件")
    ap.add_argument("--port", type=int, default=9223, help="浏览器调试端口（默认 9223）")
    ap.add_argument("--write-port-file", action="store_true",
                    help="把端口写进 %%APPDATA%%\\OCS Desktop\\browser-debug-port.txt")
    args = ap.parse_args()

    dirs = [Path(args.ocs_dir)] if args.ocs_dir else candidate_dirs()
    if not dirs:
        print("[x] 没找到 OCS Desktop，请用 --ocs-dir 指定安装目录")
        return 1

    rc = 0
    for d in dirs:
        print(f"[*] {d}")
        rc |= patch_dir(d, args.check)

    if args.write_port_file and not args.check:
        write_port_file(args.port)
    if not args.check and rc == 0:
        print(f"\n下一步：python ocs_debug_start.bat（以管理员身份）重启 OCS 带上 --remote-debugging-port=9222")
        print(f"        然后 python ocs_click_play.py --list 看看能不能连上")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
