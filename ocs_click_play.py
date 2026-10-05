#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从外部点一下 OCS Desktop 里某个浏览器实例的「启动」按钮（▶ / play_circle）。

原理
----
OCS 是 Electron 应用，主窗口加载的是 resources/app/public/index.html（Vue3 SPA）。
只要 OCS 是带 --remote-debugging-port=9222 启动的，就能用 CDP 附着到它的渲染进程，
直接在那个页面里跑 JS —— 这等于人手点了一遍，不碰 OCS 的任何文件。

页面里的关键事实（读 OCS 的解包源码得到，见 resources/app/public/assets/）：
  * window.store                 整个响应式 store（全局挂上的），
                                 浏览器实体树在 store.render.browser.root.children[uid]
  * 每个浏览器一行 <div class="entity">
  * 行里的 ▶ 就是 BrowserOperators 里的
        <span class="material-icons-outlined" style="color:#165dff">play_circle</span>
    它的 onClick 是  instance.launch()，  instance = Browser.from(uid)
  * 行上带 active 类当且仅当 store.render.browser.currentBrowserUid === uid

所以点击 = 先把 currentBrowserUid 设成目标 uid（Vue 会给那一行加 active），
再点掉那一行的 play_circle。点下去之后 OCS 自己 fork script.js 起 Playwright +
它那个已登录的 profile，跟我们没关系了。

用法
----
  python ocs_click_play.py                    # 点 config.json 里 browser_name 那个
  python ocs_click_play.py --name 22          # 指定实例名
  python ocs_click_play.py --list             # 只列出 OCS 里所有浏览器实例，不点
  python ocs_click_play.py --dump             # 打印 UI 上每一行的文本和图标，排查选择器用
  python ocs_click_play.py --no-wait          # 点完就走，不等状态变化

退出码: 0 = 点成功(且等到已启动) / 1 = 失败 / 2 = 端口没开(OCS 没带调试端口启动)

不依赖任何第三方库，只用标准库（服务器带宽有限，不再装包）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9222
HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# 最小 WebSocket 客户端（RFC6455，只做 CDP 需要的那点：文本帧 + ping/pong）
# --------------------------------------------------------------------------
class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 20.0):
        # ws://127.0.0.1:9222/devtools/page/XXXX
        if not url.startswith("ws://"):
            raise WSError(f"不支持的 URL: {url}")
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host
        self.port = int(port or 80)
        self.path = "/" + path
        self.timeout = timeout
        self._buf = b""
        self.sock = None
        self._next_id = 0

    # ---- 连接 ----------------------------------------------------------
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        # 读到 \r\n\r\n
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("握手时连接被关闭")
            head += chunk
        header, _, remainder = head.partition(b"\r\n\r\n")
        self._buf = remainder
        first = header.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in first:
            raise WSError(f"WebSocket 握手失败: {first}")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # ---- 帧 ------------------------------------------------------------
    def _send_text(self, payload: str):
        data = payload.encode("utf-8")
        header = bytearray([0x81])  # FIN + opcode=text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("连接被关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, data: bytes):
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_message(self) -> str:
        """读一条完整的文本消息，自动处理 ping / 分片。"""
        buf = bytearray()
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:       # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:       # close
                raise WSError("对端关闭了 WebSocket")
            if opcode == 0xA:       # pong
                continue
            buf += payload
            if fin:
                return buf.decode("utf-8", "replace")

    # ---- CDP -----------------------------------------------------------
    def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        self._next_id += 1
        mid = self._next_id
        self._send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        old = self.sock.gettimeout()
        if timeout:
            self.sock.settimeout(timeout)
        try:
            while True:
                msg = json.loads(self.recv_message())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise WSError(f"CDP {method} 出错: {msg['error']}")
                    return msg.get("result")
                # 其它事件（Runtime.consoleAPICalled 之类）忽略
        finally:
            try:
                self.sock.settimeout(old)
            except OSError:
                pass

    def eval_js(self, expression: str, timeout: float = 30.0):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,   # 让 click() 带上用户手势，避免被当成脚本触发
            },
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            raise WSError(f"页面里抛异常: {json.dumps(res['exceptionDetails'], ensure_ascii=False)[:500]}")
        return res.get("result", {}).get("value")


# --------------------------------------------------------------------------
# 准备 JS 片段
# --------------------------------------------------------------------------
JS_WALK = r"""
function __walkBrowsers() {
  var st = window.store;
  var root = st && st.render && st.render.browser && st.render.browser.root;
  if (!root) return null;
  var out = [];
  (function walk(node, where) {
    var kids = node && node.children;
    if (!kids) return;
    Object.keys(kids).forEach(function (uid) {
      var c = kids[uid];
      if (!c) return;
      // parent = 它所在的那层文件夹 uid。OCS 的列表只渲染「当前文件夹」的子项，
      // 所以点击前要先把 currentFolderUid 切到这个文件夹，否则 DOM 里根本没有这一行。
      if (c.type === 'browser') out.push({ uid: uid, name: c.name, where: where, parent: node.uid });
      walk(c, where + '/' + (c.name || uid));
    });
  })(root, '');
  return out;
}
"""

JS_LIST = (
    "(function(){"
    + JS_WALK
    + "var list = __walkBrowsers();"
    "if (list === null) return { error: 'window.store 里没有 render.browser.root' };"
    "return { browsers: list, currentBrowserUid: window.store.render.browser.currentBrowserUid || null };"
    "})()"
)

JS_DUMP = r"""
(function(){
  var rows = Array.prototype.slice.call(document.querySelectorAll('div.entity'));
  return {
    count: rows.length,
    rows: rows.map(function (r) {
      var icons = Array.prototype.map.call(
        r.querySelectorAll('span.material-icons-outlined'),
        function (s) { return s.textContent.trim(); }
      );
      return {
        cls: r.className,
        text: r.textContent.replace(/\s+/g, ' ').trim().slice(0, 140),
        icons: icons
      };
    })
  };
})()
"""


def js_select(uid: str, folder_uid: str | None = None) -> str:
    folder_line = (
        f"st.render.browser.currentFolderUid = {json.dumps(folder_uid)};"
        if folder_uid else ""
    )
    return (
        "(function(){"
        "var st = window.store;"
        "if (!st || !st.render || !st.render.browser) return {ok:false, reason:'no store'};"
        f"{folder_line}"
        f"st.render.browser.currentBrowserUid = {json.dumps(uid)};"
        "return {ok:true,"
        "  currentBrowserUid: st.render.browser.currentBrowserUid,"
        "  currentFolderUid: st.render.browser.currentFolderUid};"
        "})()"
    )


JS_CLICK_TEMPLATE = r"""
(function(){
  var NAME = __NAME__;
  var rows = Array.prototype.slice.call(document.querySelectorAll('div.entity'));
  function iconsOf(r){ return Array.prototype.map.call(r.querySelectorAll('span.material-icons-outlined'), function(s){return s.textContent.trim();}); }
  function playOf(r){
    if (!r) return null;
    var ss = r.querySelectorAll('span.material-icons-outlined');
    for (var i = 0; i < ss.length; i++) if (ss[i].textContent.trim() === 'play_circle') return ss[i];
    return null;
  }
  function nameHit(r){
    if (!r) return false;
    var spans = r.querySelectorAll('span');
    for (var i = 0; i < spans.length; i++) {
      if (spans[i].children.length === 0 && spans[i].textContent.trim() === NAME) return true;
    }
    return false;
  }

  var row = null, how = '';
  // 1) 优先用刚被选中的 active 行
  var actives = rows.filter(function(r){ return r.classList.contains('active'); });
  if (actives.length === 1 && playOf(actives[0])) { row = actives[0]; how = 'active-class'; }
  // 2) 退化：按名字文本匹配
  if (!row) {
    for (var i = 0; i < rows.length; i++) {
      if (nameHit(rows[i]) && playOf(rows[i])) { row = rows[i]; how = 'name-match'; break; }
    }
  }
  // 3) 再退化：整个 UI 里只有一个 play_circle，那就是它
  if (!row) {
    var all = [];
    rows.forEach(function(r){ var p = playOf(r); if (p) all.push([r, p]); });
    if (all.length === 1) { row = all[0][0]; how = 'only-play-button'; }
  }

  if (!row) {
    return { ok: false, reason: 'play-button-not-found',
      rows: rows.map(function(r){ return { cls: r.className, text: r.textContent.replace(/\s+/g,' ').trim().slice(0,140), icons: iconsOf(r) }; }) };
  }

  var play = playOf(row);
  var target = play.parentElement || play;   // Icon 组件根节点上挂的 onClick
  target.click();
  return { ok: true, how: how, clicked_class: String(target.className),
           row_text: row.textContent.replace(/\s+/g,' ').trim().slice(0,140) };
})()
"""

JS_STATE_TEMPLATE = r"""
(function(){
  var NAME = __NAME__;
  var rows = Array.prototype.slice.call(document.querySelectorAll('div.entity'));
  var out = [];
  rows.forEach(function(r){
    var spans = r.querySelectorAll('span'), hit = false;
    for (var i = 0; i < spans.length; i++) if (spans[i].children.length === 0 && spans[i].textContent.trim() === NAME) hit = true;
    if (!hit) return;
    out.push({
      text: r.textContent.replace(/\s+/g,' ').trim().slice(0,140),
      icons: Array.prototype.map.call(r.querySelectorAll('span.material-icons-outlined'), function(s){return s.textContent.trim();})
    });
  });
  return { rows: out };
})()
"""


def js_click(name: str) -> str:
    return JS_CLICK_TEMPLATE.replace("__NAME__", json.dumps(name))


def js_state(name: str) -> str:
    return JS_STATE_TEMPLATE.replace("__NAME__", json.dumps(name))


# --------------------------------------------------------------------------
# 连到 OCS
# --------------------------------------------------------------------------
def http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_ocs_page(port: int) -> dict:
    """在 CDP 的目标列表里挑出 OCS 主窗口那个 page。"""
    try:
        targets = http_json(f"http://127.0.0.1:{port}/json")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"[x] 连不上 http://127.0.0.1:{port}/json —— OCS 不是带调试端口启动的。\n"
            f"    先以管理员身份运行 ocs_debug_start.bat（会重启 OCS 并带上 --remote-debugging-port={port}）。\n"
            f"    底层错误: {e}"
        )

    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise SystemExit(f"[x] 调试端口开了，但没有 page 目标: {json.dumps(targets, ensure_ascii=False)[:400]}")

    # OCS 主窗口是 file:// .../public/index.html；退一步认 title
    for t in pages:
        if "public/index.html" in (t.get("url") or ""):
            return t
    for t in pages:
        if (t.get("title") or "").strip().upper() == "OCS":
            return t
    return pages[0]


def load_target_name(cli_name: str | None) -> str:
    if cli_name:
        return cli_name
    cfg = HERE / "config.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if data.get("browser_name"):
                return str(data["browser_name"])
        except (OSError, ValueError):
            pass
    raise SystemExit("[x] 没给 --name，config.json 里也没有 browser_name")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="外部点击 OCS 里某个浏览器实例的启动按钮")
    ap.add_argument("--name", help="浏览器实例名（默认取 config.json 的 browser_name）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"OCS 调试端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--list", action="store_true", help="只列出所有浏览器实例")
    ap.add_argument("--dump", action="store_true", help="打印 UI 每行的文本/图标，排查用")
    ap.add_argument("--no-wait", action="store_true", help="点完不等状态变化")
    ap.add_argument("--wait", type=float, default=90.0, help="点完后等状态变化的最长秒数（默认 90）")
    args = ap.parse_args()

    target = find_ocs_page(args.port)
    ws = WebSocket(target["webSocketDebuggerUrl"])
    ws.connect()
    try:
        if args.dump:
            print(json.dumps(ws.eval_js(JS_DUMP), ensure_ascii=False, indent=2))
            return 0

        listing = ws.eval_js(JS_LIST)
        if listing.get("error"):
            print(f"[x] {listing['error']}", file=sys.stderr)
            return 1

        browsers = listing["browsers"]
        if args.list:
            print(f"OCS 里共 {len(browsers)} 个浏览器实例（当前选中: {listing.get('currentBrowserUid')}）")
            for b in browsers:
                tag = " *" if b["uid"] == listing.get("currentBrowserUid") else "  "
                print(f"{tag} {b['name']}    uid={b['uid']}    所在文件夹={b['where'] or '/'}")
            return 0

        name = load_target_name(args.name)
        match = [b for b in browsers if str(b["name"]) == str(name)]
        if not match:
            print(f"[x] 没找到名字叫 {name!r} 的浏览器实例。现有:", file=sys.stderr)
            for b in browsers:
                print(f"      - {b['name']}  (uid={b['uid']})", file=sys.stderr)
            return 1
        if len(match) > 1:
            print(f"[!] 有 {len(match)} 个都叫 {name!r}，用第一个 uid={match[0]['uid']}")

        uid = match[0]["uid"]
        folder_uid = match[0].get("parent")
        print(f"[*] 目标: {name}  (uid={uid}, 所在文件夹={match[0].get('where') or '/'})")

        # 切到它所在的文件夹 + 选中它 —— 两件事都得做：
        # 前者保证那一行真的被渲染出来，后者让 Vue 给它加 active 类。
        sel = ws.eval_js(js_select(uid, folder_uid))
        if not sel.get("ok"):
            print(f"[x] 选中失败: {sel}", file=sys.stderr)
            return 1
        time.sleep(0.5)   # 等 Vue 把列表和 active 类刷到 DOM 上

        res = ws.eval_js(js_click(name))
        if not res.get("ok"):
            print(f"[x] 没点到启动按钮：{res.get('reason')}", file=sys.stderr)
            for r in res.get("rows", []):
                print(f"      {r['cls'][:40]:40s} | {r['icons']} | {r['text'][:60]}", file=sys.stderr)
            print("      用 --dump 看完整 UI 行。", file=sys.stderr)
            return 1

        print(f"[+] 已点击（匹配方式: {res['how']}）")

        if args.no_wait:
            return 0

        # 等那一行的 play_circle 消失 = OCS 已经切到 launching/launched 状态
        deadline = time.time() + args.wait
        while time.time() < deadline:
            time.sleep(1.0)
            st = ws.eval_js(js_state(name))
            rows = st.get("rows") or []
            if rows and "play_circle" not in rows[0]["icons"]:
                print(f"[+] 状态已变化，OCS 接管了: icons={rows[0]['icons']}")
                return 0
            if not rows:
                print("[+] 那一行已经不在了（换页/过滤了？）")
                return 0
        print("[!] 点下去了，但等待期内没看到状态变化（可能已经在启动中，或按钮没生效）。")
        print("    用 --dump 看当前 UI 行。")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WSError as e:
        print(f"[x] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
