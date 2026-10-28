# chromium/patches

Browser CLI 对 Chromium 的修改按设计 §10 保持窄、小、清晰、可重放。

## 清单校验与重放工具

`chromium/patch-manifest.json` 是 001→014 的唯一机器可读顺序与哈希清单（`schema_version=1`，
`baseline_revision`/`baseline_version` 来自 `chromium/VERSION`）。`scripts/apply-patches.py`
（Python 3，仅标准库，Windows/macOS 通用）读取 manifest 与版本文件，默认只做只读校验：

```sh
python3 scripts/apply-patches.py --check
python3 scripts/apply-patches.py --check --checkout /path/to/chromium/src
python3 scripts/apply-patches.py --apply --checkout /path/to/chromium/src
python3 scripts/apply-patches.py --apply --dry-run --checkout /path/to/chromium/src
```

默认动作为 `--check`。未给 `--checkout` 时只校验 manifest/顺序/sha256/版本文件并输出
`STATE unverified`；给出 `--checkout` 时会额外校验 `HEAD == baseline_revision`，并用临时
`GIT_INDEX_FILE` + `git read-tree HEAD` 对整链执行 `git apply --cached --check`/`--cached`
（不触碰真实 index 与 worktree），最后判定 `STATE pristine`、`STATE applied` 或
`STATE partial`/`STATE conflict`。`--apply` 只接受 pristine：按 manifest 顺序对 worktree
执行 `git apply`（**不** `--index`、不 commit、不动真实 index），每步前重新 `--check`，
全部完成后再做反向链与工作区一致性自检；若已是 applied 则拒绝并提示改用 `--check`。
`--dry-run` 与 `--apply` 组合只打印将执行的 git 命令。

退出码：`0` = pristine/applied（`--check`）或应用成功（`--apply`；无 checkout 的纯静态校验也为 0）；
`1` = hash/HEAD/脏树/partial/conflict/应用失败等 fail-closed 情况；`2` = 参数错误。

**patch 文件必须是 LF 字节**，不得规范化后应用。仓库根 `.gitattributes` 固定
`chromium/patches/*.patch -text`；若工具报 CRLF，请先确认该属性并重新
`git checkout -- chromium/patches`（或删除后 `git checkout-index -f --`），不要用编辑器转换换行。

## 已入库 patch

| patch | 对应阶段 | 内容 | 状态 |
| --- | --- | --- | --- |
| `001-managed-window-mode.patch` | Phase 2 Gate A + Gate B（非 branding 全量切片） | BACKGROUND_PRESENTATION 三层拦截（policy + host + AppKit/bridge 兜底）+ 结构化日志；per-window `USER_EXPLICIT`/HUMAN 所有权与 origin 归因（Dock/Launchpad/app reopen 复用既有窗口、Extension/Tabs API、延迟 show/activate 归因延续）；程序化窗口变更门禁（fullscreen/bounds/z-order/minimize/restore/maximize/deactivate）；reopen 窗口选择器；3 个单测目标 | 与主负责人验证通过的 checkout 完全一致；从固定 revision 用临时 index 顺序重放 001→002→003 通过；增量构建与单测已复核；未 commit |
| `002-browser-cli-branding.patch` | 稳定签名/产品身份 + Logo | 新增 `chrome/app/theme/browser_cli/BRANDING`（Browser CLI / com.jiangao.browsercli / team RAXC5D3A3S）；新增 `mac_app_icon_dir` GN 参数并让 `chrome_app_icon`/`chrome_asset_catalog` 使用 Browser CLI 图标目录（图标文件由 `scripts/generate-icons.sh` 从已批准源图生成，不进入 patch） | 保持 2026-09-22 版本不变（sha256 未变）；与 checkout 反向校验一致；未 commit |
| `003-user-explicit.patch` | Phase 3（CLI 显式 `show` 生产者切片） | 纯函数分类器（`--browser-cli-user-explicit-show` 仅在无 URL/`--app`/`--app-id`/`--incognito`/`--enable-automation`/`--headless` 时才是 pure show）+ 14 个单测；macOS ObjC++ 共享展示单元 `chrome/browser/browser_cli/user_explicit_presentation.{h,mm}`（把 001 中放在 `app_controller_mac.mm` 的 Dock/Launchpad/reopen 展示 helper 迁移进来，保持唯一 per-window USER_EXPLICIT 路径）；接冷启动（`PreMainMessageLoopRunImpl`）与 ProcessSingleton 转发（`ProcessSingletonNotificationCallbackImpl`）两条入口；失败/拒绝 fail closed 并输出 `BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer` 结构化日志 | 静态重放 001→002→003 与当前已验证 checkout 逐字节一致（见下）；主负责人证据：`user_explicit_show_unittests` 14/14 PASS、policy 12/12、AppKit 2/2、reopen 8/8、`autoninja -C out/Dev chrome` exit 0；**可见 runtime/人工 Gate B 未执行（REVIEW）**；未 commit |
| `004-foreground-lease.patch` | Phase 4（Foreground Assist Slice 1：仅策略层 lease，非 runtime） | `ForegroundLease` 数据结构 + `login`/`captcha`/`confirmation`/`other` 原因枚举与稳定日志字符串；policy 单实例 per-window lease（`RequestForegroundAssist` 以**显式 requester origin** 校验且仅 `kAgentAssist` 可获批、busy/invalid/HUMAN-owned 拒绝、同窗口幂等不续期；`ReleaseForegroundAssist`；惰性 TTL `ReleaseExpiredForegroundAssistLeases`；`IsForegroundAssistLeased`）；`AllowPresentation` 追加 leased 分支（agent 仅 `show-and-activate`/`show-inactive`/`set-bounds`，internal 允许，extension/web-page/session-restore 拒绝），kUserExplicit 接管**先释放 lease 再授 HUMAN**；日志 `state=assist leased=yes`（`owned=yes` 仍仅表示 HUMAN）；新增 2 个单测目标 | 静态重放 001→002→003→004 与当前已验证 checkout 逐字节一致（见下）；主负责人证据：lease **8/8**、policy **27/27**（既有 12 项无回归），TTL 半开区间 `[granted_at, expires_at)` 已修正；**无 producer/host 接入/计时器/runtime 证据，未构建 chrome、未 commit（REVIEW）** |
| `005-foreground-assist-integration.patch` | Phase 4（Foreground Assist Slice 2：producer + host 接入 + 超时计时器，非 runtime） | 纯函数 CLI 分类器 `chrome/browser/browser_cli/foreground_assist.{h,cc}`（request/release 双开关、固定 reason 词表、严格十进制 uint64 target、**任何 tab switch 一律拒绝（不支持 tab 级目标）**、launch-args/automation/headless/与 USER_EXPLICIT 冲突全部 fail closed；**所有 assist 意图一律 consumed，绝不回落为普通 URL/app launch**；**冷启动 sanitizer `CommandLineForColdStartStartup()`：assist intent 时去掉 URL/app/incognito/automation/headless 并加 `--no-startup-window`，普通启动字节不变**）；超时控制器 `foreground_assist_controller.{h,cc}`（唯一 `OneShotTimer`，超时只回退**被跟踪的单个窗口**，HUMAN-owned / 已销毁窗口绝不 hide；**ASSIST→HUMAN 接管显式取消跟踪与计时器**，agent release 亦按窗口匹配取消）；macOS 集成 `foreground_assist_presentation.{h,mm}`（冷启动 + ProcessSingleton 两入口共用分类器与消费语义；`target_window` 必须是**顶层 BrowserWindow**：host 顶层性 + `FindBrowserWithWindow` 双校验；grant→present、release→hide 前双重校验非 HUMAN；`GetForegroundAssistController()` 供 USER_EXPLICIT 路径**成功后**取消 assist 计时器）；host 新增 `GetFromWidgetId`/`IsEligibleForegroundAssistTarget`/`RequestForegroundAssistLease`/`IsForegroundAssistLeased`/`ReleaseForegroundAssistLease`/`PresentForAgentAssistAction`/`HideForForegroundAssistRelease`（hide 前重新武装 AppKit 抑制；析构释放 lease）；`ui/views/BUILD.gn` 暴露 lease 头给 chrome；新增 2 个单测目标 | 静态重放 001→005 与 worktree 逐字节一致；单测：assist **17/17**、assist_controller **10/10**、lease **8/8**、policy **27/27**、回归 reopen **8/8**、CLI show **14/14**、AppKit **2/2**；新增静态门禁 `tests/check-foreground-assist-cold-start-order.sh` **7/7 PASS**；`ninja -C out/Dev chrome` **exit 0**（增量编译并重链接 `libchrome_dll.dylib`）；**未做浏览器 runtime/可见 UI 测试、未签名、未 commit（REVIEW）**；tab 级目标明确不支持 |
| `006-dock-reopen-timing.patch` | Phase 3（Dock reopen 耗时归因，**仅观察日志，非 runtime**） | 只在 `chrome/browser/app_controller_mac.mm` 的 `applicationShouldHandleReopen:` 路径新增结构化观察日志 `BROWSER_CLI_DOCK_REOPEN`：单次 reopen 以 `request_id` 关联 `reopen-enter` / `profile-ready` / `window-created` / `present-return`（及 `reopen-exit` 终止边界），每条记录含 `branch=reused|new|visible-windows|profile-picker|session-restore`、`result=`、单调 `elapsed_ms`（自 reopen-enter 起）与 `wall_ms`（Unix epoch 毫秒，可与外部 watcher 的 epoch 采样对齐）、`managed=yes|no`；复用分支在 `PresentExistingWindowForUserExplicitAction` 返回后记 `present-return`，新建分支在 `RunInLastProfileSafely` 回调内按 profile-ready → window-created → present-return 记边界。**纯观察**：不改变任何 show/hide/activate 行为，不把 `applicationDidBecomeActive` 当作点击；未编译、无 runtime/可见 UI 证据、未 commit（REVIEW） |
| `007-origin-fail-closed.patch` | P0（Origin fail-closed，**policy/单测 + 菜单入口归因，非 runtime**） | `ForegroundOrigin` 新增 `kUnknown` 并作为线程默认来源（`g_current_foreground_origin`、`ResetForTesting`）；`IsAutomationOrigin` 把 `kUnknown` 与 agent/extension/web-page/session-restore 同等对待，因此 HUMAN-owned 窗口对未知来源全动作 deny（含 `set-bounds`），无 lease 的未归属请求继续不上屏、不能借用 lease；`kInternal` 收敛为**显式 scoped 的可信 Chrome 用户 UI**（AppKit 菜单分发 `-executeCommand:withProfile:`、`-showPreferencesForProfile:`、`-orderFrontStandardAboutPanelForProfile:` 三处加 `ScopedForegroundOrigin(kInternal)`，保证用户菜单/手动入口不被 fail-closed 误挡）；新增/更新 policy 单测（未归属默认拒绝全矩阵、USER_EXPLICIT 仍 allow、可信 UI 仍 allow、BACKGROUND 仍抑制、lease 不接受未知来源、异步 continuation 默认 kUnknown）；`applicationShouldHandleReopen: hasVisibleWindows=YES` 的 ASSIST→HUMAN P0 只加调用链注释与 TODO，不做行为修复 | 静态重放 001→007 `git diff --quiet` rc=0、`git apply --check -R` 通过（见下）；**未编译、未 gn、未签名、未启动 App、无 runtime/可见 UI 证据、未 commit（REVIEW）**；独立复核发现的同窗口新旧 lease ABA（P0）与可见 ASSIST Dock 接管（P0）未在本 patch 修复 |
| `008-lease-generation-aba.patch` | P0（Foreground Assist lease generation ABA，**lease/controller/host/presentation + 单测，非 runtime**） | lease generation（`ForegroundLease.request_seq`）贯通到 timeout/release/hide 三段：policy 新增只读 `GetForegroundAssistLeaseGeneration(window_id)`（sweep 过期后返回 0），`ReleaseForegroundAssist(..., expected_generation)` 仅在 `request_seq` 匹配时释放并返回是否释放；host 新增 `GetForegroundAssistLeaseGeneration()`，`ReleaseForegroundAssistLease(reason, expected_generation)` 与 `HideForForegroundAssistRelease(generation)`（存在任何 live lease 时拒绝 hide）；controller 以 `(window_id, generation)` 跟踪：同窗口同 generation 幂等不续期、同窗口新 generation 仅替换 timer 且不隐藏（避免可见 ASSIST 窗口闪烁）、跨窗口旧 timer 只对旧窗口做终态清理、`OnLeaseTimeout` 依次验证 timer 入口 / policy 释放 / host hide 三段；`OnLeaseReleasedByAgent(window, generation)` 不匹配时不停新 timer；producer grant→present 后把 policy 实际生成的 generation 交给 controller，release 路径先取 generation 再 release/cancel/hide。新增/重写单测：**旧 timer 不得释放/隐藏新 seq（ABA 核心）**、同 live seq 幂等不续期、同窗口 regrant 替换 timer 不隐藏、stale agent release 不停新 timer、跨窗口旧 seq 不影响新 window、用户接管后 timer 不隐藏、policy generation 读取与带 generation 释放拒绝旧 seq | 静态重放 001→008 `git diff --quiet` rc=0、`git apply --check -R` 通过（见下）；**未编译、未 gn、未签名、未启动 App、未跑单测、无 runtime/可见 UI 证据、未 commit（REVIEW）**；用户接管/ASSIST 可见 Dock 漏授 HUMAN、Origin fail-open 由 007/其它 P0 处理，不在本 patch |
| `009-visible-assist-dock-takeover.patch`（REWORK 3，单网关 + 完整集合选窗） | P0（可见 ASSIST 窗口的用户 Dock 接管，**app_controller managed 分支 + 纯逻辑单测，非 runtime**） | `reopen_window_selection.{h,cc}`：纯函数 `SelectReopenFocusCandidate()` 复刻上游 focus 规则，新增 `HandleManagedDockReopen()` 单网关决策——**只选一次**、对选中 target **只调用一次**注入的展示回调（生产为 `PresentNativeWindowForUserExplicitAction`），返回 `kPresented`（网关成功，消费请求）或 `kNotPresented`（全隐藏/目标无 host/网关拒绝，**不得激活 target**）；回调是 helper 唯一能触碰 AppKit 的路径。`app_controller_mac.mm` managed Dock 分支：**对 `GetBrowserWindows()` 完整集合的每个 ordered NSWindow 都构造候选**（先读 miniaturized/visible/onActiveSpace，再解析 host；**host 缺失 = `window_id=0`，绝不 `continue` 过滤**），再 `HandleManagedDockReopen`——因此 AppKit 会聚焦的前方窗口若暂不可归因，也仍是唯一 target，**不会改选后方窗口**；被选中 `window_id=0` 时 helper 返回 `kNotPresented`，调用者走受控 USER_EXPLICIT 回退。`kPresented` 仅记录 `present-return visible-windows presented` 后 return NO，**无任何直接 `deminiaturize`/`makeKeyAndOrderFront`/`activateIgnoringOtherApps`**；`kNotPresented` 以同一 Dock request ID 记录 `gateway-denied`/`no-focusable-window` 并落入既有 USER_EXPLICIT 重用/新建路径。stock 分支保持上游 `FocusWindowSetOnCurrentSpace`（该函数未被修改）。无窗口纯单测：7 个 focus 选择 + 11 个单网关决策用例，含 **前方 A(id=0) + 后方 B(id≠0) 负例（可见与最小化各一）：不得 present B、回调零调用**。**无可见 browser tests、无 BUILD.gn 改动、未编译/未运行** | 静态重放 001→009 `git diff --quiet` rc=0、`git apply --check -R`、两次提取 sha256 一致（见下）；**未编译、未 gn、未签名、未启动 App、未跑测试、无 runtime/可见 UI 证据、未 commit（REVIEW）**；跨 Space/最小化动画/用户切走不抢焦点只能签名隔离构建后人工验收 |
| `010-user-menu-origin-propagation.patch`（REWORK 1） | 24E（用户菜单 USER_EXPLICIT + TabRestorer 异步 origin 传播，**app_controller + 纯逻辑单测 + 静态接线 checker，非 runtime**） | 三个 AppKit 用户菜单回调的同步 scope 由 `kInternal` 改为 **`kUserExplicit`**：`executeCommand:withProfile:`（`commandDispatch:`/`commandFromDock:` 进入）、`showPreferencesForProfile:`（`main_menu_builder.mm:79` 的 "Preferences…"）与 `orderFrontStandardAboutPanelForProfile:`（`:74` 的 "About"）——证据：`chrome::OpenOptionsWindow()`/`OpenAboutWindow()`（`browser_mac.cc:41/13`）会 `CreateBrowserWindow` + `browser->GetWindow()->Show()`，`kInternal` 在 managed 后台对未归属窗口 deny，用户菜单将打开不可见窗口；`kUserExplicit` 经既有 PresentationPolicy/NativeWidget gateway 呈现且只授目标窗口 HUMAN。`TabRestorer`：`RestoreByID` 按值捕获 `views::CurrentForegroundOrigin()` 传入实例（header 用 opaque scoped-enum 前置声明 `enum class ForegroundOrigin;` + typed 成员，无 int 往返、不依赖 ui/views）；`TabRestoreServiceLoaded` 经共享 seam `app_controller_mac::RunWithForegroundOrigin(foreground_origin_, BindOnce(&DoRestoreTab,...))` 重建 scope，回调后恢复 kUnknown；源码确认 `RestoreMostRecentEntry`→`RestoreEntryById`→`LiveTabContext::ShowBrowserWindow`/`CreateLiveTabContext` 全同步。C++ 纯单测 `AppControllerTest.RunWithForegroundOriginScopesOnlyItsCallback` 覆盖该生产 seam；thin repo `tests/tools/user-menu-origin-wiring-check.py`（7 checks + 8 fixture 自测）静态验证三菜单 scope、RestoreByID 捕获、ServiceLoaded 包装、typed 成员与 helper 实现。**未编译/未运行**；残留边界：History 菜单项 `history_menu_cocoa_controller.mm:46` 直接调用 `RestoreByID`（无 scope，仍 kUnknown）属未覆盖 producer | 静态重放 001→010、`git apply --check -R`、两次提取 sha256 一致（见下）；**未编译、未 gn、未签名、未启动 App、未跑单测、无 runtime/可见 UI 证据、未 commit（REVIEW）** |
| `011-omnibox-everywhere-activation-attribution.patch`（REWORK 1，补两个漏改 WebUI 调用方） | 24I（Omnibox Everywhere native activation 归因，**producer + gateway 路由 + 纯逻辑单测 + 静态接线 checker**） | `mac_window_util.{h,mm}`：新增纯函数 `DecideWindowActivationRoute(managed_mode, user_gesture)`（managed+无来源→`kNoop` fail-closed；managed+已证明浏览器进程用户输入→`kExplicitGateway`；stock→`kDirectAppKit`）；`ActivateBrowserWindowOnMac(bwi, user_gesture)` 先走该决策——managed 下 `kNoop` 直接返回（不 `activateIgnoringOtherApps`/不 `deminiaturize`/不 `makeKeyAndOrderFront`），`kExplicitGateway` 只经 `NativeWidgetMacNSWindowHost::PresentForUserExplicitAction()`（唯一 policy gateway，per-window HUMAN），只有 stock 才保留原 raw AppKit 序列（行为不变）。`omnibox_everywhere_service.{h,cc}`：`OpenUrl` 增加显式 `bool user_gesture`（按值传递，不依赖 TLS）并转发给 macOS helper；无来源默认 false。producer 归因（`omnibox_everywhere_ui_manager.cc`）：`OpenURLFromTab`（WebContents delegate/WebUI 回调，`params.user_gesture` 为渲染方自报、不可信）固定传 false；`ExecuteCommand` 的 4 个原生 context-menu 命令（kPasteAndSearch 的异步剪贴板回调按值携带、kManageSearchEngines、kCustomizeKeyboardShortcut、kSettings）传 true。**REWORK 1**：`composebox_everywhere_handler.cc`/`omnibox_everywhere_handler.cc` 两个 WebUI 调用方补 `/*user_gesture=*/false`（首次编译发现漏改，见下）。纯逻辑单测 `mac_window_util_unittest.cc`（窗口无关 route 矩阵）；静态 checker `tests/tools/omnibox-everywhere-origin-check.py`（6 checks + 8 fixture 自测：raw AppKit 必须晚于 managed 分支、缺 gateway/缺来源转发/`OpenURLFromTab` 传 true/context-menu 传 false 全部 FAIL） | REWORK 1 替换后：顺序重放 001→014 与 worktree 逐字节一致、worktree 反向 apply 通过、checker self-test 8/8 + checkout 6/6 复跑通过（见下）；**runtime/可见 UI 证据仍缺（REVIEW）**；UX 边界：overlay 结果选择/回车走 `OpenURLFromTab`，managed 下不再前置 Chrome（fail-closed），跨 Space 体验须签名隔离运行人工验收 |
| `012-user-history-menu-origin.patch` | 24E（History 原生菜单 USER_EXPLICIT 来源传播，**单源文件 + 静态接线 checker，非 runtime**） | `history_menu_cocoa_controller.mm::OpenURLForItem` 接受 typed `views::ForegroundOrigin`；profile 已加载的直接分支显式传 `kUserExplicit`，`RunInProfileSafely` 延迟分支按值绑定同一来源；在共享执行函数顶层建立 `ScopedForegroundOrigin`，覆盖 `RestoreByID` 与 URL `Navigate` 两条分支。Checker 自测 **18/18**（含 scope 只覆盖 session 分支的反例）及真实 checkout **12/12**；patch SHA-256 `d68f34f764aaeed74df7c6a3192e6b3a895b4c346a2fb4f1987d1f01a2475d79`，仅 1 文件、16+/4-。**未编译/未 gn/未签名/未运行 C++ 单测或 App，无 runtime/可见 UI 证据；24E 仍 REVIEW** |
| `013-command-line-intent-arbitration.patch` | P0（命令行意图仲裁：show/assist 次序缺陷，**producer 仲裁纯函数 + 两个入口接线 + 单测 + 静态 checker，非 runtime**） | 根因：两个生产入口（`ProcessSingletonNotificationCallbackImpl`、冷启动 `PreMainMessageLoopRunImpl`）都先 `HandleUserExplicitShowRequest` 再 `HandleForegroundAssistRequest`，同时带 `--browser-cli-user-explicit-show` 与 assist request/release 的命令行会先被 show 分类器判为 pure show（assist 开关被当作支持开关）并展示，`kBlockedByConflictingSwitches` 不可达。修正：`foreground_assist.{h,cc}` 新增 `BrowserCliCommandLineIntent{kOrdinary,kForegroundAssist,kUserExplicitShow}` 与唯一纯函数仲裁 `ArbitrateBrowserCliCommandLineIntent()`（assist intent 无条件优先），两入口改为同一 switch：assist case 无条件消费（ProcessSingleton `return;`，冷启动 `break`），show case 保留原条件回落，ordinary 直落；冲突命令行由 assist producer 判 `kBlockedByConflictingSwitches` 并打 `denied/conflicting-producer-switches`，show producer 与 presentation gateway 零调用。单测新增 8 个（仲裁矩阵 + 冲突 deny/consumed 不变量）；静态 checker `tests/tools/command-line-intent-arbitration-check.py`（self-test 6/6、真实 checkout 3/3、suite 第 29 组）验证两入口共用仲裁、assist case 先于 show case 且 handler 只出现在仲裁 switch 内；fixture 强制 show-first 旧顺序/单入口/show-在仲裁前/case 互换/assist case 缺 handler 全部 FAIL。patch SHA-256 `bcf96d316fc5d3e0987dc022c396434b7918e05ddc074893bade9bfa6e305757`，4 文件、45+/31- |
| `014-presentation-origin-default-comment.patch` | 文档修正（**仅注释，零行为变化**） | `ui/views/widget/browser_cli_presentation_policy.h` 中 `CurrentForegroundOrigin()` 的文档注释仍写 "Defaults to ForegroundOrigin::kInternal"，与 007 之后线程默认 `kUnknown`（fail closed）的实际行为相反；该头文件是安全模型的第一入口文档，陈旧注释会让后续读者重新相信已被 007 消除的 fail-open 假设。本 patch 仅把该句改为 "Defaults to ForegroundOrigin::kUnknown (fail closed)"，不触碰任何代码 | 静态重放 001→014 与 worktree 逐字节一致（rc=0）、worktree 反向 `git apply --check -R` 通过；sha256 `5a0d98328b28982b910da59bfd3095407386a41c616540d7fc426f167ebbb240`，仅 1 文件、3+/1-；001–013 sha256 复核未变；将随本轮统一构建一起编译 |
| `015-windows-views-export.patch` | Windows M0（**仅 GN，零行为变化**） | Windows 组件构建（`is_component_build=true`）下 `ui/views/views_export.h` 把 `VIEWS_EXPORT` 展开为 `__declspec(dllimport)`，而策略实现的 `source_set("browser_cli_presentation_policy")` 没有 `VIEWS_IMPLEMENTATION`，导致 `.cc` 以 dllimport 重声明自身定义。实测 20 个 `-Werror,-Winconsistent-dllimport` 错误。修复：给该 source_set 加 `defines = [ "VIEWS_IMPLEMENTATION" ]`；两个策略单测目标同样加该 define，因为单测静态链入 source_set（缺该 define 会触发 `LNK4217: locally defined symbol imported`，已实测复现）。未改任何 C++ 代码 | **Windows 实机证据**：修复后两个 obj 干净编译、两个单测目标链接成功，`browser_cli_presentation_policy_unittests.exe` **32/32 PASS**、`browser_cli_foreground_lease_unittests.exe` **8/8 PASS**（均 exit 0，`out/WinDev`，clang-cl + SDK 10.0.28000）；sha256 `3fc27375ac8ff55f95487f35603e4ecdf1446eabb44c007ee45a278e9c28a6be`，1 文件、6+/0-；这是**共用策略在 Windows 上的首次真实运行通过**，不构成窗口展示行为的证据 |
| `016-windows-native-presentation-gateway.patch` | Windows M1 Slice 1（**原生 gateway + host 门禁 + 命令行 producer 接线**） | 新增 `ui/views/widget/browser_cli_presentation_win.{h,cc}`：HWND→policy id（`HandleToUint32`）、`BrowserCliAllowsPresentation`、`BrowserCliShouldSuppressWindowPosChange`、`BrowserCliReleaseWindowState`，以及 Windows 版 `AllowProgrammaticWindowChangeFromCurrentOrigin`；**不复制任何策略判定**。`desktop_window_tree_host_win.cc` 在 Show/Activate/Deactivate/Maximize/Minimize/Restore/SetZOrderLevel/SetFullscreen 最前面查询共享策略，deny 直接返回；析构/`HandleDestroyed` 释放 HUMAN 与 lease。`hwnd_message_handler.cc` 在 `OnWindowPosChanging` 剥离外部 `SWP_SHOWWINDOW`，`OnWindowPosChanged` 只记观察日志。`browser_view.cc` 的 bounds 门禁与 `browser_extension_window_controller.cc` 的全屏门禁从 macOS 扩展到 Windows。`chrome_browser_main.cc` 两处命令行仲裁扩展到 Windows（assist 意图消费但不展示）。新增 `user_explicit_presentation_win.cc`（只实现 `HandleUserExplicitShowRequest`，需要展示时 fail closed）。`foreground_assist.cc` 用 `AppendSwitchNative` 修 Windows 宽字符串编译问题。**未在 `SetBoundsInPixels` 加门禁**（该路径只做不带 `SWP_SHOWWINDOW` 的 `SetWindowPos`，加门禁会破坏 Chrome 自身布局）。**明确未实现**：assist 的 Windows 展示、扩展 API origin scope、菜单/标题栏/History 用户入口、会话恢复 scope、taskbar 可信来源 | **Windows 实机证据**：`gn gen` exit 0；policy 32/32、lease 8/8、新 gateway 单测 **6/6**（无窗口显示，测试不再使用屏幕外定位）；`ninja chrome` exit 0；`ui_views.dll` 含 `BrowserCliWindowIdForHwnd`/`BrowserCliShouldSuppressWindowPosChange`/`BrowserCliAllowsPresentation`/`BrowserCliReleaseWindowState`，`chrome.dll` 含 `BROWSER_CLI_USER_EXPLICIT_SHOW` 与 `windows-gateway-not-implemented`；headless（`--no-sandbox` 仅本机诊断）实跑 `--browser-cli-background-presentation --browser-cli-user-explicit-show` 产生 `stage=policy decision=deny origin=unknown`、`stage=host decision=deny native=win-hwnd`、`stage=producer result=denied` 日志，exit 0。sha256 `422a33fa37aa32d17b0062d92390f1592e2f7af311cc7fab0beb8ff9e3bb7e73`，13 文件、530+/14-；反向 `git apply --check -R` exit 0 |
| `017-windows-foreground-assist.patch` | Windows M1 Slice 2（**assist producer + lease + timeout hide**） | 新增 `chrome/browser/browser_cli/foreground_assist_presentation_win.cc`：用 `GlobalBrowserCollection::ForEach` + `BrowserWindowInterface::GetWindow()->GetNativeWindow()` + `BrowserCliWindowIdForHwnd` 解析目标窗口；只接受顶层 BrowserWindow；请求/释放/超时全部复用共享 policy 与 `ForegroundAssistController`，不复制判定；HUMAN-owned、找不到窗口、lease 拒绝/忙/无效一律 fail closed 并记 `BROWSER_CLI_FOREGROUND_ASSIST stage=producer`。`browser_cli_presentation_win.{h,cc}` 新增 `BrowserCliPresentForAgentAssistAction`（kAgentAssist scope + Show/Activate）、`BrowserCliHideForForegroundAssistRelease`（拒绝 HUMAN-owned 与新 generation）、`BrowserCliIsEligibleForegroundAssistTarget`。`chrome_browser_main.cc` 两处 assist 分支改为调用 Windows producer。GN 新增 `foreground_assist_presentation_win` target。**未实现**：USER_EXPLICIT 的 Windows 原生展示、扩展 origin scope、菜单/标题栏/History、会话恢复、taskbar 来源 | **Windows 实机证据**：`gn gen` exit 0（36111 targets）；policy 32/32、lease 8/8、gateway **8/8**（新增 2 个 hide 拒绝用例）、controller **14/14**；`ninja chrome` exit 0；`ui_views.dll` 与 `chrome.dll` 均含三个新 gateway 符号，`chrome.dll` 含 `BROWSER_CLI_FOREGROUND_ASSIST`；headless 实跑（隔离 profile、无窗口）producer 实际执行并输出 `result=denied reason=launch-arguments-not-agent-assist managed=yes native=win-hwnd`，进程按 assist 长驻语义继续运行，手动终止后无残留。sha256 `2db6344cb931590442b01daedf111baab521b40a2828a08ca36c29d4e80b7d2c`，7 文件、434+/24-；反向 `git apply --check -R` exit 0 |
| `018-windows-extension-origin.patch` | Windows M1 Slice 3（**扩展 API `kExtension` origin scope**） | `tabs_api.cc` 的 5 处 `#if BUILDFLAG(IS_MAC)`（include、`windows.create`、`windows.update`、`tabs.create` 异步 continuation 分支与 `OnBrowserWindowCreatedWithOrigin` 定义）扩展到 `IS_MAC \|\| IS_WIN`；`tabs_api.h` 的 2 处 guard（`views::ForegroundOrigin` 前置声明、continuation 成员声明）同步扩展；更新已过时的“其它平台不编译 ui/views”注释。GN 无需改动：`//ui/views` 已在 `toolkit_views` 下加入 `chrome/browser/extensions/BUILD.gn`，Windows 为 true。不改变扩展 API 其它行为 | **Windows 实机证据**：`gn gen` exit 0；`ninja chrome` exit 0；policy 32/32、lease 8/8、gateway 8/8、controller 14/14 全部 exit 0；`chrome.dll` 从 `ui_views.dll` 导入 `ScopedForegroundOrigin` 构造/析构与 `CurrentForegroundOrigin`；**headless 加载临时 MV3 扩展实测**：扩展调用 `chrome.windows.update(id,{focused:true})`，日志出现 `stage=policy decision=deny action=show-and-activate origin=extension ... reason=background-suppresses-native-presentation`（2 次），无可见窗口，进程按长驻语义由超时终止。sha256 `8f0d021f254d99f9051f127827c4c65e3025ad11af198db292aecd29ef7b0a7c`，2 文件、15+/11-；反向 `git apply --check -R` exit 0。**未验证**：HUMAN-owned 窗口上的扩展 focus 拒绝（需要可见窗口接管）、macOS 重新编译 |
| `019-windows-user-entry-origin.patch` | Windows M1 Slice 4（**用户菜单/标题栏 `kUserExplicit` scope**） | `AppMenu::ExecuteCommand` 最前面建立 `ScopedForegroundOrigin(kUserExplicit)`，覆盖主应用菜单及其 History/New Window/Preferences 等子菜单；`SystemMenuModelDelegate::ExecuteCommand` 同样建立 scope，覆盖 Views 系统菜单；标题栏 minimize/maximize/restore 三个按钮回调改为先建 scope 再调用 `BrowserWidget::Minimize/Maximize/Restore`（close 不加）。`system_menu_model_delegate_impl` 补 `//ui/views` 直接 GN 依赖（原为传递依赖，编译可过但不符合严格依赖卫生）。**明确禁止**：不在 `WM_ACTIVATE`/`WM_NCACTIVATE`/`WM_SYSCOMMAND`/`GetForegroundWindow` 上标注用户来源；原生系统菜单由 OS 直接执行，任何进程都能 `SendMessage`，永久不做该归因 | **Windows 实机证据**：`gn gen` exit 0；`ninja chrome` exit 0；policy 32/32、lease 8/8、gateway 8/8、controller 14/14 全部 exit 0；scope 位置静态确认（`app_menu.cc:1339`、`system_menu_model_delegate.cc:257`、caption buttons `:51/:62/:73`），且 `PreHandleMSG`/`OnSysCommand` 内无任何 scope；`dumpbin /imports chrome.dll` 确认 `ScopedForegroundOrigin` 构造/析构与 `CurrentForegroundOrigin` 来自 `ui_views.dll`；headless `--dump-dom about:blank` exit 0。sha256 `3b05d1fe538d73c32a79ab0295a94f84cc1810279b05fcf40ca48412113d3a3a`，4 文件、35+/6-；反向 `git apply --check -R` exit 0。**未验证**：真实桌面菜单/标题栏交互、键盘快捷键归因（后续切片）、macOS 重新编译 |
| `020-windows-session-restore-origin.patch` | Windows M1 Slice 5（**会话恢复 `kSessionRestore` scope + `Init` 隐式 show 观察**） | `session_restore.cc` 的 4 个 `GetWindow()->Show()` 点（`use_new_window`、startup tabs、`ShowBrowser`、`kLastAndUrlsStartupPref` 新建窗口）各自用 `ScopedForegroundOrigin(kSessionRestore)` 包住 Show；保持 automation 语义（未归属窗口仍被拒绝，不改成 internal/user-explicit）。`sessions` 的 `impl` target 在 `enable_session_service`（`!is_android`）下补 `//ui/views` 依赖。`DesktopWindowTreeHostWin::Init` 的 `window()->Show()` 后加 managed-mode 观察日志 `transition=content-window-shown reason=init-implicit-show`，不阻止、不改变可见性 | **Windows 实机证据**：`gn gen` exit 0；`ninja chrome` exit 0（`ui_views.dll`/`chrome.dll` 重链）；policy 32/32、lease 8/8、gateway 8/8、controller 14/14 全部 exit 0；headless 实跑出现 `transition=content-window-shown ... reason=init-implicit-show`；两次会话恢复实测：第一次创建会话，第二次 `--restore-last-session` 出现 `stage=policy decision=deny action=show-and-activate origin=session-restore ... reason=background-suppresses-native-presentation`，无可见窗口（第二次进程未自动退出，手动终止，未诊断原因）。sha256 `cc6a1c8d7b3da542ed590f1f5465c9f731155306f232f2a98ea8c1b8f81f0d2b`，3 文件、33+/3-；反向 `git apply --check -R` exit 0。**未验证**：真实桌面会话恢复、ChromeOS/macOS 编译、第二次 headless 不自动退出的根因 |
| `021-windows-accelerator-origin.patch` | Windows M1 Slice 6（**键盘快捷键 `kUserExplicit` 归因**） | 仅在 `BrowserView::AcceleratorPressed`（`browser_view.cc:5254`）函数体第一行建立 `ScopedForegroundOrigin(kUserExplicit)`；这是 `views::AcceleratorManager` 分发键盘快捷键的窄入口。**不改** `BrowserCommandController`/`chrome::ExecuteCommand`（它们也接收扩展/WebUI 调用，blanket scope 会误归因）。**源码纠正**：`ScopedFullscreenVisibility` 在该 revision 从未被实例化（`fullscreen_handler.cc:64` 只有 `unique_ptr` 声明，无赋值），其 `ShowWindow(SW_SHOW)` 是死代码；且 `FullscreenHandler::SetFullscreen` 只由已被 patch 016 门禁的 `DesktopWindowTreeHostWin::SetFullscreen` 到达。因此本 patch 不改 fullscreen 代码 | **Windows 实机证据**：`gn gen` exit 0；`ninja chrome` exit 0（`browser_view.obj` 重编、`chrome.dll` 重链）；policy 32/32、lease 8/8、gateway 8/8、controller 14/14 全部 exit 0；静态确认 scope 只在 `AcceleratorPressed`，`browser_command_controller.cc`/`browser_commands.cc` 内无任何 scope；headless `--dump-dom about:blank` exit 0。sha256 `f8574e04da7eb472c16793cab252b12d4eccc3a17d25663aaa80374604220571`，1 文件、5+/0-；反向 `git apply --check -R` exit 0。**未验证**：真实桌面键盘快捷键归因（headless 无法注入快捷键）、macOS 重新编译 |
| `022-windows-user-explicit-presentation.patch` | Windows M1 Slice 7（**USER_EXPLICIT 复用已有窗口展示，REVIEW**） | `user_explicit_presentation_win.cc`：`FindReusableWindow()` 用 `GlobalBrowserCollection::ForEach(Order::kActivation)` 按激活顺序遍历浏览器窗口，只接受顶层 BrowserWindow，优先已 HUMAN 所有的窗口；`HandleUserExplicitShowRequest` 只在 managed + `kShow` 时查找窗口，并用 `PlanUserExplicitShow(..., has_reusable_window)` 决定。`kPresentExistingWindow` 在 `ScopedForegroundOrigin(kUserExplicit)` 内只调用 `Show()`+`Activate()`，由共享 policy 与 patch 016 host 门禁决定；成功后 `GetForegroundAssistController()->OnLeaseReleasedByUserTakeover(window_id)` 取消 assist 计时器，并记录 `handled/existing-window-presented`；失败记录 `failed/presentation-denied`。`kCreateWindowThenPresent` 仍 fail closed（`window-creation-not-implemented`），留 patch 023。`raw_ptr<views::Widget>` 用于结构体字段（chromium-rawptr 插件要求） | **Windows 实机证据（仅编译/单测/producer 路径）**：`gn gen` exit 0；`ninja chrome` exit 0；policy 32/32、lease 8/8、gateway 8/8、controller 14/14 全部 exit 0；静态确认 kUserExplicit scope 只包住 Show/Activate，创建路径无 Show/Activate；headless 诊断因 `--headless`/`about:blank` 被分类为 `launch-arguments-not-user-explicit`，**未执行到复用窗口展示路径**。sha256 `645ba87118eb5c376ccbf8082d0c322be9d9fc1fe892b6a275e1fbe84a7e65dc`，2 文件、126+/12-；反向 `git apply --check -R` exit 0。**状态 REVIEW**：可见窗口的展示/激活/HUMAN 所有权、assist 计时器取消、多窗口选择行为均未在真实桌面验证；headless 无法触发该路径（分类器把 `--headless` 当 launch argument）。**未做**：无窗口时创建（patch 023） |
| `023-windows-tsf-managed-skip.patch` | Windows 启动可靠性（**managed 模式跳过第三方 TIP 激活**） | 根因（cdb 60 帧栈）：`ui::InitializeInputMethod` → `TSFBridge::Initialize` → `ITfThreadMgr::Activate` → `MSCTF!CTip::Activate` → `SogouTSF`/`SogouPY!ImeExtension` 抛出 C++ 异常；搜狗自行处理该异常后调用 `ExitProcess`，因此**任何调用侧 SEH/C++ 保护都无法拦截**（实测 `try/catch`、`__try/__except`、外层 SEH 辅助函数三种写法均无效）。修复：managed 模式（`--browser-cli-background-presentation`）下 `TSFBridge::Initialize()` 直接返回 `E_FAIL` 并记录 `BROWSER_CLI_IME stage=tsf decision=skip`，浏览器在**无 IME** 状态下继续启动；`InputMethodWinTSF` 的所有调用点本就对空 bridge 做了判空。同时保留 `InitializeTsfBridgeGuarded()`（SEH）作为纵深防御，并给 `ui/base/ime/win` 开启 `//build/config/compiler:exceptions` | **Windows 实机证据**：`ninja ui_base_ime_win.dll chrome` exit 0；managed 模式 `--headless=new --dump-dom` **exit 0** 且返回 DOM（修复前 exit 3），日志出现 `BROWSER_CLI_IME stage=tsf decision=skip reason=managed-mode-avoids-third-party-tip`；**stock 模式（不带 managed 开关）仍 exit 3** 且无该日志，证明未改变 stock 行为。sha256 `d9ec9381be6507f7fe9e15e57d69b5efc3dafd16da7e33aaf3c70f1c36fe5111`，2 文件、约 60+/1-；反向 `git apply --check -R` exit 0。**代价**：managed 模式下无中文输入法（IME 候选窗不可用）；如需输入法，需用户移除/更换问题 IME。**未验证**：可见桌面下 managed 模式的 IME 降级体验、其他第三方 IME |
| `024-windows-user-explicit-window-creation.patch` | Windows M1 Slice 8（**USER_EXPLICIT 无窗口时创建窗口**） | `user_explicit_presentation_win.cc`：`kCreateWindowThenPresent` 不再 fail closed。新增 `ResolveLoadedProfileForUserExplicitShow()`（`GetStartupProfilePath(..., ignore_profile_picker=true)` + `GetProfileByPath`，profile 未加载/profile picker 一律拒绝）；随后 `chrome::OpenEmptyWindow(profile, should_trigger_session_restore=false)` 创建**恰好一个**空窗口，从返回的 `BrowserWindowInterface*` 解析 views host 与 HWND id（不会误用“最近窗口”），在 `ScopedForegroundOrigin(kUserExplicit)` 内 `Show()+Activate()`，成功后 `OnLeaseReleasedByUserTakeover` 取消 assist 计时器并记录 `handled/window-created-and-presented`。GN 增加 `//chrome/browser:browser_process`、`//chrome/browser/profiles:profile(_manager)`、`//chrome/browser/ui`、`//chrome/browser/ui/startup` 依赖 | **Windows 实机证据（沙箱开启，无 --no-sandbox）**：`gn gen` exit 0；`ninja chrome` exit 0；policy 32/32、lease 8/8、gateway 8/8、controller 14/14 全部 exit 0；冷启动实测 `chrome.exe --browser-cli-background-presentation --browser-cli-user-explicit-show --no-startup-window` 输出 `transition=human-ownership-granted window_id=...` 与 `BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=handled reason=window-created-and-presented`，进程按预期常驻。sha256 `fa1b75a94353ce47be68e1c308bb8f45fddc8b9da92a5c857d60740555c78d15`，2 文件；反向 `git apply --check -R` exit 0。**未验证**：可见桌面下新窗口的视觉确认（本轮仅用窗口枚举与日志）；profile picker 分支 |
| `025-windows-icon-branding.patch` | Windows M4（**品牌图标接入**） | `build/config/chrome_build.gni` 新增 `browser_cli_win_icon_dir`（空=stock；放在共享 gni 因为 `//chrome/app/BUILD.gn` 也要读）；`chrome_dll.rc` 增加 `#elif defined(BROWSER_CLI_WIN_ICON)` 分支，用完整路径宏（**RC 不拼接相邻字符串字面量**，实测会报 RC2135）；`chrome/BUILD.gn` + `chrome/app/BUILD.gn` 在参数非空时定义 `BROWSER_CLI_WIN_ICON`。`.ico` 由新增的 `scripts/generate-windows-icons.py`（Pillow，可复现）从已批准源图生成，**不入库**，由 `configure-windows.sh` 在 `gn gen` 前生成到 checkout。**不改 `chrome_exe.rc`**：其图标 ID 来自 `chrome_dll_resource.h`，而该头只被 `chrome_dll.rc` include，改 exe 的 rc 会报 `unknown resource` | **Windows 实机证据**：`configure-windows.sh` 端到端 PASS（生成 6 个 ico + gn gen 36111 targets）；`build-windows.sh -j 6` PASS；`chrome.ico` 的 **7/7** 尺寸载荷均在 `chrome.dll` 中命中；`verify-windows.sh` 6 项全 PASS。sha256 `a4a6893999a99433e7d2c0e8bc4c55900255d14cdd19c4b397ec212f218bb475`，4 文件、33+/0-；正向 `git apply --cached --check` 与反向 `git apply --check -R` 均 rc=0。**未验证**：任务栏/Alt-Tab/Explorer 的视觉确认；`chrome_exe.rc` 仍引用 stock 图标 |
| `026-browser-cli-capability-api.patch` | Phase 7（**BrowserPilot capability 接入**） | 新增 `chrome.browserCli.getCapabilities()`：`chrome/common/extensions/api/browser_cli.json` 定义 `Capabilities` 类型（`product`/`browserVersion`/`protocolVersion`/`managedMode`/`backgroundWindows`/`backgroundCapture`/`hardFocusIsolation`/`foregroundAssist`/`maxConcurrentWindows`）；实现 `chrome/browser/extensions/api/browser_cli/browser_cli_api.{h,cc}` **每个字段都从活状态推导**——`managedMode` 读 `BrowserCliPresentationPolicy::IsManagedModeEnabled()`，其余能力位与并发上限全部由它派生，所以同一份二进制在缺 `--browser-cli-background-presentation` 时如实报 false/1；`_api_features.json` 以**扩展 ID 的 SHA-1 + SHA-256 哈希**（不是 ID 字符串）allowlist 到 BrowserPilot，`contexts: ["privileged_extension"]`，网页与任意扩展不可见；直方图枚举 + `enums.xml` 同步；`api_sources.gni` / `chrome/browser/extensions/api/BUILD.gn` 接线 | 静态：`gn gen` rc=0（36112 targets）；`api_registration.lib` 与 `generated_api_types.lib` 构建 PASS，生成代码含 `BrowserCliGetCapabilitiesFunction` 注册与 allowlist 双哈希；`apply-patches.py --check --checkout D:\c\src` → **STATE applied**（26/26）；`test-core.mjs` 新增能力探测 7 组离线用例全 PASS（接口缺失/抛异常/协议不认识/`managedMode=false` 伪绿/正常/非法上限/UA 源码守卫）。sha256 `20d83a5d86f5fa8d469bb4817a0ddef9b4d27f5df415cbef98583f16c1d5a9fc`，9 文件、311 行 diff；反向 `git apply --check -R` rc=0 |


`012` 校验（24E History 原生菜单来源传播）：sha256
`d68f34f764aaeed74df7c6a3192e6b3a895b4c346a2fb4f1987d1f01a2475d79`；仅修改
`chrome/browser/ui/cocoa/history_menu_cocoa_controller.mm`（16 insertions / 4 deletions），基线为
001→011。静态 checker `tests/tools/user-menu-origin-wiring-check.py` 自测 **18/18**、真实
checkout **12/12**；patch 反向检查通过。主负责人复核修正了两个先前假通过：分支接线必须逐分支
检查，RAII scope 必须在顶层词法范围覆盖 `RestoreByID` 与 `Navigate`，并有负例验证 scope
仅覆盖 session 分支时失败。未编译、未运行 C++ 单测或 Browser CLI，无 runtime/UI 证据，仍为
REVIEW。

`013` 校验（P0 命令行意图仲裁，本 P0 首次修复 REWORK 0）：sha256
`bcf96d316fc5d3e0987dc022c396434b7918e05ddc074893bade9bfa6e305757`；共 4 个文件
（4 修改，无新增；`chrome/browser/browser_cli/foreground_assist.{h,cc}`、
`foreground_assist_unittest.cc`、`chrome/browser/chrome_browser_main.cc`），
45 insertions / 31 deletions，基线为 001→012 之后的 worktree。提取方式同 003–012
（`git diff` 于 001..012 累积临时 index 与 worktree）：与 worktree diff **逐字节一致**、
`git apply --cached --check` 前向通过、`git apply --check -R` 反向通过、重放到累积 index 后
`git diff --quiet` rc=0；001–012 的 sha256 复核未变。静态 checker
`tests/tools/command-line-intent-arbitration-check.py` 自测 **6/6**（show-first 旧顺序、
单入口、show-在仲裁前、case 互换、assist case 缺 handler 均 FAIL）、真实 checkout **3/3**；
`run-script-tests.sh` 第 29 组 4 项 PASS（套件 227/227），hygiene 14 入口 OK。**未编译、
未 gn、未签名、未启动 App、未运行 C++ 单测、无 runtime/可见 UI 证据、未 commit（REVIEW）**；
单测为静态编写，冲突矩阵的 runtime 证明需串行构建后运行
`foreground_assist_unittests` 复核。

`014` 校验（文档修正，2026-09-24 新负责人提取）：sha256
`5a0d98328b28982b910da59bfd3095407386a41c616540d7fc426f167ebbb240`；仅修改
`ui/views/widget/browser_cli_presentation_policy.h` 的 `CurrentForegroundOrigin()`
文档注释（3 insertions / 1 deletion，无代码变化），基线为 001→013 之后的 worktree。
提取前先验证 worktree 与 001→013 累积临时 index 逐字节一致（rc=0）；提取方式同
003–013（`git diff` 于累积 index 与 worktree）；重放 001→014 后 `git diff --quiet`
rc=0，worktree 反向 `git apply --check -R` 通过，001–013 sha256 未变。头文件被
001/007/008 共同触碰，本修正不回写 001（冻结不动），仅作为增量文档 patch 入库。

`001` 校验：sha256 `5e1bd1e47a7a180dddfce28aca43eea2b801dd56c0f184fa2a717c8cec99aafb`；
共 23 个文件（14 改 + 9 新增），1698 insertions / 8 deletions；应用基线为
`chromium/VERSION` 记录的 revision `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940`。
范围与 002 严格不重叠：001 不含 `chrome/BUILD.gn` 与 `chrome/app/theme/browser_cli/**`
（BRANDING 与图标属于 002/生成脚本 scope）；`mac/{app.icns,Assets.car}` 为
`scripts/generate-icons.sh` 的生成物，不进入任何 patch。
001 中 `ui/views/cocoa/native_widget_mac_ns_window_host.mm` 含一处仅格式变化
（`accessibilitySetPresenterProcessIdentifier` 选择器换行，无逻辑变化），系对验证通过状态的
原样打包。

`002` 校验：sha256 `acd496a18a0c2d1c2133b2cf1848974e6d40332bc4f959a5e989e19a45cccf61`；
共 2 个文件（`chrome/BUILD.gn` 修改 + `chrome/app/theme/browser_cli/BRANDING` 新增），
25 insertions / 2 deletions；应用基线同上。

`003` 校验：sha256 `9852de6570056b0f4d5bc72749e9dc17d54fb42d7e50f03017db54f8d8192e43`；
共 9 个文件（4 改 + 5 新增），597 insertions / 78 deletions；应用基线为 `001`+`002` 之后的
worktree。新增文件为 `chrome/browser/browser_cli/user_explicit_{presentation.h,presentation.mm,
show.cc,show.h,show_unittest.cc}`；修改 `chrome/browser/{BUILD.gn,chrome_browser_main.cc,
app_controller_mac.mm}` 与 `chrome/browser/browser_cli/BUILD.gn`。
003 是**从固定 revision + 001 + 002 到当前 CLI-show 源码的精确 delta**：除新增 CLI `show`
生产者外，还把 001 位于 `app_controller_mac.mm` 的 Dock/Launchpad/reopen 展示 helper
（`PresentBrowserWindowUserExplicitly` / `PresentExistingBrowserWindowForUserExplicitReopen`）
整体迁入新 ObjC++ 单元 `user_explicit_presentation.mm`（保留唯一 per-window USER_EXPLICIT 路径，
因此 003 含 78 行删除）。开发过程中该展示单元由 `.cc` 改名为 `.mm`（Objective-C++），并补上
`ui/base/base_window.h`（`ui::BaseWindow`）include，之后 `autoninja -C out/Dev chrome` exit 0。

`004` 校验：sha256 `4fb2bd2a9b66f93d3e47b623ba729d67b2d2ab9cb10e0e8adf67114799c838d4`；
共 7 个文件（3 新增 + 4 修改），1070 insertions / 13 deletions；应用基线为 `001`+`002`+`003`
之后的 worktree。新增 `ui/views/widget/browser_cli_foreground_lease.{h,cc}` 与
`browser_cli_foreground_lease_unittest.cc`；修改 `ui/views/widget/browser_cli_presentation_policy.{h,cc}`、
`browser_cli_presentation_policy_unittest.cc` 与 `ui/views/BUILD.gn`。范围严格限于设计 §6 的
**策略层 Slice 1**：不含 `requestForeground` 生产者、`NativeWidgetMacNSWindowHost` 展示/释放接入、
超时计时器、自动 hide/展示副作用与任何 runtime 证据（这些留给后续切片）。提取方式与 003 相同：
从 `001+002+003` 的临时 index 与当前 worktree 生成 `git diff`（新增文件先 `git add -N`），
两次独立提取逐字节一致。

`005` 校验：sha256 `7517c427aa346a71f091f7d3c13b2d072aa76b1c9e5452d5ce2645a39faf6e66`；
共 15 个文件（8 新增 + 7 修改），1490 insertions / 6 deletions；应用基线为
`001`+`002`+`003`+`004` 之后的 worktree。历史版本（已作废，记录保留）：
首版 `0dc439aa6c4be39ffb5319833ad9ef10111c05e425ba35c94035a9cb7be11e5d`（1134/2）、
复核修正版 `3665b71dedde4d9d83270518e594081fa865e2b5ea1d06b2edbade9ffa48c164`（1332/2）。
新增 `chrome/browser/browser_cli/foreground_assist.{h,cc}`、
`foreground_assist_controller.{h,cc}`、`foreground_assist_controller_unittest.cc`、
`foreground_assist_unittest.cc`、`foreground_assist_presentation.{h,mm}`；修改
`chrome/browser/{BUILD.gn,chrome_browser_main.cc}`、`chrome/browser/browser_cli/BUILD.gn`、
`ui/views/BUILD.gn`、`ui/views/cocoa/native_widget_mac_ns_window_host.{h,mm}` 与 003 的
`chrome/browser/browser_cli/user_explicit_presentation.mm`。范围严格限于
**producer + host 接入 + 超时计时器**：`target_tab` 明确不支持（任何 tab switch 被
`kBlockedByUnsupportedTabTarget` 拒绝）；不含浏览器 runtime/可见 UI 测试、签名，与
Extension/WebPage/SessionRestore/AgentAssist 其余来源的生产者（见设计 §6 与 PROJECT-PROGRESS D 区）。
提取方式同 003/004，两次独立提取 sha256 一致；`git apply --check -R` 反向校验通过。
**两次复核修正均已包含在上述 sha256 中**：(1) 接管取消计时器 / blocked assist 强制 consumed /
tab 目标拒绝 / target 限顶层 BrowserWindow；(2) 冷启动 assist intent 在 `Start` 前改用
`CommandLineForColdStartStartup()` 构造无 URL/app 副作用（并加 `--no-startup-window`）的启动命令行、
USER_EXPLICIT 接管改为呈现成功后取消计时器。详见
`docs/phase2-background-presentation-2026-09-22.md` §追加 7 及其两个修正小节。

`006` 校验：sha256 `90c7a6c1dd4ba1830a09fb27414d148d9aababb367dc878f150517c1d91d5a64`；
共 1 个文件（`chrome/browser/app_controller_mac.mm` 修改），96 insertions / 10 deletions；
应用基线为 `001`+`002`+`003`+`004`+`005` 之后的 worktree。范围严格限于
`applicationShouldHandleReopen:` 的观察日志（`BROWSER_CLI_DOCK_REOPEN`）：
`reopen-enter` → 复用分支 `present-return branch=reused`，或新建分支
`profile-ready` → `window-created` → `present-return branch=new`；其余终止路径记
`reopen-exit`（`visible-windows` / `profile-picker` / `session-restore`）。日志含
`request_id`、单调 `elapsed_ms`、epoch 毫秒 `wall_ms`、`branch`、`result`、`managed`。
不改任何 show/hide/activate 行为，不把 `applicationDidBecomeActive` 当点击；**本 patch
未编译、未 gn、未签名、未启动 App，无 runtime/可见 UI 证据（REVIEW）**。提取方式同
003/004/005（`git diff` 于 `001..005` 累积临时 index 与 worktree），两次独立提取 sha256
一致；`git apply --cached --check` 顺序重放 001→006 后 `git diff --quiet` rc=0，
`git apply --check -R` 反向校验通过。

`007` 校验：sha256 `e6d85e631663ae31583f05780a42e80a8c7ada976de15bbc803a902421221054`；
共 4 个文件（4 修改，无新增），171 insertions / 35 deletions；应用基线为
`001`+`002`+`003`+`004`+`005`+`006` 之后的 worktree。范围严格限于
`ui/views/widget/browser_cli_presentation_policy.{h,cc}`（`kUnknown` 默认 +
fail-closed 判定 + 注释）、`browser_cli_presentation_policy_unittest.cc`（失败/回归
测试）与 `chrome/browser/app_controller_mac.mm`（三处 AppKit 菜单入口显式
`ScopedForegroundOrigin(kInternal)` + 可见 ASSIST Dock 接管 P0 的调用链 TODO，
无行为修复）。**未编译、无 runtime 证据（REVIEW）**；提取方式同 003–006
（`git diff` 于 `001..006` 累积临时 index 与 worktree，无新增文件），两次独立提取
sha256 一致；`git apply --cached --check` 顺序重放 001→007 后 `git diff --quiet` rc=0，
`git apply --check -R` 反向校验通过。

`008` 校验：sha256 `d7468870a824c0750e89fd431888e488828bce2fd163a30fdbdde4c546d7bf2f`；
共 9 个文件（9 修改，无新增），494 insertions / 118 deletions；应用基线为
`001`+`002`+`003`+`004`+`005`+`006`+`007` 之后的 worktree。范围严格限于
`chrome/browser/browser_cli/foreground_assist_controller.{h,cc,_unittest.cc}`、
`foreground_assist_presentation.mm`、`ui/views/cocoa/native_widget_mac_ns_window_host.{h,mm}`、
`ui/views/widget/browser_cli_presentation_policy.{h,cc,_unittest.cc}`；**未触碰
`app_controller_mac.mm`、001–007 旧 patch、tests/ 与 Gate C/F**。提取方式同 003–007
（`git diff` 于 `001..007` 累积临时 index 与 worktree），两次独立提取 sha256 一致；
`git apply --cached --check` 顺序重放 001→008 后 `git diff --quiet` rc=0，
`git apply --check -R` 反向校验通过。

`009` 校验（REWORK 3，单网关 + 完整集合选窗）：sha256
`c96af999f8caf100c6468d64a011cc3906c30c07045d8e4b1ddc655fd2848577`；
共 6 个文件（6 修改，无新增），490 insertions / 21 deletions；应用基线为
`001`…`008` 之后的 worktree。范围严格限于
`chrome/browser/app_controller_mac.mm`（仅 managed Dock 分支 + include）、
`chrome/browser/browser_cli/reopen_window_selection.{h,cc,_unittest.cc}` 与
`chrome/browser/browser_cli/user_explicit_presentation.{h,mm}`；**未触碰 001–008 旧 patch、
`app_controller_mac_browsertest.mm`、`chrome/browser/BUILD.gn`、`FocusWindowSetOnCurrentSpace`、
tests/ 与 Gate C/F**。提取方式同 003–008（`git diff` 于 `001..008` 累积临时 index 与
worktree），两次独立提取 sha256 一致；`git apply --cached --check` 顺序重放 001→009 后
`git diff --quiet` rc=0，`git apply --check -R` 反向校验通过。

`010` 校验（REWORK 1：24E 用户菜单 USER_EXPLICIT + TabRestorer origin 传播）：sha256
`4694aeb6d285b65588de6bb2186df96c86ede74ad451ac5c658df9d127b0b977`；
共 4 个文件（4 修改，无新增），128 insertions / 20 deletions；应用基线为
`001`…`009` 之后的 worktree。范围严格限于
`chrome/browser/app_controller_mac.{h,mm}`、`app_controller_mac_unittest.mm` 与
`chrome/browser/BUILD.gn`（仅为 `core_unit_tests` 的 is_mac deps 增加 `//ui/views`，
供纯逻辑单测包含 `browser_cli_foreground_origin.h`）；**未触碰 001–009 旧 patch、
`user_explicit_presentation.*`、policy/host 实现与 tests/**。REWORK 1 相对首稿：
① 三个用户菜单入口（executeCommand / Preferences / About）统一 kUserExplicit；
② `TabRestorer` 改用 opaque scoped-enum 前置声明 + typed `views::ForegroundOrigin` 成员
（去掉 int 往返），并把延迟重建收敛到生产共享 seam
`app_controller_mac::RunWithForegroundOrigin()`；C++ 单测改测该 seam，
thin repo `tests/tools/user-menu-origin-wiring-check.py`（自测 8/8、真实 checkout 7/7）
静态验证生产接线。提取方式同 003–009
（`git diff` 于 `001..009` 累积临时 index 与 worktree），两次独立提取 sha256 一致；
`git apply --cached --check` 顺序重放 001→010 后 `git diff --quiet` rc=0，
`git apply --check -R` 反向校验通过。

`011` 校验（24I Omnibox Everywhere activation 归因）：sha256
`b38a3abbffba5cf2f6e80dd35518b5e8099b3bef1bc160a77123d747021a32e5`；
共 7 个文件（6 修改 + 1 新增），165 insertions / 14 deletions；应用基线为
`001`…`010` 之后的 worktree。范围严格限于
`chrome/browser/ui/omnibox/omnibox_everywhere/{mac_window_util.h,mac_window_util.mm,
mac_window_util_unittest.cc,omnibox_everywhere_ui_manager.cc,BUILD.gn}` 与
`chrome/browser/ui/omnibox/omnibox_everywhere_service.{h,cc}`；**未触碰 001–010 旧 patch、
app_controller/010 用户菜单入口、policy/host 实现与 tests/**。提取方式同 003–010
（`git diff` 于 `001..010` 累积临时 index 与 worktree；新增文件先 `git add -N`），
两次独立提取 sha256 一致；`git apply --cached --check` 顺序重放 001→011 后
`git diff --quiet` rc=0，`git apply --check -R` 反向校验通过。

`011` REWORK 1（2026-09-24 统一构建首次编译发现，REWORK 1）：原 011 修改
`OmniboxEverywhereService::OpenUrl` 签名（第 4 参新增 `bool user_gesture`）但漏改两个
WebUI 调用方，`ninja chrome` 在
`chrome/browser/ui/webui/omnibox_everywhere/composebox_everywhere_handler.cc:127` 与
`omnibox_everywhere_handler.cc:391` 报 `no matching member function for call to 'OpenUrl'`
（静态 REVIEW 阶段无法发现的集成缺口，正是本次统一构建的目的）。两处均为 renderer 经
mojo 调入的 WebUI handler，手势状态为渲染方自报、不可信，按 011 既有模型补
`/*user_gesture=*/false`（fail-closed，与 `OpenURLFromTab` 固定 false 同类）。新 sha256
`48b1376760dcc51d19b2a64d65223a532d949483b599061eacc942c81a36767a`，共 **9 个文件**
（8 修改 + 1 新增）；提取方式：`001..010` 累积临时 index（跳过旧 011）+ `git add -N`
（unittest）后 `git diff` 于上述 7 文件 + 2 个 WebUI handler；替换后顺序重放
001→014 全系列与 worktree `git diff --quiet` rc=0，worktree 反向 `git apply --check -R`
通过；`omnibox-everywhere-origin-check.py` 复跑 self-test **8/8**、真实 checkout **6/6**。
001–010、012–014 的 sha256 未变。


复现与校验（顺序必须 001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011 → 012 → 013 → 014 → 015 → 016 → 017 → 018 → 019 → 020 → 021 → 022 → 023 → 024 → 025）：

```sh
cd ~/dev/chromium/src
git rev-parse HEAD   # 7d0f28c30d1c0fadbce17ecec0ee2565aef9a940

# 无 checkout / 无构建校验（临时 index；不触碰工作区与真实 index）
export GIT_INDEX_FILE="$(mktemp)"
git read-tree HEAD
for p in 001-managed-window-mode 002-browser-cli-branding 003-user-explicit 004-foreground-lease 005-foreground-assist-integration 006-dock-reopen-timing 007-origin-fail-closed 008-lease-generation-aba 009-visible-assist-dock-takeover 010-user-menu-origin-propagation 011-omnibox-everywhere-activation-attribution 012-user-history-menu-origin 013-command-line-intent-arbitration; do
  git apply --cached --check /path/to/browser-cli/chromium/patches/$p.patch
  git apply --cached       /path/to/browser-cli/chromium/patches/$p.patch
done
git diff --quiet && echo "index == validated source state"
unset GIT_INDEX_FILE

# 在未打 patch 的 checkout 上重放（顺序执行）
git apply /path/to/browser-cli/chromium/patches/001-managed-window-mode.patch
git apply /path/to/browser-cli/chromium/patches/002-browser-cli-branding.patch
git apply /path/to/browser-cli/chromium/patches/003-user-explicit.patch
git apply /path/to/browser-cli/chromium/patches/004-foreground-lease.patch
git apply /path/to/browser-cli/chromium/patches/005-foreground-assist-integration.patch
git apply /path/to/browser-cli/chromium/patches/006-dock-reopen-timing.patch
git apply /path/to/browser-cli/chromium/patches/007-origin-fail-closed.patch
git apply /path/to/browser-cli/chromium/patches/008-lease-generation-aba.patch
git apply /path/to/browser-cli/chromium/patches/009-visible-assist-dock-takeover.patch
git apply /path/to/browser-cli/chromium/patches/010-user-menu-origin-propagation.patch
git apply /path/to/browser-cli/chromium/patches/011-omnibox-everywhere-activation-attribution.patch
git apply /path/to/browser-cli/chromium/patches/012-user-history-menu-origin.patch
git apply /path/to/browser-cli/chromium/patches/013-command-line-intent-arbitration.patch

# 已应用的 checkout 反向校验（当前完整 patch 栈 001…013 均可直接 -R）
git apply --check -R /path/to/browser-cli/chromium/patches/013-command-line-intent-arbitration.patch
```

003/004 的提取/校验不依赖 checkout 或构建：从上一序号累积的临时 index 与当前 worktree 生成
`git diff`（新增文件先 `git add -N`），重复生成逐字节一致；再把 001→002→003→004 应用到新的
临时 index，`git diff --quiet` 证明与已验证源码的所有 tracked 文件一致（仅
`chrome/app/theme/browser_cli/mac/{app.icns,Assets.car}` 为生成物、按设计不入 patch）。

注意：`002` 生效后产品改名为 `Browser CLI.app`，`chromium/args.gn` 会把它作为
`branding_file_path` 的选择条件；必须重新构建，旧 `Chromium.app` 为过期 ad-hoc 产物。

验证与结果记录见 `docs/phase2-background-presentation-2026-09-22.md` 与
`docs/signing-and-product-identity-2026-09-22.md`。

## 约束

- patch 相对 `chromium/VERSION` 的 revision 生成（`git diff`），必须能用 `git apply` 顺序重放；升级 Chromium 时只允许少数 patch 冲突。
- 禁止把 Chromium 源码或大文件复制进本仓库。
- 编号 002 已被 branding、003 已被 CLI `show` 生产者、004 已被 Foreground Assist Slice 1、
  005 已被 Foreground Assist Slice 2 集成、006 已被 Dock reopen 观察日志、007 已被
  Origin fail-closed、008 已被 lease generation ABA、009 已被可见 ASSIST Dock 接管、
  010 已被用户菜单 USER_EXPLICIT + TabRestorer origin 传播、011 已被 Omnibox Everywhere
  activation 归因、012 已被 History 原生菜单 origin 传播、013 已被命令行意图仲裁
  （show/assist 次序缺陷）占用；后续新 patch 从 014 起；设计 §10 的建议名依次顺延
  （原 `005-background-compositor`、`006-background-capture`、`007-popup-policy`）。
- `003` 的 runtime/人工 Gate B（可见 Dock/Launchpad/CLI show）尚未执行：当前证据只到
  单测、静态重放与完整构建；相关 runbook 见
  [`../tests/phase3-human-ownership-manual.sh`](../tests/phase3-human-ownership-manual.sh)。
- `004` 无 producer / host 接入 / 计时器 / runtime 证据：lease 只能由 policy API 建立，过期仅
  惰性 sweep，没有自动 hide；不得据此宣称 Foreground Assist 可用。后续切片见设计 §6。
- `005` 只到静态重放 + 单测 + 静态门禁 + `ninja chrome` exit 0：**没有任何浏览器 runtime/可见 UI
  证据**（未启动 App、未签名），`target_tab` 明确不支持（tab switch 一律拒绝）、agent 身份未与
  native host 会话绑定；`target_window` 已限制为顶层 BrowserWindow（host 顶层性 +
  `FindBrowserWithWindow`），非顶层 widget/dialog fail closed。冷启动"assist 不回落到普通 launch"
  目前由 `CommandLineForColdStartStartup()` 单测 + `tests/check-foreground-assist-cold-start-order.sh`
  静态门禁（含 upstream `--no-startup-window` 顺序断言）证明；**未 runtime 验证**，因此不得宣称
  blocked assist 已在真实运行中 fail-closed。运行时验收前必须用签名 App 走一次隔离 assist 流程
  （grant → present → timeout/release → 用户接管后旧 deadline 不 hide；冷启动带 URL/app 的
  blocked 负例不产生任何可见窗口）。
- `006` 只有源码 + 静态重放证据：**未编译、未签名、未启动 App、无任何 runtime/可见 UI 证据**。
  日志仅观察 reopen 边界（`request_id` 关联 + 单调 `elapsed_ms` + epoch `wall_ms`），
  不改变 show/hide/activate，也不把 `applicationDidBecomeActive` 当作用户点击；因此**不得**
  据此宣称 Dock 重开性能问题已修复或已定位。下一步只能由用户用带 `--enable-logging=stderr`
  的签名 App 复现 Dock 点击，再以 `BROWSER_CLI_DOCK_REOPEN` 的 `wall_ms` 与外部 watcher
  的 epoch 采样对齐做人工判定。
- `007` 只有源码 + 单测 + 静态重放/格式/空白证据：**未编译、未跑单测、无 runtime/可见 UI
  证据**。它把未归属来源改为 `kUnknown` 并对 HUMAN-owned 全动作 deny；以下边界**尚未证明**，
  必须在串行 build 后由人工 Gate B 逐项验证，失败即回滚或补显式可信来源：
  ①仅显式 scoped 的三个 AppKit 菜单入口被归为可信 Chrome UI，其它会程序化操作窗口的
  用户路径（bookmark/history/downloads 菜单触发的 `ScopedTabbedBrowserDisplayer`、
  `singleton_tabs`、`application:openURLs:`、WebApp 启动、`ActivateContents`、
  `BrowserView::SetBounds` 的 snap/session 路径）仍为默认 `kUnknown`，在 HUMAN 窗口上会被
  deny——需人工确认用户可见功能未回归，否则补显式归因（BLOCKED 项，不做猜测式扩散）；
  ②可见 ASSIST 窗口的 Dock 接管漏授 HUMAN 是独立 P0，由 `009` 处理（007 只留调用链 TODO）；
  ③lease generation ABA（P0）由 `008` 处理；④所有当前未被单测覆盖的 origin 生产者
  （session restore、app shim、CDP target handler）仍无归因，属于 fail-closed 后的已知 deny 面。
  运行时验收至少需要：
  `browser_cli_presentation_policy_unittests` 全 PASS（27→30：新增 3 个 fail-closed
  测试，原默认来源测试改名并翻转断言）、回归
  `user_explicit_show_unittests`/`reopen_window_selection_unittests`/AppKit 2/2、
  `ninja -C out/Dev chrome` exit 0，以及签名 App 的 Dock/Launchpad 打开、CLI show、
  用户原生最小化/恢复/全屏、菜单栏各入口、HUMAN 下另建后台窗口的人工矩阵。
- `008` 只有源码 + 单测 + 静态重放/格式/空白证据：**未编译、未跑单测、无 runtime/可见 UI
  证据，未 commit（REVIEW）**。generation 已贯通 timer 入口 / policy release / host hide
  三段；以下边界**未证明**，必须串行 build + 隔离 runtime 后才可宣称 ABA 已修复：
  ①`foreground_assist_controller_unittests` 的 FakeDelegate 只镜像 policy/host 合同，
  真实 policy+host 组合仍无 runtime 证据；②producer 的 AgentRelease 只带 window id
  （release 路径先读取当前 generation 再 release/cancel/hide，无法区分“为旧 seq 发的
  release 指令”——CLI 合同未带 generation，属已知语义：release 释放“当前 lease”）；
  ③`HideForForegroundAssistRelease(generation)` 的“存在 live lease 即拒绝 hide”依赖
  policy 单槽不变量；④用户接管路径仍由 `OnLeaseReleasedByUserTakeover` 只按 window
  cancel（接管时 policy 已释放 lease，window 变 HUMAN，无法再获新 lease，ABA 不适用）。
  串行验收：先 `autoninja -C out/Dev foreground_assist_controller_unittests`（含新增
  ABA/regrant/stale-release 用例）与 policy/lease/assist/CLI show/reopen/AppKit 回归全
  PASS，再 `ninja -C out/Dev chrome` exit 0，最后签名 App 隔离运行走
  grant→present→（惰性过期+同窗口 regrant）→timeout 序列，确认不误隐藏新 lease 且旧
  deadline 的日志不产生 release/hide。
- `009`（REWORK 3，单网关 + 完整集合选窗）只有源码 + 无窗口纯逻辑单测 + 静态重放/格式/
  空白证据：**未编译、未运行单测、无 runtime/可见 UI 证据，未 commit（REVIEW；按主负责人
  门禁等待复核，不自行标 DONE）**。门禁对照：
  ⓪**完整集合同一性（08:48 UTC 反例）**：候选循环对所有属于 `browserWindows` 的
  ordered NSWindow 都 push（先读 focus 属性；host 缺失只把 `window_id` 置 0，绝不
  `continue`），因此前方无 host 的 A 仍是选中目标，后方有 host 的 B 不会被改选；被选中
  `window_id=0` 时 helper 返回 `kNotPresented`，走受控 USER_EXPLICIT 回退。纯负例
  `FrontTargetWithoutHostNeverPresentsTheWindowBehind` 与最小化变体断言回调零调用；
  ①`SelectWindowToFocus(..., false)` 语义由纯函数 `SelectReopenFocusCandidate` 承接，
  app_controller 只构造一次候选、只调用一次 `HandleManagedDockReopen`，因此对 target
  只选一次、只调 gateway 一次；
  ②`kPresented` 时仅写同一 request ID 的 `present-return visible-windows presented`
  并 return NO，managed 分支内**不存在**任何 `deminiaturize`/`makeKeyAndOrderFront`/
  `activateIgnoringOtherApps`；
  ③gateway 返回 false（或全隐藏/无 host）时不激活 target，写同一 request ID 的
  `gateway-denied`/`no-focusable-window` 并落入 USER_EXPLICIT 重用/新建路径；创建路径
  失败时由既有 `present-return branch=new result=not-presented`/`profile-ready
  result=no-profile` 明确记录，不假报成功；
  ④stock 分支仍为上游 `FocusWindowSetOnCurrentSpace`（该函数未被修改）；
  ⑤无窗口单测共 26 个（原 8 + focus 7 + 单网关决策 11），断言成功时 gateway 恰好 1 次、
  target ID 与唯一选中窗口一致，失败时同一 target 恰好 1 次且 helper 无任何其它激活
  路径（原生激活次数为 0 由结构保证），不创建可见窗口。
  以下边界**未证明**，必须串行 build + 人工验收：
  ①`SelectReopenFocusCandidate` 是上游 focus 循环的纯函数镜像，真实 `orderedWindows`/
  `onActiveSpace` 行为只在 runtime 确认；②最小化恢复由单网关的 native bridge
  (`activateIgnoringOtherApps`→`makeKeyAndOrderFront`) 完成，动画/去最小化语义与跨
  Space 行为必须人工验收（未验收不得标 DONE）；
  ③用户切走后不抢焦点、以及 gateway 拒绝后的重用/新建回退体验需人工确认；
  ④`PresentNativeWindowForUserExplicitAction` 只按 id 解析既有 BrowserWindow/host，
  不创建窗口、不做任何直接 AppKit 调用；
  ⑤无窗口单测不覆盖 AppKit，也不存在自动可见窗口测试。
  串行验收：先 `autoninja -C out/Dev reopen_window_selection_unittests`（期望 26/26）
  与 policy/assist/CLI show/reopen/AppKit 回归全 PASS，再 `ninja -C out/Dev chrome`
  exit 0；安全签名隔离构建后由**用户手动**执行：可见 ASSIST 窗口点 Dock → 只有该窗口
  HUMAN、lease/timer 终止、之后 TTL/release 都不再隐藏它；全隐藏点 Dock → 能打开
  （reuse 或新建）且不上屏错误窗口；最小化 ASSIST 点 Dock → 恢复并 HUMAN；gateway
  拒绝路径（可由人工构造）不误报“已打开”；普通窗口/Launchpad/菜单栏行为与 Gate B
  既有矩阵一致。
- `010`（REWORK 1）只有源码 + 无窗口纯逻辑单测 + 静态接线 checker + 静态重放/格式/
  空白证据：**未编译、未运行单测、无 runtime/可见 UI 证据，未 commit（仅 REVIEW）**。
  ①三个 AppKit 菜单回调（`executeCommand:` / `showPreferencesForProfile:` /
  `orderFrontStandardAboutPanelForProfile:`）统一 `kUserExplicit`。前者的窗口分支为
  New Window/New Incognito/Restore Recent Tab；后两者最终会走到
  `chrome::OpenOptionsWindow()`/`OpenAboutWindow()`（`browser_mac.cc:41/13`：
  `CreateBrowserWindow` + `Show()`），`kInternal` 对未归属窗口 deny、用户菜单将打开
  不可见窗口，因此按 §3/§5.2 同属用户显式菜单入口；未把其它内部/异步 producer 批量
  改为 USER_EXPLICIT。scope 均同步结束；`IDC_OPTIONS` 等之后另行调用的
  `RunInLastProfileSafely` 回调各自带自己的 scope，不继承本 scope。
  ②`TabRestorer`：`RestoreByID` 按值捕获 typed `views::ForegroundOrigin`（opaque
  scoped-enum 前置声明，无 int 往返），`TabRestoreServiceLoaded` 经共享 seam
  `RunWithForegroundOrigin()` 重建后调用 `DoRestoreTab`；真实 restore 链在该回调内
  全同步（`ShowBrowserWindow`/`CreateLiveTabContext`），无第二异步边界。C++ 单测
  `AppControllerTest.RunWithForegroundOriginScopesOnlyItsCallback` 覆盖该生产 seam
  （值只在回调内可见、回调后回 kUnknown、kUnknown 不可借用）；生产接线本身由
  `tests/tools/user-menu-origin-wiring-check.py` 静态校验（自测 8/8：缺捕获、缺包装、
  kInternal、int 往返、注释伪装全部 FAIL；真实 checkout 7/7 PASS）。该 checker 尚未
  接入 `run-script-tests.sh`。
  ③未证明/需人工：①的三个菜单在 managed 后台下真实呈现与 HUMAN 归属；嵌套同步调用
  （设置页/对话框子 widget）不扩大 BrowserWindow 归属；History 菜单项
  `history_menu_cocoa_controller.mm:46` 直接调用 `RestoreByID`（无 scope，仍 kUnknown）
  属**未覆盖 producer**，不得声称所有 Restore Tab 入口都已用户显式化；timer/真实
  AppKit 行为均需人工。串行验收：先构建并运行
  `unit_tests --gtest_filter=AppControllerTest.RunWithForegroundOriginScopesOnlyItsCallback`
  （纯逻辑，无窗口）与既有 policy/CLI show/reopen/AppKit 回归，再 `ninja -C out/Dev chrome`
  exit 0；随后签名隔离构建由用户手动执行 managed 后台 + 菜单 New Window/New Incognito/
  Restore Recent Tab（Cmd+Shift+T）、Preferences…、About 与 History 菜单恢复项，核对
  只有目标窗口 HUMAN、其它后台/网页/SessionRestore 请求不被误授，且既有 007 误拦修复
  面无回归。
- `011`（24I）只有源码 + 无窗口纯逻辑单测 + 静态接线 checker + 静态重放/格式/空白证据：
  **未编译、未运行单测、无 runtime/可见 UI 证据，未 commit（仅 REVIEW）**。
  ①根因：`ActivateBrowserWindowOnMac` 原来无条件 raw AppKit（`activateIgnoringOtherApps`
  + `deminiaturize` + `makeKeyAndOrderFront`），完全绕过 policy/gateway；调用方
  `OmniboxEverywhereService::OpenUrl` 混合来源（`OpenURLFromTab` WebUI/渲染回调、
  context-menu 命令）。②修正：managed 下 raw 路径被 `DecideWindowActivationRoute`
  fail-closed/gateway 分支挡住（`kNoop` 无任何 AppKit；`kExplicitGateway` 只走
  `PresentForUserExplicitAction`）；provenance 显式按值传递，`OpenURLFromTab` 固定
  false（渲染方 `params.user_gesture` 不可信），4 个原生 context-menu 命令 true。
  ③未证明/需人工：managed 下 overlay 结果选择/回车不再前置 Chrome 是**有意 fail-closed
  的 UX 缺口**（缺可信 transient-activation 来源，不得靠 display-hide 补偿）；
  managed gateway 展示在跨 Space/全屏 overlay 场景是否等价于原 raw 路径（激活顺序、
  Mission Control Space 切换）须签名隔离运行人工验收；`PresentForUserExplicitAction`
  会授目标窗口 HUMAN（用户点击 context-menu 属设计范围，但需 Gate B 语义复核）。
  串行验收：构建并运行 `omnibox_everywhere` unit_tests（mac：`mac_window_util_unittest.cc`
  纯 route 矩阵，无窗口）与
  `python3 ../browser-cli/tests/tools/omnibox-everywhere-origin-check.py --checkout .`
  （6 checks + 8 fixture 自测），再 `ninja -C out/Dev chrome` exit 0；随后签名隔离运行
  人工核对：managed 后台下无来源的程序化 `OpenURLFromTab` 不激活 app/不授 HUMAN、
  不出现窗口；context-menu Preferences/Search 等按用户点击只呈现目标窗口并 HUMAN；
  stock Chromium 行为不变。
