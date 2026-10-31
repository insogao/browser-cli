# Windows RC 验收汇总（2026-11-01）

状态：**Draft / 未发布**。所有 PR 保持 Draft；未 merge。构建为
`D:\c\src\out\WinDev`，Chromium 156.0.8067.0，patch 001–035（普通模式只需
001–033 + `disable_fieldtrial_testing_config=true`）。

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

修复方向（未实现）：
1. 产品层：managed presentation 被拒绝时同步隐藏 WebContents/widget，避免“逻辑可见但不呈现”；
2. Chromium：undrawn/superseded frame 发送 failed presentation feedback，renderer 消费失败
   回调并加 buffer 上限保护。
没有通过周期重启页面掩盖。PR：browser-cli #3；证据 `D:\c\logs\hs-*`。

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
  network service sandbox 是否显式启用仍是发布安全决策。官方 Chrome 无 seed 的隔离 profile
  同样两者皆关；server-side variations seed 状态 NOT-TESTED。
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
  `chromium/patches/proposed/036-browser-cli-cookie-import.patch` **proposed only**，未入
  manifest、未编译、未集成。原生 CookieStore 端点仍未打通，不能宣称产品闭环。
- PR：BrowserPilot #5（v2）、browser-cli #4（proposed native patch）。

## 5. PR / 状态

- browser-cli #1：patch 030–033 + TSF field-trial + gate + docs；Draft。
- browser-cli #2：patch 034/035 + WGC harness + security/lifecycle；Draft，依赖 #1。
- browser-cli #3：hidden long-run reproduction；Draft，依赖 #1。
- browser-cli #4：P1 v2 proposed native patch; Draft，依赖 #1。
- BrowserPilot #1：K4/K15/latch + A harness + fixture；Draft。
- BrowserPilot #2：F3 screenshot fallback；Draft，依赖 #1。
- BrowserPilot #3：normal-mode + IME；Draft，依赖 browser-cli #1。
- BrowserPilot #4：P1 prototype；Draft。
- BrowserPilot #5：P1 v2 product flow；Draft，依赖 #4。
- BrowserPilot #6：release-like E2E matrix；Draft，依赖 #1。

## 6. 结论

Release-like E2E 已 PASS（210/210），TSF 和 DComp 普通/in-process 路径均有构建验证；
hidden 长驻崩溃已复现并定位但**未修复**；P1 原生端点仍未实现；安全等价未证明。
因此仍不能宣称 Windows RC 发布就绪或合并到 main。
