# Windows 移植接线矩阵与编译根因（基线 2026-10-22）

基线：`D:\work\browsercli` @ `f7556d9`；Chromium `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940`（156.0.8067.0）。
本文件是主 agent 的审查结论，作为 Windows 移植基线文档入库（未 commit）；供后续 opencode CLI 任务引用。

## 0. 结论

Windows 现在有三类问题，必须分开处理，不能把“编译通过”当作移植完成：

1. **Patch 应用层**：本机 `core.autocrlf=true`，工作区 patch 已是 CRLF；git blob 仍是 LF，README 的 sha256 对应 LF。直接 `git apply` 会上下文不匹配或把 CRLF 写进 Chromium 源码。
2. **GN/链接层**：共用策略代码被当作 `ui/views` 的 source_set 但使用 `VIEWS_EXPORT`；Windows 组件构建下会产生 dllimport 定义冲突。部分 Mac-only 测试目标无条件包含 `.mm`/`Cocoa.framework`。
3. **静默失效层（最危险）**：001/003/005/013 的关键生产接线在 `#if BUILDFLAG(IS_MAC)` 内；Windows 即使编译成功，也只会得到一个“没有 Browser CLI 行为”的 Chromium。必须用符号/日志/单测证明接线进入 Windows 二进制，而不是只看 `ninja chrome` 退出码。

## 1. 接线矩阵

| 能力/入口 | 共用 C++（跨平台） | macOS 原生接线 | Windows 现状 | 级别 |
| --- | --- | --- | --- | --- |
| 展示策略/状态/来源 | `ui/views/widget/browser_cli_presentation_policy.{h,cc}`、`browser_cli_foreground_origin.h`、`browser_cli_foreground_lease.{h,cc}`（001/004/007/008） | `NativeWidgetMacNSWindowHost::SetVisibilityState/PresentForUserExplicitAction/AllowProgrammaticChangeFromCurrentOrigin/ApplyVisibilityState`（001） | 无 HWND gateway；策略代码未在 Windows 链接进 chrome | P0 W-01/W-02 |
| 策略的 bounds 例外 | `AllowPresentation` 对未归属窗口放行 `kSetBounds`，理由明确基于 macOS `NSWindow setFrame:` 不展示 | 同上 | 不能继承该证明；Windows `SetWindowPos`/z-order 语义未验证，必须 fail closed 或平台能力判定 | P0 W-02 |
| CLI 显式 show（冷启动） | `user_explicit_show.{h,cc}` 纯分类/计划（003） | `user_explicit_presentation.mm` 的 `HandleUserExplicitShowRequest`；`chrome_browser_main.cc` 调用在 `#if BUILDFLAG(IS_MAC)`（003/013） | 分类器未链接；handler 无 Windows 实现；冷启动不处理 show switch | P0 W-04/W-07 |
| CLI show（ProcessSingleton 转发） | 同上 | `chrome_browser_main.cc:557` 的 Mac 块；013 改为统一仲裁 | Windows `process_singleton_win` 转发路径未接；无仲裁 | P0 W-04/W-07 |
| 命令行意图仲裁 | `ArbitrateBrowserCliCommandLineIntent()`（013） | 两个入口都在 Mac 块内使用 | Windows 不调用；show+assist+URL 冲突可能回落为普通 launch | P0 W-04 |
| Assist request/release | `foreground_assist.{h,cc}` 分类器（005/013） | `foreground_assist_presentation.mm` + controller + Mac host（005/008） | 分类器未链接；handler/delegate 无 Windows 实现 | P0 W-04 |
| Lease 超时/ABA | `foreground_assist_controller.{h,cc}`（005/008） | Mac delegate 路由到 `NativeWidgetMacNSWindowHost` | controller 未链接；无 Windows delegate | P0 W-04 |
| 扩展 `chrome.windows.create/update` | `views::ScopedForegroundOrigin`（策略） | `tabs_api.cc` 用 `#if BUILDFLAG(IS_MAC)` 包住 scope；注释明确其它平台不编译 ui/views 进该 target（001） | Windows 完全绕过策略；直接放开会撞 GN 依赖层 | P0 W-04 |
| 扩展 `chrome.tabs.create` | 同上 | `TabsCreateFunction` 异步 continuation 带 origin，仅 Mac（001） | Windows 绕过 | P0 W-04 |
| 扩展 fullscreen | `AllowProgrammaticWindowChangeFromCurrentOrigin()` 声明在共用头 | 实现只在 `NativeWidgetMacNSWindowHost`；`browser_extension_window_controller.cc` 调用在 Mac 块（001） | Windows 无实现，无法直接放开 | P0 W-01/W-04 |
| 程序化 bounds | `BrowserView::SetBounds` 的 gate 在 `#if BUILDFLAG(IS_MAC)`（001） | Mac host 策略 | Windows 绕过；且不能照搬 Mac 的“不展示”证明 | P0 W-02 |
| 异步 show/activate 归因 | `BrowserView::Show/Activate` 的 origin 跨 deferral 捕获（001，跨平台代码） | 走 Mac host | 代码可编译，但最终仍需要 Windows gateway 才有效果 | P1 |
| 用户菜单入口 | 策略 `kUserExplicit` | `app_controller_mac.mm` 三处菜单 scope；010 | Windows Views 菜单未归因；放开策略后会 fail closed 误挡用户菜单 | P1 W-04 |
| Dock/Launchpad/任务栏接管 | `reopen_window_selection.{h,cc}` 纯逻辑（Mac 输入模型） | `app_controller_mac.mm` managed 分支；009 | Windows taskbar/开始菜单语义未实现；不能照搬 AppKit 焦点规则 | P0 W-01/W-07 |
| History 菜单恢复 | 策略 `kUserExplicit` | `history_menu_cocoa_controller.mm`；012 | Windows history 菜单未归因 | P1 |
| Omnibox Everywhere 激活 | `OmniboxEverywhereService::OpenUrl(..., user_gesture)`（跨平台签名） | `mac_window_util.mm` 的 `DecideWindowActivationRoute`；011 | Windows 激活路径未审计；不能把 renderer 自报 gesture 当可信 | P1 W-07 |
| Session restore | 策略有 `kSessionRestore` 来源 | 未完整接线（README/进度已标注） | 同样未接线；默认 Unknown 会 fail closed | P1 |
| 冷启动 assist 净化 | `CommandLineForColdStartStartup()`（跨平台） | 只在 Mac 块调用（005/013） | Windows 带 URL/app 的 assist 命令会回落为普通 launch | P0 W-04 |
| 窗口可见性/状态 | `ui::BaseWindow`、`views::Widget` | Mac host + AppKit | Windows 需在 `DesktopWindowTreeHostWin`/`HWNDMessageHandler` 侧找全部入口 | P0 W-01 |
| 后台渲染/截图 | 无 | Gate C 未完成 | 未开始；不得用“先上屏再隐藏”补救 | P1 W-08 |
| Profile/登录态 | 无 | Mac keychain 隔离测试 | Windows DPAPI/OSCrypt/Profile 独占未设计 | P1 W-09 |
| Patch 重放 | 无 manifest/tool；README 只有手动命令 | Mac 验证过 001→014 | 无幂等 apply/verify；CRLF 使手动重放不可靠 | P1 W-05 |
| 构建/签名/打包 | `chromium/args.gn` 固定 arm64/mac；脚本是 macOS | mac 已验证 | 无 Windows args/preflight/build/sign/package | P1 W-06 |
| CI | 无 | 无 | 无 | P2 W-11 |

### 2026-10-23 状态更新（patch 015/016 之后）

上表是 2026-10-22 的初始审计快照，保留作为历史基线。以下为实测更新：

- **W-01（无 HWND gateway）**：**部分关闭**。patch 016 新增
  `ui/views/widget/browser_cli_presentation_win.{h,cc}`，并在
  `DesktopWindowTreeHostWin` 的 Show/Activate/Deactivate/Maximize/Minimize/Restore/
  SetZOrderLevel/SetFullscreen 最前面查询共享策略；`HWNDMessageHandler::OnWindowPosChanging`
  剥离外部 `SWP_SHOWWINDOW`。**仍开放**：taskbar/开始菜单可信来源、菜单/标题栏/History
  用户入口、`ScopedFullscreenVisibility` 的 `ShowWindow(SW_SHOW)` 旁路、
  `DesktopWindowTreeHostWin::Init` 的隐式 show。
- **W-02（bounds 证明不能继承）**：**关闭并修正**。源码证明
  `SetBoundsInPixels → SetBoundsInternal` 只调用不带 `SWP_SHOWWINDOW` 的 `SetWindowPos`，
  非展示；门禁放在 `BrowserView::SetBounds`（扩展 API 入口），不放在最低层 host，
  避免破坏 Chrome 自身布局。外部 `SetWindowPos(SWP_SHOWWINDOW)` 由 patch 016 的
  `OnWindowPosChanging` 剥离，并有单测覆盖。
- **W-04（Windows 无 producer/仲裁）**：**大部分关闭**。`chrome_browser_main.cc` 两个命令行
  入口已扩展到 Windows，共用 `ArbitrateBrowserCliCommandLineIntent`；新增
  `user_explicit_presentation_win.cc` 实现 `HandleUserExplicitShowRequest`（展示路径
  fail closed）。patch 017 又新增 `foreground_assist_presentation_win.cc`：assist 的
  request/release/timeout 已接到共享 policy + `ForegroundAssistController` + Windows
  gateway；headless 实跑已产生 policy deny + host deny + producer denied 日志。
  **仍开放**：扩展 API 的 `kExtension` origin scope、会话恢复 scope、用户菜单/标题栏入口、
  USER_EXPLICIT 的 Windows 原生展示。
- **W-05（patch 重放）**：**关闭**。`chromium/patch-manifest.json` + `scripts/apply-patches.py`
  已验证 001→016 全栈 `STATE applied`。
- **W-06（Windows 构建入口）**：**部分关闭**。新增 `chromium/args-windows.gn`、
  `scripts/configure-windows.sh`、`scripts/build-windows.sh` 与 15 个单测；
  签名/打包/CI 仍未做。

## 2. 编译根因（按层次）

### 2.1 Patch 应用

- 证据：`git ls-files --eol chromium/patches/001-managed-window-mode.patch` → `i/lf w/crlf`；`git show HEAD:... | sha256sum` = README 的 `5e1bd1e4...`，工作区文件 = `17c92208...`；本机 `core.autocrlf=true`，仓库无 `.gitattributes`。
- 根因：Windows clone 把 LF patch 转成 CRLF。`git apply` 的上下文行与 Chromium LF 源码不匹配，或把 CRLF 写进源码。
- 修复：提交 `.gitattributes`（至少 `*.patch text eol=lf`、`*.sh text eol=lf`、`*.py text eol=lf`），并把工作区重新 checkout 为 LF；`patch-manifest.json` 用 LF 归一化后的 sha256 校验（与 README 一致）。重放工具遇到 CRLF 应显式拒绝或先归一化到临时文件，绝不能静默 apply。

### 2.2 GN / 链接

以下为高概率根因，需在固定 revision checkout 后用 `gn gen`/`ninja` 验证：

1. **`VIEWS_EXPORT` + source_set**：`ui/views/BUILD.gn` 把 `browser_cli_presentation_policy` 作为 `component("views")` 的 dep，但该 source_set 未定义 `VIEWS_IMPLEMENTATION`。Windows 组件构建下 `VIEWS_EXPORT` 变成 `__declspec(dllimport)`，在 source_set 内定义这些函数会触发 C2491（definition of dllimport function not allowed）。涉及 `browser_cli_presentation_policy.h`、`browser_cli_foreground_lease.h` 的 `VIEWS_EXPORT` 符号。修复候选：给 source_set 加 `defines = [ "VIEWS_IMPLEMENTATION" ]`，或改成带独立 export 宏的 component；需评审哪个不破坏单测链接。
   **2026-10-23 实测结论（已修复，patch 015）**：确认为真，实际错误是 20 个
   `-Werror,-Winconsistent-dllimport`（不是 C2491）。加 `defines = [ "VIEWS_IMPLEMENTATION" ]`
   后策略 obj 干净编译；但单测目标也必须加同一 define，否则因静态链入 source_set 而报
   `LNK4217: locally defined symbol imported`（已实测复现）。修复后 Windows 上
   policy 单测 32/32、lease 单测 8/8 全部通过。
2. **`components/remote_cocoa/app_shim/BUILD.gn`**：001 新增的 `test("browser_cli_managed_presentation_unittests")` 无条件包含 `.mm` 和 `frameworks = [ "Cocoa.framework" ]`。如果该 BUILD.gn 在 Windows 被解析，GN 会对非 Apple 平台的 `frameworks` 报错。应包在 `if (is_mac)` 内（或确认该 target 不在 Windows 依赖图中）。
3. **共用 target 未进入 Windows 图**：`chrome/browser/BUILD.gn` 的 001/003/005 改动把 `:reopen_window_selection`、`:user_explicit_presentation`、`:foreground_assist`、`:foreground_assist_presentation` 加在 `if (is_mac)` 的 deps 块（上下文 `shutdown_watchdog_mac`、`app_shim`、`browser/mac`）。Windows 不会编译/链接 `user_explicit_show`、`foreground_assist`、`foreground_assist_controller`。必须把纯 C++ target 移到跨平台 deps，平台展示 target 单独 `is_mac`/`is_win`。
4. **声明与实现分离**：`AllowProgrammaticWindowChangeFromCurrentOrigin()` 在共用 policy 头声明，但实现只在 `NativeWidgetMacNSWindowHost`（Mac .mm）。Windows 一旦调用就是 unresolved external；需要 Windows gateway 实现或平台无关的 dispatcher。
5. **Handler 只有 .mm 实现**：`HandleUserExplicitShowRequest` / `HandleForegroundAssistRequest` / `GetForegroundAssistController` 定义在 `user_explicit_presentation.mm` / `foreground_assist_presentation.mm`。Windows 需要平行实现，且最好把 producer 逻辑抽成平台无关的 .cc + gateway delegate，避免复制业务规则。
6. **扩展 API 依赖层**：`tabs_api.cc` 的注释明确“other platforms do not compile ui/views into this target”。直接改 `#if BUILDFLAG(IS_MAC)` 为 `IS_MAC || IS_WIN` 很可能导致 GN 依赖/循环或链接膨胀；需要先看 `//chrome/browser/extensions/api/tabs` 的 GN 图，决定用轻量 facade target 还是把 origin 合同下移到更低层。
7. **Branding/资源**：002 只加了 macOS `app.icns`/`Assets.car` 和 `mac_app_icon_dir`。Windows 若设置 `branding_file_path` 指向 `chrome/app/theme/browser_cli/BRANDING`，需要确认 Windows 需要的图标/版本资源键是否存在；否则 M0 先保持 stock branding，Windows 产品身份单独做。
8. **Windows args**：`chromium/args.gn` 固定 `target_cpu="arm64"`、`use_lld=false`、`mac_app_icon_dir`。Windows 需独立 `args/win.gn`（x64、component build、symbol_level=0、不设 mac icon），不能复用 mac args。

### 2.3 静默失效（编译成功但行为不存在）

- `chrome_browser_main.cc`：003/005/013 的 include、ProcessSingleton 处理、冷启动 `HandleUserExplicitShowRequest`/`HandleForegroundAssistRequest` 全部在 `#if BUILDFLAG(IS_MAC)` 内。Windows 上 CLI show/assist/仲裁完全不运行。
- `tabs_api.cc`：扩展 windows create/update 与 tabs create 的 origin scope 仅在 Mac；Windows 扩展自动化不受策略约束。
- `browser_extension_window_controller.cc`：fullscreen 门禁仅 Mac。
- `browser_view.cc`：`SetBounds` 门禁仅 Mac；Windows 扩展可改 HUMAN 窗口 bounds，且可能触发 `SWP_SHOWWINDOW` 类展示语义。
- `app_controller_mac.mm`、`history_menu_cocoa_controller.mm`、`omnibox_everywhere/mac_window_util.mm`：Mac-only producer。Windows 对应入口（Views 菜单、History 菜单、Omnibox Everywhere/Windows 激活）未归因；一旦把策略默认改为 fail closed，用户菜单可能被误挡。
- 007 之后默认来源是 `kUnknown`（fail closed）。这提升了安全性，但也意味着 Windows 上所有未接线的用户入口会变成“点了没反应”，必须在 M1 一并接可信来源，不能只放开策略。
- 005 的 `CommandLineForColdStartStartup()` 只在 Mac 块调用；Windows 的 assist+URL 命令会按普通 launch 处理，可能创建可见窗口。
- 001 的 `kSetBounds` 例外基于 macOS 链证明。Windows 的 `SetWindowPos`、z-order、`SWP_SHOWWINDOW` 语义不同，必须默认拒绝或提供经测试的平台能力判定。

## 3. M0/M1 最小补丁顺序（建议）

### M0-1（仓库卫生，必须先做）
1. 加 `.gitattributes` 并归一化工作区，确保 patch/hash 与 README LF 一致。
2. 新增 `chromium/patch-manifest.json`：基线 revision、001-014 顺序、LF sha256。
3. 新增 `scripts/apply-patches.py`：
   - `--check`：校验 HEAD == manifest revision、worktree 干净、整链在临时 index 上 `git apply --cached --check` 全部通过（半应用/顺序错误立即失败）。
   - `--apply`：先整链 dry-run，再按序 apply；拒绝脏树、拒绝已部分应用的 checkout；不覆盖既有 worktree 修改。
   - 输出每个 patch 的 hash 与结果；Windows/macOS 共用。
4. 单元测试覆盖：CRLF patch 拒绝、顺序错误、半应用、HEAD 不符、脏树、重复 apply 幂等/拒绝。

### M0-2（构建图拆分）
1. `chromium/args/{common,mac,win}.gn` 或生成器；Windows `out/WinDev` 从 clean checkout 生成。
2. 修复 `VIEWS_EXPORT`/source_set 的 Windows 组件构建问题（先按上面候选方案评审，不盲改）。
3. 把 `user_explicit_show`、`foreground_assist`、`foreground_assist_controller` 等纯 C++ target 加入 Windows chrome 依赖；Mac 展示 target 保持 `is_mac`，Windows 先加 fail-closed 的占位实现（不产生 UI）。
4. 把 `remote_cocoa` 的 `.mm` 测试 target 包进 `if (is_mac)`。
5. Windows 构建基线验收：`gn gen` + `ninja chrome` 成功，且用 `dumpbin`/`llvm-nm`/字符串检查确认 `BROWSER_CLI_PRESENTATION`、`ArbitrateBrowserCliCommandLineIntent` 等符号/日志串进入 `chrome.dll`/`chrome.exe`；不能只看 ninja 退出码。

### M1（Windows native gateway，独立新 patch）
1. 在固定 revision 的 checkout 中审计 Windows 展示入口（**2026-10-23 已由只读源码审计确认**，
   完整报告：`D:\c\logs\m1-windows-gateway-map.md`）：
   - `ui/views/widget/desktop_aura/desktop_window_tree_host_win.*`
   - `ui/views/widget/desktop_aura/desktop_native_widget_aura.*`
   - `ui/views/win/hwnd_message_handler.*`（WM_SHOWWINDOW/WM_WINDOWPOSCHANGING/WM_ACTIVATE 等）
   - `chrome/browser/ui/views/frame/browser_desktop_window_tree_host_win.*`、
     `browser_widget.*`、`browser_caption_button_container_win.*`
     （**纠正**：本 revision **不存在** `browser_frame_win.*` 与
     `ui/views/widget/native_widget_win.cc`；BrowserFrame 类已被 `BrowserWidget` 取代）
   - `chrome/browser/process_singleton_win.*` / `process_singleton.*`
   - `chrome/browser/ui/browser_window_state.cc`、`chrome/browser/ui/views/frame/browser_view.cc`
   - `chrome/browser/extensions/api/tabs/tabs_api.cc` 的 GN 依赖图
   以上类名需在 checkout 中逐个确认，不能凭记忆假定。
2. 建立平台无关的 `NativeWindowGateway` 合同（show/activate/hide/minimize/restore/fullscreen/z-order/bounds + 结果日志），macOS 适配现有 `NativeWidgetMacNSWindowHost`，Windows 适配 HWND 路径。业务判定只保留一份。
3. Windows 默认策略：未归属窗口的 `kSetBounds` 不得沿用 Mac 例外；`WM_ACTIVATE`/`GetForegroundWindow` 只能作为 OS 状态证据，不能单独判 USER_EXPLICIT。用户入口（CLI show、任务栏/开始菜单）需要明确的可信路径。
4. 接通 show/assist 两个 producer 与 controller delegate；确保 Windows 冷启动/ProcessSingleton 都走 013 的同一仲裁和 005 的冷启动净化。
5. 验收：0 窗口冷启动、真实 HTTPS/JS、tab 复用、extension/webpage focus/bounds/fullscreen 被拒、USER_EXPLICIT 只授一个窗口、HUMAN 不被自动化隐藏/最小化/改焦点、lease timeout/ABA。

## 4. 待用户/环境决策

1. **ATL/MFC 缺失**：VS BuildTools 未装 ATL/MFC。若 `chrome` 构建因 ATL 失败，需要管理员权限安装 `Microsoft.VisualStudio.Component.VC.ATL`（可能还需 MFC）；自动化不会弹 UAC。
2. **长路径**：`LongPathsEnabled=0`，当前用短根目录 `D:\c\src` 规避。若 Chromium 构建仍因 MAX_PATH 失败，需要管理员开启 LongPathsEnabled。
3. **CLI show 信任模型（W-07）**：本机任意程序都能传 `--browser-cli-user-explicit-show`；Windows 上如何区分真实用户入口与自动化调用，需要产品决策。不得把任意 CLI 参数或 `WM_ACTIVATE` 简化成可信人体手势。
4. **Windows 产品身份/签名/打包**：不复用 Apple 的 `config/product-identity.env` 与 Developer ID 流程；Windows exe/msi/签名/更新需要独立方案。
5. **是否允许本地 commit/PR**：AGENTS.md 规定除非用户明确要求不 commit；当前所有改动应保持工作区可审阅。

## 5. 环境安装任务状态（2026-10-22 18:50 最终）

- depot_tools、Chromium 固定 revision 源码、DEPS 依赖与 hooks 均已就绪（`D:\c\src`，HEAD/tree 见下）。
- git pack 不可用，改用 Gitiles archive + partial clone：
  - archive sha256 `7997af005cb399a89b2ab967d0f874458b3f45aac0b113085dac04dd5063a881`；
  - `git fetch --depth=1 --filter=blob:none` 只取 commit+trees（61s/19MB）；
  - HEAD `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940`，tree `abb974d2f95d2858237827db4aad78aa32b44d04`；
  - 21 个 symlink 平台降级为 skip-worktree；`git status` clean。
- 001-014 已在真实 checkout 上通过 `scripts/apply-patches.py --check`（STATE pristine）与 `--apply`，当前为 `STATE applied`；patched-source `gn gen` 成功（36095 targets）。
- **唯一阻塞：精确 Windows 工具链**。Chromium 156 的 `build/vs_toolchain.py` / `setup_toolchain.py` 要求
  `SDK_VERSION=10.0.28000.0`；本机只有 VS2022 BuildTools 17.14 + SDK 10.0.26100，且无 ATL/MFC、无
  Debugging Tools for Windows。
  - hermetic toolchain（`TOOLCHAIN_HASH=3bfcb536c8`）下载被 Google 401 拒绝：
    `chrome-wintoolchain` bucket 无匿名访问权限。
  - 诊断性本地绕行（SDK_VERSION 改 26100）在 abseil 即失败：
    `FILE_INFO_BY_HANDLE_CLASS` unknown type（SDK 26100 头文件不认 `NTDDI_WIN11_BR`），另有
    `dbghelp.dll not found ... Debugging Tools for Windows`。
  - 已回退本地工具链脚本改动；Chromium 源码本身未改，001-014 仍处于 applied。
- 需要用户以管理员权限安装：VS 2026 BuildTools（或更新后的 VS2022）包含
  “Desktop development with C++” + Windows 11 SDK 10.0.28000.0 + C++ ATL/MFC +
  Debugging Tools for Windows；或为 gsutil 提供可访问 `chrome-wintoolchain` 的 Google 凭据。
  安装后无需重新下载源码，直接 `gn gen`/`ninja` 继续 M0。
