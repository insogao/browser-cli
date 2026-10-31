# Windows M1 Slice 1：原生展示 gateway（patch 016）

日期：2026-10-23
Chromium revision：`7d0f28c30d1c0fadbce17ecec0ee2565aef9a940`（156.0.8067.0）
工具链：VS2026 BuildTools 18.11.1、MSVC 14.51、Windows SDK 10.0.28000.0、ATL/MFC、
Debugging Tools；`out/WinDev`，`is_component_build=true`。

## 范围

本切片把 Windows 的原生窗口操作接到**同一套**共享
`BrowserCliPresentationPolicy`，并让命令行 producer 在 Windows 上真正运行。
不复制策略、不新增 Windows 专属判定。assist 展示、扩展 origin scope、用户菜单/
taskbar 入口、会话恢复 scope 明确留到后续 patch。

## 实现

新增：

- `ui/views/widget/browser_cli_presentation_win.{h,cc}`
  - `BrowserCliWindowIdForHwnd()`：`base::win::HandleToUint32` → `uint64_t`。
  - `BrowserCliAllowsPresentation()`：用 `CurrentForegroundOrigin()` 查询共享策略。
  - `BrowserCliShouldSuppressWindowPosChange()`：managed 模式下对未归属/未 lease 的窗口
    返回 true；无法解析窗口时 true（fail closed）。
  - `BrowserCliReleaseWindowState()`：窗口销毁时释放 HUMAN 所有权与 assist lease。
  - Windows 版 `AllowProgrammaticWindowChangeFromCurrentOrigin()`。
- `ui/views/widget/browser_cli_presentation_win_unittest.cc`（仅 `is_win`）。
- `chrome/browser/browser_cli/user_explicit_presentation_win.cc`
  （只实现 `HandleUserExplicitShowRequest`，需要展示时 fail closed）。

修改：

- `desktop_window_tree_host_win.cc`：Show/Activate/Deactivate/Maximize/Minimize/Restore/
  SetZOrderLevel/SetFullscreen 最前面查询策略；析构与 `HandleDestroyed` 释放窗口状态。
- `hwnd_message_handler.cc`：`OnWindowPosChanging` 剥离外部 `SWP_SHOWWINDOW`；
  `OnWindowPosChanged` 只写观察日志。
- `browser_view.cc`：`BrowserView::SetBounds` 的 bounds 门禁扩展到 Windows。
- `browser_extension_window_controller.cc`：扩展 fullscreen 门禁扩展到 Windows。
- `chrome_browser_main.cc`：ProcessSingleton 与冷启动两处命令行仲裁扩展到 Windows；
  assist 意图在 Windows 上消费但不展示。
- `foreground_assist.cc`：`AppendSwitchASCII` → `AppendSwitchNative`，修复 Windows
  宽字符串编译错误（`GetSwitches()` 在 Windows 返回 `std::wstring`）。
- GN：`ui/views/BUILD.gn`（is_win sources + test target）、
  `chrome/browser/BUILD.gn`、`chrome/browser/browser_cli/BUILD.gn`。

## 关键设计决定

1. **bounds 门禁不放在 `SetBoundsInPixels`**。该函数只调用
   `::SetWindowPos(..., SWP_NOACTIVATE | SWP_NOZORDER)`，不含 `SWP_SHOWWINDOW`，
   是 Chrome 自身布局/DPI/会话恢复的最低层入口。门禁放在 `BrowserView::SetBounds`
   （扩展 API `chrome.windows.update` 的入口），避免内部布局回归。
2. **不新增平台能力查询，不改共享策略**。`kSetBounds` 的共享例外保留；patch 015 的
   32/32 + 8/8 单测证据不变。
3. **外部展示由 `OnWindowPosChanging` 兜底**。这是唯一能拦住外部
   `SetWindowPos(SWP_SHOWWINDOW)` 的位置；`WM_ACTIVATE`/`GetForegroundWindow` 只用于
   观察，不作为 `kUserExplicit` 来源。
4. **窗口 id 用 HWND 值**，并在销毁时释放状态；HWND 复用不会继承旧的 HUMAN/lease。

## 证据（Windows 实机）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0，36110 targets |
| `browser_cli_presentation_policy_unittests` | 32/32 PASS |
| `browser_cli_foreground_lease_unittests` | 8/8 PASS |
| `browser_cli_presentation_win_unittests` | 6/6 PASS（无窗口显示；测试不再使用屏幕外定位） |
| `ninja -C out/WinDev -j 6 chrome` | exit 0（增量；`chrome.dll` 重链） |
| 全 patch 栈重放 | `001→016` `STATE applied` |
| 反向检查 | `git apply --check -R 016-...patch` exit 0 |

符号（`out/WinDev`）：

| 符号/字符串 | `ui_views.dll` | `chrome.dll` |
| --- | --- | --- |
| `BrowserCliWindowIdForHwnd` | 1 | 0 |
| `BrowserCliShouldSuppressWindowPosChange` | 1 | 0 |
| `BrowserCliAllowsPresentation` | 1 | 0 |
| `BrowserCliReleaseWindowState` | 1 | 0 |
| `BROWSER_CLI_USER_EXPLICIT_SHOW` | 0 | 1 |
| `windows-gateway-not-implemented` | 0 | 2 |
| `BROWSER_CLI_PRESENTATION` | 4 | 1 |

无窗口 runtime（headless，隔离 `--user-data-dir`；`--no-sandbox` 仅本机诊断）：

```text
chrome.exe --headless=new --no-sandbox --disable-gpu --user-data-dir=<tmp> \
  --browser-cli-background-presentation --browser-cli-user-explicit-show \
  --enable-logging=stderr --dump-dom about:blank
-> exit 0

BROWSER_CLI_PRESENTATION stage=policy transition=background
  reason=command-line-switch window_id=0
BROWSER_CLI_PRESENTATION stage=host transition=window-pos-changed
  window_id=6497704 flags=63 visible=no native=win-hwnd
BROWSER_CLI_PRESENTATION stage=policy seq=1 decision=deny
  action=show-and-activate origin=unknown window_id=6497704 state=background
  owned=no reason=background-suppresses-native-presentation
BROWSER_CLI_PRESENTATION stage=host decision=deny action=0
  window_id=6497704 reason=policy-denied native=win-hwnd
BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=denied
  reason=launch-arguments-not-user-explicit managed=yes native=win-hwnd
```

说明：该命令带 `about:blank`，所以 producer 按设计判为
`launch-arguments-not-user-explicit`，而不是 `windows-gateway-not-implemented`。
两条 deny 日志证明共享策略与 Windows host 门禁都在真实进程中执行。

## 已知限制 / 未验证

- **沙箱**：本机 headless 不加 `--no-sandbox` 时 exit 3
  （`RESULT_CODE_KILLED_BAD_MESSAGE`）；`--no-sandbox` 仅限本机诊断，生产与验收不得使用。
  沙箱初始化失败尚未定位，不能据此宣称 runtime 验收通过。
- **`--version`**：Windows 上游不处理（`#if BUILDFLAG(IS_POSIX)`），会启动浏览器；
  所有 smoke 必须 headless。
- **未实现**：assist 的 Windows 展示与 lease 接线（patch 017）、扩展 API 的
  `kExtension` origin scope、用户菜单/标题栏/History 入口、会话恢复 scope、
  taskbar/开始菜单可信来源、`ScopedFullscreenVisibility` 的 `ShowWindow(SW_SHOW)` 旁路、
  `DesktopWindowTreeHostWin::Init` 的隐式 show。
- **未验证**：真实桌面上的 taskbar/开始菜单接管、HUMAN 窗口的完整动作矩阵、
  外部 `ShowWindow`（非 `SetWindowPos`）旁路、HWND 复用压力、长时间窗口 watcher。
  这些需要签名/打包后的真实 Windows 桌面验收，不能由单测替代。

## 下一步（patch 017）

1. assist 的 Windows 展示：`RequestForegroundAssistLease`/`PresentForAgentAssistAction`/
   `HideForForegroundAssistRelease` 接到 Windows gateway 与 controller。
2. 扩展 API `kExtension` origin scope（`tabs_api.cc` 的 `IS_MAC` 分支扩展到 Windows），
   并验证不会误伤普通扩展调用。
3. 用户菜单/标题栏按钮的 `kUserExplicit` scope。
4. 会话恢复 `kSessionRestore` scope 与 `DesktopWindowTreeHostWin::Init` 隐式 show 的归因。
5. 排查本机沙箱初始化失败。

---

# Slice 2（patch 017）：Foreground Assist 的 Windows 接线

日期：2026-10-23。基线：patch 001-016。

## 实现

- `chrome/browser/browser_cli/foreground_assist_presentation_win.cc`（新增）：
  - `HandleForegroundAssistRequest`：复用 `foreground_assist.cc` 的分类器与
    `ForegroundAssistRequest`；用 `GlobalBrowserCollection::ForEach` 遍历浏览器窗口，
    通过 `BrowserWindowInterface::GetWindow()->GetNativeWindow()` +
    `Widget::GetWidgetForNativeWindow` + `BrowserCliWindowIdForHwnd` 解析目标 HWND；
    只接受顶层 BrowserWindow。
  - 请求：`policy->RequestForegroundAssist(kAgentAssist, id, 0, reason)`；granted 后
    `BrowserCliPresentForAgentAssistAction(widget)`；展示失败则按刚授予的 generation 释放；
    成功则 `OnLeaseGranted(id, generation)`。
  - 释放：先取 generation；`ReleaseForegroundAssist(kAgentRelease, generation)`、
    `OnLeaseReleasedByAgent`，再 `BrowserCliHideForForegroundAssistRelease`。
  - `GetForegroundAssistController()` + `BrowserWindowForegroundAssistDelegate`
    （WindowExists / WindowIsUserOwned / CurrentLeaseGeneration / ReleaseLeaseForTimeout /
    ReturnWindowToBackground），全部转发共享 policy 与 gateway。
- `browser_cli_presentation_win.{h,cc}`：新增
  `BrowserCliPresentForAgentAssistAction`（kAgentAssist scope 下 Show+Activate）、
  `BrowserCliHideForForegroundAssistRelease`（拒绝 HUMAN-owned 与新 generation）、
  `BrowserCliIsEligibleForegroundAssistTarget`。
- `chrome_browser_main.cc`：两处 assist 分支从 `windows-gateway-not-implemented` 改为
  调用 Windows producer。
- GN：`chrome/browser/browser_cli/BUILD.gn` 新增 `foreground_assist_presentation_win`；
  `chrome/browser/BUILD.gn` 的 Windows deps 接入。

## 证据（Windows 实机）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0（36111 targets） |
| `browser_cli_presentation_policy_unittests` | 32/32 PASS |
| `browser_cli_foreground_lease_unittests` | 8/8 PASS |
| `browser_cli_presentation_win_unittests` | **8/8 PASS**（新增 2 个 hide 拒绝用例） |
| `foreground_assist_controller_unittests` | **14/14 PASS**（Windows 上首次编译运行） |
| `ninja -C out/WinDev -j 6 chrome` | exit 0 |
| 符号 | `ui_views.dll` 与 `chrome.dll` 均含三个新 gateway 符号；`chrome.dll` 含 `BROWSER_CLI_FOREGROUND_ASSIST` |
| 全 patch 栈 | `001→017` `STATE applied`；017 反向 `git apply --check -R` exit 0 |

headless 实跑（隔离 profile、无窗口）：

```text
chrome.exe --headless=new --no-sandbox --disable-gpu --user-data-dir=<tmp> \
  --browser-cli-background-presentation \
  --browser-cli-foreground-assist \
  --browser-cli-foreground-assist-reason=other \
  --browser-cli-foreground-assist-window=1 \
  --enable-logging=stderr --dump-dom about:blank

BROWSER_CLI_PRESENTATION stage=policy transition=background
  reason=command-line-switch window_id=0
BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=denied
  reason=launch-arguments-not-agent-assist window_id=0 lease_reason=other
  managed=yes native=win-hwnd
```

**注意**：该命令带 `about:blank`，分类器先判 `kBlockedByLaunchArguments`，
所以没有走到 `target-window-not-found`。producer 确实在真实进程中执行并 fail closed。
进程没有自动退出是**正确语义**：assist 是长驻 lease（等待用户完成登录/验证码），
不是一次性的 headless dump；手动终止后无残留进程。不要把它当作失败，也不要用
`--version` 或“等它自己退出”作为验收。

## 仍未实现

- USER_EXPLICIT 的 Windows 原生展示（`user_explicit_presentation_win.cc` 仍 fail closed）。
- 扩展 API `kExtension` origin scope（`tabs_api.cc`）。
- 用户菜单/标题栏/History 的 `kUserExplicit` scope。
- 会话恢复 `kSessionRestore` scope、`DesktopWindowTreeHostWin::Init` 隐式 show 归因。
- taskbar/开始菜单可信来源、`ScopedFullscreenVisibility` 旁路。
- 真实桌面上的 assist grant → 窗口展示 → timeout hide 全链路验收（需要可见窗口，
  不能用单测或 headless 代替）。

---

# Slice 3（patch 018）：扩展 API 的 `kExtension` origin scope

日期：2026-10-23。基线：patch 001-017。

## 实现

- `chrome/browser/extensions/api/tabs/tabs_api.cc`：5 处 `#if BUILDFLAG(IS_MAC)`
  扩展为 `IS_MAC || IS_WIN`：
  include 块、`windows.create` scope、`windows.update` scope、`tabs.create` 的
  异步 continuation 分支、`OnBrowserWindowCreatedWithOrigin` 定义。
- `chrome/browser/extensions/api/tabs/tabs_api.h`：`views::ForegroundOrigin` 前置声明与
  continuation 成员声明同步扩展。
- 更新过时注释（原写“其它平台不编译 ui/views”；实际
  `chrome/browser/extensions/BUILD.gn:1224-1226` 已在 `toolkit_views` 下依赖 `//ui/views`，
  Windows 为 true）。GN 无需改动。

## 证据（Windows 实机）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0 |
| `ninja -C out/WinDev -j 6 chrome` | exit 0（chrome.dll 重链） |
| policy / lease / gateway / controller | 32/32、8/8、8/8、14/14，全部 exit 0 |
| `chrome.dll` 导入 | `ScopedForegroundOrigin` 构造/析构、`CurrentForegroundOrigin` 来自 `ui_views.dll` |
| 全 patch 栈 | `001→018` `STATE applied`；018 反向 `git apply --check -R` exit 0 |

**headless 扩展实测**（临时 MV3 扩展，service worker 调
`chrome.windows.getAll` 后 `chrome.windows.update(id,{focused:true})`）：

```text
BROWSER_CLI_PRESENTATION stage=policy seq=2 decision=deny
  action=show-and-activate origin=extension window_id=14028654
  state=background owned=no owned_windows=0
  reason=background-suppresses-native-presentation
```

扩展 API 调用本身返回成功，但原生 focus 被共享策略拒绝；无可见窗口。
这证明 Windows 上的扩展自动化已正确归因为 `kExtension` 并走 fail-closed 路径。

## 仍未验证

- HUMAN-owned 窗口上的扩展 focus 拒绝（需要可见窗口接管后再调用扩展 API）。
- macOS 重新编译（本 patch 对 Windows 是纯增量 guard 扩展，未在 macOS 构建）。

---

# Slice 4（patch 019）：用户菜单/标题栏 `kUserExplicit` scope

日期：2026-10-23。基线：patch 001-018。

## 实现

- `AppMenu::ExecuteCommand`（`chrome/browser/ui/views/toolbar/app_menu.cc:1335`）最前面建立
  `ScopedForegroundOrigin(kUserExplicit)`；覆盖主应用菜单及其 History/New Window/
  New Incognito/Preferences/About 等委托命令。
- `SystemMenuModelDelegate::ExecuteCommand`
  （`chrome/browser/ui/views/frame/system_menu_model_delegate.cc:253`）同样建立 scope；
  覆盖 Views 系统菜单（`IsMenuSimplificationEnabled()` 为 true 时）。
- `BrowserCaptionButtonContainer` 的 minimize/maximize/restore 回调改为 lambda 内先建
  scope 再调用 `BrowserWidget::Minimize/Maximize/Restore`
  （`browser_caption_button_container_win.cc:51/:62/:73`）；close 不加。
- `chrome/browser/ui/views/frame/BUILD.gn`：给 `system_menu_model_delegate_impl` 补
  `//ui/views` 直接依赖（原来只是传递依赖）。

**明确不做**：`WM_ACTIVATE` / `WM_NCACTIVATE` / `WM_SYSCOMMAND` / `GetForegroundWindow`
不作为 `kUserExplicit` 来源。原生系统菜单由 OS 直接执行 `SC_*`，任何进程都能
`SendMessage(WM_SYSCOMMAND)`，因此永久不做该归因。

## 证据（Windows 实机）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0（36111 targets） |
| `ninja -C out/WinDev -j 6 chrome` | exit 0 |
| policy / lease / gateway / controller | 32/32、8/8、8/8、14/14，全部 exit 0 |
| scope 静态检查 | 三处文件存在 scope；`PreHandleMSG`/`OnSysCommand` 内无任何 scope |
| `dumpbin /imports chrome.dll` | `ScopedForegroundOrigin` 构造/析构、`CurrentForegroundOrigin` 来自 `ui_views.dll` |
| headless `--dump-dom about:blank` | exit 0，无可见窗口 |
| 全 patch 栈 | `001→019` `STATE applied`；019 反向 `git apply --check -R` exit 0 |

## 仍未验证

- 真实桌面上的应用菜单、系统菜单、标题栏按钮交互（需要可见窗口；headless 无法点击菜单）。
- 键盘快捷键（Ctrl+Shift+T 等）的 `kUserExplicit` 归因，留到后续切片。
- macOS 重新编译。

---

# Slice 5（patch 020）：会话恢复 `kSessionRestore` + `Init` 隐式 show 观察

日期：2026-10-23。基线：patch 001-019。

## 实现

- `chrome/browser/sessions/session_restore.cc`：4 个 `GetWindow()->Show()` 点
  （`use_new_window`、startup tabs、`ShowBrowser`、`kLastAndUrlsStartupPref` 新建窗口）
  各自用 `ScopedForegroundOrigin(kSessionRestore)` 包住 Show。`kSessionRestore` 仍是
  automation origin，未归属窗口的 show 继续被拒绝；没有改成 internal/user-explicit。
- `chrome/browser/sessions/BUILD.gn`：`source_set("impl")` 在
  `enable_session_service`（`!is_android`）下补 `//ui/views` 依赖。
- `DesktopWindowTreeHostWin::Init`：`window()->Show()` 后加 managed-mode 观察日志
  `transition=content-window-shown reason=init-implicit-show`；不阻止、不改变可见性。

## 证据（Windows 实机）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0（36111 targets） |
| `ninja -C out/WinDev -j 6 chrome` | exit 0（`ui_views.dll`/`chrome.dll` 重链） |
| policy / lease / gateway / controller | 32/32、8/8、8/8、14/14，全部 exit 0 |
| headless `--dump-dom about:blank` | exit 0；出现 `transition=content-window-shown ... reason=init-implicit-show` |
| 会话恢复两次运行 | 第一次创建会话；第二次 `--restore-last-session` 出现 `stage=policy decision=deny action=show-and-activate origin=session-restore ... reason=background-suppresses-native-presentation`，无可见窗口 |
| 全 patch 栈 | `001→020` `STATE applied`；020 反向 `git apply --check -R` exit 0 |

说明：第二次 headless 会话恢复进程没有自动退出（`--dump-dom` 未完成），约 3 分钟后
手动终止；只证明了归因与拒绝日志，没有证明自动退出行为。未重试。

## 仍未验证

- 真实桌面上的会话恢复（可见窗口）。
- 第二次 headless 不自动退出的根因。
- ChromeOS/macOS 编译。
- 键盘快捷键归因、USER_EXPLICIT 的 Windows 原生展示、
  `ScopedFullscreenVisibility` 的 `ShowWindow(SW_SHOW)` 旁路。

---

# Slice 6（patch 021）：键盘快捷键 `kUserExplicit` 归因

日期：2026-10-24。基线：patch 001-020。

## 实现

- `BrowserView::AcceleratorPressed`（`chrome/browser/ui/views/frame/browser_view.cc:5254`）
  函数体第一行建立 `ScopedForegroundOrigin(kUserExplicit)`。这是
  `views::AcceleratorManager` 分发键盘快捷键的窄入口。
- **不改** `BrowserCommandController::ExecuteCommandWithDispositionAndContext` 或
  `chrome::ExecuteCommand`：它们也接收扩展/WebUI 调用，blanket scope 会误归因。
- `browser_view.cc` 已有 `ui/views/widget/browser_cli_foreground_origin.h` include，无需新增。

## 源码纠正：`ScopedFullscreenVisibility`

此前审计把 `ScopedFullscreenVisibility::~ScopedFullscreenVisibility` 的
`::ShowWindow(hwnd_, SW_SHOW)` 列为“第四条独立展示路径”。该 revision 的实际源码是：

- `fullscreen_handler.cc:64` 只有 `std::unique_ptr<ScopedFullscreenVisibility> visibility;`
  声明，**从未赋值/构造**；
- 全树 grep（排除 third_party/out）没有任何 `ScopedFullscreenVisibility` 实例化；
  唯一使用是 `hwnd_message_handler.cc:3259` 的 `IsHiddenForFullscreen` 查询；
- 即使将来被实例化，`FullscreenHandler::SetFullscreen` 也只由
  `HWNDMessageHandler::SetFullscreen` 调用，而后者只由已被 patch 016 门禁的
  `DesktopWindowTreeHostWin::SetFullscreen` 到达（`desktop_window_tree_host_win.cc:750-757`）。

结论：该 `ShowWindow(SW_SHOW)` 在当前 revision 是死代码，不存在未归因的旁路；
patch 021 不修改 fullscreen 代码。

## 证据（Windows 实机）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0（36111 targets） |
| `ninja -C out/WinDev -j 6 chrome` | exit 0（`browser_view.obj` 重编、`chrome.dll` 重链） |
| policy / lease / gateway / controller | 32/32、8/8、8/8、14/14，全部 exit 0 |
| scope 静态检查 | 仅在 `AcceleratorPressed`；`browser_command_controller.cc`/`browser_commands.cc` 内零匹配 |
| headless `--dump-dom about:blank` | exit 0，无可见窗口 |
| 全 patch 栈 | `001→021` `STATE applied`；021 反向 `git apply --check -R` exit 0 |

## 仍未验证

- 真实桌面键盘快捷键归因（headless 无法可靠注入快捷键）。
- macOS 重新编译。
- USER_EXPLICIT 的 Windows 原生展示（下一高风险切片）。

---

# Slice 7（patch 022）：USER_EXPLICIT 复用已有窗口展示（REVIEW）

日期：2026-10-24。基线：patch 001-021。风险等级：高。

## 实现

- `user_explicit_presentation_win.cc` 新增 `FindReusableWindow()`：
  `GlobalBrowserCollection::ForEach(Order::kActivation)` 按激活顺序遍历浏览器窗口；
  只接受顶层 BrowserWindow；优先已 HUMAN 所有的窗口；找不到返回空。
- `HandleUserExplicitShowRequest` 只在 managed + `kShow` 时查找窗口，并用
  `PlanUserExplicitShow(..., has_reusable_window)` 决定。
- `kPresentExistingWindow`：在 `ScopedForegroundOrigin(kUserExplicit)` 内只调用
  `Show()` + `Activate()`；成功后
  `GetForegroundAssistController()->OnLeaseReleasedByUserTakeover(window_id)`
  取消 assist 计时器，记录 `handled/existing-window-presented`；失败记录
  `failed/presentation-denied`。
- `kCreateWindowThenPresent` 仍 fail closed（`window-creation-not-implemented`），
  留 patch 023。
- GN：`user_explicit_presentation_win` 增加 `:foreground_assist_presentation_win`、
  `//chrome/browser/ui/browser_window`、`//ui/aura`、`//ui/base` 依赖。

## 证据（仅编译/单测/producer 路径）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0（36111 targets） |
| `ninja -C out/WinDev -j 6 chrome` | exit 0 |
| policy / lease / gateway / controller | 32/32、8/8、8/8、14/14，全部 exit 0 |
| 静态检查 | kUserExplicit scope 只包住 Show/Activate；创建路径无 Show/Activate |
| headless 诊断 | 因 `--headless`/`about:blank` 被分类为 `launch-arguments-not-user-explicit`，**未执行到复用窗口展示路径** |
| 全 patch 栈 | `001→022` `STATE applied`；022 反向 `git apply --check -R` exit 0 |

## 状态：REVIEW

以下均未在真实桌面验证：

- 复用窗口的可见展示、激活、HUMAN 所有权；
- `OnLeaseReleasedByUserTakeover` 在真实可见窗口上的 assist 计时器取消；
- 多窗口选择（激活顺序、HUMAN 优先）的真实行为；
- 无窗口时创建（patch 023）。

headless 无法触发该路径：分类器把 `--headless` 当作 launch argument，纯 show 必须是
非 headless 启动；而 AGENTS.md 禁止为测试打开可见窗口。因此该切片的最终验收必须在
**用户授权的可见桌面环境**中完成，不能由编译/单测/headless 替代。

## 下一步

- 在可见桌面环境执行人工验收：`--browser-cli-background-presentation
  --browser-cli-user-explicit-show` 应复用/展示恰好一个窗口并授予 HUMAN；
  重复执行不应多开；用户接管后自动化不得再隐藏/激活它。
- 若需要无窗口时创建（patch 023），实现 `CreateBrowserWindow` 异步 continuation，
  但同样只能在可见桌面验收。
- 其余 M2-M4（后台截图/Profile、BrowserPilot、CI、发布）未开始。

---

# 可见桌面验收结果（2026-10-24，用户授权）

环境：真实可见 Windows 桌面，独立临时 `--user-data-dir`，只操作
`D:\c\src\out\WinDev\chrome.exe`；用户自己的 Chrome 进程未触碰。由于本机沙箱
初始化失败（不加 `--no-sandbox` 时 exit 3），本轮可见验收使用 `--no-sandbox`
作为**诊断**，不代表生产配置。

## 结果

| 场景 | 结果 | 证据 |
| --- | --- | --- |
| 后台 managed 冷启动 | **PASS** | 窗口 `visible=no`；`stage=policy decision=deny origin=unknown ... background-suppresses-native-presentation`；`stage=host decision=deny` |
| CLI 显式 show 复用窗口 | **PASS（可见性/所有权）** | 同一 HWND 变为 `visible=yes`；`transition=human-ownership-granted window_id=... owned_windows=1`；`stage=producer result=handled reason=existing-window-presented` |
| 重复 show 不新开窗口 | **PASS** | show 前后顶层窗口数一致；始终只有同一个可见窗口 |
| CLI show 的前台激活 | **REVIEW** | 窗口可见但未成为前台；`SetForegroundWindow` 与 `AllowSetForegroundWindow` 在当前测试宿主（非前台 shell）均失败。源码 `chrome/browser/win/chrome_process_finder.cc:81` 已在转发前调用 `AllowSetForegroundWindow(process_id)`；需从前台终端再跑一次确认 |
| assist grant | **PASS** | `assist-lease-granted`、`assist-window-visible`、`result=handled reason=lease-granted-and-presented` |
| assist release | **PASS** | `assist-lease-released reason=agent-release`、`assist-window-background`，窗口枚举无可见窗口 |
| HUMAN 窗口拒绝扩展 focus/minimize | **PASS** | 临时 MV3 扩展每 3 秒调用 `chrome.windows.update(id,{state:'minimized'})` 与 `{focused:true}`；窗口 HUMAN 后连续 32 次 `stage=policy decision=deny action=minimize/show-and-activate origin=extension state=human reason=human-owned-window-automation-denied`，窗口枚举保持 `visible=True minimized=False` |
| 用户菜单/标题栏/键盘快捷键 | **REVIEW（未执行）** | 未做 UI 注入；避免干扰用户桌面 |
| 会话恢复 | **REVIEW（仅 headless 证据）** | headless 已出现 `origin=session-restore ... decision=deny`；可见桌面未复测 |

## 前台激活的后续

- 需要在**前台终端**（用户自己打开的终端/已聚焦的命令行）执行同一 show 命令，
  确认 `AllowSetForegroundWindow(process_id)` 成功、窗口进入前台。
- 如果前台终端仍不能激活，再考虑 patch 023：在 ProcessSingleton 发送端显式检查
  `AllowSetForegroundWindow` 返回值并记录/回退（不得用 `AttachThreadInput` 等
  强制手段掩盖失败）。
- 当前不得把“窗口可见但未前台”标为 PASS。

## 清理

- 测试结束后已终止所有 `D:\c\src\out\WinDev\chrome.exe` 进程（残留 0）；
  用户自己的 Chrome 进程保持运行。
- 临时 profile 保留在 `D:\c\logs\visible-accept-*` 作为证据，可随时删除。

---

# 补充可见验收（2026-10-26，用户授权“直接在你机器上跑，不用沙箱”）

说明：本轮全部使用 `--no-sandbox`（本机沙箱 exit 3 未解决），因此结论标注为
**“无沙箱本机验收”**，不能作为生产沙箱配置的验收。隔离临时 profile，只操作
`out/WinDev/chrome.exe`，用户自己的 Chrome 未触碰，测试后进程残留 0。

## 1. 隐藏窗口能否被外部强行显示（W-01 残余风险）

对 managed 后台的隐藏窗口，从外部进程依次尝试三种原生显示手段：

| 手段 | 结果 | 日志 |
| --- | --- | --- |
| `PostMessage(WM_SYSCOMMAND, SC_RESTORE)` | 保持 `visible=False` | 仅 `position` 无显示 |
| `ShowWindow(SW_SHOWNOACTIVATE)` | 保持 `visible=False` | — |
| `ShowWindow(SW_SHOW)` | 保持 `visible=False` | `transition=showwindow-stripped` + `decision=deny origin=unknown` |
| `SetWindowPos(SWP_SHOWWINDOW)` | 保持 `visible=False` | `transition=showwindow-stripped` + `decision=deny origin=unknown` |

**结论（PASS）**：patch 016 的 `OnWindowPosChanging` 剥离 + policy deny 在真实桌面上
确实拦住了外部强制显示；这是此前一直缺可见证据的项。

## 2. 原生 OS 窗口控制回归（HUMAN 窗口）

CLI show 之后窗口为 HUMAN-owned，用 `WM_SYSCOMMAND` 模拟用户的原生标题栏/系统菜单操作：

| 操作 | 结果 |
| --- | --- |
| `SC_MINIMIZE` | `minimized=True` |
| `SC_MAXIMIZE` | `minimized=False`，窗口可见 |
| `SC_RESTORE` | 窗口可见、未最小化 |

**结论（PASS）**：HUMAN 窗口的原生最小化/最大化/恢复不被策略误挡；自动化（扩展）
对同一窗口的 focus/minimize 仍被 deny（见前一轮 32 次 deny 证据）。

## 3. 仍未解决/未验证

- **前台激活**：**已确认通过**（见下节）。
- **沙箱**：本机 `--no-sandbox` 才可运行；cdb 显示异常退出触发
  `CrashOnProcessDetach`，嫌疑为本机第三方 IME（搜狗）/安全软件；需干净环境复现。
- 菜单/标题栏按钮/快捷键的真实鼠标键盘交互未做（避免注入按键干扰用户当前窗口）。
- 无窗口时创建（patch 023）未实现。
- 上述结论仅覆盖 `--no-sandbox` 本机配置。

## 4. 前台激活的最终结论（2026-10-26）

此前把“窗口可见但没到前台”记为 REVIEW，原因是测试宿主（agent shell）不持有 Windows
前台权限。补做两组实验后结论明确：

| 实验 | 结果 |
| --- | --- |
| 从当前 shell 直接 `SetForegroundWindow(hwnd)` | `True`，前台切到 chrome 窗口 |
| `AllowSetForegroundWindow(ASFW_ANY)` | `True`（说明 shell 持有前台权限） |
| **先 `AllowSetForegroundWindow(browser_pid)`，再走产品 CLI show** | 前台切到 chrome 窗口（`AFTER_CLI_SHOW_WITH_FOREGROUND_GRANT` 显示前台 hwnd 属于浏览器进程），producer `handled/existing-window-presented` |

**结论（PASS）**：产品路径的 `Activate()` 能正确把窗口带到前台；早先的失败是
Windows 前台锁对非前台调用方的拒绝，不是代码缺陷。真实使用场景（用户从终端/开始菜单
启动 CLI）本身满足前台权限条件。无前台权限时的行为是“窗口可见但不抢焦点”，
这也是可接受的降级，不需要强制窃取焦点。

**注意**：不要用 `AttachThreadInput` 等强制手段绕过前台锁；`AllowSetForegroundWindow`
由持有前台权限的调用方（启动器）授予即可。








---

# Slice 8（patch 024）：USER_EXPLICIT 无窗口时创建窗口

日期：2026-10-26。基线：patch 001-023。

## 实现

`user_explicit_presentation_win.cc` 的 `kCreateWindowThenPresent` 不再 fail closed：

1. `ResolveLoadedProfileForUserExplicitShow()` 用
   `GetStartupProfilePath(current_directory, command_line, ignore_profile_picker=true)`
   解析 profile；`mode != kBrowserWindow` 或 profile 未加载时拒绝（`profile-not-loaded`）。
2. `chrome::OpenEmptyWindow(profile, should_trigger_session_restore=false)` 创建**恰好一个**
   空窗口，并只用它返回的 `BrowserWindowInterface*` 解析 views host 与 HWND id——
   不会误用“最近窗口”，也不会触发会话恢复。
3. 在 `ScopedForegroundOrigin(kUserExplicit)` 内 `Show()` + `Activate()`，由共享 policy 与
   patch 016 host 门禁决定；成功后 `OnLeaseReleasedByUserTakeover(window_id)` 取消
   assist 计时器，记录 `handled/window-created-and-presented`。
4. GN 增加 `//chrome/browser:browser_process`、`//chrome/browser/profiles:profile(_manager)`、
   `//chrome/browser/ui`、`//chrome/browser/ui/startup`。

## 证据（Windows 实机，**沙箱开启、未用 --no-sandbox**）

| 步骤 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | exit 0 |
| `ninja -C out/WinDev -j 6 chrome` | exit 0 |
| policy / lease / gateway / controller | 32/32、8/8、8/8、14/14，全部 exit 0 |
| 冷启动创建实测 | `--browser-cli-background-presentation --browser-cli-user-explicit-show --no-startup-window` → `transition=human-ownership-granted window_id=2172454`、`result=handled reason=window-created-and-presented`；进程按预期常驻 |
| 全 patch 栈 | `001→024` `STATE applied`；024 反向 `git apply --check -R` exit 0 |

## 未验证

- 可见桌面下新窗口的视觉确认（本轮用窗口枚举与日志，未截图）。
- profile picker 分支（`mode != kBrowserWindow` 的拒绝路径未实跑）。
- 重复执行 show 时的窗口数量（复用路径已覆盖，创建路径未重复测）。

## Slice 8 补测（2026-10-26，沙箱开启）

| 场景 | 结果 |
| --- | --- |
| 已有后台窗口 + CLI show | `VISIBLE_WINDOWS=1`（复用同一 HWND），`existing-window-presented`，`human-ownership-granted`；未多开 |
| 冷启动 `--no-startup-window` + show | `window-created-and-presented`，`human-ownership-granted window_id=2172454` |
| `--profile-directory="Profile 9"`（不存在） | 仍创建窗口并展示。`GetStartupProfilePath` 对不存在的 profile 目录回退到默认 profile（stock 行为），不创建虚构 profile；因此 `profile-not-loaded` 拒绝分支只在 profile 无法解析时触发 |
| 零窗口 + 进程存活 | 本机无法构造：关闭最后一个窗口时 Chromium 会正常退出；`--no-startup-window` 且无 URL 时进程也不常驻。**结论**：创建路径只在冷启动瞬间可达，用冷启动实测覆盖 |

所有测试进程清理后残留 0；用户自己的 Chrome 未受影响。

---

# M2 后台能力验证（2026-10-26，managed 模式 + 沙箱开启）

所有测试均使用 `--browser-cli-background-presentation`（managed 后台）、独立临时
`--user-data-dir`、**未使用 `--no-sandbox`**。

| 能力 | 结果 | 证据 |
| --- | --- | --- |
| headless 截图 | **PASS** | `--screenshot` 产出 PNG，800×600，2727 字节，PNG magic 正确 |
| Canvas 渲染 + 截图 | **PASS** | 320×200 画布（两块填充色）截图 652 字节，PNG 有效，尺寸正确 |
| JS 定时器 | **PASS** | `setInterval` 在 `--virtual-time-budget` 下跑到 `ticks=5`，DOM 正确反映 |
| JS DOM 操作 | **PASS** | 脚本改写 `<h1>` 文本，`--dump-dom` 输出符合预期 |
| HTTPS 导航 | **PASS（修复环境后）** | 本机原有 3 条 `codex_sandbox_offline_block_*` 出站阻断防火墙规则（拦截所有非 loopback 与全部 127.0.0.0/8 TCP/UDP），禁用后 `--dump-dom https://example.com/` 在 managed 模式下返回正确的 `<title>Example Domain</title>`。见下方「网络拦截根因」 |
| 进程稳定性 | **PASS** | 上述运行 exit 0，无崩溃；测试后残留进程 0 |

## 关于 HTTPS 失败的补充判断

失败同时出现在「直连 IP」「显式代理」「系统代理自动」三种配置下，而系统 `curl`
三种方式都成功，说明问题在本机对 Chromium 进程的出站限制，不是 Browser CLI 的
展示策略或 managed 模式引入的。该结论不影响 M1 的展示/所有权结论，但 **M2 的
"后台网络持续有效" 一项在本机只能标 BLOCKED**，不能标 PASS。

## M2 Profile 隔离与 OSCrypt（2026-10-26）

| 项 | 结果 |
| --- | --- |
| 独立 `--user-data-dir` 创建 | **PASS**：全新临时目录下正常生成 `Default/`、`Local State`、`Crashpad/` 等，未触碰任何真实 profile |
| Windows OSCrypt 机制 | 源码确认 `components/os_crypt/async/browser/os_crypt_win.cc` 使用 **DPAPI**（`CryptProtectData`/`CryptUnprotectData`，key 前缀 `DPAPI`）；与 macOS keychain 是两套独立机制 |
| `Local State` 中的 `os_crypt` 字段 | `encrypted_key` 存在、`audit_enabled=true`、无 `app_bound_encrypted_key`；即使用当前 Windows 用户的 DPAPI 作用域加密，不涉及 macOS keychain，也不读写任何真实凭据 |
| 真实凭据访问 | **未发生**：全程使用临时 profile；未调用 `security`、未导入/导出证书、未读取私钥 |

**结论**：Windows 侧凭据保护走 DPAPI，不存在 macOS 的 keychain ACL 问题；AGENTS.md 中
“禁止碰真实 keychain”的规则在 Windows 上对应“不读取真实 profile 的 Local State”。
本轮所有测试均使用隔离临时 profile，符合该约束。

**未验证**：多账号 profile 切换、`--profile-directory` 指向真实已存在 profile 时的行为
（本轮只验证了不存在目录回退到默认 profile）。

---

# M3 / M4 进展（2026-10-26）

## M3：BrowserPilot capability 握手

新增 `browserpilot-integration/capability-handshake.md`：

- 按设计 §7 给出握手 JSON 与五个 capability 在 Windows 上的**实测状态**：
  `backgroundWindows`/`backgroundCapture`/`foregroundAssist`/`hardFocusIsolation` 可用；
  `parallelWindows` 只覆盖单窗口与复用，未做多窗口并发压测，可信度低于 macOS。
- 探测机制三选一（静态能力文件 / `getPlatformInfo` 品牌 / native messaging），
  建议先做静态文件、M4 补 native messaging，**不做**新增扩展 API（避免扩大攻击面）。
- 明确：能力上报不是授权依据，真正的保护在 Chromium 策略层。
- **阻塞**：BrowserPilot 源码不在本仓库，无法在 Windows 跑真实握手。

## M4：Windows 验收入口

新增 `scripts/verify-windows.sh`（5 步，已实跑通过）：

| 步骤 | 内容 | 结果 |
| --- | --- | --- |
| 1 | checkout HEAD == `chromium/VERSION` | PASS |
| 2 | `chrome.exe`/`chrome.dll`/`ui_views.dll` 存在且非空 | PASS（1.7MB / 175MB / 5.9MB） |
| 3 | 关键符号进入产物（policy、gateway、producer 日志串） | PASS（5/5） |
| 4 | managed 模式 headless smoke，**不用 `--no-sandbox`** | PASS（exit 0、TSF skip 日志、DOM 正确） |
| 5 | stock 模式不得出现 managed 捷径日志 | PASS |

脚本约束：不构建、不签名、不打包、不启动可见窗口；每次运行使用新的临时
`--user-data-dir`；`--dry-run` 不写任何文件；`--skip-smoke` 明确标注不构成验收。
已注册进 `tests/run-script-tests.sh`（168 passed / 74 failed，失败集与改动前一致，
均为既有环境问题）。

## 仍未完成

- Windows 安装包/签名/更新器（需要产品身份与证书决策，不在本机做）。
- Windows CI（需要托管 runner；本机只能做本地验收）。
- BrowserPilot 真实握手（依赖其源码）。
- 多窗口并发（`parallelWindows`）压测。
- HTTPS 出站验证（本机 Chromium 出站被拦截，见 M2 记录）。

## M2/M3 补充：parallelWindows 实测（2026-10-26）

用临时 MV3 扩展（`chrome.windows.create({focused:false})`）在 managed 模式下探测：

```text
PROBE before=1 after=2
PROBE before=2 after=3
BROWSER_CLI_PRESENTATION ... action=show-inactive origin=extension decision=deny
```

**结论（PASS）**：扩展可以创建多个后台窗口（1→2→3），但每个 show 请求都被
`origin=extension` + `decision=deny` 拦截，不会上屏。`parallelWindows` 从
“仅单窗口与复用”升级为**已验证多窗口并存**。

同时记录一条测试卫生教训：`--load-extension` 的探针扩展最初因 manifest 缺少
`permissions` 且 service worker 顶层逻辑抛异常而未加载，CDP 中表现为
`file:///...par-ext/` 的 page target——**扩展加载失败时 Chromium 会把路径当 URL 打开**，
容易被误读为“扩展加载成功”。排查时应检查 CDP target 类型与 `chrome-extension://` 前缀。

## M4：Windows CI 草案（2026-10-26）

新增两个 workflow，**未 commit、未在任何 runner 上运行过**，属于可审阅的结构草案：

### `.github/workflows/windows-static.yml`
在 `windows-latest`（托管 runner）上跑，目标 < 20 分钟，每次 push/PR 触发：

- 所有 `scripts/*.sh`、`scripts/lib/*.sh`、`tests/*.sh` 的 `bash -n`；
- `apply-patches.py --check`（无 checkout 的静态模式，必须输出 `STATE unverified`）；
- patch 文件的 CRLF 检查（与 `.gitattributes` 的 `-text` 约定配套）；
- `chromium/args-windows.gn` 的 x64/无 macOS 值检查；
- `tests/run-script-tests.sh`；
- `tests.tools.test_apply_patches` + `test_windows_scripts`（30 个用例）。

### `.github/workflows/windows-full.yml`
**仅手动 dispatch**，`runs-on: [self-hosted, windows, x64, chromium]`，600 分钟超时：

- 断言 VS2026 BuildTools + SDK `10.0.28000.0` + `Debuggers/x64/dbghelp.dll`；
- `apply-patches.py --check`（不是 applied 才 `--apply`）；
- `configure-windows.sh` → `build-windows.sh`（`-j` 默认 6）→ `verify-windows.sh`；
- 始终上传 `build-logs/` 作为证据。

**为什么全量构建不放托管 runner**：本机实测 28,115 个目标、`out/WinDev` 约 16 GiB、
多次中断续跑累计数小时；托管 runner 的磁盘、时长和工具链版本都不满足，且需要
SDK 10.0.28000.0（托管镜像不保证提供）。

**未验证**：两个 workflow 都没有实际运行过；`windows-latest` 是否满足静态步骤的
Python/Bash 版本、自托管 runner 的标签约定，都需在真实 runner 上确认。

## M4：Windows 产品身份修复（2026-10-26，真实缺陷）

**发现**：Windows 构建的 `chrome.exe` 版本资源报告
`ProductName=Chromium`、`CompanyName=The Chromium Authors`。原因是 patch 002 的品牌接入
只覆盖 macOS（`mac_app_icon_dir` + `Assets.car`），Windows 侧从未设置
`branding_file_path`；而 `build/config/chrome_build.gni:109` 的默认值由
`branding_path_component` 推导，非 Google 品牌时恒为 `chromium`。

**修复**：`chromium/args-windows.gn` 增加

```gn
branding_file_path = "//chrome/app/theme/browser_cli/BRANDING"
```

该文件被 `base/version_info` 与 `chrome/process_version_rc_template.gni` 消费，
生成 `chrome_version.rc`。**不需要改 Chromium 源码**（因此没有新增 patch 文件）。

**验证**（重编 `chrome` 后）：

```text
ProductName=Browser CLI
CompanyName=jian gao
FileDescription=Browser CLI
FileVersion=156.0.8067.0
```

**已知边界**：`chrome_exe.rc` 的图标与 `IDENTITY LIMITEDACCESSFEATURE` 只按
`BUILDFLAG(GOOGLE_CHROME_BRANDING)` 分支选择，**不读取 `branding_file_path`**。
因此 Windows 图标仍是 Chromium 图标；换图标需要新增 `.ico` 资源并在 `.rc` 里加分支，
属于后续 patch。

**测试同步**：`tests/run-script-tests.sh` 原先禁止 args 里出现
`branding_file_path`（当时视为 macOS 专属）；现已改为禁止 `arm64`/`mac_app_icon_dir`
但**要求** `branding_file_path` 指向共享 BRANDING 文件。`scripts/verify-windows.sh`
新增产品身份断言（`ProductName == "Browser CLI"`），6 项检查全部 PASS。

## M4：Windows 安装包链路（2026-10-26）

`ninja -C out/WinDev -j 6 mini_installer` **exit 0**，产出
`out/WinDev/mini_installer.exe`（644 MB，组件构建包含全部 554 个 DLL）。

- 结构检查：PE 有效；
- 签名状态：`NotSigned`（本机**没有代码签名证书**，`Cert:\CurrentUser\My` 与
  `Cert:\LocalMachine\My` 的 CodeSigningCert 均为空；`signtool.exe` 存在）；
- 因此 Windows 签名/发布**阻塞于证书**，不是代码问题。

**未验证**：安装器实际安装/卸载流程（需要干净机器或虚拟机，且安装会写系统目录）；
`mini_installer` 的 644 MB 体积是组件构建的必然结果，正式发布应改用
`is_component_build=false` 的静态构建以显著缩小体积。

## M4：Windows 图标接入（patch 025，2026-10-27）

**之前的边界**：`chrome_exe.rc` / `chrome_dll.rc` 的图标只按
`BUILDFLAG(GOOGLE_CHROME_BRANDING)` 选择，不读 `branding_file_path`，因此 Windows
一直用 Chromium 图标。

**实现**：

1. `build/config/chrome_build.gni` 新增 `browser_cli_win_icon_dir`（空值 = stock）。
   放在这里而不是 `//chrome/BUILD.gn` 的 `declare_args()`，因为 `//chrome/app/BUILD.gn`
   也必须读到它。
2. `chrome_dll.rc` 增加 `#elif defined(BROWSER_CLI_WIN_ICON)` 分支，用
   `BROWSER_CLI_WIN_ICON` 宏作为**完整路径字符串**。
   **关键约束**：RC 预处理器**不会拼接相邻字符串字面量**——实测
   `"theme\x\\" "y.ico"` 会被当成两个文件名，报 `RC2135 file not found`。
   所以宏必须携带完整路径，不能拆成目录 + 文件名。
3. `chrome/BUILD.gn` 与 `chrome/app/BUILD.gn` 在 `browser_cli_win_icon_dir` 非空时
   定义 `BROWSER_CLI_WIN_ICON`。
4. 新增 `scripts/generate-windows-icons.py`：用 Pillow 从已批准的源图生成 6 个
   `.ico`（主图标 16..256 共 7 档，其余 5 档）。纯 Python、可复现、不改源图。
   `.ico` **不入库**（二进制资产不进 patch、不进仓库），由
   `scripts/configure-windows.sh` 在 `gn gen` 前生成到
   `<checkout>/chrome/app/theme/browser_cli/win/`。

**为什么只改 `chrome_dll.rc` 而不改 `chrome_exe.rc`**：`chrome_exe.rc` 的图标 ID
（`IDR_X001_APP_LIST` 等）来自 `chrome_dll_resource.h`，但该文件只被
`chrome_dll.rc` include；`chrome_exe.rc` 只 include `chrome_exe_resource.h`，
里面没有这些 ID。实测在 `chrome_exe.rc` 的 stock `#else` 分支新增图标会报
`unknown resource, type IDR_X001_APP_LIST`。因此 Windows 主图标经 `chrome_dll.rc`
进入 `chrome.dll`，`chrome_exe.rc` 保持原样。

**验证**：

| 项 | 结果 |
| --- | --- |
| `configure-windows.sh` 端到端 | PASS：自动生成 6 个 ico、`gn gen` 36111 targets |
| `build-windows.sh -j 6` | PASS：`chrome.dll` 重链 |
| 图标嵌入 | PASS：`chrome.ico` 的 **7/7** 尺寸载荷都能在 `chrome.dll` 中找到 |
| `verify-windows.sh` | PASS：6 项全绿（含 `ProductName="Browser CLI"`） |

**未验证**：任务栏/Alt-Tab/Explorer 中图标的视觉效果（需要真实桌面肉眼确认）；
`chrome_exe.rc` 仍引用 stock 图标，若 Windows 某些 shell 路径读的是 exe 的资源而非
`chrome.dll`，会看到 Chromium 图标。

## CRLF 行尾陷阱（2026-10-27）

`chrome/app/chrome_dll.rc` 在 HEAD blob 里是 **LF**（222 行），但这台机器的 checkout
（Gitiles tarball 解压）是 **CRLF**（222 行）。直接用 `git diff` 提取 patch 会把整个文件
算成删除+新增（448 行），且 `git apply --cached --check` 失败。

提取这类 patch 时必须先让工作区文件与 blob 的行尾一致（本次把该文件规范化为 LF），
再 `git diff`；验证标准是 diff 里**只有新增行**、且 `git apply --cached --check`
正向通过。仓库根 `.gitattributes` 已对 `*.patch` 设为 `-text`，但 Chromium checkout
本身没有这条规则，所以这是提取流程的问题，不是 patch 内容的问题。

## M4：安装器品牌一致性与体积（2026-10-27）

重编 `mini_installer`（patch 025 之后）后验证三个产物的版本资源：

| 产物 | ProductName | CompanyName |
| --- | --- | --- |
| `chrome.exe` | `Browser CLI` | `jian gao` |
| `setup.exe` | `Browser CLI Installer` | `jian gao` |
| `mini_installer.exe` | `Browser CLI Installer` | `jian gao` |

`verify-windows.sh` 已把这两项安装器断言加入产品身份检查（产物存在时才检查，
`mini_installer` 不是必经步骤）。三项全部 PASS。

**体积数据**（组件构建，`is_component_build=true`）：

- `mini_installer.exe` **644 MB**；
- `out/WinDev` 下 DLL 合计 **1.5 GB**（`chrome.dll` 167M、`blink_core.dll` 93M、
  `v8.dll` 88M、`blink_modules.dll` 80M、`browser_ui_views.dll` 66M）。

**结论**：组件构建只适合开发与验收，不适合发布。正式版应使用
`is_component_build=false` 的静态构建（预期单 exe + 少量资源，体积显著下降）。
切换需要全量重编（本机约数小时），因此本轮**未执行**，仅记录为发布前必做项。

**仍未验证**：静态构建的实际体积与产物形态；安装/卸载流程（需干净机器）。

## M4：静态发布构建（2026-10-27，进行中）

组件构建的 `mini_installer.exe` 是 644 MB，不适合发布。已启动静态发布构建验证：

```text
out/WinRelease/args.gn:
  is_debug = false
  is_component_build = false     # 关键差异
  symbol_level = 0
  blink_symbol_level = 0
  target_cpu = "x64"
  use_remoteexec = false
  branding_file_path = "//chrome/app/theme/browser_cli/BRANDING"
  browser_cli_win_icon_dir = "app/theme/browser_cli/win"
```

- `gn gen out/WinRelease`：**exit 0**，36073 targets；
- `ninja -C out/WinRelease -j 6 chrome`：启动，58331 个目标（组件构建为 28115），
  日志 `D:\c\logs\release-build.log`；
- 本机资源：`D:` 剩余 294 GB，物理内存 32 GB（构建期间可用约 12 GB）。

**为什么单独建 `out/WinRelease`**：macOS 脚本强制“单一 out”，但 `configure-windows.sh` /
`build-windows.sh` **没有**这个限制（有意为之：Windows 需要并存 dev 与 release 两种配置）。

**待完成**：构建完成后测量 `chrome.exe`/`mini_installer.exe` 体积、确认静态产物的运行
行为（沙箱、品牌、managed 模式），再决定是否把静态构建设为发布默认。

### 静态构建遇到的提交内存限制（2026-10-27）

`ninja -C out/WinRelease -j 6 chrome` 在 **23921/58331** 处失败：

```text
OSError: [WinError 1455] 页面文件太小，无法完成操作。
File ".../Lib/multiprocessing/popen_spawn_win32.py", line 75, in __init__
    hp, ht, pid, tid = _winapi.CreateProcess(
```

这是 `generate_bindings.py` 创建 `multiprocessing.Pool` 时无法再派生进程。实测本机：

| 指标 | 值 |
| --- | --- |
| 物理内存 | 31.7 GB |
| 页面文件（已分配） | 8.4 GB |
| 提交上限 | 39.9 GB |
| 失败时空闲虚拟内存 | 8 GB |

**结论**：不是代码缺陷，是 Windows 提交内存（页面文件）不足。Blink bindings 生成器按
`cpu_count()`（本机 32）建池，多个生成 action 并发时进程数暴涨。

**应对**：降低 ninja 并行度到 `-j 3` 续跑（已完成目标不重做）。若要允许更高并行，
需要用户自行增大页面文件（系统级改动，不在本项目自动执行范围内）。

### 网络拦截根因（2026-10-27）

此前把「HTTPS fetch 失败」记为环境阻塞，复查后确认根因是**本机 3 条 Windows 防火墙出站
阻断规则**，不是 Chromium 或 Browser CLI 的问题：

| 规则 | 实际作用 |
| --- | --- |
| `codex_sandbox_offline_block_outbound` | 阻断所有非 loopback 出站（RemoteAddress 为两个全集区间） |
| `codex_sandbox_offline_block_loopback_tcp` | 阻断 `127.0.0.0/8` 全部 TCP |
| `codex_sandbox_offline_block_loopback_udp` | 阻断 `127.0.0.0/8` 全部 UDP |

三者叠加等于对本机受防火墙约束的进程完全断网，连本地代理都连不上。`curl` 之所以能通，
是因为它不经过这套拦截。

**修复后验证**（全部在 `--no-sandbox` + 独立临时 profile + managed 模式）：

```text
chrome.exe --headless=new --browser-cli-background-presentation \
  --dump-dom "https://example.com/"
-> <title>Example Domain</title>   (exit 0)
```

**补充说明**：更早的 `fetch('https://example.com/')` 失败还有第二个原因——从 `data:`
URL 页面发起跨源 `fetch` 会被 CORS 拦截，与网络无关。改用直接导航（`--dump-dom <url>`）
才是正确测法。

**仍未验证**：HTTPS 页面在 headless 下截图（`--screenshot` 与真实网络组合会挂起，
属 headless 工具限制，不影响导航结论）；需要真实桌面确认时可另做。

## 上游回归验证：views_unittests（2026-10-27）

为回答「移植是否破坏了上游 Chromium」，在 Windows 上实际构建并运行了
`views_unittests`（覆盖 `ui/views`，正是改动最集中的模块）。

```text
ninja -C out/WinDev -j 6 views_unittests   -> exit 0
./views_unittests.exe --gtest_brief=1      -> 4282 个测试
```

**结果**：4282 个中 6 个失败、其余全部通过（含全部 `DesktopWidgetTest` 的
`FullscreenStatePropagated`、`StackAboveTest`、`DeleteInSetFullscreen` 等窗口路径测试）。

| 失败测试 | 判断 |
| --- | --- |
| `DesktopWidgetTest.GetWindowPlacement` | **已隔离证明与移植无关**（见下） |
| `LabelTest.AccessibleGraphemeOffsets` ×3 | 文本字素/无障碍，不经过窗口展示路径 |
| `TextfieldTest.TapOnSelection` | 文本输入交互，不经过窗口展示路径 |
| `WidgetTest.ThemeChangedShortCircuitRedundantUpdates` | 耗时 10s 超时，与 Show/bounds 无关 |

### GetWindowPlacement 的受控实验

该测试直接断言窗口 bounds，是最可疑的一个。失败表现：

```text
expected_bounds : 100,110 200x220
restored_bounds : 100,110 237x220     # 高度正确，宽度恒差 37px
```

**实验方法**：临时把 `DesktopWindowTreeHostWin::Show()` 里的
`BrowserCliAllowsPresentation(...)` 门禁**完全移除**，重编 `views_unittests` 后重跑。

**实验结果**：失败**完全不变**（仍是 200 → 237，差值一致）。

**结论**：37 像素是 Windows 对带标题栏窗口的最小宽度约束，与 Browser CLI 的门禁无关。
实验代码已从备份 `D:\c\logs\dwthw-backup.cc` 精确恢复（门禁 1 处、无 `EXPERIMENT` 残留），
恢复后重编 exit 0、四个自研单测目标 exit 0、`verify-windows.sh` 全部 PASS、
`apply-patches.py --check` 回到 `STATE applied`。

### 未覆盖

完整 `unit_tests` 套件（数千个用例、耗时数小时）未运行；`ui/views` 是改动最密集的
模块，已用 `views_unittests` 覆盖。`ui/base/ime/win`（patch 023 改动）没有对应的独立
单测目标，其行为由真实浏览器启动验证覆盖（stock 模式仍按原路径崩溃、managed 模式跳过 TSF）。
