# tests

当前包含**脚本回归测试**与 **Phase 2 Gate A 无窗口 smoke**；完整 Gate A/B/C 浏览器行为测试仍属后续阶段。

## `run-script-tests.sh`

轻量、离线、可随时运行：不联网、不编译、不启动浏览器/UI。

```sh
tests/run-script-tests.sh     # 0 = 全部通过；1 = 有失败
```

覆盖内容（基线离线复跑：2026-09-23 21:25 CST，`223 passed / 0 failed`，含第 28 组 6 项；本次新增第 29 组 4 项与第 26 组 PNG filter/chunk 反例断言 2 项，本机有真实 checkout 时预期 `229 passed / 0 failed`。无 checkout 时第 28/29 组各有 1 项 `--checkout` 检查转为 `SKIP`（不计 PASS），实际 PASS 数以复跑为准。仅语法、help/dry-run、临时 fixture 与只读静态检查，不启动 Browser CLI、不构建、不操作可见窗口）：

| 组 | 内容 |
| --- | --- |
| 1 | 所有脚本 `bash -n` |
| 2 | 5 个入口 `--help` rc=0 |
| 3 | 5 个入口 `--dry-run`（在 `mktemp -d` 模拟新机器上）rc=0 |
| 4 | `checkout.sh` dry-run 显示 pinned revision、`checkout --detach <rev>`、`with_proxy` 与 `gclient sync --revision src@<rev>` |
| 5 | `verify-phase1.sh` dry-run 明示 “HEAD mismatch is FAIL by default” |
| 6 | **磁盘硬下限负例**：`DISK_MIN_GIB=24`、`DISK_MIN_GIB=0 DISK_MIN_FRESH_GIB=0`、非整数、`DISK_MIN_FRESH_GIB < DISK_MIN_GIB` 全部 rc!=0；25/65、30/65、80/120 合法 |
| 7 | `build.sh --no-watchdog` 被拒绝，help 不再宣传绕过 |
| 8 | **`checkout.sh --resume` 一致性负例**：伪造 tip checkout（HEAD != VERSION）必须在任何依赖同步之前 rc!=0 并提示 `--repin` |
| 9 | **`verify-phase1.sh` 严格 HEAD 负例**：伪造 tip checkout 上 `--check-only` 必须 FAIL（`reproducibility FAIL`）；`--allow-tip` 作为显式 opt-out 可通过 |
| 10 | 真实 checkout 只读检查：`verify-phase1.sh --check-only`（无 checkout 时自动 SKIP） |
| 11 | 静态审计：所有 `fetch`/`gclient sync`/`git fetch`/`curl` 调用必须经 `with_proxy` |
| 12 | Phase 2 smoke 脚本：`bash -n`、`--help`、默认 dry-run 且提示 `--run`、不得引用 `open`/`osascript`/`screencapture` |
| 13 | **浏览器启动卫生门禁**：真实仓库通过 `check-browser-launch-hygiene.sh`；smoke dry-run 必须含 `--use-mock-keychain`；合成负例（缺 mock keychain/隔离 profile）必须被拒绝；合成正例通过 |
| 14 | **产品身份/签名工具静态检查**：`check-identity-consistency.sh` 真实仓库通过、合成错配被拒；`sign-browser-cli.sh --help/--dry-run`（dry-run 不执行 codesign）；`verify-signature.sh --help` 与缺失 App 负例；`build.sh --dry-run` 包含强制签名+验收且无签名绕过 |
| 15 | 产品图标生成静态检查：`generate-icons.sh --help/--dry-run` 计划 `app.icns` + `Assets.car`；`configure.sh --dry-run` 提及图标生成；已批准源图存在 |
| 16 | 真实 HTTPS 后台检查静态：`phase2-https-background-network.sh` 的 `bash -n`、`--help`、dry-run 含 `--use-mock-keychain` 且目标为 `https://example.com/`、不得引用 `open`/`osascript`/`screencapture` |
| 17 | 真实 Cookie/隔离 profile 静态检查：`phase2-cookie-profile-isolation.sh` 的 `bash -n`、`--help`、dry-run 含 `--use-mock-keychain` 与新 mktemp profile B 阶段、不得引用 `open`/`osascript`/`screencapture` |
| 18 | Tab/Window harness 静态检查：driver `bash -n`、两个 Python 工具语法、`--help`、dry-run 含 `--use-mock-keychain`/`mktemp`/`newWindow=false` 且不执行、无可见 UI 工具、CDP harness 含 `--remote-debugging-pipe`、page server 提供 `/page` |
| 19 | window.open() popup harness 静态检查：driver `bash -n`、Python 工具语法、`--help`、dry-run 含 `--use-mock-keychain`/`mktemp`/`window.open`/不执行、无可见 UI 工具、opener 页与 `__bcliOpenChild`/`openerId` 证据齐备；共享 CDP lib 强制 pipe+mock keychain+temp profile |
| 20 | Extension window-focus harness 静态检查：driver `bash -n`、Python 工具语法、fixture manifest（MV3+tabs）有效、`--help`、dry-run 含 `--use-mock-keychain`/`mktemp`/`--load-extension`/`chrome.windows.create`/不执行、无可见 UI 工具、fixture 实现 create+update、shared launcher 以 `disable_extensions=True` 默认门控、extension harness 绝不三参数并用、SW 发现 ≤10s、deterministic id 经加载验证、调用的 cdp_pipe_lib helper 必须已导入 |
| 21 | Session-restore harness 静态检查：driver `bash -n`、Python 工具语法、`--help`、dry-run 含 `--use-mock-keychain`/`mktemp`/`--restore-last-session`/不执行、无可见 UI 工具、shared launcher 的 `no_startup_window` 参数与说明、harness 使用 restore flag + 无 `--no-startup-window` 的启动 + `Browser.close` 干净退出 + Sessions 证据、helper 导入检查 |
| 22 | Restart/crash 可见性 harness 静态检查：driver `bash -n`、Python 工具语法、watcher 以 app identity（proc_pidpath）匹配且不用截图 API、`--help`、dry-run 含 `--use-mock-keychain`/`mktemp`/`--remote-debugging-pipe`/不执行、无可见 UI 工具、异常终止仅针对身份已校验的精确 PID（无宽泛 pkill）、watcher 先于首次启动、watcher max=0 门禁、采样 10ms（≤20ms 门禁）且记录 interval、crash 前 Sessions 持久化 update+stability（timing-only）门禁、`--cycles N` 动态覆盖、JSON 提取支持 list、stability 为整段无变化窗口、crash 进程必被回收且未 kill 则跳过 recovery |
| 23 | HUMAN ownership 手动证据脚手架静态检查：driver `bash -n`、collect/sink Python 语法、ops fixture manifest（MV3+tabs+loopback）、ops 覆盖 activate/deactivate/minimize/maximize/restore/fullscreen/bounds + 后台 Window/Tab + sink 上报、`--help`、无 open/osascript/pkill/codesign/security/ninja、绝不直接执行 app 二进制（仅 runbook/ watcher identity）、prepare 只启动被动 sink/watcher 并生成 runbook、BLOCKED 矩阵（child dialog/app-shim/TabsCreate）、collector 具备 PASS/NOT-EVALUATED/CONTRADICTION 判定、runbook 命令用 bash 3.2 兼容 shell_quote 生成（含空格/单引号合成路径经 `bash -n` 与 argv 往返验证）；`state.env` 全部路径值经 shell_quote（含空格的 App 路径可被 `collect`/`stop` source）；runbook 含**用户执行、绝不自动运行**的 CLI 显式 `show` 用例（既有窗口 / 零窗口 `--no-startup-window` / 可选 blocked 负例，命令 print-only）；collector 分类 `BROWSER_CLI_USER_EXPLICIT_SHOW` producer（handled/denied/failed、managed 与 policy allow 一致性） |
| 24 | **CLI 显式 `show` producer 确定性静态 fixture（不启动浏览器）**：合成 `BROWSER_CLI_PRESENTATION` + `BROWSER_CLI_USER_EXPLICIT_SHOW` 日志、watcher 采样与 sink JSONL 喂给 `human-ownership-collect.py`；handled(existing/created)+denied 全部 PASS 且 rc=0、report.md 逐条记录 outcome、failed producer 与 handled-but-missing-policy-allow 判 CONTRADICTION（rc=1）、日志无 producer 证据时三个 CLI-show 检查必须 NOT-EVALUATED（rc=0，绝不伪通过） |
| 25 | **Foreground Assist 人工验收包（静态 + fixture，不启动浏览器）**：driver `bash -n`、collector/page-server Python 语法、`--help`、无 eval/open/osascript/签名/钥匙串调用；Developer ID preflight（`TeamIdentifier` + `codesign -dv` + 拒绝 `Signature=adhoc`）与缺失 App 负例 fail closed；`prepare --dry-run` **零写入**且 runbook 含 `--use-mock-keychain`/mktemp profile/assist request+release 开关/reason+window 参数/每用例证据词汇（grant、visible、background、timeout、user-takeover、launch-arguments-not-agent-assist）与 ABORT fail-closed 步骤；确定性 fixture：全用例 PASS + hand-back **BLOCKED**（rc=0）、未授权窗口 visible CONTRADICTION（rc=1）、用户接管后 background CONTRADICTION（rc=1）、无证据全 NOT-EVALUATED + BLOCKED（rc=0）、ABORT 标记 `session-aborted-by-user` CONTRADICTION（rc=1）；**patch 005 无回落导航合同**：`name=blocked_cold` / `name=blocked_session` 任一 page-server GET 判 `assist-blocked-url-never-navigates` CONTRADICTION（rc=1），缺 page-server 日志/目标页证据/watcher 采样时该检查 NOT-EVALUATED（rc=0，绝不假 PASS）；report.md 含 producer/policy/real-page 证据分区与 BLOCKED 矩阵；driver `windows` 列出窗口 id、driver `collect` 在合成证据目录生成 report.json/report.md |

| 26 | **Gate C 后台截图计划 + 证据 fixture（静态，不启动浏览器）**：driver `bash -n`/`--help`、默认 dry-run 计划包含 Developer ID 预检、`--use-mock-keychain`、`mktemp` profile、`--browser-cli-background-presentation`、`cdp_pipe_lib`、app-identity watcher、PNG/sha256 证据契约、视频 NOT-EVALUATED；`--run` 被显式拒绝（本切片无执行模式）；driver 无 open/osascript/screencapture/pkill/killall/codesign/ninja 且绝不执行 app 二进制；checker 无启动字面量（证据输入只读、仅写 capture-report.json/md，hygiene 干净）；checker `--self-test` **102 项** fixture 负例：窗口上屏、空白/空文件 PNG、跨用例重复像素、静态截图不一致、动态全部相同、清单哈希篡改、会话契约缺失均判 CONTRADICTION，缺证据判 NOT-EVALUATED；**PNG blank 检测先按 W3C PNG §9 逆变换 scanline filter 0..4、只对重建像素做 uniform heuristic**（filter byte 不算像素；fixture 由独立 forward-filter encoder 生成并以 W3C 公式 KAT 锁定，`raw 字节 >2 种但图像 uniform` 的陷阱必须仍判 blank）；非法 IHDR/filter byte/scanline 截断/多余/坏 zlib、**chunk CRC 损坏（IHDR/IDAT/IEND/ancillary）**、**IDAT 不连续（ancillary 夹在两个 IDAT 之间）** 判 invalid → CONTRADICTION（相邻多个 IDAT 合并解码、ancillary 在 IDAT 前/后正常处理），合法但未支持的格式（16-bit/灰度/调色板/interlace）判 unsupported → capture integrity NOT-EVALUATED（unverifiable，非 PASS）；最少截图数强制（static/dynamic/canvas≥2、long≥3，不足即 CONTRADICTION）；**固定必选矩阵由 checker 代码定义**（static/dynamic/canvas/long-scroll，删减/改名/optional 化即 CONTRADICTION，仅 video 可 optional）；**09:03 UTC watcher 证据契约**：至少 1 条可解析原始 `sample ts= count=` 且**恰好 1 条完整** `final max= nonzero_samples=`，时间戳必须可解析且严格递增——缺原始样本（final-only 绝不可 PASS）或缺 final 判 NOT-EVALUATED 且 rc=3；畸形/缺字段/重复 final/负数/时间戳不递增/最大值或非零计数与原始样本不一致判 CONTRADICTION rc=1；只有真实原始样本 max=0 + 完整一致 final 才 PASS（final.max=0 不得遮蔽 count>0 的上屏样本）；退出码语义：仅当所有必选检查 PASS（视频 optional 可 NOT-EVALUATED）才为 0，证据不足 rc=3（INSUFFICIENT-EVIDENCE），矛盾 rc=1；空证据目录经 driver `check` 返回非零，`--write-fixture` 生成的完整必选证据返回 0 |
| 27 | **Gate F 多窗口并行离线 harness（静态，不启动浏览器；无运行时执行器）**：driver `bash -n`/`--help`、默认 dry-run 打印未来 runtime 契约与证据契约（Developer ID 预检、`--use-mock-keychain`、`mktemp` profile、`--browser-cli-background-presentation`、app-identity watcher、≥3 个不同真实 HTTPS 站点、每窗口独立 active tab、Cookie/session 语义、HUMAN/ASSIST 互不影响、证据契约、**截图仅列 Gate C 依赖（BLOCKED，不宣称通过）**）；**静态源码断言**：driver 拒绝执行 flag 且无 app 启动接线（无 run helper 引用/`RUN_TOOL`/`RUN_FLAG`/`APP_OVERRIDE`），`parallel-window-cdp-check.py` 离线化（无 `cdp_pipe_lib`/`launch_app`/`run_session`/`subprocess`/`xcrun`，只暴露 `--self-test`/`--write-run-fixture`）；driver 无 open/osascript/screencapture/pkill/killall/codesign/ninja 且绝不执行 app 二进制；离线 checker 无启动字面量；checker `--self-test` fixture 负例（<3 窗口、w2 标 optional、重复窗口名/URL/active tab、非 https、watcher final-only、sample>0、cookie 模式冲突 → CONTRADICTION；缺证据 → NOT-EVALUATED；退出码 0/1/3）；helper `--self-test` 覆盖离线 subset/overall 分离与严格 watcher 规则；空证据目录经 driver `check` 返回非零、完整固定矩阵返回 0；**套件不再以执行 flag 调用任何脚本** |
| 28 | **origin-wiring 静态接线 checker 纳入统一 suite（只读，不启动浏览器）**：`user-menu-origin-wiring-check.py`（patch 010/012）、`omnibox-everywhere-origin-check.py`（patch 011）的 Python 语法（`ast.parse`）、`--self-test`（离线 fixture，要求 rc=0 **且** `SELFTEST SUMMARY: N passed, 0 failed`）、真实 Chromium checkout `--checkout "$TEST_CHROMIUM_SRC"`（复用 suite 既有 checkout 路径变量：`CHROMIUM_SRC`/`CHROMIUM_ROOT` 或默认 `~/dev/chromium/src`，不硬编码个人路径；要求 rc=0 **且** `USER-MENU-ORIGIN-WIRING: OK (N checks, 0 failed)` / `OMNIBOX-EVERYWHERE-ORIGIN: OK (N checks, 0 failed)`）；无 checkout 时打印 `SKIP`（不计 PASS）；失败时打印 checker 完整输出前 20 行 |
| 29 | **命令行意图仲裁静态 checker（patch 013，只读，不启动浏览器）**：`command-line-intent-arbitration-check.py` 的 Python 语法（`ast.parse`）、`--self-test`（离线 fixture，要求 rc=0 **且** `SELFTEST SUMMARY: N passed, 0 failed`）、真实 Chromium checkout `--checkout "$TEST_CHROMIUM_SRC"`（要求 rc=0 **且** `COMMAND-LINE-INTENT-ARBITRATION: OK (N checks, 0 failed)`；无 checkout 时 `SKIP` 不计 PASS）。checker 只读 `chrome/browser/chrome_browser_main.cc`：两个生产入口必须调用同一 `ArbitrateBrowserCliCommandLineIntent`，assist case 必须排在 show case 之前且各自调用对应 handler，两个 handler 只允许出现在仲裁 switch 内；fixture 反例强制失败：show-first 旧顺序、仅单入口仲裁、show 在仲裁前调用、case 互换、assist case 缺 handler |

第 28 组把两个关键静态接线 checker 纳入统一回归（此前只在各自任务里手工运行）：checker 自身只读源码、
不启动浏览器、不 build、不签名；suite 只以 `--checkout` 传入既有 checkout 路径变量。失败判据是
**rc=0 与精确的 `0 failed` 汇总行同时成立**，避免把含 “fail” 字样的通过明细误判；失败时逐行显示原始输出。

第 29 组守住 patch 013 的 P0 修复：命令行同时携带 USER_EXPLICIT `show` 与 assist request/release 时，
共享仲裁必须判为 assist intent，assist case 先行消费（`denied/conflicting-producer-switches`），
show producer 与 presentation gateway 永不触达；fixture 里的 show-first 旧顺序（本次修复的缺陷形态）
必须判 FAIL，防止回归。

新增脚本或安全门禁时，请在此套件中补充对应负例。

## `phase2-background-presentation-smoke.sh`（Gate A，Phase 2）

证明 BACKGROUND_PRESENTATION 第一切片：真实 BrowserWindow/Renderer 可创建并执行 JS，但 managed
后台窗口**从诞生起永不上屏**，且所有内部展示请求都有结构化拒绝日志。默认 dry-run，`--run` 才真正执行：

```sh
tests/phase2-background-presentation-smoke.sh           # 只打印计划
tests/phase2-background-presentation-smoke.sh --run     # 执行（不打开可见窗口）
```

机制：

1. 启动前用 `codesign -dv` 断言 App 为 Developer ID（team `RAXC5D3A3S`）并**拒绝 ad-hoc**；
   路径按 `config/product-identity.env` 的产品名解析（当前为 `Browser CLI.app`）；
2. `Browser CLI ... --no-startup-window --browser-cli-background-presentation --remote-debugging-port=0
   --use-mock-keychain --user-data-dir=<mktemp -d profile>`
   （不使用 `open`/Dock/Launchpad/AppleScript，不创建启动窗口；mock keychain + 临时 profile
   保证绝不触碰真实钥匙串、绝不弹密码框，**禁止复用用户 profile**）；
3. 通过 DevTools HTTP `PUT /json/new` 创建真实后台窗口，并用页面内联 JS 设置标题来证明 renderer 执行；
4. 用 `tests/tools/onscreen-window-count.m`（临时用 `xcrun clang` 编译的 CoreGraphics 小工具）
   统计该 PID 的 on-screen 窗口数，必须为 0；
5. 对**运行中进程的 cmdline** 断言 `--use-mock-keychain` 与 `--user-data-dir=<本次临时目录>` 都存在；
6. 对日志断言：窗口诞生即 suppress、内部 show 被 deny、BACKGROUND 下不存在 `allow origin=internal`；
7. SIGTERM 结束进程；失败时保留临时目录。

默认等待超时 600s（`--timeout` 覆盖）：签名后的首次启动需重新校验大量二进制签名，可能明显变慢；
脚本会区分「提前退出/僵尸」与「超时」，并每 30s 打印等待进度。

## `phase2-https-background-network.sh`（Gate A：真实 HTTPS 后台响应）

证明真实 HTTPS 站点在 BACKGROUND 下完成网络响应并产生可核验内容，同时桌面可见窗口数始终为 0：

```sh
tests/phase2-https-background-network.sh          # 只打印计划（dry-run）
tests/phase2-https-background-network.sh --run    # 单次隔离运行
```

机制与证据：

1. 启动前 `codesign -dv` 断言 Developer ID（拒绝 ad-hoc），并做网络预检（直连失败才经 `PROXY_URL`）；
2. `Browser CLI.app --no-startup-window … --use-mock-keychain --user-data-dir=<mktemp -d>
   --remote-debugging-port=0 --log-net-log=<temp>/netlog.json --net-log-capture-mode=IncludeSensitive
   --browser-cli-background-presentation`；
3. DevTools `/json/new` 打开 `https://example.com/`，`/json/list` 校验 `url` 与 DOM 标题 `Example Domain`；
4. NetLog 源站证据：`:authority: example.com` + `:scheme: https`，以及 `:status: 200` + `content-type: text/html`；
5. 用 CGWindowList 全程采样该 PID 的 on-screen 窗口数，必须始终为 0；
6. 断言策略日志（窗口诞生 suppress、内部 show deny、BACKGROUND 下无 internal allow）；
7. 优雅结束并输出证据目录 `build-logs/phase2-https-<timestamp>/`（summary/netlog/devtools-list/window-samples）。

2026-09-22 单次运行 PASS（11/11），证据：`build-logs/phase2-https-20260922-140823/`。
其中 `post-run-notes.txt` 说明：运行时的通用 `HTTP/<n> 200` 断言可能被代理 CONNECT 行满足，
已用同一份不可变 NetLog 抽取源站 200 + `text/html` 证据，脚本门禁已改为源站特异性模式。

## `phase2-cookie-profile-isolation.sh`（Gate A：Cookie 与 profile 隔离）

证明**同一临时 profile 内 Cookie 可写可读**、**不同临时 profile 不继承**，且桌面窗口始终为 0：

```sh
tests/phase2-cookie-profile-isolation.sh          # 只打印计划（dry-run）
tests/phase2-cookie-profile-isolation.sh --run    # 单次隔离调用（两个各自隔离的 mktemp profile）
```

机制：

1. 本地 `tests/tools/cookie-test-server.py`（仅 127.0.0.1、临时端口）提供
   `/set`（`Set-Cookie`，标题 `COOKIE_SET_<value>`）与 `/read`（标题 `COOKIE_READ_<value|ABSENT>`），
   并逐行记录收到的 `Cookie` 请求头；
2. 阶段 A：签名 App + `--use-mock-keychain` + 全新 mktemp profile A → `/set` 后 `/read` 必须回读到该值，
   服务器日志必须出现该 Cookie 头；
3. 阶段 B：另起全新 mktemp profile B → `/read` 必须为 `ABSENT`，服务器不得收到该 Cookie，
   B 的 Cookie 库不得有该行；
4. 全程 CGWindowList 采样（含为等待 Chromium ~30s cookie 提交定时器而保持浏览器存活的
   `--commit-wait`，默认 35s），窗口数必须始终为 0；断言策略 suppress/deny 且无 internal allow；
5. 结束后定位 profile 内的 `Cookies` 库（版本无关）并轮询持久化行；证据写入
   `build-logs/phase2-cookie-<timestamp>/`。

**当前复核状态（REVIEW）**：2026-09-22 首次运行时 harness 有两处缺陷（5 条 `$(case …)` 断言假 FAIL、
浏览器在 ~30s cookie 提交定时器前被结束）；修复后经主负责人授权**干净复跑一次，18/18 全 PASS**，
包含磁盘持久化行（`127.0.0.1|bc_cookie`）。历史失败证据保留于
`build-logs/phase2-cookie-20260922-142936/`，通过证据：
`build-logs/phase2-cookie-20260922-150327/`（含 `stdout.txt`、`rerun-notes.txt`）。
另：测试服务器现在解析真实解释器并在清场时先杀子进程，避免 depot_tools python 包装器留下的孤儿进程。

## `phase2-tab-window-creation.sh`（Gate A：AI 创建 Tab / 新 Window；**DONE（2026-09-22 主负责人复核）**）

覆盖两个相邻条目：**“AI 创建普通 Tab：后台成功且不聚焦”** 与
**“AI 创建新 Window：内部存在且桌面不可见”**。harness、runtime 与离线 reverified 均已完成，
状态见本节末尾。

```sh
tests/phase2-tab-window-creation.sh          # 只打印计划（dry-run）
tests/phase2-tab-window-creation.sh --run    # 未来的一次隔离运行
```

设计（一次隔离运行内完成两项）：

1. `tests/tools/tab-window-cdp-check.py` 以 `--remote-debugging-pipe`（fd 3/4，NUL 分隔 JSON）
   启动签名 App：`--no-startup-window --use-mock-keychain --user-data-dir=<新 mktemp>
   --browser-cli-background-presentation`，不使用 open/Dock/Launchpad，不显示/隐藏窗口；
2. `Target.createTarget({newWindow:true})` → 独立 BrowserWindow W1；
   `Target.createTarget({newWindow:false})` → Tab，必须通过 `Browser.getWindowForTarget`
   证明与 W1 同一 windowId；再建 `{newWindow:true}` → W2（windowId ≠ W1）；
3. 对每个 target `Runtime.evaluate` 校验真实页面内容（本地 HTTP `/page?name=` 的
   `BCLI_PAGE_*` 标题与 `BCLI_BODY_*` DOM 文本）、`location.href`、`document.hasFocus()`
   （必须 false = 不聚焦/不激活）与 `visibilityState`；
4. `tests/tools/onscreen-window-count.m` 在运行期间持续采样浏览器 PID 的 on-screen 窗口数，
   必须始终为 0；
5. 断言结构化策略日志：窗口创建即 suppress、`show-and-activate` 被 deny、
   BACKGROUND 下无 internal allow；
6. 证据输出 `build-logs/phase2-tab-window-<timestamp>/`（check-results/cdp-results.json/
   browser.log/page-server.log/summary/sha256）。

静态门禁：`tests/run-script-tests.sh` 第 18 组与 `tests/check-browser-launch-hygiene.sh`
（已把 `--remote-debugging-pipe` 与 Python `tempfile.mkdtemp` 纳入规则）。

**当前状态（REVIEW，主负责人待最终复核）**：修复 fd 继承后，2026-09-22 17:07 的授权运行首次真正
进入用例，CDP harness 成功：Tab 加入 W1（同 windowId）、W1/W2 独立、真实页面内容正确、
PID on-screen 全程 0、策略 suppress + deny show-and-activate 且无 internal allow。依主负责人判定，
`document.hasFocus()/visibilityState` 仅代表 WebContents 内部 active/render 状态（后台持续运行需要），
**不再作为 OS 聚焦的失败条件**；OS 不显示/不激活以 `CGWindowList=0` + native policy deny/no-allow
为权威。已按同一份 170708 原始证据离线生成 `reverified-check-results.txt`（门禁 **18/18 PASS**），
原始 `check-results.txt` 保留不覆盖。证据：
`build-logs/phase2-tab-window-20260922-170708/`；更早两次失败证据保留于 `...-164031/`、`...-165311/`。

## `phase2-window-open-popup.sh`（Gate A：网页 window.open()；**DONE（2026-09-22 主负责人复核）**）

覆盖条目 **“网页 window.open()：后台创建且不 Show/Activate”**。当前只完成 harness 与静态门禁，
**尚未 runtime**（等待与 B 线 Chromium 修改串行安排）。

```sh
tests/phase2-window-open-popup.sh          # 只打印计划（dry-run）
tests/phase2-window-open-popup.sh --run    # 未来的一次隔离运行
```

设计（复用已修好的 CDP pipe 基础设施与本地页面服务器）：

1. 新增共享库 `tests/tools/cdp_pipe_lib.py`（从已验证的 Tab/Window harness 机械提取）：
   `--remote-debugging-pipe` + fd 3/4、`--use-mock-keychain`、`--user-data-dir=`、`tempfile.mkdtemp`
   与 `close_fds=False` 修复；`tab-window-cdp-check.py` 改为引用该库（行为/CLI/结果 JSON 不变）。
2. `tests/tools/page-test-server.py` 新增 `/opener?name=parent&child=child`：真实 opener 页面，
   内含 `window.__bcliOpenChild()`（真实 JS `window.open('/page?name=child', 'bcli_child', 'width=320,height=240')`）。
3. `tests/tools/window-open-cdp-check.py`：在一次隔离启动中加载 opener 页并用 `userGesture`
   触发页面自身 `window.open()`（弹窗拦截器不是被测对象），随后取证：
   - 返回的 child WindowProxy 存活（非 blocked/closed）；
   - 新 target/WebContents（`type=page`、child URL、`BCLI_PAGE_child`/`BCLI_BODY_child` DOM）；
   - 父子 opener 关系三向核验：父页 `window.__bcliLastChild.location.href`、
     子页 `window.opener.location.href`、结构字段 `TargetInfo.openerId == parent targetId`；
   - child `Browser.getWindowForTarget`（信息）；DOM `hasFocus/visibilityState` 仅信息。
4. 权威 OS 门禁：`CGWindowList` 对浏览器 PID 持续采样必须为 0；策略日志 suppress + deny
   `show-and-activate` + BACKGROUND 无 internal allow。
5. 证据目录 `build-logs/phase2-window-open-<timestamp>/`。

静态门禁：`tests/run-script-tests.sh` 第 19 组与 `tests/check-browser-launch-hygiene.sh`
（7 入口，含共享 CDP lib）。

**当前状态（DONE，2026-09-22 主负责人复核）**：2026-09-22 18:12–18:15 单次授权隔离运行
**21/21 门禁 PASS**：真实页面 JS `window.open()` 返回存活 child WindowProxy，产生新
target/WebContents（`type=page`、`/page?name=child`、`BCLI_PAGE_child`/`BCLI_BODY_child`），
三向 opener 关系成立（父页引用、子页 `window.opener`、`TargetInfo.openerId`），
PID on-screen 全程采样 0，策略 suppress + deny `show-and-activate` 且无 internal allow；
DOM focus/visibility 仅作信息。证据：`build-logs/phase2-window-open-20260922-181544/`
（stdout/check-results/cdp-results/browser.log/page-server.log/summary/run-notes/sha256）。

## `phase2-extension-window-focus.sh`（Gate A：Extension 焦点请求；**DOING，待串行 runtime（REVIEW 前说明）**）

覆盖两个相邻条目：**“Extension `chrome.windows.create({focused:true})`：创建成功但 focus 被拒绝”**
与 **“Extension `windows.update({focused:true})`：focus 被拒绝”**。当前只完成 harness、隔离扩展
fixture 与静态门禁，**尚未 runtime**；按主负责人要求本节为 REVIEW 前说明，运行通过前不标 REVIEW/DONE。

```sh
tests/phase2-extension-window-focus.sh          # 只打印计划（dry-run）
tests/phase2-extension-window-focus.sh --run    # 未来的一次隔离运行
```

设计（未来一次隔离运行覆盖两项）：

1. 隔离扩展 fixture：`tests/tools/extensions/browser-cli-window-focus/`
   （MV3：`manifest.json` + `sw.js` + `test.html` + `test.js`；`permissions=["tabs","storage"]`）。
   `test.js` 暴露 `__bcliRunWindowFocusTest(url)`，用真实 API 依次执行
   `chrome.windows.create({url, focused:true})` 与 `chrome.windows.update(id, {focused:true})`，
   原样返回 API 结果与 window/tab 标识（不预设 API 一定 resolve 或抛错）。
2. 启动参数：共享 CDP pipe launcher（`--remote-debugging-pipe`、`--use-mock-keychain`、
   本次 mktemp `--user-data-dir`）再加 `--disable-extensions-except=<fixture>` +
   `--load-extension=<fixture>`，**仅加载本测试扩展**。共享 launcher 的 `disable_extensions`
   默认 `True`（既有三个 harness 行为不变）；本 harness 显式传 `False`，**绝不把
   `--disable-extensions` 与 except/load 三参数并用**（静态断言保证）。
3. 扩展 id：**优先**用 deterministic unpacked id（sha256(path) 映射）并**通过真实加载
   `test.html` 验证**；仅当失败才使用 ≤10s 的 service-worker 发现窗口（MV3 SW 可能不驻留），
   再用观测 id 重试并验证；结果记录 `id_source` / `id_verified` / `sw_discovery_seconds` / attempts。
4. 在扩展自身页面 `chrome-extension://<id>/test.html` 中 `Runtime.evaluate` 调用上述函数
   （`awaitPromise`），并独立用 CDP `Target.getTargets` + `Browser.getWindowForTarget` 证明
   被创建的 window/WebContents 真实存在（不依赖扩展自述）。
5. 权威 OS 门禁：`CGWindowList` 对浏览器 PID 持续采样必须为 0；策略日志 suppress（创建即抑制）+
   deny `show-and-activate` + BACKGROUND 无 internal allow。扩展返回的 `focusedFlag` 与 DOM
   `hasFocus/visibilityState` 仅作信息（模型内状态 ≠ macOS native focus）。
6. 证据目录 `build-logs/phase2-extension-window-focus-<timestamp>/`。

静态门禁：`tests/run-script-tests.sh` 第 20 组与 `tests/check-browser-launch-hygiene.sh`
（8 入口，含本 driver 与共享 CDP lib）。

**当前状态（DONE，2026-09-22 主负责人复核）**：2026-09-22 19:07 第二次（最后一次授权）单次隔离运行
**18/18 门禁 PASS**：`windows.create({url,focused:true})` → `ok=true`、`windowId=617439280`、
`tabIds=[617439281]`、`focusedFlag=False`；`windows.update(617439280,{focused:true})` → `ok=true`、
同一 windowId、`focusedFlag=False`；独立 CDP 证明被创建 target/WebContents（`type=page`、
`/page?name=ext_child`、windowId 与扩展自报一致）；deterministic 扩展 id 经真实加载验证；
实测 cmdline 无 blanket `--disable-extensions`；CGWindowList max=0；policy suppress + deny
`show-and-activate` 且无 internal allow；DOM 焦点仅信息。证据：
`build-logs/phase2-extension-window-focus-20260922-190733/`（stdout/check-results/cdp-results/
browser.log/page-server.log/summary/run-notes/sha256）；首次失败证据保留于 `...-184407/`。
未进行第三次运行。

## `phase2-session-restore.sh`（Gate A：Session restore；**DOING，待串行 runtime**）

覆盖条目 **“Session restore：恢复内部结构但不得 native show”**。当前只完成 harness 与静态门禁，
**尚未 runtime**；运行通过前不标 REVIEW/DONE。

```sh
tests/phase2-session-restore.sh          # 只打印计划（dry-run）
tests/phase2-session-restore.sh --run    # 未来的一次隔离运行（两阶段共享同一 profile）
```

Chromium 机制研究结论（来自源码，不靠猜参数）：

1. **恢复开关**：`--restore-last-session`（`switches::kRestoreLastSession`）。`StartupBrowserCreator::
   GetSessionStartupPref` 在存在该开关且 `profile->IsNewProfile()==false` 时把启动偏好强制为
   `SessionStartupPref::LAST`；因此第二阶段必须复用第一阶段已建立的同一 profile（非 new）。
2. **恢复阶段绝不能带 `--no-startup-window`**：`StartupBrowserCreator::ShouldLoadProfileWithoutWindow`
   在该开关存在时直接跳过窗口/恢复启动；恢复窗口仍由内核 presentation policy 抑制。
3. **干净退出**：CDP `Browser.close` → `ChromeDevToolsManagerDelegate::CloseBrowserSoon()` →
   `chrome::ExitIgnoreUnloadHandlers()`，其中显式 `MarkAsCleanShutdown()`（"We always mark exit
   cleanly"），因此不会进入 crash-restore 气泡路径；另加 `--hide-crash-restore-bubble` 兜底。
4. **会话文件**：`<profile>/Default/Sessions/Session_*`（含 marker；`FindLastSessionFile` 在下次启动
   选取最新有效文件）。第一阶段在关闭前轮询该文件非空作为证据，不依赖 `MoveCurrentSessionToLastSession`
   的额外触发。
5. **origin 归因现状**：`ForegroundOrigin::kSessionRestore` 枚举已存在但生产者未必已贯通，因此门禁
   只断言 suppress/deny/no-internal-allow，并单独记录实际出现的 `origin=`（信息项）。

设计（一次 driver 调用、同一新 mktemp profile、两阶段）：

1. 阶段 1：共享 CDP pipe launcher（`--remote-debugging-pipe`、`--use-mock-keychain`、本次
   `--user-data-dir`）建立 Window A（tabs `restore_a1` + `restore_a2`）与 Window B（`restore_b1`，
   均为本地真实页面），校验内容与 `Browser.getWindowForTarget` 分组，等待 `Sessions/Session_*`，
   再经 CDP `Browser.close` 干净退出。
2. 阶段 2：同一 profile + `--restore-last-session --hide-crash-restore-bubble`、**不带**
   `--no-startup-window` 重启；证明三个 target 被恢复、内容可读、原始分组（a1+a2 同 window、
   b1 不同 window）。
3. 权威 OS 门禁：两阶段 `CGWindowList` 对浏览器 PID 采样均须为 0；策略日志 suppress + deny
   `show-and-activate` + BACKGROUND 无 internal allow；DOM focus/visibility 仅信息。
4. 证据目录 `build-logs/phase2-session-restore-<timestamp>/`。

静态门禁：`tests/run-script-tests.sh` 第 21 组与 `tests/check-browser-launch-hygiene.sh`
（9 入口，含本 driver 与共享 CDP lib）。

**当前状态（DONE，2026-09-22 主负责人复核）**：2026-09-22 19:30 单次授权隔离运行 **20/20 门禁 PASS**：
阶段 1 建立 Window A(a1,a2)/Window B(b1)、真实内容正确、写入
`Default/Sessions/Session_13434550228026217`（3389 B）、`Browser.close` 干净退出（exit 0）、
窗口采样 0；阶段 2 同 profile + `--restore-last-session`（无 `--no-startup-window`）恢复三个 target、
内容与分组一致（a1/a2 同 window、b1 独立）、窗口采样 0；两阶段 policy suppress + deny
`show-and-activate` 且无 internal allow；实际 origins 仅 `internal`（`kSessionRestore` 生产者未贯通，
信息项）。证据：`build-logs/phase2-session-restore-20260922-193032/`（stdout/check-results/
cdp-results/browser-phase1,2.log/page-server.log/summary/run-notes/sha256）。

## `phase2-restart-crash-visibility.sh`（Gate A：多次启动/退出/崩溃恢复零短暂上屏；**DOING，待串行 runtime**）

覆盖条目 **“多次启动、退出和崩溃恢复均无短暂上屏”**。当前只完成 harness 与静态门禁，**尚未 runtime**；
运行通过前不标 REVIEW/DONE。

```sh
tests/phase2-restart-crash-visibility.sh          # 只打印计划（dry-run）
tests/phase2-restart-crash-visibility.sh --run    # 未来的一次隔离运行
```

设计（单次 driver 调用、同一新 mktemp profile）：

1. **消除 PID sampler 的 spawn 盲区**：新增 `tests/tools/onscreen-window-watch.m`，在任何启动**之前**
   启动，按 **app identity**（`proc_pidpath` 解析 owner 可执行路径，与目标 `Browser CLI` 主二进制精确
   匹配）连续计数 on-screen 窗口。采样间隔固定 **10ms（门禁 ≤20ms）**，driver 记录实际 interval 与
   样本数，并要求首次 launch 前已有 **≥10 个基线样本**；不使用任何截图 API、不读取窗口标题。
   覆盖 spawn → 运行 → 退出 → 崩溃 → 恢复完整时间线，输出 `sample/detail/final max=` 证据；
   driver 以 `watcher max == 0` 为门禁。
2. **预检**：启动前确认无 Browser CLI 进程（精确路径 `pgrep -f "$BIN"` 与 `pgrep -x "Browser CLI"`）。
3. **多次 clean start/close 周期**（`--cycles N`，默认 2，校验 ≥1）：签名 App + `--use-mock-keychain`
   + 同一隔离 profile；每周期创建真实页面 `cycle_N`、校验内容，经 CDP `Browser.close` 干净退出（exit 0）。
   driver 的每周期内容/退出/policy 检查与日志集合均**按 N 动态生成**（不再硬编码 cycle1/2）。
4. **一次异常终止（严格限定）**：crash 阶段先记录 `<profile>/Default/Sessions` 快照（文件集合/size/
   mtime_ns），创建并校验 `crash_marker` 页面后，轮询到 Sessions 快照出现**明确更新**，再要求一个
   **稳定窗口**（无进一步变化），只有两者都成立才允许继续；这是**持久化时序证据，明确不解析二进制
   session 文件的 URL**（结果中记录 `sessions_persistence_basis="timing-only…"`）。随后校验
   `ps -ww -o command=` 同时包含目标 app 二进制与本次 `--user-data-dir`，仅对该精确 PID 发 SIGKILL。
5. **crash recovery**：同一 profile + `--restore-last-session --hide-crash-restore-bubble`、不带
   `--no-startup-window`（源码依据：该开关关闭 `HasPendingUncleanExit` 气泡路径；restore 开关在非 new
   profile 下强制 `SessionStartupPref::LAST`）；校验 `crash_marker` 页面恢复且内容可读。
6. **权威门禁**：watcher 全程 max=0；所有阶段（N 个 cycle + crash + recovery）日志均有 policy suppress
   + deny `show-and-activate` 且 BACKGROUND 无 internal allow；DOM focus/visibility 仅信息。
7. 证据目录 `build-logs/phase2-restart-crash-<timestamp>/`（含 `watcher-samples.txt` 与各阶段 browser log）。

静态门禁：`tests/run-script-tests.sh` 第 22 组与 `tests/check-browser-launch-hygiene.sh`（10 入口）。

**当前状态（DONE，2026-09-22 主负责人复核）**：2026-09-22 20:17 第二次（最后一次授权）单次隔离运行
**23/23 门禁 PASS**：watcher 10ms / 1255 样本 / 首启前 39 基线 / **max=0 / nonzero=0**；
两轮 clean 周期真实内容且 exit 0；crash 前 Sessions update=True 且连续 2s 无变化（timing-only）；
cmdline 校验后对精确 PID SIGKILL（exit -9）；同 profile + `--restore-last-session
--hide-crash-restore-bubble` 恢复 `crash_marker` 内容（exit 0）；全阶段 policy suppress + deny +
无 internal allow。证据：`build-logs/phase2-restart-crash-20260922-201728/`
（stdout/check-results/cdp-results/watcher-samples/browser-*.log/summary/run-notes/sha256）；
首次失败证据保留于 `...-200358/`。未进行第三次运行。

## `phase3-human-ownership-manual.sh`（Gate B：USER_EXPLICIT/HUMAN per-window 证据脚手架；**仅证据，不自动打开窗口**）

为 Gate B（USER_EXPLICIT / HUMAN 所有权）提供**手动在环**的验收脚手架：脚本自身
**绝不启动/打开/激活/点击/显示 Browser CLI**，也不使用 `open`/`osascript`/AppleScript；所有浏览器动作由
用户手动完成，脚本只负责准备环境、被动采集与离线判定。

```sh
tests/phase3-human-ownership-manual.sh prepare --dry-run   # 只打印阶段/下一步预览与 runbook，不创建任何东西
tests/phase3-human-ownership-manual.sh prepare    # 生成证据目录/临时 profile/runbook，启动被动 watcher+sink
tests/phase3-human-ownership-manual.sh ready      # 只读阶段检查：prepare/signed/process-spawned/browser-ready（本次 profile 绑定）
tests/phase3-human-ownership-manual.sh ready --wait  # 轮询到本次 profile READY（开发构建首次签名冷启动约 3 分钟 dyld；超时非零）
# （用户按 runbook 手动操作浏览器）
tests/phase3-human-ownership-manual.sh collect    # 只读采集并生成 report.json/report.md
tests/phase3-human-ownership-manual.sh stop       # 按记录 PID 停止被动 helper；不碰用户浏览器
```

设计要点：

1. **隔离与卫生**：`prepare` 用 `mktemp -d` 生成独立 profile；runbook 中的手动启动命令固定
   `--user-data-dir=<该 profile> --use-mock-keychain --no-first-run --remote-debugging-port=<free>
   --enable-logging=stderr --disable-extensions-except=<ops fixture> --load-extension=<ops fixture>
   --browser-cli-background-presentation`，绝不使用真实 profile/钥匙串；启动命令只打印、不执行。
   `prepare` 输出为**短格式**：五阶段状态 + 3 条下一步 + “Known-unsupported follow-up areas 是信息项、
   不是报错”的说明；完整 runbook 只在 `--print-runbook` 时打印。
2. **readiness 阶段（`tests/tools/browser-readiness-check.py`，只读）**：明确区分
   `prepare` → `signed` → `process-spawned` → `browser-ready`。`browser-ready` **只认本次 profile**：
   其主进程存在（cmdline 同时含 app 二进制与本次 profile，且带 `--remote-debugging-port`）+ 该端口
   应答（只读 GET `/json/version`）+ **监听者属于本次 profile 主进程**（`lsof` owner，必要时用
   `<profile>/DevToolsActivePort` 校验）；无法把 DevTools 绑定到本次 profile 时输出 **BLOCKED 并
   非零退出（fail closed）**。**app-wide watcher 采样只作 INFO**，绝不单独触发成功或宣称本次窗口可见
   （旧 profile 浏览器常驻窗口不得造成假通过）；`--wait` 仅在本次 profile READY 时返回 0，**超时非零并
   记录 TIMEOUT**。首个 Developer ID 签名 component build 冷启动在 dyld 映射约 500 dylib 阶段约 3 分钟，
   这是**本开发构建已观测到的性能问题（不是正常、也不是可接受的产品行为）**。工具只读
   `ps`/`lsof`/`codesign -dv`（60s 熔断）/state.env/watcher 采样，绝不启动/点击/显示，也不碰钥匙串；
   `readiness.jsonl` 记录阶段与 spawn→ready 耗时。fixture 单测：
   `python3 tests/tools/browser-readiness-check.py --self-test`。
3. **会话互不伤害（强制）**：`prepare` 在创建任何东西之前用只读 bind 预检 `--sink-port`
   （以及显式给出的 `--port`），**占用即 fail closed 并提示换端口或由对方会话自行 stop，绝不杀别人的
   helper**；若 `prepare` 在启动 helper 之后失败（例如 signed 阶段 FAIL），EXIT trap 会按**精确 PID +
   cmdline 身份校验**停掉它自己启动的 watcher/sink，不留残留；`stop` 同样只用精确 PID+身份校验，
   禁止 `pkill`/`killall`/广泛匹配。
4. **被动采集（无窗口）**：启动 `onscreen-window-watch.m`（10ms、app identity）与本地结果 sink
   （`ops-result-sink.py`，127.0.0.1 被动 HTTP）；二者都不打开窗口、不接触浏览器。
5. **证据可区分来源**：policy 日志解析出 `origin=user-explicit/extension/internal/session-restore…`、
   `decision=allow/deny`、`action=show-and-activate/minimize/maximize/restore/deactivate/fullscreen/
   set-bounds/set-z-order`、`owned=yes/no`、`owned_windows=N`、`transition=human-ownership-granted/
   released` 与 `window_id`；extension 侧证据来自手动 ops 页写入 sink 的 API 结果与
   `chrome.windows.getAll` 清单（模型层，明确标注）。
6. **人工覆盖矩阵**（runbook 明确列出）：Dock/Launchpad reopen；用户原生最小化/恢复/全屏；
   Extension 对 HUMAN 窗口的 activate/deactivate/minimize/maximize/restore/fullscreen/bounds
   尝试必须被拒绝；HUMAN 存在时另建后台 Window/Tab。
7. **CLI 显式 `show`（用户执行，绝不自动运行）**：runbook 追加直接运行 app 二进制的两个正例——
   第 8 步“运行中的既有窗口”（第二终端触发 ProcessSingleton 转发，期望
   `result=handled reason=existing-window-presented`）与第 9 步“零窗口 / `--no-startup-window`”
   （期望 `result=handled reason=window-created-and-presented`，恰好创建一个窗口且无 profile
   picker/session restore）；另附第 10 步可选负例（show + URL 位置参数，必须
   `result=denied reason=launch-arguments-not-user-explicit`，绝不进入 USER_EXPLICIT）。
   这些命令只打印、由用户粘贴执行，脚本不 `eval`、不代运行。
8. **判定（fail-closed）**：`human-ownership-collect.py` 输出
   `PASS / NOT-EVALUATED / CONTRADICTION / BLOCKED`——无证据即 `NOT-EVALUATED`，绝不伪通过；
   发现 `allow(automation, owned=yes)` 或 `allow(internal, state=background)` 判 `CONTRADICTION` 并
   以 rc=1 结束；watcher max≠0 同样 `CONTRADICTION`。CLI show 生产者日志单独分类：handled 但缺失
   `origin=user-explicit` policy allow、`failed`、或 managed 字段与结果不符均判
   `cli-show-producer-consistency` `CONTRADICTION`；完全无 producer 证据时只报
   `NOT-EVALUATED`。
9. **BLOCKED/后续矩阵**：child dialog 归因（`window.open()` 子对话框/alert sheet）、远程 app-shim
   （PWA）窗口、`chrome.tabs.create` 异步归因均标 **BLOCKED**，只能以显式 policy 证据接受，
   否则保持 `NOT-EVALUATED`。

静态门禁：`tests/run-script-tests.sh` 第 23/24 组与 `tests/check-browser-launch-hygiene.sh`
（12 入口；readiness 工具是只读检查、不构成启动入口）。本脚手架**不运行 runtime**；
`prepare/ready/collect/stop` 由用户在手动会话前后调用。

**当前状态（REVIEW，可见 runtime/人工 Gate B 未执行）**：CLI `show` 生产者源码（patch 003）已有
主负责人单测/构建证据（`user_explicit_show_unittests` 14/14 PASS、policy 12/12、AppKit 2/2、
reopen 8/8、`autoninja -C out/Dev chrome` exit 0），但**尚无真实运行中实例的 CLI show 呈现、
Dock/Launchpad 点击或 HUMAN 隔离的可见证据**；runbook 的 CLI show 步骤（含零窗口重启）必须由
用户实际执行并跑 `collect` 后才能把该项转 DONE。

## `phase4-foreground-assist-manual.sh`（Foreground Assist 人工验收包；**仅证据，不自动打开窗口**）

为 Foreground Assist（Slice 2：生产者 + host 接入 + 超时归还）提供**手动在环**的配套验收包：
脚本自身**绝不启动/打开/激活/点击/显示 Browser CLI**，不使用 `open`/`osascript`/AppleScript；
所有浏览器动作由用户手动完成，脚本只负责准备环境、被动采集与离线判定。与工作线 B 的
`phase3-human-ownership-manual.sh` 并存，不修改其文件。

```sh
tests/phase4-foreground-assist-manual.sh prepare --dry-run   # 只打印 runbook（零写入、不检查签名）
tests/phase4-foreground-assist-manual.sh prepare             # 生成证据目录/临时 profile/runbook，启动被动 watcher+page server
tests/phase4-foreground-assist-manual.sh windows             # 只读：列出日志中的 window id（填 --window= 用）
# （用户按 runbook 手动操作浏览器）
tests/phase4-foreground-assist-manual.sh collect             # 只读采集并生成 report.json/report.md
tests/phase4-foreground-assist-manual.sh stop                # 按记录 PID 停止被动 helper；不碰用户浏览器
```

设计要点：

1. **卫生与签名（强制）**：`prepare`（非 dry-run）先做只读 `codesign -dv` 断言 pinned
   Developer ID（`TeamIdentifier=RAXC5D3A3S`），**拒绝 ad-hoc** 并要求 `scripts/build.sh` 重签；
   runbook 中所有命令固定 `--use-mock-keychain --user-data-dir=<fresh mktemp>`，绝不使用真实
   profile/钥匙串。任何系统弹窗 → 用户立即停止、不点击、`touch $EVIDENCE_DIR/ABORT`（collector
   判 `session-aborted-by-user` CONTRADICTION，fail closed）。
2. **被动采集（无窗口）**：`onscreen-window-watch.m`（10ms、app identity）+ 本地
   `assist-page-server.py`（仅 127.0.0.1，逐请求记录 `path=`，提供 `BCLI_PAGE_*` 真实页面）；
   二者都不打开窗口、不接触浏览器。
3. **人工覆盖矩阵**（runbook 逐步给出命令与期望证据）：
   - **A 冷启动 assist+URL 负例**：producer `denied reason=launch-arguments-not-agent-assist`，
     先于任何 grant，且无 `assist-window-visible`；**blocked URL 不得产生任何 page-server GET**
     （patch 005 合同：cold-start sanitizer 去掉 URL、ProcessSingleton handler 消费请求，绝不回落
     普通导航）；
   - **C grant 只显示目标 Window**：恰好目标窗口出现（`assist-lease-granted` +
     `origin=agent-assist ... state=assist leased=yes` + `assist-window-visible` 同一 window id）；
   - **D release/timeout 回后台**：agent release 与 TTL 超时分别产生
     `assist-lease-released reason=agent-release|timeout` + `assist-window-background`；
   - **E 用户接管后旧 TTL 不 hide**：Dock/Launchpad 接管产生 `reason=user-takeover` +
     `human-ownership-granted`，其后**不得**出现该窗口的 `assist-window-background` 或再次 grant；
   - **B ProcessSingleton blocked 请求不污染 HUMAN**：HUMAN 存在时的 blocked assist+URL 不得产生
     该窗口的 lease/visible/background/release，`owned=yes` 保持不变，且不得出现
     `name=blocked_session` 的 page-server GET。
4. **判定（fail closed）**：`foreground-assist-collect.py` 输出
   `PASS / NOT-EVALUATED / CONTRADICTION / BLOCKED`；顺序敏感（日志行序即时间序），无（有序）证据
   即 `NOT-EVALUATED`；未授权窗口 visible、HUMAN 接管后 background/regrant、producer 不一致
   （failed / handled 无对应 policy grant / managed 不符）、**blocked_cold/blocked_session 任一
   page-server GET（无回落导航合同）** 均判 `CONTRADICTION` 且 rc=1；watcher max>1 同样
   CONTRADICTION。`assist-blocked-url-never-navigates` 在缺少 producer denied 行、page-server 日志
   不可读、日志无 `name=target`（无法证明 server 当时在记录）或无 watcher 采样时一律
   `NOT-EVALUATED`，绝不假 PASS。
5. **BLOCKED/后续矩阵**：用户“交还 AI”生产者尚未实现（PROJECT-PROGRESS C），旧 TTL 在交还后的
   子检查保持 **BLOCKED**；冷启动 vs ProcessSingleton 的入口归因未进日志，标 BLOCKED 并说明两条
   入口共用同一分类器；对话框只能由用户 ABORT 标记 fail closed，脚本无法自行检测。

静态门禁：`tests/run-script-tests.sh` 第 25 组与 `tests/check-browser-launch-hygiene.sh`
（当前 12 入口，含本 driver）。本包**不运行 runtime**；`prepare/collect/stop` 由用户在手动会话
前后调用。

**当前状态（REVIEW，人工 runtime 未执行）**：静态与 fixture 证据齐备（见第 25 组），本机当前
`out/Dev/Browser CLI.app` 为 **ad-hoc**（`Signature=adhoc`、`TeamIdentifier=not set`），因此
`prepare` 会按设计 fail closed 拒绝；人工验收前必须先用 `scripts/build.sh` 产出 Developer ID
签名构建。判定合同已按 patch 005 收紧：**blocked assist URL 必须零 page-server GET**（无回落
导航）；当前 Chromium 源码已落地 cold-start sanitizer（`CommandLineForColdStartStartup`）与
ProcessSingleton handler-first 消费，实际行为仍待 runtime 核验（本轮未运行浏览器）。待人工
执行项：A/C/D(release+timeout)/E/B 五个用例的真实窗口与日志证据，以及 `report.json/report.md`
的 PASS 判定。

## `phase5-background-capture.sh`（Gate C 后台截图验收 harness；**本轮仅计划/离线检查，无执行模式**）

为 Gate C（BACKGROUND_CAPTURE：窗口从未 Show + 后台 surface + 截图正常）提供默认 dry-run 的验收
harness。**本切片没有执行模式**：不启动浏览器、不显示/激活/最小化任何窗口、不接受 `--run`
（显式拒绝且 rc=1），**Gate C 未被通过**。

```sh
tests/phase5-background-capture.sh                        # 默认 dry-run：打印执行计划与证据契约
tests/phase5-background-capture.sh check --evidence DIR   # 校验未来会话证据（证据输入只读；checker 仅写出 capture-report.json/md）
python3 tests/tools/capture-evidence-check.py --self-test # fixture 负例（102 项，含 PNG filter 0..4 KAT 与 chunk CRC/IDAT 连续性）
```

设计要点：

1. **执行要求（留给后续切片）**：Developer ID 签名预检（拒绝 ad-hoc）、`--use-mock-keychain` +
   新 `mktemp` profile、`--browser-cli-background-presentation`、启动前先开 app-identity watcher
   （10ms，全程 on-screen 计数必须 0）、截图只经浏览器自身 CDP surface/renderer 路径
   （remote debugging pipe，复用 `tests/tools/cdp_pipe_lib.py`），**禁止 show/activate/reveal/
   minimize/temporary-normal 补救**（policy 日志出现任何 presentation allow 即 CONTRADICTION）。
2. **一次隔离会话覆盖**：static HTML（2 张，哈希应相同）、dynamic JS/CSS 动画（≥2 张，哈希必须不同）、
   Canvas（≥2 张，必须不同）、长页（≥3 张滚动步进，必须不同）；**视频为 optional**——本轮没有确定性
   本地素材，固定 **NOT-EVALUATED**，绝不假 PASS。
3. **证据契约**（`<evidence>/`）：`watcher-samples.txt`（时间序列）、`browser.log`
   （`BROWSER_CLI_PRESENTATION` policy/host/appkit）、`capture-plan.json`（会话契约 + 用例）、
   `captures.json`（case/kind/file/sha256/size/captured_at/scroll_index）、`captures/*.png`（原始 PNG）、
   `page-content.json`（真实页面 title/body/url）。
4. **判定（fail closed）**：`capture-evidence-check.py` 输出 PASS / NOT-EVALUATED / CONTRADICTION；
   拒绝的假通过包括：watcher max>0（窗口上屏）、空/空白/过小 PNG、sha256/size 与清单不符、
   跨用例像素重复、dynamic/canvas/long-scroll 全部相同（未捕捉到变化）、static 截图不一致、
   **截图数量不足（static/dynamic/canvas 需 ≥2、long-scroll 需 ≥3）**、**capture-plan.json 删减/
   改名/optional 化四个固定必选用例**（矩阵由 checker 代码定义，仅 video 可 optional）、
   **watcher sample 最大值与 final.max 不一致**（raw samples 为准，final.max=0 不得遮蔽上屏）、
   任何 presentation allow 或 AppKit allow、会话契约（签名 + mock keychain + managed）不成立；
   缺证据一律 NOT-EVALUATED。
   **PNG 有效性（blank 检测）**：先校验 IHDR（合法 color type/bit depth/compression/filter/
   interlace）、IHDR/IDAT/IEND chunk 结构、**每个 chunk 的 CRC（覆盖 chunk type+data，§5.5）**
   与 zlib 流，再按 **W3C PNG Third Edition §9** 逐 scanline 逆变换 filter 0..4（Sub/Up/
   Average/Paeth，bpp 按通道、模 256），最后在**重建后的像素字节**上做 uniform heuristic——
   filter byte 绝不算作像素；scanline payload 必须精确等于 `height*(1+width*channels)`，
   截断/多余、filter byte 不在 0..4、非法 IHDR 字段、坏 zlib、**任一 chunk CRC 不匹配、
   多个 IDAT 之间夹入 ancillary（§5.6/§11.2.3）** 一律 **invalid → CONTRADICTION**（chunk 数据
   被改坏但 zlib/scanline 仍可解码时也必须 fail closed）；多个相邻 IDAT 合并为一个 zlib 流、
   ancillary 在 IDAT 前/后仍正常处理；**合法但不在已核实截图子集内的 PNG（非 8-bit RGB/RGBA、
   interlaced、超大）一律 unsupported → capture integrity NOT-EVALUATED（unverifiable），
   绝不猜测为非 blank，也绝不 PASS**。Chromium `Page.captureScreenshot` 的 PNG 由
   `SkPngRustEncoder` 生成（`Filter::Up` 或 `Filter::Adaptive` 0..4），因此必须支持全部五类 filter。
   **退出码**：仅当所有必选检查 PASS（视频 optional 可为 NOT-EVALUATED）才返回 0；存在矛盾返回 1；
   证据不足返回 3（INSUFFICIENT-EVIDENCE）。checker 以只读方式读取证据，仅写出 `capture-report.json/md`。
5. **静态门禁**：`tests/run-script-tests.sh` 第 26 组与 `tests/check-browser-launch-hygiene.sh`
   （当前 13 入口，含本 driver；checker 无启动字面量，证据输入只读、仅写报告文件）。

**当前状态（REVIEW，Gate C 未通过）**：本切片只有 dry-run 计划与 fixture/静态证据；
2026-09-23 13:20 CST 主负责人审查发现判定器两处假通过（最少截图数未强制、全部 NOT-EVALUATED 时
返回 0），已修正并新增 fixture 负例——**Gate C 判定器第 1 次返工**；
2026-09-23 13:35 CST 第 2 次审查发现两处假通过（必选清单取自证据自带 plan、final.max 遮蔽
sample 上屏），已改为**代码内固定必选矩阵**与 **sample/final 上界一致性判定**并新增对应负例——
**第 2 次返工**；
2026-09-23（本轮定向任务）主负责人定位 **blank 检测根因**：原实现只对 zlib 解压后的 IDAT 流做
字节频率统计，把每行 scanline 的 filter byte 与过滤后的字节当成像素（fixture 只造 filter=0，
无法证伪该错误）。已实现标准子集 PNG 解码：IHDR 合法性 + filter 0..4 逆变换 + 精确 scanline
payload/截断/多余/非法 filter 的 fail-closed 判定 + 未支持格式 unverifiable；fixture 改由
**独立 forward-filter encoder** 生成（含 W3C 公式 KAT、uniform/non-uniform × 5 filter × RGB/RGBA、
raw 字节陷阱与 unsupported/invalid 负例），self-test **90/90**——**Gate C 判定器第 3 次修正**；
2026-09-23 主负责人定向复核发现两个 PNG 结构缺口（chunk CRC 从未读取比对；IDAT 连续性未检查），
已最小修正：每个 chunk 校验 CRC（覆盖 chunk type+data，§5.5）、IDAT 必须连续（§5.6/§11.2.3），
新增 IHDR/IDAT/IEND/ancillary CRC 损坏、split IDAT 合并、ancillary 夹在 IDAT 之间等正/反例，
self-test **102/102**——**本子项第 1 次复核修正（REWORK 1）**；
**runtime 仍缺**：签名 App 的隔离会话执行器（CDP pipe 截图 + 素材页 + watcher 接线）、
真实 PNG/时间序列证据与 `capture-report.md` 的 PASS 判定；视频素材（确定性本地）未构建，
固定 NOT-EVALUATED。

## `phase6-parallel-windows.sh`（Gate F 多窗口并行验收 harness；**纯离线：无运行时执行器**）

为 Gate F（后台多窗口并行）提供离线 harness。**本切片没有任何运行时执行器，也未获授权**：不启动
浏览器、不显示/激活/最小化任何窗口、**没有 `run`/执行模式**，执行 flag 一律 fail-closed 拒绝（rc=1）；
脚本不再引用任何 app 启动 helper，`tests/tools/parallel-window-cdp-check.py` 也只剩纯证据函数与
fixture（无 launch/subprocess/签名执行/CDP 导入）。**Gate F 未被通过**；未来 runtime 必须在 gate
（Gate C 仍 BLOCKED）之后由新的显式任务实现，本切片不描述也不祝福任何可执行路径。

```sh
tests/phase6-parallel-windows.sh                        # 默认 dry-run：打印未来 runtime 契约与证据契约
tests/phase6-parallel-windows.sh check --evidence DIR   # 离线校验证据（证据输入只读；仅写 parallel-report.json/md）
python3 tests/tools/parallel-evidence-check.py --self-test     # 离线 checker fixture 负例
python3 tests/tools/parallel-window-cdp-check.py --self-test   # 纯证据函数 fixture（离线）
```

设计要点：

1. **未来 runtime 契约（本切片不实现、不授权）**：Developer ID 签名预检（拒绝 ad-hoc）、
   `--use-mock-keychain` + 新 `mktemp` profile、`--browser-cli-background-presentation`、启动前先开
   app-identity watcher（10ms，**原始样本必留**，全程 on-screen 计数必须 0）、经 CDP pipe 创建
   **≥3 个后台 BrowserWindow**，每个加载**不同的真实 HTTPS 站点**并拥有**独立 active tab**；
   任何窗口不得被 present/activate/reveal（无 show/reveal/minimize 补救）。以上仅是文字契约，
   代码中没有对应执行入口。
2. **Cookie/session 语义**：按 plan 声明的 `cookie_mode` 校验——`shared-profile`（同一 jar、每窗口可回读）
   或 `isolated-profiles`（jar 互异、无交叉回读）。
3. **HUMAN/ASSIST 互不影响**：用户手动接管一个窗口时，其余后台窗口的 URL/tab 保持不变，
   且不出现针对它们的 presentation allow。
4. **证据契约**（`<evidence>/`）：`parallel-plan.json`（会话契约 + cookie_mode + 窗口矩阵）、
   `windows.json`（name/window_id/url/active_tab_id/loaded）、`page-content.json`（真实页面 title/body）、
   `cookie-evidence.json`（jar_id/readback）、`human-assist.json`（接管窗口 + 逐窗口 untouched）、
   `watcher-samples.txt`（**原始 10ms 样本** + final max=）、`browser.log`（policy 日志）。
5. **判定（fail closed，吸取 Gate C 教训）**：固定窗口矩阵（w1/w2/w3 background + 真实 HTTPS + 独立 tab）
   **定义在 checker 代码内**，删减/改名/optional 化即 CONTRADICTION；**watcher 必须存在原始样本**，
   final-only 的 `max=0` 不是证据（CONTRADICTION），sample 最大值与 final 必须一致且为 0；重复窗口
   id/URL/active tab、非 https、cookie 模式冲突、HUMAN/ASSIST 期间后台窗口被触碰均 CONTRADICTION；
   缺证据一律 NOT-EVALUATED。退出码：仅当所有必选检查 PASS 才 0，矛盾 1，证据不足 3。
6. **截图**：**不在本契约内**——逐窗口截图仍是 Gate C 依赖（Gate C 离线判定器 **BLOCKED**，返工待决），
   本切片不宣称截图覆盖、也不得据此声称 Gate F 通过。
7. **静态门禁**：`tests/run-script-tests.sh` 第 27 组与 `tests/check-browser-launch-hygiene.sh`
   （含本 driver；第 27 组改为**静态源码断言**：driver 拒绝执行 flag、无 app 启动接线，helper 无
   launch/subprocess/签名执行且只暴露 `--self-test`/fixture 参数；套件不再以执行 flag 调用任何脚本）。

**当前状态（REVIEW，Gate F 未通过；无运行时执行器）**：本切片只有 dry-run 计划、离线 checker 与纯
fixture/静态证据；**没有运行时证据，也没有可执行入口**。**未来 runtime（须新任务、gate 之后）仍需的明确验收条件**：

1. 签名 App + `--use-mock-keychain` + 新 mktemp profile + managed 开关，watcher（10ms）在启动前运行并保留原始样本；
2. ≥3 个后台 BrowserWindow 并行加载不同真实 HTTPS 站点（window id 互异），全程 `CGWindowList`/app-identity 计数 0，policy 无 presentation allow；
3. 每窗口独立 active tab（tab id 互异），激活某窗口的 tab 不影响其它窗口、不使窗口上屏；
4. Cookie/session 证据符合声明的 profile 模式（共享回读或隔离不串）；
5. 用户手动接管一个窗口时，其余后台窗口 URL/tab 不变且无 presentation allow；
6. 证据目录完整并经 `phase6-parallel-windows.sh check` 得 rc=0；
7. 逐窗口截图：待 Gate C 解除 BLOCKED 后单独验收（本契约不覆盖）；
8. CPU/GPU/内存与长期运行数据：PROJECT-PROGRESS F 区独立条目，不在本契约内。

## `check-browser-launch-hygiene.sh`（静态门禁，强制）

扫描 `tests/ scripts/ cli/ launcher/ browserpilot-integration/` 下所有会启动
`Chromium.app/Contents/MacOS/Chromium` 或 `Backlight.app` 的 shell/python/js 入口；缺少
`--use-mock-keychain` 或缺少 `mktemp` 隔离的 `--user-data-dir` 即 FAIL：

```sh
tests/check-browser-launch-hygiene.sh            # 0 = 全部合规；1 = 有违规
tests/check-browser-launch-hygiene.sh --root DIR # 自测/合成目录
```

任何新增测试入口都必须先通过该门禁。规则根因与生产约束见 [`../AGENTS.md`](../AGENTS.md)：
**生产 Browser CLI 不得使用 mock keychain**，正式版依靠稳定签名 + 稳定安装路径。

## `check-identity-consistency.sh`（静态门禁，强制）

校验唯一身份配置源 `config/product-identity.env` 与 `chromium/args.gn`（`branding_file_path`）以及
Chromium checkout 中 `chrome/app/theme/browser_cli/BRANDING` 完全一致；checkout 未打 patch 002 时
对应项 SKIP：

```sh
tests/check-identity-consistency.sh
```

## `user-menu-origin-wiring-check.py`（patch 010 静态接线检查，无浏览器）

静态、只读地验证 patch 010 的两条接线（纯 C++ 单测看不到的部分）：
`executeCommand:`/`showPreferencesForProfile:`/`orderFrontStandardAboutPanelForProfile:`
三个 AppKit 菜单回调使用 `ScopedForegroundOrigin(kUserExplicit)`（而非 kInternal）；
`TabRestorer::RestoreByID` 按值捕获 `views::CurrentForegroundOrigin()` 传给 `TabRestorer`；
`TabRestorer::TabRestoreServiceLoaded` 用保存的值经共享 helper
`RunWithForegroundOrigin()` 包住 `DoRestoreTab`；头文件保存 typed
`views::ForegroundOrigin`（无 int 往返、用 opaque scoped-enum 前置声明避免 ui/views 依赖）。
只读取 `chrome/browser/app_controller_mac.{h,mm}` 文本，不启动浏览器/不写文件/不访问
profile/keychain。**尚未接入 `run-script-tests.sh`**（该套件正由工作线 A 修改中），
主负责人可在其收敛后把 `--self-test` 加入静态组。

```sh
python3 tests/tools/user-menu-origin-wiring-check.py --checkout ~/dev/chromium/src  # 7 checks
python3 tests/tools/user-menu-origin-wiring-check.py --self-test   # 8 个 fixture 正/负例
```

## `omnibox-everywhere-origin-check.py`（patch 011 静态接线检查，无浏览器）

静态、只读地验证 patch 011（Omnibox Everywhere native activation 归因）：
`mac_window_util.mm::ActivateBrowserWindowOnMac` 必须先经纯函数
`DecideWindowActivationRoute`；managed + 无来源 → `kNoop`（不 activate/不
deminiaturize/不 order-front），managed + 已证明浏览器进程用户输入 → 经
`NativeWidgetMacNSWindowHost::PresentForUserExplicitAction()` 唯一 gateway，只有 stock
才落到 raw AppKit；`omnibox_everywhere_service.cc::OpenUrl` 必须把 `user_gesture`
按值转发且自身没有 raw AppKit；`omnibox_everywhere_ui_manager.cc` 的
`OpenURLFromTab`（WebUI/渲染回调）必须传 `user_gesture=false`，`ExecuteCommand` 的
4 个原生 context-menu 来源必须传 `true`；并由纯逻辑单测
`mac_window_util_unittest.cc` 覆盖 route 矩阵。只读源码文本，不启动浏览器/不写文件/
不访问 profile/keychain。**尚未接入 `run-script-tests.sh`**（该套件正由工作线 A 修改中）。

```sh
python3 tests/tools/omnibox-everywhere-origin-check.py --checkout ~/dev/chromium/src  # 6 checks
python3 tests/tools/omnibox-everywhere-origin-check.py --self-test   # 8 个 fixture 正/负例
```

## 签名相关脚本

- `scripts/sign-browser-cli.sh`：inside-out 强制签名；`--dry-run` 只做身份预检并打印顺序，
  不执行 codesign、不启动 App；本机缺少固定身份时 exit 3（不降级 ad-hoc）。
- `scripts/verify-signature.sh`：只读验收（Bundle ID 派生、`--deep --strict`、Team ID、非 ad-hoc、
  designated requirement）；不启动 App、不访问钥匙串。
- smoke / verify-phase1 在启动 App 前会拒绝 ad-hoc 构建（见 `AGENTS.md` §3）。

2026-09-22 Gate A smoke 记录：

- `build-logs/phase2-smoke-20260922-0945.log`：早期未加 mock keychain 的运行（历史，勿模仿）；
- `build-logs/phase2-smoke-isolated-20260922.*`：加 mock keychain 后的隔离运行；
- `build-logs/phase2-smoke-signed-20260922.{log,run.txt}`：Developer ID 签名后的 `Browser CLI.app`
  运行，**7/7 PASS**，无系统弹窗（首次冷启动超时后提高默认超时并重试一次，原因见文件内说明）。

## 边界

- 不在此目录实现产品功能，不引入第二套窗口可见性逻辑。
- 测试绝不读写真实钥匙串、绝不触发系统对话框：任何浏览器启动必须带 `--use-mock-keychain`
  与隔离临时 `--user-data-dir`，并由 `check-browser-launch-hygiene.sh` 强制；出现一次弹窗即 FAIL。
- 后续 Gate A/B/C 测试必须能判定「全程零可见」；禁止把 watchdog 事后隐藏当作成功（设计 §5.1、§14）。
- smoke 允许短暂运行 Chromium 进程（无窗口）；如不希望出现 Dock 图标，请在用户明确授权后再运行。
- `check-browser-launch-hygiene.sh` 允许**仅启动 Windows 二进制**的入口显式声明豁免：在源文件里写
  一行注释 `# browser-launch-hygiene: windows-only (no keychain)`。理由：`AGENTS.md` §1 的
  `--use-mock-keychain` 是 macOS 钥匙串 ACL 的绕过手段，Windows 用 DPAPI，该开关在那里既无意义、
  生产环境又被禁止（§1.5）。声明是显式且可评审的，不做启发式推断；**隔离临时 profile 的要求
  对 Windows 入口同样强制**，并由 fixture 保证「不声明仍会被拦」「声明了但缺 profile 仍会被拦」。

## Windows 宿主机上的已知差异

`tests/run-script-tests.sh` 在 Windows（Git Bash）上会有 15 项 FAIL，全部来自 **macOS 专属脚本**，
与 Windows 移植无关：

- `scripts/setup-mac.sh` 的 dry-run 断言（gclient sync / `--detach` / 代理）；
- `scripts/preflight.sh` 的磁盘熔断阈值；
- `scripts/generate-icons.sh` 的 `.icns` + `Assets.car` 计划；
- `scripts/prepare-app.sh` 的 Developer ID 预检。

这些脚本在非 macOS 上主动拒绝执行，属于设计行为。Windows 侧的对应门禁是
`tests/tools/test_windows_scripts.py`（24 项）与 `scripts/verify-windows.sh`（6 步），两者都必须全绿。
若 `python3` 在 PATH 中指向 Chromium 自带的 hermetic 解释器（无第三方包），
请用系统 Python 运行 Python 测试；`scripts/configure-windows.sh` 会自行挑选能 `import PIL` 的
解释器来生成图标。
