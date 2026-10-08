# autotestOCS

从外部驱动 [OCS Desktop](https://www.ocsjs.com/) 拉起的那只浏览器，在本机的真实登录态下走完超星课程的视频播放，最后回课程首页读今日积分，判定"今天有没有刷满"。

**用途**：超星学习通《大学生安全教育》这门课的**每日积分**自动化 —— 那门课的积分是靠看视频刷的，这个脚本每天替你把它刷满。（课程名和视频名在 `config.json` 里改，换成别的课也能用。）

> **跑之前先在 OCS 里把自动化登录脚本配好。**
> 这套东西**完全不碰登录**：它接管的是 OCS 已经拉起来、已经登录好的那只浏览器。超星的登录、以及登录态失效后的重登，全部由你在 OCS 里配的那个自动化登录脚本负责，这边只管点课、找视频、等时间、读积分。OCS 那边没配好登录脚本，这边第一步就卡在"等超星页面跳到位"。

判定标准只有一条：**课程首页的今日积分有没有到目标分**（默认 32，也就是超星面板上写的"每日积分上限32分"）。到了就算通过，没到就推一条 Bark 通知，通知里带着当次报告路径。

整套东西不依赖 OCS 自带的浏览器监控（那个在 4G 内存的机器上跑不动），内存压力和开一个普通 Chrome 一样。

## 它是怎么接上去的

OCS 拉起的浏览器是它自己用 Playwright `launchPersistentContext` 起的，走的是 `--remote-debugging-pipe`（匿名管道），**默认没有任何 TCP 端口**，外部 CDP 客户端连不上。所以链路是：

```
[1] CDP 连 OCS 渲染进程(9222) ──► 点实例行上的 ▶
[2] OCS fork script.js ──► Playwright launchPersistentContext ──► Chrome
        └─ ocs_patch.py 给它加的启动参数：--remote-debugging-port=9223
        └─ 端口一通就把窗口最大化（CDP Browser.setWindowBounds）
[3] 等超星自动登录脚本把页面带到 i.chaoxing.com（**只轮询 /json，先不接管**）
[4] Playwright connect_over_cdp(9223) 接管那个 context
[5] 点四步进视频页 ──► 确认在播 ──► 之后这个页面一句 JS 都不发
[6] 放够 play_minutes 分钟 ──► 关掉视频页 ──► 回课程首页点「首页」并刷新
[7] 读 p#dayScore ──► 到目标分 = 通过；没到推 Bark ──► 关浏览器
```

几个非做不可的细节，都是实测撞出来的：

- **不能一 attach 就接管**。超星的自动登录脚本进来先跳转，正赶上它跳转中途附加上去，那个页面会卡死在 `about:blank`。先纯 HTTP 轮询 `/json` 等 title/url 到位，再 attach。
- **播放期间别碰那个页面**。反复 evaluate / 截图，超星那边会把登录页拉起来，播放直接断。所以判定改成看最终积分，而不是盯着播放器采样。
- **新标签页要挂 `expect_page()`**。课程卡片和知识点封面都是 `<a target="_blank">`，点一下另开标签页；光轮询 URL 差集在浏览器忙的时候会漏。
- **`browser.close()` 关不掉这只 Chrome**。`connect_over_cdp` 拿到的 browser 调 `close()` 只是断连接，进程还活着、9223 还在响应，下一轮接管会认到上一轮残留的页签。真正关掉要发 CDP `Browser.close`（`ocs_drive.py:close_browser()` 干的就是这个，关不掉会推 Bark）。
- **起点页要校正**。OCS 在当天没刷满时会自己把浏览器拉起来放视频，接管时常常已经开着上一轮的 `tsjy` / `studentstudy` 页签，按 URL 挑目标页会挑错，所以接管后先认 `i.chaoxing.com` 的页签、顺手把残留页签收掉。
- **窗口要最大化，而且只能走 CDP**。OCS 是 `viewport: null` 起的（见 `worker/index.js` 的 `launchBrowser`），窗口多大页面就多大 —— 默认那个小窗口会让元素可见性、坐标全按小视口算。所以浏览器 CDP 端口一通，`ocs_drive.py:maximize_browser()` 就用 `Browser.setWindowBounds` 把它收成最大化。**不要改用启动参数 `--start-maximized`**：OCS 的 launch 里写死了 `--window-position=0,0`，而 Chrome 只要拿到显式窗口位置就把 `--start-maximized` 吃掉（实测 Chrome for Testing 137：光 `--start-maximized` → maximized；和 `--window-position=0,0` 同时给 → normal，两种顺序都是）。`setWindowBounds` 这条实测管用。

## 装

1. **Python 依赖**

   ```
   python -m pip install -r requirements.txt
   ```

   不需要 `playwright install` —— 这套用的是 OCS 自带的 Chrome for Testing，路径从 OCS 的配置里读，不会再下一份 Chromium。

2. **给 OCS 打补丁**（让它拉起的浏览器多听一个调试端口）

   ```
   python ocs_patch.py --ocs-dir "D:\你的\OCS Desktop" --write-port-file
   ```

   自动找安装目录也可以，直接 `python ocs_patch.py`。原文件会备份成 `index.js.orig`，重复运行不会重复打。补丁是**可选性**的：端口来源先读环境变量 `OCS_BROWSER_DEBUG_PORT`，再读 `%APPDATA%\OCS Desktop\browser-debug-port.txt`，两个都没有时行为和原生一模一样。

   > 这个文件是每次点 ▶ 时由 OCS fork 出来的 `script.js` 重新加载的，所以**打完不用重启 OCS**。

3. **把 OCS 带着调试端口起起来**

   ```
   ocs_debug_start.bat        （右键，以管理员身份运行；脚本自己也会请求提权）
   ```

   它会关掉旧 OCS、带上 `--remote-debugging-port=9222` 重启、然后等端口就绪。OCS 装在哪用 `ocs_path.txt`（一行完整路径）或环境变量 `OCS_EXE` 告诉它。

   验证一下：`python ocs_click_play.py --list`

4. **配置**

   ```
   copy config.example.json config.json
   ```

   然后改 `config.json`：课程名、视频名、播放时长、目标分、Bark 推送地址、要跑哪些实例、各在几点几分。`config.json` 已经在 `.gitignore` 里，不会进仓库。

## 装到服务器上

**放哪个目录**:放一个**独立的纯英文目录**,比如 `C:\autotestOCS` —— **别放在 OCS 的安装目录里**。原因:

- OCS 升级 / 重装会把那个目录覆盖掉,你的代码和 `runs/` 里的报告跟着一起没。
- OCS 多半装在 `Program Files` 或者带中文的路径下,往那儿写要提权;项目代码没必要跟着受这个约束。
- `ocs_patch.py` 往 OCS 目录里写的是补丁和 `index.js.orig` —— 那是**补丁**该待的地方,不是**项目**该待的地方。

放哪都行,脚本会自己去找 OCS 目录(环境变量 `OCS_EXE` → 本目录的 `ocs_path.txt` → 常见安装路径);实在找不到就 `python ocs_patch.py --ocs-dir "C:\...\OCS Desktop"`,或者把路径写进 `ocs_path.txt`(一行完整路径)。路径**别带中文和空格**,也别放需要管理员才能写的目录。

**`config.json` 不在仓库里**(里面有 Bark key 和你的账号标识),clone 完自己造一份:

```
copy config.example.json config.json
notepad config.json
```

**服务器上如果超星只认某个出口**(机房 IP 被拉黑、必须走代理才能进登录页):

1. mihomo 开**系统代理** + 切**全局模式**,或者保持规则模式加一条超星域名规则指向节点。这两件事是分开的:全局模式管"送到 mihomo 之后怎么走",系统代理管"送不送给 mihomo"。**只切全局、系统代理没开,等于没切。**
2. **OCS 和 mihomo 必须跑在同一个 Windows 用户下**。系统代理写在 `HKCU` 里,是"当前用户"的设置;OCS 要是跑在别的账号或 SYSTEM 下(计划任务、服务),它看不到这个代理 —— 现象是"明明代理通了,OCS 还是被拦"。
3. **Windows 上 Python 的 urllib 会读注册表里的系统代理**,连打 `127.0.0.1` 的请求也会被丢给代理去。所以项目里访问本机调试端口的代码一律用 `ocs_click_play.urlopen_local()` 强制直连(`ocs_net_check.py` 那种要测真实出网路径的除外)。判断代理生不生效用 `curl.exe -x http://127.0.0.1:<mixed-port> https://myip.ipip.net`。
4. 全局模式下**所有**出网流量都走节点,包括跟超星无关的。视频那 34 分钟是实打实的出口流量,按流量计费的机器上要算一下。

## 跑

```
python ocs_drive.py                     # 单个实例跑一轮（默认 34 分钟）
python ocs_drive.py --play-minutes 1    # 短测：流程全走，只放 1 分钟
python ocs_drive.py --browser 33        # 换一个 OCS 实例
python ocs_drive.py --dry-run           # 只走到点击流程结束，不看积分
python ocs_drive.py --keep-open         # 结束不关浏览器
python ocs_drive.py --no-bark           # 这一轮不推通知
python ocs_drive.py --list              # 看 OCS 里实例的当前状态
```

退出码：`0` 通过（积分到目标分），`1` 没通过，`2` 连 OCS / 浏览器都起不来。

输出全在 `runs/<时间戳>/`：`full.log` 日志、`shots/` 各步骤截图、`report.json` 结论。

## 多实例排期

4G 的机器同时只能跑一只 OCS 浏览器，所以 `ocs_schedule.py` 是**串行**的：到点拉起一个 → 跑完（浏览器也关干净）→ 再等下一个的点。时间就在 `config.json` 的 `instances` 里：

```json
"instances": [
  { "name": "22", "start_at": "01:00", "enabled": true },
  { "name": "33", "start_at": "07:00", "enabled": false },
  { "name": "44", "start_at": "13:00", "enabled": false }
]
```

`name` 要和 OCS 界面上那个实例名一模一样。每个实例还能单独覆盖 `play_minutes` / `course_name` / `video_name` / `target_score`。`enabled` 是开关。

```
python ocs_schedule.py                  # 常驻，跨天自动接着跑
python ocs_schedule.py --list           # 只看今天的排期表，不起任何东西
python ocs_schedule.py --once           # 今天的跑完就退出（挂 Windows 计划任务用这个）
python ocs_schedule.py --force 33       # 不管几点，现在立刻跑这个实例
python ocs_schedule.py --play-minutes 1 # 覆盖播放时长（测流程用）
```

跑过的实例记在 `runs/schedule_YYYYMMDD.json`，调度器重启不会重复跑同一天。

两个注意事项：

- **两个实例的时间至少隔 40 分钟**。34 分钟播放加点击和收尾大约 36 分钟，排太密后面那个的点会撞上前一个还在跑。
- **别把实例排在 00:00 附近**。积分每天零点归零，跨零点那一轮回来查分读到的已经是新一天的数，必然判失败。最早排到 00:40 之后。

## 文件

| 文件 | 干什么的 |
| --- | --- |
| `ocs_drive.py` | 外部驱动主入口：起浏览器 → 点击 → 等待 → 查分 → 收尾 |
| `ocs_schedule.py` | 多实例按时刻轮流开（串行） |
| `ocs_patch.py` | 给 OCS 的 `worker/index.js` 打调试端口补丁 |
| `ocs_debug_start.bat` | 以管理员身份带调试端口重启 OCS |
| `ocs_video_test.py` | 点击步骤的选择器 + 播放器定位 |
| `ocs_click_play.py` | 纯标准库 CDP 客户端，点 OCS 自己的 ▶、看实例状态 |
| `ocs_net_check.py` | 排查"到超星这条网路通不通"：DNS / TCP / 重定向链 / 代理 / 时钟 |
| `ocs_clear_cookies.py` | 清掉那只浏览器里超星的 cookie，治登录重定向死循环 |
| `ocs_eval.py` / `ocs_probe.py` | 调试用：在任意页面 / 每个 frame 里跑 JS |
| `config.example.json` | 配置模板，复制成 `config.json` 改 |

## 出问题了先看这个

**`连不上 http://127.0.0.1:9222/json`，底层错误是 `HTTP Error 502: Bad Gateway`** —— **502 是代理回给你的，不是"端口没起"**（端口没起会是 connection refused）。这台机器开着系统代理，而 Python 的 urllib 会读注册表里的系统代理，于是连打本机 9222 的请求也被丢给了代理。项目里的本地请求已经走 `urlopen_local()` 直连了；要是还报，查一下有没有 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量（urllib 优先读环境变量）。

**`page.goto: net::ERR_TOO_MANY_REDIRECTS at http://i.chaoxing.com/`** —— 这是 OCS 自己那个超星自动登录脚本报的，跟本项目的代码无关（它挂在 OCS 里的用户脚本上），意思是 Chrome 跟了 20 跳重定向还没落地。两条路分开查：

1. `python ocs_net_check.py` —— 网络到不到得了超星。链路干净（几跳停在登录页 200）说明是下面第 2 条；链路里落到非超星域名、或一直 302 到某个认证页，那就是服务器网络被截了（校园网认证 / 代理 / DNS 劫持）。
2. **cookie 半失效**：`passport2` 还留着"已登录"的 cookie，`i.chaoxing.com` 的会话却已经作废，两边互相踢皮球。清掉重登：`python ocs_clear_cookies.py`（没有调试端口时就删 `%APPDATA%\OCS Desktop\Network\Cookies`）。同一个超星账号在多台机器同时登录也会这样，得保证同时只有一处在用。

## 已知限制

- 只在 Windows + OCS Desktop v2.9.x（Chrome 134/137）上验过。OCS 换版本后 `ocs_patch.py` 的插入点可能变，脚本会提示；选择器在 `ocs_video_test.py` 里，超星改版可能会失效，此时 `runs/<时间戳>/shots/` 里的截图就是现场。
- 判定靠"今日积分"这个前端展示值，不是后端的播放上报。积分没到就只推通知，不会自动重试 —— 重试策略交给调度器（多实例、多个时间点）。
