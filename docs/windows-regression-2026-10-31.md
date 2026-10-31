# 001–033/034 稳定性收敛与回归（2026-10-31）

构建：`D:\c\src\out\WinDev`，Chromium 156.0.8067.0。
- 正常模式构建：patch 001–033 + `disable_fieldtrial_testing_config=true`；
- in-process GPU 验证构建：另加 patch 034。
- 所有测试使用独立临时 profile；未使用 `--no-sandbox`；测试进程按 PID/creation-time/marker 清理，
  注册表恢复到测试前值。

## 1. R2 首个故障点与全量 E2E

### 1.1 最小复现

| 顺序 | 结果 |
| --- | --- |
| `--case=R2` | 22/22 PASS |
| `--case=R1,R2` | 32/32 PASS |
| `--until=R2`（完整前序） | **FAIL**：R2/K0/K15/K1/K2/K3 全部 `Runtime.evaluate timed out after 30000ms` |

首个故障不是 CDP 连接或执行上下文丢失。根因是长期驻留的 `chrome-extension://.../popup/index.html`
在 managed-hidden 窗口中持续重绘，presentation callback 累积到 61/60，Blink
`layer_tree_view.cc:554` DCHECK 杀死 extension popup renderer。浏览器级 CDP 仍健康，
SW target 仍存在，但 popup 的 cached/fresh session、`chrome.tabs.query`、
`chrome.windows.getAll` 均超时，`newTargets` 为空，证明新窗口从未创建。R2 只是第一个后续
需要 popup 的用例。

### 1.2 修复

Test-only harness（BrowserPilot PR #1，提交 `89531ea`）：

- 记录 `Target.targetCrashed/Destroyed`、`Inspector.detached`、execution-context 事件；
- `ensurePopup()` 在证明 renderer 死亡后回收/重建；对活页面超时不盲目重试；
- popup 在每个 case 边界回收，避免 60s hidden presentation callback 累积；
- IME 重新解析 IME tab 的顶层 HWND/render widget；不 show/activate 人属窗口；
  先 plain click，再 forced fallback；校验 OS foreground + `document.hasFocus()`；
- 增加 `--case` / `--until` / `--diag` / 故障注入验证。

### 1.3 全量结果

- Kernel 阶段（新构建）：**106/106 checks PASS，VERDICT: PASS**。
- 完整矩阵（新构建）：**204/204 checks PASS，32/32 可执行 case PASS；S5 按设计 blocked；
  harness VERDICT: PARTIAL**。
- 不再把 127/139 写成 PASS；当前所有已执行检查通过，剩余 PARTIAL 只因 S5 真实 TSF
  形态按设计不跑（IME 真机路径已单独 39/39 PASS）。
- IME 全量焦点失败已修复：IME tab 自己的窗口/widget 重新解析，foreground/focus 证据见
  `[ime-focus]` 日志；HUMAN/ASSIST/真实按键均通过。

## 2. GPU-A：TSF / 搜狗 ExitProcess(3)

- 实际退出调用方：`SogouPY!ImeExtension+0x10ce3c9` → `ExitProcess(3)`，线程
  `CrBrowserMain`，不是 GPU 进程。
- 首次 C++ 异常：`Xbyak::Error(ERR_CANT_PROTECT=14)`；SogouPY 的 Xbyak JIT 调
  `VirtualProtect(PAGE_EXECUTE_READWRITE)` 失败，Win32 `0x677 ERROR_DYNAMIC_CODE_BLOCKED`。
- 根因：非 Google 品牌构建默认应用 Chromium 的测试 field-trial 配置，其中 Windows
  `BrowserDynamicCodeDisabled` 在 TSF 初始化前启用 ACG。官方 Chrome/Edge 不应用该配置。
- 修复（browser-cli PR #1 `fc5e294`）：`chromium/args-windows.gn` 设置
  `disable_fieldtrial_testing_config = true`。不修改 TSF、不拦截 ExitProcess、不改搜狗 DLL、
  不降低安全机制。
- 构建后 headless 基线 exit 0（修复前 exit 3）；普通 GPU 进程窗口 WGC 抓到完整彩色页面。
- 旧的 `--in-process-gpu` 不是 GPU 根因，只是让 ACG 设置路径提前返回；已不再作为正常模式依赖。

## 3. GPU-B：DComp 黑屏

- 可信 WGC A/B（Agent B）：
  - 官方 Chrome 156 `--in-process-gpu` 与 Browser CLI `--in-process-gpu` 窗口表面 hash
    完全相同（`14864142134207728000`，9 点近黑）；CDP renderer 均彩色；
  - `--disable-direct-composition` 变纯白（`14905162914385234000`），不是页面内容；
  - 普通 GPU 进程路径 WGC 彩色，出现 `Intermediate D3D Window`。
- 根因：`GpuInit::InitializeInProcess()` 不调用 `gl::InitializeDirectComposition()`；
  只有独立 GPU 进程的 `GpuInit::Initialize()` 调用。`DirectCompositionSupported()` 为 false，
  `DCompPresenter` 未创建，fallback 不向窗口呈现。
- 修复：patch 034（browser-cli Draft PR #2），在 `InitializeInProcess()` 补同一初始化。
  构建后 `bcli-dcomp` WGC 3 帧 294/296/292 unique colors、页面彩色；CDP hidden/bottom
  截图仍彩色。
- 正常模式不再使用 in-process GPU：BrowserPilot normal launcher 已移除
  `--in-process-gpu --disable-direct-composition`。`--disable-direct-composition` 不是有效
  workaround（白屏不是页面）。
- 待回归：`--single-process` 等复用 in-process 初始化的路径；`InitializePlatformOverlaySettings()`
  顺序导致 overlay_info.direct_composition 可能保守为 false。

## 4. 门禁 / P1 / PR

- Windows script gate：**223 passed / 0 failed**；macOS-only 检查明确 SKIP，未改断言；
  macOS 仍执行原检查，独立 release-preflight 入口仍待补。
- P1：真实 inso WorkBuddy 登录态已由来源扩展导出 + 目标原生 CookieStore 写入验证；PR #5
  已加入来源扩展、授权/来源选择/进度/失败/重试/三态结果和 `browser-cli.cookie-import/v1`
  契约，33/33 product tests + 15/15 + 14/14 PASS。**Browser CLI 原生 CookieStore 端点仍未实现**，
  目前 local target 明确标记 test-only；不宣称产品闭环。
- Draft PR：
  - browser-cli #1：patch 030–033 + TSF/回归/文档 + 门禁；Draft。
  - browser-cli #2：GPU-B WGC harness + patch 034；Draft，依赖 #1。
  - BrowserPilot #1：K4/K15/latch + A 修复；Draft。
  - BrowserPilot #2：F3 截图兜底；Draft，依赖 #1。
  - BrowserPilot #3：normal-mode + IME；Draft，依赖 browser-cli #1。
  - BrowserPilot #4：P1 原型；Draft。
  - BrowserPilot #5：P1 产品流/契约/来源扩展；Draft，依赖 #4。
- 未 merge、未 force push、未 reset/clean；未把真实会话写入测试 fixture。

## 5. 结论

R2 阻断、TSF 根因、DComp 普通路径与 in-process 根因均已定位；A/C 修复已构建验证，
B 的 patch 034 已构建验证。仍不能宣称 Windows 正式发布就绪：S5 按设计未跑、patch 034
的复用路径/overlay 顺序待回归、P1 原生端点未实现、macOS release-preflight 未建。
