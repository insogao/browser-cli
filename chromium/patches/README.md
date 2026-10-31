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
| `030-windows-explicit-show-first-show-retry.patch` | Windows K4（**USER_EXPLICIT 首次 ShowWindow 被 STARTUPINFO 吞掉时重试一次**） | 新增 `views::BrowserCliPresentForUserExplicitAction(Widget*)`：先校验顶层窗口（否则 fail closed），在 `ScopedForegroundOrigin(kUserExplicit)` 内 `Show()+Activate()`；若仍不可见，**只重试一次**同样的 Show/Activate，并记录 `BROWSER_CLI_PRESENTATION stage=host transition=presentation-retry reason=first-showwindow-not-honored`。重试不是放宽策略：两次尝试都重新经过共享 policy 与 patch 016 的 Windows host 门禁，窗口已可见时不会第二次触碰。`HandleUserExplicitShowRequest` 的 `kPresentExistingWindow`/`kCreateWindowThenPresent` 统一调用该 helper，删除两处内联 Show/Activate。根因：Windows 在 `STARTF_USESHOWWINDOW` 下把窗口的**第一次** `ShowWindow` 按启动时的 `wShowWindow` 解析；由隐藏控制台宿主（node `windowsHide`、服务、CI）启动的浏览器，若创建时的 show 已被后台策略拒绝，用户显式 show 的第一次调用会被静默忽略，HWND 仍不可见——这正是 `created-window-presentation-denied` 的真实原因，不是“不支持” | **Windows 实机证据（2026-10-30，out/WinDev，沙箱开启，无 --no-sandbox）**：`ninja -C out/WinDev -j 6 chrome` exit 0（`ui_views.dll`/`chrome.dll` 重链）。Path A：先以 `--browser-cli-background-presentation` 启动隐藏 managed 窗口，再以同一 profile 发第二次 `--browser-cli-user-explicit-show`，Win32 状态 `0x214B8 visible=False → visible=True foreground=yes`，producer `handled/existing-window-presented`。Path B：`--browser-cli-background-presentation --browser-cli-user-explicit-show --no-startup-window` 冷启动，producer `handled/window-created-and-presented`，Win32 `0x13149E visible=True foreground=yes`；日志出现 `transition=presentation-retry` 后 `window-pos-changed ... visible=yes`。两条路径的 policy 序列均为 `deny(background) → allow(user-explicit)`，未见任何 origin 放宽。sha256 `a5f97b9447334781fe3e088efe6a42cf3ea5c19fb80f954540d39636cf1e0ba0`，3 文件、58+/17-、119 行；正向 `git apply --check`（在 016+017 / 016+022+024 的基线文件上）rc=0。**未验证**：可见 managed 窗口的 K4 真实人类输入/IME（下一步 D）；正常可见控制台启动时首次 Show 未被吞的路径（重试仅在不不可见时触发，不会二次 Show）；多窗口选择；F3 产品截图仍受独立 P0 阻塞。**无单测**：该行为依赖真实 HWND 的 STARTUPINFO 语义，单测中显示窗口会违反“测试不得让窗口上屏”；以隔离真实 GUI 复现为准 |
| `031-input-ownership-new-tab-inheritance.patch` | P3 K15（**新 tab 立即继承窗口输入快照**） | content：`InputOwnershipOrigin{kNone,kExplicit,kInherited}` 加入 snapshot；`SetInputOwnership(..., origin)` 改为显式来源参数；explicit 覆盖 inherited 时**不比较版本**（独立版本空间），inherited 写入遇到 explicit 返回新增 `kOverridePresent` 且不改状态；新增 `ClearInheritedInputOwnership`（tab 移出窗口时清 inherited，explicit no-op）、`TouchInputOwnership`（窗口 heartbeat 只刷新 explicit tab 的 liveness）、`InputOwnershipStaleAfter`。chrome：新增 `browser_cli_input_ownership_window_bindings.{h,cc}`，`WindowOwnershipRegistry` 挂在 `BrowserContext` user data，按 `BrowserWindowInterface::GetSessionID()` 每窗口一个 `TabStripModelObserver`；`OnTabStripModelChanged` 的 `kInserted`/`kReplaced` 同步写 inherited、`kRemoved(kInsertIntoOtherWindow)` 清 inherited；窗口账本自身 version 单调、30s `last_ack` 新鲜度；`setOwnership({windowId})/heartbeat({windowId})/getOwnership({windowId})` 全部改走 registry，删除旧 `static map`/逐 tab 展开；无账本 heartbeat → `UNKNOWN_TARGET`；窗口 heartbeat 跳过 explicit tab（只 Touch），窗口 setOwnership 永不覆盖 explicit；feature off 不创建 registry/observer | **Windows 实测（2026-10-30，out/WinDev）**：`gn gen` exit 0（36113 targets）；`ninja -C out/WinDev -j 6 chrome content_unittests` exit 0；`content_unittests --gtest_filter='InputOwnership*'` **53/53 PASSED**（新增 OriginSeparatesInheritedAndExplicitWrites、InheritedVersionTouchAndClear）。真实 kernel E2E（K15，feature-on，真实扩展/`chrome.debugger`/native host）：同窗口 `setOwnership(windowId, ai, 1)` 后新 tab A **不 heartbeat** 立即 `getOwnership` = ai/1/stale=false，AI click 立即落地；tab A explicit human/1 后窗口 `setOwnership(ai, 2)` 与新 tab B 继承 ai/2，窗口 heartbeat(2) ×3 后 tab A 仍 human/1；切换激活不改变；K15 **15/15 checks PASS**。本轮 kernel 合计 79/79 checks、5 PASS、1 blocked（K4 真实人类输入，属 D 的 IME/可见窗口依赖，不是 K15）。sha256 `ea19872405d88055967d5a44024978e10eaef39fb97ef09270b9bb01ad41159a`，10 文件、760+/223-、1359 行；在 001–030 基线 `git apply --check` rc=0。**未覆盖**：会话恢复/跨窗口移动/`kReplaced` 只做了代码路径与 content 单测，未跑 browser test/E2E；K9/K10/K11 的恢复与移动场景待补齐；origin 不进入扩展 API（D3） |
| `033-windows-user-explicit-browserview-show.patch` | Windows K4 可见窗口内容初始化（**BrowserWindow::Show，而非只 Widget::Show**） | `ReusableWindow` 增加 `BrowserWindowInterface*`；新增 `PresentBrowserWindowForUserExplicitAction(browser, widget)`：在 `ScopedForegroundOrigin(kUserExplicit)` 内先 `BrowserWindow::Show()`（进入 `BrowserView::Show`，初始化/同步浏览器内容与可见性），再 `widget->Show()` 强制 HWND（含 patch 030 的首次 Show 重试），最后 `Activate()`；若仍不可见则同样序列重试一次。两条 producer 路径（既有窗口/冷启动创建）都改走该 helper。`BrowserView::Show` 的 `InitialWebUIManager` deferral 保留其 origin scope。解决“HWND visible 但 window surface 黑、AX/CDP 内容正常”的路径缺口；实际黑屏还与 dev build 的 `--in-process-gpu` 默认 GPU 合成有关，正常可视运行需配合 `--disable-direct-composition`（见 docs/normal-mode-ux-2026-10-30.md） | **Windows 实测（2026-10-30，out/WinDev）**：`ninja -C out/WinDev -j 6 chrome` exit 0；`PresentBrowserWindowForUserExplicitAction` 编译/链接通过。managed + BrowserPilot 窗口在 `--in-process-gpu --disable-direct-composition` 下屏幕中心像素 RGB **255,255,255**、窗口 `visible=yes`；不加 `--disable-direct-composition` 时 surface 仍黑（RGB≈35,31,29），因此本 patch 不能单独宣称修复 GPU 合成黑屏。sha256 `91321ed22d2358400b1080338b8bc84ee3491a3e256c5fb98cde38b93e28b4f8`，1 文件、40+/3-、87 行；在 001–032 基线 `git apply --check` rc=0。**未覆盖**：正常 GPU 合成下的独立验证；BrowserView deferral 的异步展示边界 |


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
