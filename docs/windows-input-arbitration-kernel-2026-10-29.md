# Windows 内核侧输入仲裁（最小集，patch 028）— 2026-10-29

> 范围：Chromium 内核侧「最小可用输入仲裁」，Feature **默认关闭**，本轮只做源码 + 单元测试，
> 不实现扩展 API（`chrome.browserCli.inputOwnership`），不更新 `patch-manifest.json`，
> 不 commit / push。所有结论都可用本文件中的原始命令与输出复现。
>
> 设计依据：`docs/input-ownership-design-2026-10-28.md` §13（内核方案、信任模型、
> 四个入口、Feature/回滚要求）。基线：`D:\c\src` HEAD
> `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940` + patch 001-027，构建目录 `out/WinDev`。

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| `features::kInputOwnershipEnforcement` | 已加入 `content_features.{h,cc}`，`FEATURE_DISABLED_BY_DEFAULT` |
| 默认 mode | **`kAssist`（fail closed）**：未设置 mode 时无 grant，debugger 输入全部丢弃，人类输入照常（2026-10-29 按主代理审核修正，见 §4） |
| 决策器（纯逻辑、可单测） | `content::InputOwnershipDecider`，不依赖 WebContents / renderer / DevTools |
| 每 WebContents 控制器 | `InputOwnershipController`（`WebContentsUserData`） |
| 四个入口接线 | 3 个 `Forward*` + `Input.insertText` 全部接上 |
| Feature 关闭 | 决策恒「放行」、零状态写入、不创建 controller（双层证据，见 §5） |
| 构建 | 首次 `chrome` 655 步 + 收尾 20 步；默认值修正后 `chrome` 23 步、`content_unittests` 2 步（先 2202 步）；全部 EXIT=0，最终 `ninja -n` = no work |
| 单测 | `--gtest_filter=InputOwnership*` → 22 tests，`[ PASSED ] 22 tests`，EXIT=0 |
| patch | `chromium/patches/028-input-ownership-arbitration.patch`，1258 行，16 个文件段（11 改 + 5 新增） |
| patch sha256 | `c108e0f9c20c899be4e4dd9b13aec7f18651cd10338bf17abe1b7890e0a4889b` |
| manifest / commit | **未更新、未提交**（按要求留给主代理审核） |

## 1. 改动文件

### 1.1 新增

| 文件 | 作用 |
|---|---|
| `content/public/browser/input_ownership.h` | 公共门面：`InputOwnershipMode{kAi,kAssist,kHuman}`、`InputOwnershipSnapshot{mode,version}`（`mode` 默认 `kAssist`）、纯决策器 `InputOwnershipDecider` 声明。只前置声明 `blink::WebInputEvent`，不 include `web_contents.h`，因此可被 `renderer_host/` 引用 |
| `content/browser/input_ownership/input_ownership_decider.cc` | 决策器实现（模式判定、`kFromDebugger` 判定、grant 生命周期、手势级计数、human presence 去抖） |
| `content/browser/input_ownership/input_ownership_controller.h` | `InputOwnershipController : WebContentsUserData`；`ShouldDropAutomationEvent` / `ShouldDropAutomationTextInsert` / `snapshot()` + testing 写入口 |
| `content/browser/input_ownership/input_ownership_controller.cc` | controller 实现与 `WEB_CONTENTS_USER_DATA_KEY_IMPL` |
| `content/browser/input_ownership/input_ownership_controller_unittest.cc` | 19 个决策器测试 + 3 个 controller 测试（见 §7） |

### 1.2 修改

| 文件 | 改动 |
|---|---|
| `content/public/common/content_features.h/.cc` | 新增 `kInputOwnershipEnforcement`，`FEATURE_DISABLED_BY_DEFAULT` |
| `content/browser/renderer_host/render_widget_host_delegate.h/.cc` | 新增 `virtual InputOwnershipController* GetInputOwnershipController();`，默认返回 `nullptr` |
| `content/browser/web_contents/web_contents_impl.h/.cc` | override `GetInputOwnershipController()`：Feature 关闭返回 `nullptr`（不创建 user data）；开启时 `GetOrCreateForWebContents` |
| `content/browser/renderer_host/render_widget_host_impl.cc` | 文件内 helper `ShouldDropAutomationInput()` + 3 个 `Forward*` 汇合点检查 |
| `content/browser/devtools/protocol/input_handler.cc` | `InputHandler::InsertText` 在插入前查询同一 controller，丢弃时 `Response::ServerError` |
| `content/browser/BUILD.gn` / `content/public/browser/BUILD.gn` / `content/test/BUILD.gn` | 新文件加入 `:browser`、`browser_sources`、`content_unittests` |

改动只落在 `content/`，未触碰 patch 001-027 涉及的文件（chrome/ui/viz），也未触碰其它 worktree。

## 2. 关键设计

### 2.1 分层

```
RendererWidgetHostImpl::Forward*  ─┐
InputHandler::InsertText          ─┴─► RenderWidgetHostDelegate::GetInputOwnershipController()
                                            │ (WebContentsImpl override)
                                            ▼
                              InputOwnershipController (WebContentsUserData, 每 WebContents 一个)
                                            │
                                            ▼
                              InputOwnershipDecider (纯逻辑：mode + kFromDebugger + grant)
```

- 决策器不依赖 WebContents，单测可直接构造；时间通过 `base::TimeTicks now` 显式传入，
  单测不依赖真实时钟。
- controller 只做「per-WebContents 身份 + 转发」；它不产生用户身份，也不自行改 mode。
- **初始 mode = `kAssist`（fail closed）**：`InputOwnershipSnapshot::mode` 与决策器的 `mode_`
  默认值都是 `kAssist`。在受信扩展报告 mode 之前，没有 grant，所以 debugger 输入全部丢弃、
  人类输入照常。忘记 `setOwnership` 不会退化成「AI 全放行」。

### 2.2 Feature 门禁（默认关闭）

- 所有入口的第一行都是 `base::FeatureList::IsEnabled(features::kInputOwnershipEnforcement)`；
  关闭时 `ShouldDropAutomationInput()` 直接返回 `false`，**不查询 delegate、不查询 controller**。
- `WebContentsImpl::GetInputOwnershipController()` 关闭时返回 `nullptr`，因此连
  `WebContentsUserData` 都不会被创建。
- 决策器内部同样先判 Feature，关闭时 `ShouldDeliverDebuggerEvent()` / `ShouldDeliverDebuggerTextInsert()`
  返回 `true` 且**不读不写任何状态**（不消耗 grant、不记录 human presence）。
- `NotifyHumanPresenceForTesting()` 在 Feature 关闭时也是 no-op（不发事件语义）。
- 例外（有意）：`SetModeForTesting` / `GrantDebuggerInputForTesting` 无论 Feature 开关都会写状态。
  它们是测试驱动入口，不是生产写路径；正是为了证明「即便状态是 HUMAN + 有 grant，Feature
  关闭时仍全部放行」。生产写路径（受信扩展 API）本轮不实现。

### 2.3 grant 采用「手势级」计数（不是逐事件）

与设计 §13.5.3 对齐，避免一次点击吃掉 2 次 grant、一次按键吃掉 3 次：

| 手势 | 扣减时机 | 同一手势内免扣 |
|---|---|---|
| 鼠标 | `kMouseDown`；无按键的 move/hover 每次扣 1 | 已授权的 press→release，以及 release 后 200ms 内的 move/click |
| 滚轮 | 每个 `kMouseWheel` 扣 1 | — |
| 键盘 | `RawKeyDown` / `KeyDown` 扣 1 | 同一物理键的 `kChar` / `kKeyUp`，直到下一次 RawKeyDown |
| 文本 | 每次 `insertText` 扣 1；300ms 内的连续插入共享 1 次 | 滚动窗口覆盖 `type` 的逐字输入 |

其它约束：

- 未被授权的 `kMouseDown` 会连带丢弃其 `kMouseUp`（页面不会看到「只抬起没按下」）。
- 未授权的 `RawKeyDown` 在 RWHI 里同时置 `suppress_events_until_keydown_ = true`，
  复用既有机制把后续 `kChar`/`kKeyUp` 一并挡掉。
- grant 写入时夹紧：`actions ∈ [0, 10000]`、`ttl ≤ 600s`（与扩展侧 §12.5 常量一致）。
- grant 只在 ASSIST 生效；`SetModeForTesting` 变更 mode 时清空 grant 与所有手势中间态。

### 2.4 四个入口的精确位置

| 入口 | 位置 | 丢弃动作 |
|---|---|---|
| `ForwardMouseEventWithLatencyInfo` | 类型 `CHECK` 之后、`mouse_event_callbacks_` 之前 | `return`（不触发任何 observer/callback） |
| `ForwardWheelEventWithLatencyInfo` | `ScopedSafetyChecksExclusion` 之后、`IsIgnoringWebInputEvents` 之前 | `return` |
| `ForwardKeyboardEventWithCommands` | **`PreHandleKeyboardEvent` 之后**、touch emulator 之前 | `RawKeyDown/KeyDown` 置 `suppress_events_until_keydown_=true` 后 `return` |
| `InputHandler::InsertText` | 解析出 `widget_host` 之后、`ImeCommitText` 之前 | `callback->sendFailure(Response::ServerError("InputOwnership: insertText rejected (grant required)"))` |

键盘检查放在浏览器快捷键处理之后，保证 HUMAN 模式下 Ctrl+L / Ctrl+T / Ctrl+W / F5 仍然由浏览器
处理（设计 §4.1 不变量 4）；只有真正要进页面的键盘输入才受仲裁。

## 3. 决策矩阵

`fromDebugger = (event.GetModifiers() & kFromDebugger) != 0`。Feature 关闭时下表全部为 D（放行）。
**未设置 mode 时按 `kAssist` 处理（fail closed）**，因此首行就是「人类 D / debugger 丢弃」。

| mode | fromDebugger | 事件 | grant 状态 | 结果 |
|---|---|---|---|---|
| **未设置（默认 kAssist）** | 是 | 任意鼠标/滚轮/键盘/text | 无（初始无 grant） | **丢弃** |
| 未设置（默认 kAssist） | 否（人类） | 任意 | 任意 | **D**，并记一次 human presence |
| 任意 | 否（人类） | 任意鼠标/滚轮/键盘 | 任意 | **D**，并记一次 human presence（300ms 去抖） |
| kAi | 是 | 任意 | 任意 | **D**，不消耗 grant |
| kHuman | 是 | 任意（鼠标/滚轮/键盘/text） | 任意（含已授权） | **丢弃**，不消耗 grant |
| kAssist | 是 | `kMouseDown` | 无/过期/用尽 | **丢弃** |
| kAssist | 是 | `kMouseDown` | 有效 | **D**，`remaining--`，开启鼠标手势 |
| kAssist | 是 | `kMouseUp` / 手势内的 move 等 | 同一次已授权手势 | **D**，`kMouseUp` 关闭手势并给 200ms 宽限 |
| kAssist | 是 | `kMouseUp`（无匹配按下） | 无 | **丢弃** |
| kAssist | 是 | 无按键 move（手势外、宽限外） | 无有效 grant | **丢弃** |
| kAssist | 是 | 无按键 move | 有效 | **D**，`remaining--` |
| kAssist | 是 | `kMouseWheel` | 无有效 grant / 有效 | **丢弃** / **D** 且 `remaining--` |
| kAssist | 是 | `RawKeyDown`/`KeyDown` | 无有效 grant / 有效 | **丢弃** / **D** 且 `remaining--` |
| kAssist | 是 | `kChar`/`kKeyUp`（同一已授权按键） | — | **D**（免费） |
| kAssist | 是 | `kChar`/`kKeyUp`（无已授权按下） | — | **丢弃** |
| kAssist | 是 | `insertText` | 300ms 窗口内已扣过 | **D**（免费，窗口顺延） |
| kAssist | 是 | `insertText`（窗口外） | 无有效 grant / 有效 | **丢弃**（ServerError）/ **D** 且 `remaining--` |
| kAssist | 是 | 触摸/手势等未接线类型 | 无有效 grant / 有效 | **丢弃** / **D** 且 `remaining--`（保守 fail-closed） |

版本语义：`snapshot().version` 在每次**实际** mode 变更时 +1；重复设置同一 mode 不变（幂等）。

## 4. 与设计 §13 的差异（本轮最小集，主代理需确认）

| 设计 §13 | 本轮实现 | 说明 |
|---|---|---|
| 快照未知 → `kAssist`（fail closed） | **已对齐**：`InputOwnershipSnapshot::mode` 与 `InputOwnershipDecider::mode_` 默认值均为 `kAssist` | 2026-10-29 按主代理审核修正（原为 `kAi`）。未设置 mode 且无 grant 时 debugger 输入全部丢弃，由 `DefaultModeIsAssistFailClosed` 覆盖 |
| 30s 无更新 → 降级 ASSIST（stale） | 未实现 stale 定时器 | 默认值已是 ASSIST，因此「从未 setOwnership」是 fail-closed；但若扩展先 `setOwnership(kAi)` 之后失联/崩溃，状态会一直保持 `kAi`。扩展 API 那一轮必须补心跳（或每 30s 失效回 ASSIST） |
| grant 表按 `DevToolsAgentHost*`（session）分桶 | 单个全局 grant 桶 | 本轮无扩展 API，无法安全获得受信 session 身份；分桶留到扩展 API 那一轮（需要 `ExtensionDevToolsClientHost` 匹配） |
| `grantInput` 只由受信扩展 API 写入 | 只有 `GrantDebuggerInputForTesting` | 符合「本轮不实现扩展 API」 |
| `human_presence` 通过 `SetHumanPresenceCallback` 上报 chrome 层 | 只在决策器内 300ms 去抖计数 | 无 chrome 层回调，避免本轮引入未受审核的事件桥 |
| `InputOwnershipSnapshot.stale` 字段 | 无（只有 `mode` + `version`） | 按本轮任务书的结构体定义 |

## 5. Feature-off 证据

代码层（三条独立证据）：

1. `render_widget_host_impl.cc` 的 `ShouldDropAutomationInput()` 第一行即
   `if (!base::FeatureList::IsEnabled(features::kInputOwnershipEnforcement)) return false;`
   —— Feature 关闭时不触碰 delegate / controller，三个 `Forward*` 的既有分支顺序和返回值不变。
2. `WebContentsImpl::GetInputOwnershipController()` 在 Feature 关闭时返回 `nullptr`，不创建
   `WebContentsUserData`（`InputOwnershipControllerTest.FeatureOffDoesNotCreateController` 断言
   `GetInputOwnershipController()` 与 `FromWebContents()` 都是 `nullptr`）。
3. `InputOwnershipDecider::ShouldDeliverDebuggerEvent()` / `ShouldDeliverDebuggerTextInsert()`
   先判 Feature；关闭时返回 `true` 且不写任何状态。单测
   `FeatureOffDeliversEverything` 在 **HUMAN mode + 5 次 grant** 下断言鼠标/滚轮/键盘/文本全部
   放行、grant 仍为 5、human presence 计数为 0（连 `NotifyHumanPresenceForTesting` 都不计数）。

构建/运行层：

- Feature 默认值 `FEATURE_DISABLED_BY_DEFAULT`，`out/WinDev` 未传任何 `--enable-features`，
  单测进程内默认关闭；上述 22 个测试全部在默认（关闭）状态下启动，其中
  `FeatureOffDeliversEverything` / `FeatureOffDoesNotCreateController` 显式关闭 Feature 断言。
- 未新增 chrome://flags 条目（见 §8 未覆盖项）；需要显式开启时用
  `--enable-features=InputOwnershipEnforcement`（`base::Feature` 名称即
  `kInputOwnershipEnforcement` 去掉前缀 `k`）。

## 6. 构建命令与结果

环境（Git Bash）：

```bash
export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
export PATH="/d/c/src/third_party/cpython3/host/bin:$PATH"
cd /d/c/src
```

```bash
# 1) chrome（首次 655 步；因 content_features.h 变更触发 content/ 与 chrome/ 增量重编 + 链接）
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome
# → EXIT=0，最后 [652/655] LINK(DLL) chrome.dll；日志 build-logs/028-chrome-build.log

# 2) 注释改动后收尾（20 步，含 input_ownership_controller.obj / input_handler.obj / content.dll）
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome
# → EXIT=0

# 3) content_unittests（首次 2202 步）
third_party/ninja/ninja.exe -C out/WinDev -j 6 content_unittests
# → EXIT=0，[2202/2202] LINK content_unittests.exe；日志 build-logs/028-content-unittests-build.log

# 4) 默认值改为 kAssist 后重编（chrome 23 步、content_unittests 2 步）
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome            # → CHROME_EXIT=0
third_party/ninja/ninja.exe -C out/WinDev -j 6 content_unittests # → UNITTESTS_EXIT=0
# 日志 build-logs/028-rebuild-assist-default.log

# 5) no-work 复核
third_party/ninja/ninja.exe -C out/WinDev -n -j 6 chrome          # → ninja: no work to do.
third_party/ninja/ninja.exe -C out/WinDev -n -j 6 content_unittests # → ninja: no work to do.
```

未启动可见窗口；未启动 Backlight；未使用 `--use-mock-keychain`（Windows 不涉及）；未触碰用户
Chrome / profile；未出现任何系统对话框。

## 7. 单元测试原始输出

命令：

```bash
cd /d/c/src/out/WinDev
./content_unittests.exe --gtest_filter=InputOwnership*
```

原始输出（`build-logs/028-input-ownership-tests-assist-default.log`，完整未删减）：

```text
IMPORTANT DEBUGGING NOTE: batches of tests are run inside their
own process. For debugging a test inside a debugger, use the
--gtest_filter=<your_test_name> flag along with
--single-process-tests.
Using sharding settings from environment. This is shard 0/1
Using 1 parallel jobs.
Note: Google Test filter = InputOwnershipDeciderTest.FeatureOffDeliversEverything:InputOwnershipDeciderTest.DefaultModeIsAssistFailClosed:InputOwnershipDeciderTest.HumanInputIsAlwaysDelivered:InputOwnershipDeciderTest.HumanInputDoesNotConsumeGrant:InputOwnershipDeciderTest.HumanModeDropsDebuggerInput:InputOwnershipDeciderTest.AiModeDeliversDebuggerInput:InputOwnershipDeciderTest.AssistWithoutGrantDropsDebuggerInput:InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture:InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease:InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit
[==========] Running 10 tests from 1 test suite.
[----------] Global test environment set-up.
[----------] 10 tests from InputOwnershipDeciderTest
[ RUN      ] InputOwnershipDeciderTest.FeatureOffDeliversEverything
[       OK ] InputOwnershipDeciderTest.FeatureOffDeliversEverything (0 ms)
[ RUN      ] InputOwnershipDeciderTest.DefaultModeIsAssistFailClosed
[       OK ] InputOwnershipDeciderTest.DefaultModeIsAssistFailClosed (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanInputIsAlwaysDelivered
[       OK ] InputOwnershipDeciderTest.HumanInputIsAlwaysDelivered (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanInputDoesNotConsumeGrant
[       OK ] InputOwnershipDeciderTest.HumanInputDoesNotConsumeGrant (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanModeDropsDebuggerInput
[       OK ] InputOwnershipDeciderTest.HumanModeDropsDebuggerInput (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AiModeDeliversDebuggerInput
[       OK ] InputOwnershipDeciderTest.AiModeDeliversDebuggerInput (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistWithoutGrantDropsDebuggerInput
[       OK ] InputOwnershipDeciderTest.AssistWithoutGrantDropsDebuggerInput (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture
[       OK ] InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease
[       OK ] InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit
[       OK ] InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit (0 ms)
[----------] 10 tests from InputOwnershipDeciderTest (20 ms total)

[----------] Global test environment tear-down
[==========] 10 tests from 1 test suite ran. (22 ms total)
[  PASSED  ] 10 tests.
[1/22] InputOwnershipDeciderTest.FeatureOffDeliversEverything (0 ms)
[2/22] InputOwnershipDeciderTest.DefaultModeIsAssistFailClosed (0 ms)
[3/22] InputOwnershipDeciderTest.HumanInputIsAlwaysDelivered (0 ms)
[4/22] InputOwnershipDeciderTest.HumanInputDoesNotConsumeGrant (0 ms)
[5/22] InputOwnershipDeciderTest.HumanModeDropsDebuggerInput (0 ms)
[6/22] InputOwnershipDeciderTest.AiModeDeliversDebuggerInput (0 ms)
[7/22] InputOwnershipDeciderTest.AssistWithoutGrantDropsDebuggerInput (0 ms)
[8/22] InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture (0 ms)
[9/22] InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease (0 ms)
[10/22] InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit (0 ms)
Note: Google Test filter = InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerTick:InputOwnershipDeciderTest.AssistGrantOneAllowsOneKeyPress:InputOwnershipDeciderTest.AssistTextInsertSharesOneBurst:InputOwnershipDeciderTest.AssistGrantExpiry:InputOwnershipDeciderTest.AssistGrantExhaustion:InputOwnershipDeciderTest.GrantClearedOnModeChange:InputOwnershipDeciderTest.ModeVersionIsMonotonic:InputOwnershipDeciderTest.HumanPresenceIsDebounced:InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence:InputOwnershipControllerTest.FeatureOffDoesNotCreateController
[==========] Running 10 tests from 2 test suites.
[----------] Global test environment set-up.
[----------] 9 tests from InputOwnershipDeciderTest
[ RUN      ] InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerTick
[       OK ] InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerTick (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistGrantOneAllowsOneKeyPress
[       OK ] InputOwnershipDeciderTest.AssistGrantOneAllowsOneKeyPress (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistTextInsertSharesOneBurst
[       OK ] InputOwnershipDeciderTest.AssistTextInsertSharesOneBurst (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistGrantExpiry
[       OK ] InputOwnershipDeciderTest.AssistGrantExpiry (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistGrantExhaustion
[       OK ] InputOwnershipDeciderTest.AssistGrantExhaustion (0 ms)
[ RUN      ] InputOwnershipDeciderTest.GrantClearedOnModeChange
[       OK ] InputOwnershipDeciderTest.GrantClearedOnModeChange (0 ms)
[ RUN      ] InputOwnershipDeciderTest.ModeVersionIsMonotonic
[       OK ] InputOwnershipDeciderTest.ModeVersionIsMonotonic (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanPresenceIsDebounced
[       OK ] InputOwnershipDeciderTest.HumanPresenceIsDebounced (0 ms)
[ RUN      ] InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence
[       OK ] InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence (0 ms)
[----------] 9 tests from InputOwnershipDeciderTest (19 ms total)

[----------] 1 test from InputOwnershipControllerTest
[ RUN      ] InputOwnershipControllerTest.FeatureOffDoesNotCreateController
[       OK ] InputOwnershipControllerTest.FeatureOffDoesNotCreateController (106 ms)
[----------] 1 test from InputOwnershipControllerTest (106 ms total)

[----------] Global test environment tear-down
[==========] 10 tests from 2 test suites ran. (126 ms total)
[  PASSED  ] 10 tests.
[11/22] InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerTick (0 ms)
[12/22] InputOwnershipDeciderTest.AssistGrantOneAllowsOneKeyPress (0 ms)
[13/22] InputOwnershipDeciderTest.AssistTextInsertSharesOneBurst (0 ms)
[14/22] InputOwnershipDeciderTest.AssistGrantExpiry (0 ms)
[15/22] InputOwnershipDeciderTest.AssistGrantExhaustion (0 ms)
[16/22] InputOwnershipDeciderTest.GrantClearedOnModeChange (0 ms)
[17/22] InputOwnershipDeciderTest.ModeVersionIsMonotonic (0 ms)
[18/22] InputOwnershipDeciderTest.HumanPresenceIsDebounced (0 ms)
[19/22] InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence (0 ms)
[20/22] InputOwnershipControllerTest.FeatureOffDoesNotCreateController (106 ms)
Note: Google Test filter = InputOwnershipControllerTest.OneControllerPerWebContents:InputOwnershipControllerTest.DefaultAssistThenAiThenHuman
[==========] Running 2 tests from 1 test suite.
[----------] Global test environment set-up.
[----------] 2 tests from InputOwnershipControllerTest
[ RUN      ] InputOwnershipControllerTest.OneControllerPerWebContents
[       OK ] InputOwnershipControllerTest.OneControllerPerWebContents (109 ms)
[ RUN      ] InputOwnershipControllerTest.DefaultAssistThenAiThenHuman
[       OK ] InputOwnershipControllerTest.DefaultAssistThenAiThenHuman (105 ms)
[----------] 2 tests from InputOwnershipControllerTest (234 ms total)

[----------] Global test environment tear-down
[==========] 2 tests from 1 test suite ran. (234 ms total)
[  PASSED  ] 2 tests.
[21/22] InputOwnershipControllerTest.OneControllerPerWebContents (109 ms)
[22/22] InputOwnershipControllerTest.DefaultAssistThenAiThenHuman (105 ms)
SUCCESS: all tests passed.
Tests took 1 seconds.
```

### 7.1 矩阵逐项对应

| 任务书要求 | 覆盖测试 |
|---|---|
| **Feature 开启但未设置 mode → debugger 丢弃（fail-closed）** | `DefaultModeIsAssistFailClosed`（snapshot=ASSIST/version=0；鼠标/滚轮/键盘/text 全丢弃；人类输入仍放行）；`DefaultAssistThenAiThenHuman`（controller 层同一行为） |
| Feature 关闭 → 任何 mode 都放行 | `FeatureOffDeliversEverything`（HUMAN + 5 grant 仍全放行）、`FeatureOffDoesNotCreateController` |
| 人类输入（无 `kFromDebugger`）在 HUMAN/ASSIST 都放行 | `HumanInputIsAlwaysDelivered`（三种 mode 各一组）、`HumanInputDoesNotConsumeGrant` |
| HUMAN 下 debugger 鼠标/键盘/滚轮丢弃 | `HumanModeDropsDebuggerInput`（另含 `insertText`） |
| ASSIST 无 grant 丢弃 | `AssistWithoutGrantDropsDebuggerInput` |
| grant 1 次只放行 1 次 | `AssistGrantOneAllowsOnePointerGesture`、`AssistGrantOneAllowsOneKeyPress`、`AssistBareMoveCostsOneUnit`、`AssistWheelCostsOneUnitPerTick`（「1 次 = 1 个手势」，与 §2.3 计数口径一致） |
| 过期 / 用尽拒绝 | `AssistGrantExpiry`、`AssistGrantExhaustion` |
| AI 放行（显式设置） | `AiModeDeliversDebuggerInput`（测试内显式 `SetModeForTesting(kAi)`，并断言不消耗 grant） |
| grant 只在 ASSIST 有效，切到 HUMAN/AI 清空 | `GrantClearedOnModeChange` |
| mode 版本单调（初始为 ASSIST/0） | `ModeVersionIsMonotonic` |
| human presence 去抖（附加） | `HumanPresenceIsDebounced`（默认 ASSIST 下人类事件仍计数）、`DebuggerInputIsNotHumanPresence`（显式 kAi 下确认 debugger 事件不计数） |
| controller 每 WebContents 一个（附加） | `OneControllerPerWebContents`（并断言新 controller 为 ASSIST）、`DefaultAssistThenAiThenHuman` |

## 8. 未覆盖项（明确不做，待后续）

1. **扩展 API（K7/K8/K9）**：`browserCli.inputOwnership.setOwnership / grantInput / getOwnership`、
   `onHumanPresence`、受信扩展身份校验、`_api_features.json` allowlist、managed policy 门禁——
   本轮完全未实现。当前只有 testing 写入口。
2. **会话级 grant 分桶**：未按 `DevToolsAgentHost*` 分桶；第三方 CDP 与自有 AI 共享同一桶。
   后果只会「少放行」不会「多放行」（设计 §13.5.2 已列为已知限制）。
3. **30s 快照过期 / 降级 ASSIST**、`InputOwnershipSnapshot.stale` 字段：未实现（见 §4）。
   默认值已改为 ASSIST，因此「从未 setOwnership」是 fail-closed；但 `setOwnership(kAi)` 之后
   扩展失联不会自动降级，需要扩展 API 那一轮补心跳/过期。
4. **浏览器级 / OOPIF / 子帧浏览器测试**：未做（本轮约束禁止启动可见窗口）；`Forward*` 在
   OOPIF 本地根上是否走同一个 `delegate_`，只有代码推断（`RenderWidgetHostImpl` 的 delegate 是
   每个 widget 创建时传入的 FrameTree render_widget_delegate，即 `WebContentsImpl`），未实测。
5. **`Input.dispatchTouchEvent` / `ForwardGestureEvent` / `ImeSetComposition`**：未接线（决策器对
   未识别类型在 ASSIST 下保守要求 grant，但这些路径当前根本不会查询 controller）。
6. **`human_presence` 事件桥**：只做了内核侧去抖计数，没有 `SetHumanPresenceCallback`、
   没有扩展事件。
7. **chrome://flags 条目**：未加 `about_flags.cc` 条目；显式开启需用
   `--enable-features=InputOwnershipEnforcement`。
8. **UMA / TRACE 事件**：只加了 3 个 `TRACE_EVENT0`（丢弃时），未加 UMA。
9. **`patch-manifest.json`、commit、push**：按要求未做。
10. **端到端「DOM 计数不变」证据**：需要 browser test 或真实窗口，本轮未做；现有证据只到
    决策器/controller 层与编译链接层。

## 9. 风险

| 风险 | 影响 | 缓解 / 建议 |
|---|---|---|
| ~~默认 mode 为 `kAi`~~ **已修复（2026-10-29）** | 原实现与设计 §13.1「未知 → ASSIST fail-closed」相反 | 默认值已改为 `kAssist`（`InputOwnershipSnapshot::mode` + `InputOwnershipDecider::mode_`），新增 `DefaultModeIsAssistFailClosed`；该风险项关闭 |
| `setOwnership(kAi)` 之后扩展失联，状态不会自动降级 | 一直保持 AI 全放行，直到扩展重连或 WebContents 销毁 | 默认值只能覆盖「从未上报」；扩展 API 那一轮必须补 30s 心跳/过期降级回 ASSIST（见 §4、§8.3） |
| Feature 默认关闭但状态可被 testing 入口写入 | 仅测试可达，生产无调用点 | `#if !defined(UNIT_TEST)`? 未加，保持与 Chromium 常见 `*ForTesting` 一致；建议审核时确认不需要 `DCHECK_IS_ON()` 门禁 |
| 手势级计数口径与「逐事件」预期不同 | 同一手势的 release/char 不扣 grant，可能被误读为「放了不止一次」 | §2.3 与 §7.1 已明确「1 次 = 1 个手势」；若主代理要求逐事件，需要改 4 处分支并重写 4 个测试 |
| 单个全局 grant 桶 | 第三方 CDP 可消耗扩展授权的预算 | 只减少可用次数，不会造成未授权输入；后续按 session 分桶 |
| 丢弃 `Input.insertText` 返回 `ServerError` | 第三方客户端会看到显式错误（这是有意的 fail-closed），可能与某些自动化框架的重试逻辑冲突 | 已按设计 §13.4 写死错误文本；扩展侧应把它当「无授权」处理 |
| 鼠标宽限窗口 200ms / 文本窗口 300ms | 慢速拖拽或长间隔输入可能多消耗 grant | 常量集中在 `input_ownership_decider.cc` 匿名命名空间，便于按实测调整 |
| 键盘检查位于 `PreHandleKeyboardEvent` 之后 | 浏览器快捷键不受仲裁（符合设计），但若未来把仲裁扩展到 `HandleKeyboardEvent` 之后可能重复消费 | 入口注释已说明「只剩进页面的输入」 |
| `content_features.h` 变更触发大范围重编 | 构建时间（首次 chrome 655 步 + unittest 2202 步；默认值修正后 chrome 23 步、unittest 2 步） | 属预期；增量构建后已 no work |
| 组件构建（`content.dll`）下 `for_content_tests` 的 `check_includes` 关闭 | 头文件包含规则未由 gn check 全量校验 | 新文件只 include content 内部/公共头，风险低；可在主代理侧用完整构建复核 |

## 10. 复现与回退

复现（在当前树已应用改动的前提下）：

```bash
cd /d/c/src && export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
export PATH="/d/c/src/third_party/cpython3/host/bin:$PATH"
third_party/ninja/ninja.exe -C out/WinDev -j 6 content_unittests
out/WinDev/content_unittests.exe --gtest_filter=InputOwnership*
```

回退（任选其一，均不触碰 patch 001-027）：

```bash
# a) 反向应用本 patch（会删除 5 个新文件并还原 11 个文件）
git -C /d/c/src apply -R /d/work/browsercli/chromium/patches/028-input-ownership-arbitration.patch
# b) 只删新文件 + 还原 11 个改动文件（等价）
```

Feature 常量保留在 `content_features.{h,cc}` 也无副作用（没有任何生产代码在 Feature 关闭时
引用 controller）；若要彻底回退可连同 `content/BUILD.gn` 的三行源文件条目一起删除。

## 11. 交付物清单

| 项 | 路径 | 状态 |
|---|---|---|
| patch 028 | `D:\work\browsercli\chromium\patches\028-input-ownership-arbitration.patch` | 已重新生成（含 kAssist 默认值），1258 行，sha256 `c108e0f9c20c899be4e4dd9b13aec7f18651cd10338bf17abe1b7890e0a4889b` |
| 本文档 | `D:\work\browsercli\docs\windows-input-arbitration-kernel-2026-10-29.md` | 已生成（2026-10-29 修正默认值后同步更新） |
| 源码改动 | `D:\c\src` 工作区（11 M + 2 新增路径 / 5 个新文件） | 保留，未 commit |
| `patch-manifest.json` | `D:\work\browsercli\chromium\patch-manifest.json` | **未修改**（按要求） |
| 构建日志 | `D:\work\browsercli\build-logs\028-chrome-build.log`、`028-content-unittests-build.log`、`028-rebuild-assist-default.log`、`028-input-ownership-tests-assist-default.log`、`028-baseline-status.txt` | 已保存 |
