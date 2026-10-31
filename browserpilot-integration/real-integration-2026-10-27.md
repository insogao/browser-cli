# M3：BrowserPilot 集成（已用真实扩展验证）

状态：**Windows 上的真实扩展加载与行为验证已完成**。
测试对象：`https://github.com/insogao/BrowserPilot`（commit 由 `git log -1` 记录于下方）。

## 结论：不需要 capability 握手

原计划假设 BrowserPilot 需要一个 capability 握手来发现 Browser CLI 的额外能力。
读完真实源码后这个假设不成立：

- BrowserPilot 的 `capabilities` 字段是**页面模板的语义标签**（`src/background/templates.ts`），
  与浏览器能力协商无关。
- 它通过 `chrome.debugger` + native messaging 驱动网页，不依赖任何浏览器侧探测 API。
- 它在扩展内部已有自己的前台协调机制：`src/background/foreground-lease.ts`
  （90s TTL、30s 续期、`FOREGROUND_BUSY` 互斥），语义与内核的
  `ForegroundAssistController` 平行但独立。

因此正确做法是**让两者各司其职**：扩展负责多 agent 之间的互斥，内核负责最终的窗口策略。
不需要新增扩展 API，也不需要 native messaging 探测。

## BrowserPilot 自己的前台语义（源码核对）

`src/background/spaces.ts`：

```ts
export function wantsVisible(args) {
  const a = args ?? {};
  if (a.background === true) return false;
  return a.focus === true || a.keepVisible === true ||
         a.visible === true || a.background === false;
}
```

默认**不抢焦点**。`ensureTabVisible(tabId, state, focus=false)` 在 `focus=false` 时只把
`minimized` 恢复为 `normal`，不设置 `focused: true`。只有显式 `focus/visible/keepVisible`
才走激活路径。

这与内核策略方向一致：扩展默认后台、内核默认拒绝自动化上屏。两者叠加是**双保险**，
不是冲突。

## 真实集成测试（2026-10-27，Windows，managed 模式）

方法：把探针注入 BrowserPilot 自己的 `dist/background.js`（作为模块 import），
让它在 service worker 里调用**真实的** `chrome.windows.create/update`，然后加载整个
`dist/` 为扩展。测试后已完整移除探针（`BP_INTEGRATION_PROBE` 计数为 0）。

```text
PROBE before=1
PROBE created_bg id=1107428009 state=normal
PROBE before=2
PROBE created_bg id=1107428011 state=normal
PROBE after=3
PROBE final_state=normal
```

同时内核侧产生 6 条策略日志，全部 deny：

```text
BROWSER_CLI_PRESENTATION stage=policy decision=deny action=show-inactive    origin=extension
BROWSER_CLI_PRESENTATION stage=policy decision=deny action=restore          origin=extension
BROWSER_CLI_PRESENTATION stage=policy decision=deny action=show-and-activate origin=extension
```

**结论（PASS）**：真实的 BrowserPilot 扩展在 Windows managed 模式下

1. 能创建多个后台窗口（1→2→3），窗口状态 `normal`，页面可正常渲染；
2. 实际收到的所有原生展示请求都被归因为 `kExtension` 并拒绝，不会自动上屏；
3. 未出现崩溃、未出现权限对话框、未触碰真实 profile。

## 正向路径（显式 focus）——已通过 CDP 验证

不需要桌面点击：正向路径的实质是扩展在收到用户手势后调用
`chrome.windows.update(id, {focused:true})`，用 CDP 在扩展 service worker 上下文里
执行同一调用即可等价复现。

**方法**：`--remote-debugging-port` 启动 managed 模式浏览器并加载 BrowserPilot，
通过 CDP `Runtime.evaluate` 在扩展 SW 上下文里执行：

```js
const w = await chrome.windows.create({url:'data:text/html,...', focused:false, state:'normal'});
await chrome.windows.update(w.id, {focused:true});      // 显式 focus 请求
const g = await chrome.windows.get(w.id);
```

**结果**：

```json
{"before":1, "created":1283190889, "after_state":"normal", "after_count":2}
```

| 观察点 | 结果 |
| --- | --- |
| 扩展 API 是否抛错 | **否**（`chrome.windows.update` 正常返回） |
| 窗口是否创建 | 是，窗口数 1→2 |
| 窗口最终 state | `normal`（未变前台） |
| 内核策略日志 | `decision=deny action=show-and-activate origin=extension state=background` |

**结论（PASS）**：扩展层面「请求成功」，内核层面「拒绝真正前置」。这是刻意的设计——
扩展不会因失败而反复重试或崩溃，但用户桌面不会被打扰。要让窗口真正到前台，只能由
用户自己点击标题栏/任务栏，那条路径由 patch 019/021 以 `kUserExplicit` 放行。

## 尚未验证

- native messaging host 的 Windows 注册与端到端调用（`native-host/register.ps1` 未运行）。
- `chrome.debugger` 驱动网页的实际抓取/点击（本轮只测窗口层）。
- 真实可见桌面上用户亲手点扩展弹窗的完整链路（CDP 已验证其内核结果等价）。
- native messaging host 的 Windows 注册与端到端调用（`native-host/register.ps1` 未运行）。
- `chrome.debugger` 驱动网页的实际抓取/点击（本轮只测窗口层）。
- BrowserPilot 自己的 `foreground-lease` 与内核 assist lease 同时存在时的交互行为。

## 对 Browser CLI 侧的影响

**无需改动**。现有 patch 018（扩展 API 的 `kExtension` scope）已经正确处理了这个扩展：
它的窗口操作被归因、被策略裁决、后台窗口照常工作。

如果将来要让 BrowserPilot 在 managed 模式下也能"用户显式前置窗口"，正确做法是
走 `chrome.windows.update(id, {focused:true})` 并让内核看到**可信用户手势**——
但扩展无法证明用户手势，所以这条路径在 managed 模式下应保持拒绝，由用户直接点击
标题栏或任务栏完成接管（那条路径已由 patch 019/021 以 `kUserExplicit` 放行）。

## 集成测试的版本基线

| 项 | 值 |
| --- | --- |
| 仓库 | `https://github.com/insogao/BrowserPilot` |
| commit | `292caa650c8232f3fe311e7b56fe665fdf7faa02` |
| 提交日期 | 2026-09-18 |
| 提交主题 | `fix: don't auto-open onboarding on install in managed background browser` |
| 构建 | `npm ci` + `npm run build`（0.6 秒），产物 `dist/` |
| `dist` 摘要 | `f7d3ba127f1048845cd6da91c7d47247`（按文件名+内容排序哈希） |

### 作者已在处理同一类问题

该提交的说明与代码注释显示，BrowserPilot 作者已经遇到过「managed 后台浏览器里插件
自动开 tab 会把 onboarding 页面顶上来」的问题，并在 `src/background/index.ts:19-23`
主动移除了 install 时的自动开 tab：

```ts
chrome.runtime.onInstalled.addListener(() => {
  void init();
  // Do not open a tab on install. In a managed/background browser the first
  // window may be created later, unexpectedly surfacing this onboarding page.
  // The user can open it explicitly from the popup's Manage button.
});
```

**对集成的意义**：插件侧已经在按「不主动弹前台」的方向收敛，与内核策略同向。
这降低了内核强制拦截造成的功能落差——两者不是对抗关系。

### 关于「是否需要重新编译浏览器」的澄清

不需要。`out/WinDev/chrome.exe` 已包含 001-025 全部补丁
（`ui_views.dll` 中 `BrowserCliPresentationPolicy` 出现 20 次、
`BROWSER_CLI_PRESENTATION` 4 次），本轮集成测试直接使用该二进制完成。

插件侧改动只需 `npm run build`（实测 **0.594 秒**），与 Chromium 编译无关。
只有修改内核补丁时才需要 `ninja` 增量编译（单文件约 1-2 分钟）。
