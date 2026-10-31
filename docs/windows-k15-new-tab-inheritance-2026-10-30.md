# K15 新 tab 立即继承窗口输入权（实现与验证，2026-10-30）

## 结论

新 tab 不再等待下一次 10s heartbeat：窗口快照在 `TabStripModelObserver` 的
`kInserted` 同一条同步通知里写入新 WebContents。tab 的显式 override 与窗口派生状态
使用**独立版本空间**，窗口写/窗口 heartbeat 永远不会覆盖 explicit tab。

实现：`chromium/patches/031-input-ownership-new-tab-inheritance.patch`
（10 文件、760+/223-、1359 行，sha256
`ea19872405d88055967d5a44024978e10eaef39fb97ef09270b9bb01ad41159a`）。

## 设计要点（D1–D7）

- content 新增 `InputOwnershipOrigin{kNone,kExplicit,kInherited}`，并进入
  `InputOwnershipSnapshot`；`SetInputOwnership` 新增显式 origin 参数。
- 版本比较只在同一 origin 内进行：
  - explicit 从 none/inherited 进入时**重置版本空间**，低版本也接受；
  - inherited 遇到 `origin_==kExplicit` 返回 `kOverridePresent`，不改状态；
  - window heartbeat 对 explicit tab 只 `Touch` liveness，不改 mode/version。
- `TabStripModelObserver`：
  - `kInserted` → 窗口账本新鲜时写 inherited；
  - `kReplaced` → explicit override 复制到新 WebContents，否则重新派生 inherited；
  - `kRemoved(kInsertIntoOtherWindow)` → `ClearInheritedInputOwnership`。
- `WindowOwnershipRegistry` 挂在 `BrowserContext` user data，按
  `BrowserWindowInterface::GetSessionID()` 保存完整 SessionID；窗口账本自身 version
  单调、`last_ack` 30s 新鲜度；窗口关闭/模型销毁时安全解绑。
- `setOwnership({windowId})` / `heartbeat({windowId})` / `getOwnership({windowId})`
  全部走 registry；没有窗口账本的 heartbeat 返回 `UNKNOWN_TARGET`（D1），不再逐 tab
  回退。
- Feature off：registry/observer 根本不会创建；content controller 也不创建。

## 验证

构建：

- `gn gen out/WinDev` exit 0（36113 targets）；
- `ninja -C out/WinDev -j 6 chrome content_unittests` exit 0。

content 单测：

- `content_unittests --gtest_filter='InputOwnership*'` **53/53 PASSED**（新增
  `OriginSeparatesInheritedAndExplicitWrites`、`InheritedVersionTouchAndClear`）。

真实 kernel E2E（`node scripts/test-input-arbitration-e2e.mjs --stage=kernel`，
feature-on，真实扩展 + native host + `chrome.debugger`）：

| 检查 | 结果 |
|---|---|
| 同窗口 `setOwnership(windowId, ai, 1)` 后现有 tab = ai/1 | PASS |
| 新建 tab A **不走 heartbeat**，立即 `getOwnership` = ai/1/stale=false | PASS |
| tab A 立即接受 AI click（DOM clicks=1） | PASS |
| tab A explicit human/1 | PASS |
| 窗口 `setOwnership(ai, 2)` 后新 tab B 继承 ai/2 | PASS |
| 窗口 heartbeat(2) ×3 后 tab A 仍 human/1 | PASS |
| 切换激活 tab 不改变 tab A human/1 | PASS |

K15 **15/15 checks PASS**。本轮 kernel 合计 79/79 checks，5 PASS、1 blocked（K4 的真实
人类输入依赖可见窗口/IME，属 D 项，不是 K15）。E2E 报告：
`D:\c\logs\input-arbitration-e2e-2026-10-30T01-57-38-139Z.json`。

## 未覆盖 / 后续

- 会话恢复、跨窗口移动、prerender/discard `kReplaced` 已实现代码路径并有 content
  单测语义覆盖，但尚未补 browser test/E2E（K7/K10）。
- 30s 过期账本下新 tab 不继承（D6）有 content `Touch/Clear` 语义测试，未做 E2E 等待。
- `origin` 不进入扩展 API JSON（D3），扩展仍只看到 mode/version/stale。
- K4 真实人类输入/`onHumanPresence`/1s pause 仍受可见窗口的人类输入注入能力限制，
  需在 D 的真实 IME 验收中一起完成。
