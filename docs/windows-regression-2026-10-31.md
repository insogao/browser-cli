# 001–033 回归与门禁状态（2026-10-31）

构建：`D:\c\src\out\WinDev`，Chromium 156.0.8067.0；patch 001–033 `STATE applied`。
真实验收使用独立临时 profile，未使用 `--no-sandbox`；测试进程按 PID/创建时间/marker 清理，
注册表恢复到测试前值。

## 1. 构建 / 静态门禁

| 项目 | 结果 |
| --- | --- |
| `ninja -n chrome` | no work（`chrome.dll` 为 001–033 构建） |
| `apply-patches.py --check` | 001–033 全部 PASS，STATE applied |
| `check-browser-launch-hygiene.sh` | OK（16 个入口） |
| `tests/run-script-tests.sh`（Windows） | **223 passed, 0 failed**；macOS-only 检查明确 SKIP |
| `chrome.browserCli` allowlist self-test | PASS |
| BrowserPilot `check:normal-launch` | PASS |
| BrowserPilot import tests | 15/15 |
| BrowserPilot cookie bridge tests | 14/14 |

`tests/run-script-tests.sh` 的 macOS-only 分流已提交：非 Darwin 宿主对 checkout/verify、
磁盘 floor、`.icns`、macOS `.app` argv、Developer ID preflight 输出 `SKIP`，不输出 PASS；
macOS 上仍执行原检查。没有删除或放宽断言。

## 2. 真实 input-arbitration E2E

### Kernel 阶段（K0/K15/K1/K2/K3 + K4 + IME）

命令：`node scripts/test-input-arbitration-e2e.mjs --stage=kernel`

结果：**97/97 checks PASS**。

覆盖：HUMAN 全输入拒绝、ASSIST grant 消费/TTL/scope/cancel、跨 CDP session 隔离、
K15 新 tab 继承、K4 可见窗口呈现 + 真实人类事件 + 30s latch、真实搜狗 TSF
input/textarea/contenteditable、HUMAN/ASSIST 切换。

### 全量矩阵

命令：`node scripts/test-input-arbitration-e2e.mjs`

结果：**127/139 checks PASS，VERDICT: PARTIAL**。

通过：H1/H3/H4、A1–A4、S1–S4/S6、O1–O4、M1–M3、F1/F2、K4 前半、
IME 的 Feature/ownership 部分、清理、注册表恢复。

失败（12 条）：

1. `R2 创建未跟踪窗口且 beacon 就绪`：`Runtime.evaluate timed out after 30000ms`；
2. 随后 `K0`、`K15`、`K1`、`K2`、`K3` **同一窗口创建步骤全部超时**，属于 R2 后的级联；
3. 全量运行中的 IME 6 条（真实按键/中文/退格/textarea/contenteditable/HUMAN 人类按键）：
   窗口 `focus=false`，按键未到达页面；同一 IME 在 kernel-only 阶段 97/97 通过。

已修复的 harness bug：`humanType` 引用了未解构的 `real/useKnownWidget`，导致 H2 抛错并
污染后续状态；提交 `3f53ec6` 后 H2 通过，失败从 13 条降为 12 条。

判读：

- **不能把全量写成 PASS**；当前是 PARTIAL。
- R2 之后的级联与 IME 全量焦点失败需要单独隔离：要么是测试状态/前窗管理问题，要么是
  SW 重启后扩展窗口创建的真实缺陷；下一轮先复现 R1→R2 两步，不先改断言。
- Kernel 专项 PASS 不等于全量 PASS；P0 GPU/TSF 仍未关闭。

## 3. GPU P0

- GPU-A：ExitProcess(3) 已在 001–033 上抓到实际栈，调用方为
  `SogouPY!ImeExtension+0x10ce3c9`，线程为浏览器主进程 `CrBrowserMain`，不是 GPU 进程。
  详见 `docs/windows-gpu-p0-2026-10-31.md`。
- GPU-B：DComp 路径已用彩色 3×3 色块 + 文字 + 动画页面复现黑屏；真实 HWND 4526636，
  `visible=True`、foreground=true，窗口表面 9 点 `RGB≈31,32,35`，而 CDP renderer
  画面正常。`--disable-direct-composition` 路径尚未取得非遮挡彩色屏幕帧；不能把白色
  当 PASS。Browser CLI 的 CDP tracing 当前 0 事件，Surface ID/Commit/Present 事件待采。
- **P0 未关闭**：正常模式仍必须带 `--in-process-gpu --disable-direct-composition`；
  不能发布。

## 4. P1 登录态导入

真实 inso Chrome → WorkBuddy 登录态已通过“来源 Chrome 已安装 Web Store 扩展导出 +
Browser CLI 原生 CookieStore 写入”验证，重启后仍登录，Agent `js`/`snapshot` 可用。
这是技术路线 PASS，不是产品闭环：仍缺 Browser CLI 本地原生命令、来源 Profile 选择、
用户授权/扩展引导、进度与失败三态 UI、取消/重试/清理；当前 PR #4 只是原型/证据。

## 5. PR / 分支

- browser-cli PR #1：**Draft**，已推送 patch 030–033、K4/K15/latch 文档、P1 证据、
  GPU-A/B 记录、门禁分流。
- BrowserPilot PR #1：OPEN（已有 host capability 历史）；本轮追加 K4/K15/latch
  E2E harness 提交 `3f53ec6`。
- BrowserPilot PR #2：Draft，F3 截图兜底，base 为 PR #1 分支，依赖 PR #1。
- BrowserPilot PR #3：Draft，normal-mode 启动器 + IME 证据，base main。
- BrowserPilot PR #4：Draft，P1 导入原型/测试/文档，base main。
- 未 merge 任何 PR，未 force push，未 reset/clean。

## 6. 下一步

1. 隔离 R1→R2：确认 SW 重启后窗口创建超时是产品缺陷还是 harness 状态；修复后重跑全量。
2. 全量 IME 焦点：在 IME 阶段前显式恢复测试窗口 foreground/focus，再判断是否仍有产品失败。
3. GPU-B：用 DWM thumbnail/WGC 或专用可见桌面取得 disable-direct-composition 彩色帧；
   再采 Browser CLI 的 `DCompPresenter::Present`/`DCLayerTree::CommitAndClearPendingOverlays`
   事件和 surface id。
4. GPU-A：定位 +1 thread/+7 handles 的具体对象与 TSF 时序差异。
5. P1 产品化：最小来源扩展 + Browser CLI 本地原生 Cookie 写入 + 三态 UI。
