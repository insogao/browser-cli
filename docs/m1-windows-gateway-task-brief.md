# M1 实现任务书：Windows Native Presentation Gateway（patch 016）

状态：待执行（等 `out/WinDev` 的 `chrome` 首次构建成功后开工）。
设计依据：`docs/windows-native-gateway-audit-2026-10-23.md`（逐行源码审计）、
`docs/windows-wiring-matrix-2026-10-22.md`（W-01..W-11）。

## 不可违反的约束

1. 只有一个决策实现：`ui/views/widget/browser_cli_presentation_policy.*`。
   Windows 文件只做原生管道，不得复制状态机、不得新增 Windows 专属判定分支。
2. 失败即关闭。无法解析窗口 id、无法确定 origin、未实现的动作，一律 deny + 日志，
   绝不"先显示再隐藏"、绝不回落到裸 `ShowWindow`。
3. `WM_ACTIVATE` / `WM_NCACTIVATE` / `GetForegroundWindow` **不得**作为
   `kUserExplicit` 生产者。它们由 Chromium 自己（`hwnd_message_handler.cc:895`/`:902`）
   和任意第三方进程产生，事后到达，不能作为来源证据。
4. `kSetBounds` 的门禁放在 `BrowserView::SetBounds`（扩展 API 入口），**不要**放在
   `DesktopWindowTreeHostWin::SetBoundsInPixels`。后者只做不带 `SWP_SHOWWINDOW` 的
   `SetWindowPos`，是 Chrome 自身布局的最低层入口，在那里 deny 会引入功能回归。
5. 不改 001-015 的既有内容；新改动作为新 patch（016 起）。
6. 不 commit / 不 push；不启动可见浏览器；不触碰真实 profile/keychain。
7. Windows 上 `chrome.exe --version` 不打印版本（`HandleVersionSwitches` 在
   `chrome_main_delegate.cc:1255` 被 `#if BUILDFLAG(IS_POSIX)` 包住），会启动浏览器；
   任何 runtime smoke 必须加 `--headless=new`，并显式使用独立 `--user-data-dir`。

## 交付物（按依赖顺序）

### 1. 共享策略：不改行为，只把 bounds 门禁放到正确位置

**2026-10-23 修正**：不新增 `PlatformBoundsChangeIsNonPresenting()`，也不改
`AllowPresentation` 的 `kSetBounds` 分支。原因是 Windows 的 bounds 实现已由源码证明非展示：
`DesktopWindowTreeHostWin::SetBoundsInPixels` → `HWNDMessageHandler::SetBoundsInternal`
只调用 `::SetWindowPos(..., SWP_NOACTIVATE | SWP_NOZORDER)`，不含 `SWP_SHOWWINDOW`
（`ui/views/win/hwnd_message_handler.cc:3910-3918`）。在 `SetBoundsInPixels` 加门禁会拦住
Chrome 自己的布局/DPI/会话恢复，属于功能回归。

正确位置是 `chrome/browser/ui/views/frame/browser_view.cc` 的 `BrowserView::SetBounds`
（扩展 API `chrome.windows.update` 在 `chrome/browser/extensions/api/tabs/tabs_api.cc:1715`
调用它）：把现有 `#if BUILDFLAG(IS_MAC)` 改为 `#if BUILDFLAG(IS_MAC) || BUILDFLAG(IS_WIN)`，
其余代码不动。这样：
- 未归属窗口 + 自动化来源 + `kSetBounds` → 允许（后台窗口可被布局/扩展调整）。
- HUMAN 窗口 + `kUnknown`/`kExtension` + `kSetBounds` → 拒绝。
- Chrome 内部 views 布局不经过该函数，不受影响。

patch 015 的 40/40 策略单测证据保持不变，本 patch 不修改任何既有策略单测期望。

### 2. 新文件 `ui/views/widget/browser_cli_presentation_win.h/.cc`（仅 `is_win`）

职责：把 Windows 原生对象映射到共享策略，不持有决策逻辑。

```cpp
// Per-window stable key for policy bookkeeping, derived from the HWND.
// HWND values are reused by the OS after destroy, so a key is only meaningful
// for a live window; callers must release ownership on destruction.
uint64_t BrowserCliWindowIdForHwnd(HWND hwnd);

// Shared-decision queries. Both fail closed when the window cannot be resolved.
bool BrowserCliAllowsPresentation(views::Widget* widget,
                                  BrowserCliPresentationAction action);
bool BrowserCliShouldSuppressWindowPosChange(HWND hwnd);

// Releases HUMAN ownership and any assist lease on window destruction.
void BrowserCliReleaseWindowState(HWND hwnd);

// Windows definition of the free function declared in the shared header.
bool AllowProgrammaticWindowChangeFromCurrentOrigin(
    gfx::NativeWindow native_window,
    BrowserCliPresentationAction action);
```

要点：
- id 用 `base::win::HandleToUint32(hwnd)` 转 `uint64_t`（该 revision **没有**
  `HandleToUint64`；`base/win/windows_handle_util.h:33` 只有 `HandleToUint32`），
  不做自增计数器（避免与 macOS 语义分叉）。
- 窗口销毁时释放 HUMAN 所有权与 lease，语义对齐
  `ui/views/cocoa/native_widget_mac_ns_window_host.mm:330-343`。
- `AllowProgrammaticWindowChangeFromCurrentOrigin` 是本 revision Windows 上的唯一实现；
  注意 `browser_view.cc:1701` 与 `browser_extension_window_controller.cc:161` 当前都在
  `#if BUILDFLAG(IS_MAC)` 内，所以未实现时不会产生链接错误（先前"unresolved external"
  的判断有误）。本 patch 把这两处扩展到 Windows 后才真正需要它。
- `BrowserCliShouldSuppressWindowPosChange` 在 managed 模式关闭时返回 false（stock 行为），
  在 managed 模式下无法解析窗口时返回 true。

### 3. `ui/views/widget/desktop_aura/desktop_window_tree_host_win.cc` 接入

在以下函数**最前面**查询共享决策；deny 时直接返回，不触碰 HWND：

| 函数 | 行 | 动作 |
| --- | --- | --- |
| `Show` | `:398` | `kShowAndActivate`（`kNormal`/`kMaximized`/`kDefault`）或 `kShowInactive`（`kInactive`/`kMinimized`） |
| `Activate` | `:529` | `kShowAndActivate`（`HWNDMessageHandler::Activate` 会 `::ShowWindow(SW_RESTORE)`，等价于展示） |
| `Deactivate` | `:537` | `kDeactivate` |
| `Maximize` | `:556` | `kMaximize` |
| `Minimize` | `:560` | `kMinimize` |
| `Restore` | `:564` | `kRestore` |
| `SetZOrderLevel` | `:580` | `kSetZOrder` |
| `SetFullscreen` | `:702` | `kFullscreen` |

**不要**在 `SetBoundsInPixels`（`:806`）加门禁；bounds 的程序化门禁在
`BrowserView::SetBounds`（见第 1 节）。

`Hide` / `HideImpl` 不设门禁（隐藏不违反任何不变量）。

### 4. `ui/views/win/hwnd_message_handler.cc` 原生兜底

- `OnWindowPosChanging`（`:3109`）：在既有 `ScopedFullscreenVisibility` 剥离处
  （`:3257-3261`）之后，若 `BrowserCliShouldSuppressWindowPosChange(hwnd())` 为真，
  剥离 `SWP_SHOWWINDOW`。这是唯一能拦住**外部** `SetWindowPos` 的位置，
  对齐 macOS 的 `-[NativeWidgetMacNSWindow orderWindow:]` 硬停止。
- `OnWindowPosChanged`（`:3270`）：只写观察日志（窗口 id、flags、最终可见性），不做决策。
- 日志格式与既有 macOS 结构化日志保持同一套键，追加 `native=win-hwnd`。

### 5. 命令行意图接线（W-04）与程序化门禁

`chrome/browser/chrome_browser_main.cc` 两处 `#if BUILDFLAG(IS_MAC)`
（`:562` 附近 ProcessSingleton 回调、`:2151` 附近冷启动）改为
`#if BUILDFLAG(IS_MAC) || BUILDFLAG(IS_WIN)`，复用同一个
`ArbitrateBrowserCliCommandLineIntent` 与 `CommandLineForColdStartStartup`。
Windows 上 assist 意图只消费、不展示（记录 `windows-gateway-not-implemented`）。

`chrome/browser/ui/views/frame/browser_view.cc` 的 `BrowserView::SetBounds`
门禁从 `#if BUILDFLAG(IS_MAC)` 扩展为 `#if BUILDFLAG(IS_MAC) || BUILDFLAG(IS_WIN)`。

`chrome/browser/extensions/browser_extension_window_controller.cc:153` 的
`#if BUILDFLAG(IS_MAC)` 扩展为 `IS_MAC || IS_WIN`，让扩展 API 的全屏自动化在 Windows
上也走共享策略（GN 已有 `//ui/views` 依赖，`chrome/browser/extensions/BUILD.gn:1224-1226`）。

新增 `chrome/browser/browser_cli/user_explicit_presentation_win.cc`：
- 只实现 `HandleUserExplicitShowRequest`，复用 `user_explicit_show.cc` 的分类器与 planner。
- 需要真正展示时，调用第 2/3 步的 gateway；若 gateway 未就绪则返回
  "consumed but not presented" 并记录 `result=failed reason=windows-gateway-not-implemented`。
- 不实现 assist 的 Windows 展示（留到 patch 017）。

GN：
- `chrome/browser/BUILD.gn`：把 `:user_explicit_show`、`:reopen_window_selection`
  移出 `if (is_mac)`，改为跨平台 dep；`:foreground_assist` 放到 `is_mac || is_win`
  （它依赖 `//ui/views`，不能无条件）；`:user_explicit_presentation`、
  `:foreground_assist_presentation` 保持 macOS-only。
- `chrome/browser/browser_cli/BUILD.gn`：新增 `if (is_win)` 的
  `source_set("user_explicit_presentation_win")`。
- `ui/views/BUILD.gn`：`if (is_win)` 加入 gateway 源文件，并新增
  `browser_cli_presentation_win_unittests` 测试目标。

### 6. 单测

新增 `ui/views/widget/browser_cli_presentation_win_unittest.cc`（`if (is_win)`）：

1. managed 模式下未归属窗口的 `BrowserCliAllowsPresentation(kShowAndActivate)` 为 false。
2. 授予 `kUserExplicit` 后，在 `ScopedForegroundOrigin(kInternal)` 下同一查询为 true，
   且 `IsWindowVisible(hwnd)` 只在此后改变（默认 origin 是 `kUnknown`，HUMAN 窗口对它
   一律 deny，所以测试必须显式 scope，不能假设默认 origin 会被允许）。
3. `kAgentAssist` 无 lease 时仍为 false。
4. 无法解析的窗口 id → suppress（fail closed）。
5. **外部 `::SetWindowPos(hwnd, ..., SWP_SHOWWINDOW)` 后 `IsWindowVisible` 仍为 false**
   （对齐 `components/remote_cocoa/app_shim/browser_cli_managed_presentation_unittests.mm:40-60`）。
6. `WM_ACTIVATE` 不产生 HUMAN 所有权：投递激活消息后 `IsUserExplicitOwned(id)` 仍为 false。

不修改任何既有策略单测期望（本 patch 不改策略行为）。

## 验收顺序（必须串行，前一步失败即停）

1. `gn gen out/WinDev` exit 0。
2. 两个策略单测目标编译 + 运行 40/40（回归 patch 015 证据）。
3. 新增 Windows gateway 单测目标编译 + 全部通过（报告实际运行数；需要真实桌面的用例
   不得静默 skip 后仍声称 PASS）。
4. `ninja -C out/WinDev -j 6 chrome` exit 0（增量）。
5. 符号/字符串检查：`BrowserCliWindowIdForHwnd`、`BrowserCliShouldSuppressWindowPosChange`
   出现在 `ui_views.dll`（组件构建的实际 DLL 名，不是 `views.dll`）；
   `BROWSER_CLI_USER_EXPLICIT_SHOW` 出现在 `chrome.dll`。
6. 无窗口 runtime smoke（必须 `--headless=new` + 独立 `--user-data-dir`）：
   `--dump-dom "data:text/html,<h1>x</h1>"` 返回对应 DOM 且 exit 0。
   注意：Windows 上 `--version` 不会打印版本，禁止用它做 smoke。
7. 只有在 1-6 全绿后，才把 diff 提取为 patch 016，更新
   `chromium/patch-manifest.json` 与 `chromium/patches/README.md`。

## 已知环境限制（记录，不在 016 修）

- 本机 headless 在不加 `--no-sandbox` 时 exit 3（`RESULT_CODE_KILLED_BAD_MESSAGE`），
  加 `--no-sandbox` 才能运行。`--no-sandbox` **仅限**本机诊断 smoke，生产与验收不得使用；
  沙箱初始化失败需单独排查（可能是本机安全策略/杀软）。
- Windows 上 `chrome.exe --version` 不打印版本（上游 `#if BUILDFLAG(IS_POSIX)` 限制），
  会启动浏览器；所有 smoke 必须 headless。

## 明确不在 016 范围

- assist 的 Windows 展示/lease 接线（017）。
- 扩展 API 的 `kExtension` origin scope（`tabs_api.cc` 的 `IS_MAC` 分支）——这会改变
  attribution 行为，需单独验证。本 patch 只扩展全屏门禁与 bounds 门禁。
- Windows 菜单/标题栏按钮/History 的用户入口归因。
- 会话恢复 `kSessionRestore` scope。
- taskbar / 开始菜单可信来源（进程内不可观测，需产品决策）。
- 构建/签名/打包/CI。
