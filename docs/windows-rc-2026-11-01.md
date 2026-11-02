# Windows RC 验收汇总（2026-11-01）

状态：**Draft / 未发布**。所有 PR 保持 Draft；未 merge。构建为
`D:\c\src\out\WinRelease`（非 component）与 `out/WinDev`，Chromium 156.0.8067.0；
WinRelease patch 001–036（含 TSF field-trial、GPU 034/035、hidden 036）。

## 1. Release-like E2E（A）

新增独立 `scripts/test-release-candidate-e2e.mjs`（BrowserPilot PR #6，依赖 PR #1）：

- **14/14 case PASS，210/210 checks PASS，verdict PASS**。
- Normal 6/6：默认 out-of-process GPU、持久独立测试 profile、无全局 remote debugging、
  无 managed；BrowserPilot + Native Host。
- Agent 8/8：默认 out-of-process GPU、managed presentation、独立 profile、Feature-on 仲裁。
- 明确无 `--in-process-gpu`、`--disable-direct-composition`、全局
  `--remote-debugging-port/pipe`、`--site-per-process`、遮挡/后台优化禁用参数、
  `--no-sandbox`、`--headless`。
- 覆盖多窗口并发、后台/遮挡截图新鲜帧、K4/K15、human latch、真实 Sogou IME、
  SW 重启、OOPIF、用户前台不被干扰。
- S5 映射：RC-A8 使用真实 Sogou TSF（compositionstart + 三个输入面中文 + HUMAN/ASSIST）
  实测 PASS；diagnostic S5 仍保留自己的 BLOCKED，不互相改写。
- 证据：`D:\c\logs\release-candidate-e2e-2026-10-31T19-10-32-428Z.json`。

注意：release 矩阵不是 diagnostic 204/204 的重解释；两者独立。release 矩阵未覆盖
long-run hidden page（见 §2）。

## 2. 长驻 hidden renderer 稳定性（B）

真实 BrowserPilot popup 在 managed-default 窗口、1 Hz 重绘，**59.842 s** 精确触发：

```text
layer_tree_view.cc:554 DCHECK failed:
callbacks.size() <= cc::PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60)
```

根因：managed 窗口原生 show 被策略剥离，但 WebContents/renderer 仍逻辑可见；
CompositorFrame 持续提交，display 不 present，presentation callback 永不消费；
viz 对 undrawn frame 节流到约 1 Hz，因此约 60s 爆掉。

触发矩阵：text/rAF/canvas/WebGL/extension/OOPIF 均在 59–75s 崩溃；`SW_HIDE`/`SW_MINIMIZE`
不改变模型可见性，仍崩；真正 model-hidden（先 show 再 hide）180s 稳定；static/no-paint 稳定；
8 窗口并发时所有动态窗口崩溃，browser/其他进程存活。周期截图 10s 可避免但只是 workaround。

两条路线都已建立：
- A1（模型可见性同步）：21 分钟 8 窗口 soak 稳定，但 WinRelease release 矩阵显示 managed 窗口 AI raw input 不落地（RC-A2/A3/A7 clicks=0），**未选**。PR #7 已关闭。
- A2（presentation feedback 生命周期，patch 036）：不隐藏 WebContents，保留 AI/人类输入语义；修复 WinRelease 缺失 patch 033 接线后，release 矩阵 **14/14 cases、226/226 checks PASS**（含 RC-A1/A5/A8），21 分钟 8 窗口 managed-hidden soak **8/8 alive、model-visible、0 crash、0 FATAL、VERDICT STABLE**。已选为根修，PR #9 已更新审计补丁（`2bab9da`）。
- 20 分钟 WinRelease+A2 多窗口长稳：0 presentation DCHECK、0 renderer/GPU/network crash、0 FATAL；所有 dynamic/Canvas/WebGL/OOPIF/extension/hidden/minimized/occluded/ownership/SW 场景 PASS；资源稳定。Native Messaging Host restart 由独立 gate 覆盖并通过（196/196、20 分钟 31/31、t=601s 真实重启，PR #7）。
- 没有删除 DCHECK、扩大 buffer、周期 reload/重启页面或额外禁用后台优化。

## 3. GPU/TSF 安全与 patch 034/035（C）

- 安全策略位（browser/GPU/renderer/storage utility/默认 network utility）在 Browser CLI
  与官方 Chrome 上逐项相同；Edge 额外 renderer AppContainer + force-relocate ASLR。
- **不能宣称安全等价**：Browser CLI 是 component build + NotSigned，browser/renderer 加载
  493/495 个安装目录模块（Chrome 5/3、Edge 8/4）；component build 的启动前 CIG 被排除；
  逐模块签名/时序未验证。
- `--in-process-gpu` 是安全降级（GPU attack surface 在 Medium-IL browser 进程、无 CIG/ACG
  等）；正常模式已使用独立 GPU 进程。
- `disable_fieldtrial_testing_config=true` 同时移除了两个 Windows 安全 study：
  browser ACG（`BrowserDynamicCodeDisabled`）和 network service sandbox
  （`NetworkServiceSandboxWindows`）。browser ACG 不能重新开启，因为 Sogou Xbyak 需要 JIT；
  normal launcher、release 矩阵、Native Messaging client wake path 和 20 分钟 long-run gate
  均已显式加 `--enable-features=NetworkServiceSandbox`；normal-launch 静态守卫现在要求该
  flag 存在。实测 network utility 为 Low IL + AppContainer + CIG/ACG。官方 Chrome 无 seed 的
  隔离 profile 同样两者皆关；server-side variations seed 状态 NOT-TESTED。
- Patch 034：in-process DComp 初始化、`Intermediate D3D Window`、WGC 彩色、后台截图 PASS；
  `--single-process` 在 `extensions_client.cc:38` FATAL（早于 GPU 初始化，patch 034 未触达）。
- Patch 035：把 `InitializePlatformOverlaySettings()` 移到 DComp 初始化之后；构建后
  `directComposition=true`、`supportsOverlays=true`，WGC 彩色。合并进 browser-cli PR #2；
  video overlay/HDR/D3D12/Graphite、同进程 GPU 重初始化仍 NOT-TESTED。
- 证据：`D:\work\wt\bc-rc-security\docs\windows-rc-security-2026-11-01.md`、`D:\c\logs\rc-sec\`。

## 4. P1 原生导入（D）

- 契约升级为 `browser-cli.cookie-import/v2`：160-bit 一次性 ticket、5 分钟 TTL、一次性、
  失败/速率限制；绑定 target process/profile/job/domain digest；目标浏览器独立授权弹窗。
- 传输改为既有 BrowserPilot Native Messaging Host 的 import-only relay + Windows named pipe；
  不做通用 HTTP/命令转发。
- 规则：HostOnly/Domain、SameSite、Secure、Partitioned、cookie prefix、PSL；Session cookie
  明确 `mayNotSurviveRestart`；target 永远不能自报 `loginVerified`。
- BrowserPilot：42/42 + 11/11 + 15/15 + 14/14 tests PASS；typecheck/build PASS。
- browser-cli：v2 contract/reference tests 62/62 + 7/7；native patch
  `chromium/patches/proposed/037-browser-cli-cookie-import.patch` 已 rebase 到当前 001–036 栈，
  修复 UI 线程阻塞 I/O/join、expiry 帧 drain、client PID 竞态和 managed-hidden 批准呈现；
  `cookie_import_unittests` 9/9 PASS。修复后的原始 WinDev 产物完成真实闭环 **9/9 场景 PASS**：
  approve 写入 2 条 Cookie、`LOGIN_OK`、重启后持久化且 session cookie 丢弃；deny/cancel/
  host 中断/source 替换/duplicate/cross-profile/DACL/expired ticket 全部 PASS；
  `authorization={targetUserConfirmed:true, simulated:false, source:target-browser-dialog}`。
  仍未入 manifest，不能宣称已合并/发布。
- PR：BrowserPilot #5（v2）、#8（native E2E harness）、browser-cli #4（proposed 037 已更新）。

## 5. PR / 状态

- browser-cli #1：patch 030–033 + TSF field-trial + gate + docs；Draft。
- browser-cli #2：patch 034/035 + WGC harness + security/lifecycle；Draft，依赖 #1。
- browser-cli #3：hidden long-run reproduction；Draft，依赖 #1。
- browser-cli #4：P1 v2 native patch compiled; Draft，依赖 #1；修复后的 037 已推送。
- browser-cli #5：WinRelease build/config/security; Draft，依赖 #1。
- browser-cli #6：20-minute long-run gate; Draft，依赖 #1。
- browser-cli #7：hidden route 1 (not selected); **closed**。
- browser-cli #8：旧 A2 route 2; **closed, superseded by #9**。
- browser-cli #9：audited A2 patch 036; Draft，base GPU #2，selected。
- BrowserPilot #1：K4/K15/latch + A harness + fixture；Draft。
- BrowserPilot #2：F3 screenshot fallback；Draft，依赖 #1。
- BrowserPilot #3：normal-mode + IME；Draft，依赖 browser-cli #1。
- BrowserPilot #4：P1 prototype；Draft。
- BrowserPilot #5：P1 v2 product flow；Draft，依赖 #4。
- BrowserPilot #6：release-like E2E matrix；Draft，依赖 #1。
- BrowserPilot #7：Native Messaging Host restart RC gate；Draft，base #1 branch。
- BrowserPilot #8：P1 native cookie-import E2E harness；Draft，base P1 product。

## 6. 结论

Release-like E2E 已在非 component WinRelease 上 **14/14 cases、226/226 checks PASS**（含
RC-A1/A5/A8）；A2 的 hidden presentation 路径在 20 分钟 8 窗口 run 中 **0 presentation
DCHECK**。Native Messaging Host restart 由独立 gate 覆盖并通过（196/196、20 分钟 31/31、
t=601s 真实重启）。P1 原生导入在修复后的原始 WinDev 产物上 **9/9 场景 PASS**（真实
CookieManager 写入、登录、重启持久化、managed-hidden 批准窗口），但 037 仍是 proposed。
NetworkServiceSandbox 已覆盖 normal launcher、release 矩阵、client wake path 和 long-run
两个 session；release 矩阵与 long-run 的 network utility 均使用 network sandbox。

仍不能宣称 Windows RC 发布就绪：
- **新阻断项**：20 分钟 long-run 中 extension service worker 替换 renderer 在空闲后触发
  `service_worker_event_queue.cc:185 HasScheduledIdleCallback` DCHECK（t=570s，
  w5 targetCrashed t=578.7s，fatal-log=1）。该崩溃与 A2 presentation 无关，但仍是 WinRelease
  FATAL；未删除 DCHECK、未用周期 reload 绕过，需单独根因/修复。
- 签名/安装器/更新/发布者身份未完成；WinRelease 仍是 unsigned internal test build，
  `releasable=false`。
- unsigned component→WinRelease 安全差异、逐模块签名、server-side seed 未验证。
- P1 patch 037 未入 manifest、未合并。
所有 PR 继续 Draft。
