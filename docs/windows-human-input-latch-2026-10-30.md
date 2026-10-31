# 人类输入 latch：SW 失联 >1s 时 AI 不得重抢输入（2026-10-30）

## 问题

patch 028 的 human pause 只有 1s：人类输入后，若扩展 service worker 失联/无响应，
1s 后 AI 的 debugger 输入就会重新被投递。用户要求：**人类介入后，如果 SW 无响应 >1s，
AI 不得重抢页面输入**。

## 修复（patch 032）

`content/browser/input_ownership/input_ownership_decider.cc`：

- `kHumanInputPause` 从 `1s` 提升为 **30s**；任何人类鼠标/键盘/滚轮/文本事件立即暂停
  全部 debugger 输入，并刷新 latch。
- 只有受信扩展的 **per-tab explicit `setOwnership`** 可以提前清除 latch：
  - 新版本 explicit 写清除；
  - 同版本/同 mode 的 explicit 幂等写也清除（可作“人类已确认、AI 可继续”的握手，
    且**不改变 version、不清 grant**）。
- **inherited 窗口写 / 窗口 heartbeat 永不清除 latch**。因此 SW 失联时，10s heartbeat
  或窗口快照不会把页面交还给 AI。
- latch 本身不消耗、不清除 grant；清除后原有 grant 仍按原预算使用。

patch：`chromium/patches/032-human-input-latch-30s.patch`
（3 文件、84+/11-、164 行，sha256
`9ba00d82ff9047fe56dbd05ea6f6062e3d804afbc1ce194a16f03ee60c5cadeb`）。

## 验证

- 构建：`ninja -C out/WinDev -j 6 chrome content_unittests` exit 0。
- content：`content_unittests --gtest_filter='InputOwnership*'` **55/55 PASSED**。
  新增/更新：
  - `ExplicitUpdateClearsHumanLatchInheritedDoesNot`
  - `ExplicitIdempotentWriteClearsHumanLatch`
  - `HumanInputPausesAllDebuggerInput` 改为 30s 边界
  - `HumanPauseExpiresThenAssistResumes` 改为 31s 后恢复
  - `DebuggerCommandMatrix` 改为 31s 边界
- 真实 on-visible kernel E2E（K4）：
  - 冷启动 `--browser-cli-user-explicit-show` 呈现可见窗口；
  - 真实 OS `mouse_event` 点击（不是 JS/PostMessage 合成 DOM 事件）；
  - `onHumanPresence` 事件到达；
  - 人类点击后 1.3s，AI `Input.dispatchMouseEvent` 仍被 30s latch 拒绝
    （`InputOwnership: debugger input rejected`）；
  - 同版本显式 `setOwnership(assist)` 清除 latch；
  - 清除后 2 次 AI click 成功、第 3 次被拒（grant 未被 latch 消耗）；
  - **K4 29/29 checks PASS**，报告：
    `D:\c\logs\input-arbitration-e2e-<latest>.json`。

## 边界

- latch 是 **30s 上限**，不是无限期。30s 后若 SW 仍失联，AI 可恢复；这满足了
  “>1s 不得重抢”的要求，但不是永久锁。若产品需要“人类接管直到明确交还”，应另定
  无限期策略并在 UX 上提供显式恢复入口。
- “人类输入”必须是真实 OS/TSF 输入栈到达内核的事件；测试中的 JS 合成事件不算，
  `computerUse`/`mouse_event`/真实 IME 属于可接受的 OS 输入路径。
- 扩展侧应在收到 `onHumanPresence` 后显式 `setOwnership` 确认；仅靠窗口 heartbeat
  不会清除 latch（这是有意的 fail-closed）。
