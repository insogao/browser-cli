# Windows 内核侧输入仲裁（最小集，patch 028）— 2026-10-29（R3 修订）

> 范围：Chromium 内核侧「最小可用输入仲裁」。Feature **默认关闭**。R1-R3 只做源码 + 单元测试；
> R4（§13，patch 029）补齐扩展 API `chrome.browserCliInputOwnership`（顶层命名空间；
> 原设计的 `browserCli.inputOwnership` 会被 renderer feature cache 当作子 feature 跳过，见 §13.11）。
> 不更新 `patch-manifest.json`，不 commit / push。
>
> 修订历史：
> - R1：patch 028 初版（四入口 + 全局 grant 桶）。
> - R2：按主代理 5 项审核要求 + 6 项补充要点重做（绝对截止、session 分桶、
>   人类暂停、拒绝先于 `Focus()`、单次消费、`Runtime.evaluate` 边界）。
> - **R3（本文档主体）**：补齐「不经过 `Forward*` / 丢失 `kFromDebugger`」的 CDP 输入旁路：
>   `Input.synthesizePinchGesture / synthesizeScrollGesture / synthesizeTapGesture /
>   emulateTouchFromMouseEvent / dispatchTouchEvent(synthetic pointer actions)`
>   全部改为命令级前置裁决，并新增 automation event window 防止其内部事件被误判为人类输入。
> - **R4（§13，patch 029）**：生产输入权 API 落地——content 生产写入口（K10-K13）、
>   `chrome.browserCliInputOwnership` 扩展 API（K14）、session 绑定（K16）、
>   `onHumanPresence` 事件。Feature 仍默认关闭；K15 见 §13.6 缺口说明。
>
> 设计依据：`docs/input-ownership-design-2026-10-28.md` §13。
> 基线：`D:\c\src` HEAD `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940` + patch 001-027，
> 构建目录 `out/WinDev`。

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| `features::kInputOwnershipEnforcement` | `content_features.{h,cc}`，**`FEATURE_DISABLED_BY_DEFAULT`（未变）** |
| 默认 mode | **`kAssist`（fail closed）** |
| grant 生命周期 | **绝对截止**：发放时一次性确定 `now + ttl`（≤600s），任何输入事件都不延长 |
| grant 分桶 | **按 `DevToolsSession*`（session token）分桶** |
| 事件级 gesture 截止 | 鼠标 5s、键盘 2s；文本 burst **≤ min(now+300ms, grant_deadline)** |
| **命令级裁决（R3）** | `ShouldDeliverDebuggerCommand(session, kind)`：HUMAN/pause 拒绝、ASSIST 无 grant 拒绝/有则扣 1、AI 放行、Feature off 放行 |
| **覆盖的合成类命令（R3）** | `dispatchTouchEvent`(synthetic pointer actions)、`emulateTouchFromMouseEvent`、`synthesizePinchGesture`、`synthesizeScrollGesture`、`synthesizeTapGesture` |
| 人类暂停 | 人类输入到达时**立即** `human_pause_until_ = now + 1s`（刷新式），暂停内 AI/ASSIST 的 debugger 输入全部丢弃 |
| 拒绝位置 | 事件路径与命令路径都在 `Focus()` / DragController / gesture 入队 / 转发之前 |
| 单次消费 | 事件路径：InputHandler 裁决+扣减 → `ScopedInputOwnershipAuthorization` → RWHI 跳过；命令路径：命令入口裁决+扣减 1，内部事件由 window 覆盖 |
| **automation event window（R3）** | 命令被接受后开启，≤5s 且由 gesture 完成回调关闭；窗口内**未带 `kFromDebugger`** 的 renderer 事件照常投递，但**不记为人类输入**（否则每次合成手势都会误触发人类暂停） |
| 无 session 的 debugger 输入 | HUMAN/ASSIST **fail closed**；AI 放行（事件与命令两条路径一致） |
| session 撤销 | `InputHandler::Disable` / 析构 / `SetRenderer` 撤销 grant + gesture/text + 该 session 的 window |
| 构建 | `content_unittests` EXIT=0；`chrome` EXIT=0；最终 `ninja -n` = no work |
| 单测 | `--gtest_filter=InputOwnership*` → **40 tests**（33 decider + 7 controller），`SUCCESS: all tests passed.` |
| patch | `chromium/patches/028-input-ownership-arbitration.patch`，**2956 行**，17 个文件段（12 改 + 5 新增） |
| patch sha256 | `c129b42baeef335b62e4f6b0ad684db550e21a72078efae07782446f646ea86e` |
| manifest / commit | **未更新、未提交** |

## 1. 改动文件

### 1.1 新增

| 文件 | 作用 |
|---|---|
| `content/public/browser/input_ownership.h` | 公共门面：`InputOwnershipMode`、`InputOwnershipSessionToken`、**`InputOwnershipCommandKind`**、`InputOwnershipSnapshot`、纯决策器 `InputOwnershipDecider`（per-session grant/gesture、人类暂停、**命令级裁决 + automation event window**） |
| `content/browser/input_ownership/input_ownership_decider.cc` | 决策器实现 |
| `content/browser/input_ownership/input_ownership_controller.h` | `InputOwnershipController : WebContentsUserData`；事件/文本/**命令**三类裁决、暂停查询、window 开关、撤销、`ScopedInputOwnershipAuthorization` |
| `content/browser/input_ownership/input_ownership_controller.cc` | controller 实现、scope 深度计数、WebContents 销毁清理 |
| `content/browser/input_ownership/input_ownership_controller_unittest.cc` | 33 个决策器测试 + 7 个 controller 测试 |

### 1.2 修改

| 文件 | 改动 |
|---|---|
| `content/public/common/content_features.h/.cc` | `kInputOwnershipEnforcement`（默认关闭） |
| `content/browser/renderer_host/render_widget_host_delegate.h/.cc` | `virtual InputOwnershipController* GetInputOwnershipController()`，默认 `nullptr` |
| `content/browser/web_contents/web_contents_impl.h/.cc` | override；Feature 关闭返回 `nullptr` |
| `content/browser/renderer_host/render_widget_host_impl.cc` | `ShouldDropAutomationInput()`：人类事件记账/暂停、无 session 的 debugger fail closed、scope 内跳过、**window 内的未标记事件不记人类**；3 个 `Forward*` 汇合点 |
| `content/browser/devtools/protocol/input_handler.h` | session token 访问器、事件/文本/**命令**裁决 helper、window 关闭 helper、撤销；`SynthesizeRepeatingScroll` / `OnScrollFinished` 增加 window id 参数 |
| `content/browser/devtools/protocol/input_handler.cc` | Injector 四路径前置裁决 + scope；`InsertText`；**5 个合成类入口前置裁决 + window 开/关**；emulated touch 的异步转发内加 scope；`Disable`/析构/`SetRenderer` 撤销 |
| `content/browser/BUILD.gn` / `content/public/browser/BUILD.gn` / `content/test/BUILD.gn` | 新文件加入构建 |

## 2. 审核要求的实现

### 2.1 grant 绝对截止与生命周期

| 要求 | 实现 |
|---|---|
| grant 发放时一次性确定截止，任何后续事件不能延长 | `grant_deadline = now + min(ttl, 600s)`，决策路径只读；唯一写入点是 `PurgeExpired()` 的清除 |
| `insertText` 300ms 窗口不能无限续期 | `text_burst_until = min(now+300ms, grant_deadline)` |
| 鼠标/键盘 gesture 绝对 deadline | 鼠标 5s、键盘 2s，且都 `min(..., grant_deadline)` |
| 手势状态不得永久保持 | `PurgeExpired()` 每决策前关闭超时手势；桶在「剩余 0 + 无手势/grace/burst」时删除 |
| 清理触发点 | 模式变更（清全部 session + window）、session 撤销（清该 session + 其 window）、grant 过期、WebContents 销毁、gesture 完成 |

### 2.2 grant 绑定 DevTools 会话 + 目标 WebContents

- 桶 key = `DevToolsSession*`（`static_cast<const void*>(session())`，不解引用）。
- controller 是 `WebContentsUserData`，因此 grant 天然只存在于被操作的那个 WebContents。
- **单次消费**：事件路径由 `InputHandler` 裁决扣减 + `ScopedInputOwnershipAuthorization` 让 RWHI 跳过；命令路径由命令入口扣减 1 次，内部事件不再扣减。
- **第三方 CDP**：第三人称会话有自己的桶，HUMAN 下被拒，ASSIST 下只能用**自己**的 grant（`SessionGrantsAreIsolated`）。
- **撤销**：`Disable()` / `~InputHandler()` / `SetRenderer()` → `RevokeInputOwnershipSession()`（经 `base::WeakPtr` 访问 controller，避免 WebContents 先销毁的悬垂）。

### 2.3 Human Presence 与 AI 输入暂停衔接

- `NoteHumanPresence(now)` 第一件事即 `human_pause_until_ = now + 1s`（刷新式、不去抖）；300ms 去抖只作用于上报计数。
- 暂停在**所有** debugger 判定之前（事件、文本、**命令**三条路径），AI 也丢；人类输入永不丢。
- 到期只凭时间比较，按当前 mode 继续；模式变更**不清**暂停（剩余 ≤1s）。
- 不改变 ownership、不锁人类输入；暂停内不 `Focus()`、不入队 gesture。

### 2.4 Feature 默认关闭

- 三层 early-return：RWHI helper 首行、`GetInputOwnershipController()` 返回 `nullptr`、决策器每个入口首行；`InputHandler` 的四个 helper 同样首行判定。
- 命令路径：Feature 关闭时 `ShouldDropDebuggerCommand` 直接放行，且**不打开 window、不记录 kind**（`FeatureOffDeliversDebuggerCommand` 断言）。
- `ScopedInputOwnershipAuthorization(nullptr)` 为 no-op。

### 2.5 不把 patch 028 描述成「完整页面修改隔离」（边界声明）

**patch 028 只是「输入事件仲裁」，不是页面修改隔离，也不是 CDP 权限系统。**

1. **`Runtime.evaluate` / `Runtime.callFunctionOn` / `DOM.*` / `Page.navigate` 直接改 DOM，完全不经过输入事件仲裁。**
2. **第三方 CDP 的信任边界**：内核能区分「带 `kFromDebugger` 的注入事件」「OS 人类输入」「命令级 CDP 输入命令」，但**无法**判断某个 CDP 会话是否可信——能 attach 的客户端都能调 `Runtime.evaluate`。
3. **BrowserPilot 命令层仍负责所有权校验**：内核是第二道防线，权威判定仍在扩展命令层与 `setOwnership` 快照。
4. 本 patch 不提供：扩展 API、`grantInput` 真实写入路径、CDP 会话身份校验、`Runtime.evaluate` 拦截。
5. 仍未覆盖的注入路径见 §9.2。

### 2.6 命令级注入旁路（R3 新增）

**问题**：`Input.synthesize*/emulateTouchFromMouseEvent/dispatchTouchEvent(pointer actions)` 产生的
renderer 事件在 `SyntheticGestureTargetAura` 里被重建为 `ui::Event`，`kFromDebugger` 位丢失；
`RenderWidgetHostImpl` 只按该位判断，因此这些事件会被当成**人类输入**（投递 + 记 presence + 1s 暂停）。
只靠 RWHI 无法区分「人类」与「合成手势内部事件」。

**实现（三层）**：

1. **命令级前置裁决**（`InputOwnershipController::ShouldDropSessionCommand` →
   `InputOwnershipDecider::ShouldDeliverDebuggerCommand`）：
   | 条件 | 结果 |
   |---|---|
   | Feature off | 放行，零状态 |
   | human pause | 拒绝 |
   | `kAi` | 放行，不扣 grant |
   | `kHuman` | 拒绝，不扣 grant |
   | `kAssist` + 有效 grant | 放行并扣 **1** 个单位 |
   | `kAssist` 无 grant / 过期 / 用尽 | 拒绝 |
   | 无 session token | 仅 AI 放行 |
   在 `InputHandler` 的 5 个入口**入队/构造之前**调用；拒绝统一返回
   `ServerError("InputOwnership: debugger input rejected (no valid grant for this session)")`，
   不触碰 `pointer_ids_`、不队列 gesture、不 `Focus()`。
2. **automation event window**：命令被接受后开启，`≤5s`，并在 gesture 完成回调里关闭
   （pinch / scroll 的最后一轮 / tap 的最后一击 / pointer-action 序列结束）。
   window 打开期间，RWHI 对**未带 `kFromDebugger`** 的事件：照常投递，但跳过
   `NoteHumanPresence()` —— 它们不会被误判为人类，也不会误触发 1s 暂停。
   window 单槽、带自增 id：新命令替换旧 window，旧回调的 close 只匹配自己的 id；
   session 撤销或模式变更会关闭对应 window。
3. **异步转发的 scope**：`emulateTouchFromMouseEvent` 的 `InsertVisualStateCallback` 回调里
   构造 `ScopedInputOwnershipAuthorization` 再 `Forward*`（该路径的事件**保留** `kFromDebugger`，
   但转发发生在命令返回之后，栈上的 scope 覆盖不到，必须在回调里建）。

**限额与固有盲区**（明确记录）：

- window 硬上限 5s；`repeat_count × duration` 超过 5s 的长滚动，尾部事件会重新被当作人类输入
  （仍会投递，只是会置 1s 暂停）。
- window 打开期间**真实人类输入不设暂停**（无法与内部事件区分）。窗口只由已授权命令开启，
  HUMAN 下不可能开启，且通常 <1s；这是为「合成手势不误判人类」付出的代价。
- 模式切到 HUMAN 时 window 关闭，但**已经启动**的合成手势无法取消，可能继续跑完
  （时长受命令参数限制），其后续事件会被当作人类输入投递。

## 3. 命令 ↔ 手势语义表

| 扩展命令 | CDP 命令序列 | 内核输入 | grant 扣减 | 绝对上限 |
|---|---|---|---|---|
| `click` | mousePressed + mouseReleased | `kMouseDown` + `kMouseUp` | **1** | 手势 ≤5s |
| `hover` / `move` | mouseMoved | `kMouseMove` | **1 / 命令** | 命令级 |
| `drag` | pressed + N×moved + released | down + moves + up | **1** | 手势 ≤5s |
| `press`（键盘） | keyDown(+keyUp) | `RawKeyDown`(+`Char`+`KeyUp`) | **1** | 按键 ≤2s |
| `wheel` | mouseWheel（1 命令） | 真实 wheel + 合成 phaseEnded | **1**（同一 scope） | 命令级 |
| `type` / `fill` | N× `Input.insertText` | `ImeCommitText` | **1**（burst） | `min(now+300ms, grant_deadline)` |
| `scrollTo`（合成） | `synthesizeScrollGesture` | 平台滚轮/手势事件（**无 `kFromDebugger`**） | **1 / 命令**（含 repeat） | 命令级 + window ≤5s |
| `pinch` | `synthesizePinchGesture` | 平台手势事件 | **1** | 命令级 + window ≤5s |
| `tap` | `synthesizeTapGesture` | 平台手势事件 | **1**（含 tapCount） | 命令级 + window ≤5s |
| `touch` | `dispatchTouchEvent`（pointer actions 后端） | 合成触摸（平台事件） | **1 / 命令** | 命令级 + window ≤5s |
| `emulateTouch` | `emulateTouchFromMouseEvent` | `ForwardMouse/Wheel`（**保留** `kFromDebugger`） | **1** | 命令级（回调内 scope） |
| `touch`（默认后端） | `dispatchTouchEvent` | touch emulator | **1** | 命令级 |

计数口径：

- **一次 CDP 命令只裁决/扣减一次**。wheel 的「真实 + phaseEnded」两个转发共享一个 scope；
  合成手势的整个序列（含 repeat / tapCount）只扣一次。
- **命令边界即入口函数**：本轮不新增 `Begin/EndDebuggerInputCommand`；跨命令手势由 gesture deadline 描述。
- 内核 grant 是第二道防线，扩展命令层仍按命令预算。

## 4. 分层、单次消费与拒绝位置

```
事件路径（mouse/key/wheel/text/touch）
  InputHandler::InputInjector::Inject*
    1) ShouldDropDebuggerInput(...)      ← Feature → pause → session grant（扣 1）
       └ 拒绝 → sendFailure(ServerError)，Focus/Drag/转发都没发生
    2) ScopedInputOwnershipAuthorization
    3) Focus() → Forward* ──────────────► RWHI: scope 内直接放行（不重复扣减）

命令路径（synthetic / emulated touch）
  InputHandler::DispatchTouchEvent(pointer actions)
    SynthesizePinch/Scroll/TapGesture / EmulateTouchFromMouseEvent
    1) ShouldDropDebuggerCommand(...)    ← Feature → pause → mode → grant（扣 1）
       └ 拒绝 → sendFailure / ServerError return，未入队、未改 pointer_ids_
    2) OpenAutomationEventWindow(...)    ← ≤5s，gesture 完成时关闭
    3) QueueSyntheticGesture(...)  or  InsertVisualStateCallback(...)
         └ 内部平台事件（无 kFromDebugger）→ RWHI: window 内投递且**不记人类**
         └ emulated touch 的异步回调：ScopedInputOwnershipAuthorization 后 Forward*
```

- **拒绝先于副作用**：事件路径在 `Focus()`/DragController 之前；命令路径在 `pointer_ids_` 修改、
  gesture 入队、`InsertVisualStateCallback` 之前。
- **只扣一次**：RWHI 的 scope 检查是纯只读（`debugger_authorization_depth_ > 0`）。
- scope 作用于 controller（每 WebContents），嵌套计数；交叉 WebContents 不误放行
  （`ScopedAuthorizationIsPerController` 用第二个 WebContents 断言）。

## 5. 决策矩阵

`fromDebugger = (event.GetModifiers() & kFromDebugger) != 0`。Feature 关闭时下表全部为 D（放行）。

| mode | 输入 | session | grant | 结果 |
|---|---|---|---|---|
| 任意 | 人类（无 `kFromDebugger`，无 window） | — | — | **D** + 立即 1s 暂停 |
| 任意 | 未标记事件，**window 开启** | — | — | **D**，**不**记人类、不暂停 |
| 任意 | debugger 事件 | 任意 | — | 暂停窗口内丢弃（含 AI） |
| 任意 | debugger 事件 | 无 token | — | AI：D；ASSIST/HUMAN：丢弃 |
| kAi | debugger 事件 / 命令 | 有 | 任意 | **D**，不扣 grant |
| kHuman | debugger 事件 / 命令 | 有 | 任意 | **丢弃**，不扣 grant |
| kAssist | debugger 事件 | 有 | 无/过期/用尽 | **丢弃**（事件路径返回 ServerError） |
| kAssist | `kMouseDown` | 有 | 有效 | **D** 且 `--`，手势 ≤5s |
| kAssist | 手势内 move/up | 有 | 同一次按下 | **D**（免费），up 后 200ms grace（受 grant 封顶） |
| kAssist | 孤立 `kMouseUp` | 有 | 无 | **丢弃** |
| kAssist | 裸 move | 有 | 有效 / 无 | **D** 且 `--` / 丢弃 |
| kAssist | 手势超 5s | 有 | — | 手势强制关闭；后续 move 变裸 move、up 丢弃 |
| kAssist | `kMouseWheel` | 有 | 有效 / 无 | **D** 且 `--` / 丢弃 |
| kAssist | 键盘按下 | 有 | 有效 / 无 | **D** 且 `--`（≤2s）/ 丢弃 |
| kAssist | `insertText` | 有 | burst 内 / 外且有效 / 无 | **D**（免费）/ **D** 且 `--` / 丢弃 |
| kAssist | **合成命令（5 类）** | 有 | 有效 / 无 | **D** 且 `--`（window ≤5s）/ **丢弃** |
| kAssist | 触摸（默认后端） | 有 | 有效 / 无 | **D** 且 `--` / 丢弃 |

版本语义不变：`snapshot().version` 只在**实际** mode 变更时 +1。

## 6. Feature-off 证据

三层代码 early-return（§2.4）+ 两层测试：

- `FeatureOffDeliversEverything`：HUMAN + 5 grant 下事件全放行、grant 不变、无暂停、无 presence。
- `FeatureOffDoesNotCreateController`：controller 与 user data 均为 `nullptr`。
- `FeatureOffDeliversDebuggerCommand`：HUMAN + 5 grant 下命令放行、grant 不变、
  `OpenAutomationEventWindow()` 返回 0、window 不开启、`last_command_kind` 不记录。

## 7. 构建命令与结果

```bash
export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
export PATH="/d/c/src/third_party/cpython3/host/bin:$PATH"
cd /d/c/src
```

```bash
# 0) 单对象预检：编译 input_ownership_* / input_handler / render_widget_host_impl
third_party/ninja/ninja.exe -C out/WinDev -j 6 \
  obj/content/browser/browser/input_ownership_controller.obj \
  obj/content/browser/browser/input_ownership_decider.obj \
  obj/content/test/content_unittests/input_ownership_controller_unittest.obj \
  obj/content/browser/browser/input_handler.obj \
  obj/content/browser/browser/render_widget_host_impl.obj
# → 首次 1 处绑定参数错误（OnScrollFinished 漏传 window id），修正后全部成功

# 1) content_unittests（11 步，含 content.dll 重链）
third_party/ninja/ninja.exe -C out/WinDev -j 6 content_unittests
# → EXIT=0（日志 028d-content-unittests-build.log）

# 2) 单测
out/WinDev/content_unittests.exe --gtest_filter=InputOwnership*
# → EXIT=0，40/40 PASS（日志 028d-input-ownership-tests.log）

# 3) chrome
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome
# → 首次失败：components_storage_monitor.dll / components_sessions.dll permission denied
#   （其他 agent 的 out/WinDev/chrome.exe 探针进程周期性占用 DLL；未使用 taskkill /IM）
# → 等待后 -j 4 重跑：EXIT=0，13 步，[11/13] LINK(DLL) chrome.dll（日志 028c-chrome-build.log）
#   最后一轮只把 window 打开点前移到 gesture 入队处（input_handler 私有 helper），
#   content.dll 导出表未变，因此 chrome 无需重链。

# 4) no-work 复核
third_party/ninja/ninja.exe -C out/WinDev -n -j 6 chrome content_unittests
# → ninja: no work to do.
```

未启动可见窗口；未触碰用户 Chrome / profile；未出现系统对话框；未杀任何进程。

## 8. 单元测试原始输出

```bash
cd /d/c/src/out/WinDev
./content_unittests.exe --gtest_filter=InputOwnership*
```

原始输出（`build-logs/028d-input-ownership-tests.log`，167 行，完整未删减，EXIT=0）：

```text
IMPORTANT DEBUGGING NOTE: batches of tests are run inside their
own process. For debugging a test inside a debugger, use the
--gtest_filter=<your_test_name> flag along with
--single-process-tests.
Using sharding settings from environment. This is shard 0/1
Using 1 parallel jobs.
Note: Google Test filter = InputOwnershipDeciderTest.FeatureOffDeliversEverything:InputOwnershipDeciderTest.DefaultModeIsAssistFailClosed:InputOwnershipDeciderTest.HumanInputIsAlwaysDelivered:InputOwnershipDeciderTest.HumanInputDoesNotConsumeGrant:InputOwnershipDeciderTest.HumanModeDropsDebuggerInput:InputOwnershipDeciderTest.AiModeDeliversDebuggerInput:InputOwnershipDeciderTest.AssistWithoutGrantDropsDebuggerInput:InputOwnershipDeciderTest.UnattributedDebuggerInputFailsClosed:InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture:InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease
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
[ RUN      ] InputOwnershipDeciderTest.UnattributedDebuggerInputFailsClosed
[       OK ] InputOwnershipDeciderTest.UnattributedDebuggerInputFailsClosed (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture
[       OK ] InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease
[       OK ] InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease (0 ms)
[----------] 10 tests from InputOwnershipDeciderTest (20 ms total)

[----------] Global test environment tear-down
[==========] 10 tests from 1 test suite ran. (28 ms total)
[  PASSED  ] 10 tests.
[1/40] InputOwnershipDeciderTest.FeatureOffDeliversEverything (0 ms)
[2/40] InputOwnershipDeciderTest.DefaultModeIsAssistFailClosed (0 ms)
[3/40] InputOwnershipDeciderTest.HumanInputIsAlwaysDelivered (0 ms)
[4/40] InputOwnershipDeciderTest.HumanInputDoesNotConsumeGrant (0 ms)
[5/40] InputOwnershipDeciderTest.HumanModeDropsDebuggerInput (0 ms)
[6/40] InputOwnershipDeciderTest.AiModeDeliversDebuggerInput (0 ms)
[7/40] InputOwnershipDeciderTest.AssistWithoutGrantDropsDebuggerInput (0 ms)
[8/40] InputOwnershipDeciderTest.UnattributedDebuggerInputFailsClosed (0 ms)
[9/40] InputOwnershipDeciderTest.AssistGrantOneAllowsOnePointerGesture (0 ms)
[10/40] InputOwnershipDeciderTest.AssistDroppedPressAlsoDropsRelease (0 ms)
Note: Google Test filter = InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit:InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerCommand:InputOwnershipDeciderTest.AssistGrantOneAllowsOneKeyPress:InputOwnershipDeciderTest.AssistTextInsertSharesOneBurst:InputOwnershipDeciderTest.AssistGrantExpiry:InputOwnershipDeciderTest.AssistGrantExhaustion:InputOwnershipDeciderTest.GrantClearedOnModeChange:InputOwnershipDeciderTest.ModeChangeClearsGestureState:InputOwnershipDeciderTest.ModeVersionIsMonotonic:InputOwnershipDeciderTest.HumanPresenceIsDebounced
[==========] Running 10 tests from 1 test suite.
[----------] Global test environment set-up.
[----------] 10 tests from InputOwnershipDeciderTest
[ RUN      ] InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit
[       OK ] InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerCommand
[       OK ] InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerCommand (0 ms)
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
[ RUN      ] InputOwnershipDeciderTest.ModeChangeClearsGestureState
[       OK ] InputOwnershipDeciderTest.ModeChangeClearsGestureState (0 ms)
[ RUN      ] InputOwnershipDeciderTest.ModeVersionIsMonotonic
[       OK ] InputOwnershipDeciderTest.ModeVersionIsMonotonic (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanPresenceIsDebounced
[       OK ] InputOwnershipDeciderTest.HumanPresenceIsDebounced (0 ms)
[----------] 10 tests from InputOwnershipDeciderTest (27 ms total)

[----------] Global test environment tear-down
[==========] 10 tests from 1 test suite ran. (28 ms total)
[  PASSED  ] 10 tests.
[11/40] InputOwnershipDeciderTest.AssistBareMoveCostsOneUnit (0 ms)
[12/40] InputOwnershipDeciderTest.AssistWheelCostsOneUnitPerCommand (0 ms)
[13/40] InputOwnershipDeciderTest.AssistGrantOneAllowsOneKeyPress (0 ms)
[14/40] InputOwnershipDeciderTest.AssistTextInsertSharesOneBurst (0 ms)
[15/40] InputOwnershipDeciderTest.AssistGrantExpiry (0 ms)
[16/40] InputOwnershipDeciderTest.AssistGrantExhaustion (0 ms)
[17/40] InputOwnershipDeciderTest.GrantClearedOnModeChange (0 ms)
[18/40] InputOwnershipDeciderTest.ModeChangeClearsGestureState (0 ms)
[19/40] InputOwnershipDeciderTest.ModeVersionIsMonotonic (0 ms)
[20/40] InputOwnershipDeciderTest.HumanPresenceIsDebounced (0 ms)
Note: Google Test filter = InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence:InputOwnershipDeciderTest.SessionGrantsAreIsolated:InputOwnershipDeciderTest.SessionRevocation:InputOwnershipDeciderTest.MouseGestureHasAbsoluteDeadline:InputOwnershipDeciderTest.KeyGestureHasAbsoluteDeadline:InputOwnershipDeciderTest.TextBurstCannotExtendPastGrantDeadline:InputOwnershipDeciderTest.TextBurstCappedByGrantWithUnitsLeft:InputOwnershipDeciderTest.HumanInputPausesAllDebuggerInput:InputOwnershipDeciderTest.HumanPauseExpiresThenAssistResumes:InputOwnershipDeciderTest.DebuggerCommandMatrix
[==========] Running 10 tests from 1 test suite.
[----------] Global test environment set-up.
[----------] 10 tests from InputOwnershipDeciderTest
[ RUN      ] InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence
[       OK ] InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence (0 ms)
[ RUN      ] InputOwnershipDeciderTest.SessionGrantsAreIsolated
[       OK ] InputOwnershipDeciderTest.SessionGrantsAreIsolated (0 ms)
[ RUN      ] InputOwnershipDeciderTest.SessionRevocation
[       OK ] InputOwnershipDeciderTest.SessionRevocation (0 ms)
[ RUN      ] InputOwnershipDeciderTest.MouseGestureHasAbsoluteDeadline
[       OK ] InputOwnershipDeciderTest.MouseGestureHasAbsoluteDeadline (0 ms)
[ RUN      ] InputOwnershipDeciderTest.KeyGestureHasAbsoluteDeadline
[       OK ] InputOwnershipDeciderTest.KeyGestureHasAbsoluteDeadline (0 ms)
[ RUN      ] InputOwnershipDeciderTest.TextBurstCannotExtendPastGrantDeadline
[       OK ] InputOwnershipDeciderTest.TextBurstCannotExtendPastGrantDeadline (0 ms)
[ RUN      ] InputOwnershipDeciderTest.TextBurstCappedByGrantWithUnitsLeft
[       OK ] InputOwnershipDeciderTest.TextBurstCappedByGrantWithUnitsLeft (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanInputPausesAllDebuggerInput
[       OK ] InputOwnershipDeciderTest.HumanInputPausesAllDebuggerInput (0 ms)
[ RUN      ] InputOwnershipDeciderTest.HumanPauseExpiresThenAssistResumes
[       OK ] InputOwnershipDeciderTest.HumanPauseExpiresThenAssistResumes (0 ms)
[ RUN      ] InputOwnershipDeciderTest.DebuggerCommandMatrix
[       OK ] InputOwnershipDeciderTest.DebuggerCommandMatrix (0 ms)
[----------] 10 tests from InputOwnershipDeciderTest (21 ms total)

[----------] Global test environment tear-down
[==========] 10 tests from 1 test suite ran. (21 ms total)
[  PASSED  ] 10 tests.
[21/40] InputOwnershipDeciderTest.DebuggerInputIsNotHumanPresence (0 ms)
[22/40] InputOwnershipDeciderTest.SessionGrantsAreIsolated (0 ms)
[23/40] InputOwnershipDeciderTest.SessionRevocation (0 ms)
[24/40] InputOwnershipDeciderTest.MouseGestureHasAbsoluteDeadline (0 ms)
[25/40] InputOwnershipDeciderTest.KeyGestureHasAbsoluteDeadline (0 ms)
[26/40] InputOwnershipDeciderTest.TextBurstCannotExtendPastGrantDeadline (0 ms)
[27/40] InputOwnershipDeciderTest.TextBurstCappedByGrantWithUnitsLeft (0 ms)
[28/40] InputOwnershipDeciderTest.HumanInputPausesAllDebuggerInput (0 ms)
[29/40] InputOwnershipDeciderTest.HumanPauseExpiresThenAssistResumes (0 ms)
[30/40] InputOwnershipDeciderTest.DebuggerCommandMatrix (0 ms)
Note: Google Test filter = InputOwnershipDeciderTest.DebuggerCommandKindsEachCostOneUnit:InputOwnershipDeciderTest.FeatureOffDeliversDebuggerCommand:InputOwnershipDeciderTest.AutomationEventWindowLifecycle:InputOwnershipControllerTest.FeatureOffDoesNotCreateController:InputOwnershipControllerTest.OneControllerPerWebContents:InputOwnershipControllerTest.DefaultAssistThenAiThenHuman:InputOwnershipControllerTest.HumanEventPausesDebuggerInput:InputOwnershipControllerTest.SessionGrantsAreIsolated:InputOwnershipControllerTest.ScopedAuthorizationIsPerController:InputOwnershipControllerTest.AutomationCommandAndWindow
[==========] Running 10 tests from 2 test suites.
[----------] Global test environment set-up.
[----------] 3 tests from InputOwnershipDeciderTest
[ RUN      ] InputOwnershipDeciderTest.DebuggerCommandKindsEachCostOneUnit
[       OK ] InputOwnershipDeciderTest.DebuggerCommandKindsEachCostOneUnit (0 ms)
[ RUN      ] InputOwnershipDeciderTest.FeatureOffDeliversDebuggerCommand
[       OK ] InputOwnershipDeciderTest.FeatureOffDeliversDebuggerCommand (0 ms)
[ RUN      ] InputOwnershipDeciderTest.AutomationEventWindowLifecycle
[       OK ] InputOwnershipDeciderTest.AutomationEventWindowLifecycle (0 ms)
[----------] 3 tests from InputOwnershipDeciderTest (19 ms total)

[----------] 7 tests from InputOwnershipControllerTest
[ RUN      ] InputOwnershipControllerTest.FeatureOffDoesNotCreateController
[       OK ] InputOwnershipControllerTest.FeatureOffDoesNotCreateController (112 ms)
[ RUN      ] InputOwnershipControllerTest.OneControllerPerWebContents
[       OK ] InputOwnershipControllerTest.OneControllerPerWebContents (104 ms)
[ RUN      ] InputOwnershipControllerTest.DefaultAssistThenAiThenHuman
[       OK ] InputOwnershipControllerTest.DefaultAssistThenAiThenHuman (102 ms)
[ RUN      ] InputOwnershipControllerTest.HumanEventPausesDebuggerInput
[       OK ] InputOwnershipControllerTest.HumanEventPausesDebuggerInput (101 ms)
[ RUN      ] InputOwnershipControllerTest.SessionGrantsAreIsolated
[       OK ] InputOwnershipControllerTest.SessionGrantsAreIsolated (101 ms)
[ RUN      ] InputOwnershipControllerTest.ScopedAuthorizationIsPerController
[       OK ] InputOwnershipControllerTest.ScopedAuthorizationIsPerController (100 ms)
[ RUN      ] InputOwnershipControllerTest.AutomationCommandAndWindow
[       OK ] InputOwnershipControllerTest.AutomationCommandAndWindow (99 ms)
[----------] 7 tests from InputOwnershipControllerTest (723 ms total)

[----------] Global test environment tear-down
[==========] 10 tests from 2 test suites ran. (743 ms total)
[  PASSED  ] 10 tests.
[31/40] InputOwnershipDeciderTest.DebuggerCommandKindsEachCostOneUnit (0 ms)
[32/40] InputOwnershipDeciderTest.FeatureOffDeliversDebuggerCommand (0 ms)
[33/40] InputOwnershipDeciderTest.AutomationEventWindowLifecycle (0 ms)
[34/40] InputOwnershipControllerTest.FeatureOffDoesNotCreateController (112 ms)
[35/40] InputOwnershipControllerTest.OneControllerPerWebContents (104 ms)
[36/40] InputOwnershipControllerTest.DefaultAssistThenAiThenHuman (102 ms)
[37/40] InputOwnershipControllerTest.HumanEventPausesDebuggerInput (101 ms)
[38/40] InputOwnershipControllerTest.SessionGrantsAreIsolated (101 ms)
[39/40] InputOwnershipControllerTest.ScopedAuthorizationIsPerController (100 ms)
[40/40] InputOwnershipControllerTest.AutomationCommandAndWindow (99 ms)
SUCCESS: all tests passed.
Tests took 2 seconds.
```

### 8.1 测试矩阵

| 要求 / 场景 | 覆盖测试 | 断言要点 |
|---|---|---|
| **绝对截止**：grant 不被事件延长 | `AssistGrantExpiry`、`TextBurstCannotExtendPastGrantDeadline`、`TextBurstCappedByGrantWithUnitsLeft` | 300ms grant：+290ms 免费、+300ms 起拒绝 |
| **鼠标手势 5s 截止** | `MouseGestureHasAbsoluteDeadline` | +4s move 免费；+6s move 变裸 move 扣 1；随后 up 丢弃 |
| **键盘 2s 截止** | `KeyGestureHasAbsoluteDeadline` | +1s Char 免费；+2.5s Char/KeyUp 丢弃 |
| **模式变更清 gesture/text** | `GrantClearedOnModeChange`、`ModeChangeClearsGestureState` | 切模式后 grant=0、up 不再放行 |
| **session 分桶** | `SessionGrantsAreIsolated`（decider + controller） | A 用完不影响 B；B 不能借 A |
| **session 撤销** | `SessionRevocation` | revoke 后 up/down 拒绝；同 token 重授权可用；B 不受影响 |
| **无 session 的 debugger** | `UnattributedDebuggerInputFailsClosed` | ASSIST/HUMAN 丢、AI 放行、不消耗他人 grant |
| **人类暂停立即生效** | `HumanInputPausesAllDebuggerInput` | +1ms 鼠标/键盘/滚轮/文本全丢；人类输入仍送达；暂停刷新 |
| **暂停到期恢复** | `HumanPauseExpiresThenAssistResumes` | +1.1s ASSIST 恢复并扣 1 |
| **人类输入永不丢** | `HumanInputIsAlwaysDelivered`、`HumanInputDoesNotConsumeGrant`、`HumanEventPausesDebuggerInput` | 三 mode + session 路径 |
| **单次消费 / scope** | `ScopedAuthorizationIsPerController` | 嵌套计数；第二个 WebContents 不授权；nullptr no-op |
| **Feature 关闭** | `FeatureOffDeliversEverything`、`FeatureOffDoesNotCreateController`、`FeatureOffDeliversDebuggerCommand` | 事件/命令全放行、零状态、不建 controller、不开 window |
| **FAIL-CLOSED 默认** | `DefaultModeIsAssistFailClosed`、`DefaultAssistThenAiThenHuman` | ASSIST/version=0，全丢 |
| **HUMAN 丢 debugger** | `HumanModeDropsDebuggerInput`、`DebuggerCommandMatrix` | 事件与命令都丢，grant 不变 |
| **AI 放行** | `AiModeDeliversDebuggerInput`、`DebuggerCommandMatrix` | 不扣 grant；unattributed 也放行 |
| **手势级计数（click/key/wheel/text）** | `AssistGrantOneAllowsOnePointerGesture`、`AssistGrantOneAllowsOneKeyPress`、`AssistWheelCostsOneUnitPerCommand`、`AssistTextInsertSharesOneBurst`、`AssistBareMoveCostsOneUnit` | 1 单位 = 1 手势 |
| **命令级矩阵（R3）** | `DebuggerCommandMatrix` | ASSIST 无 grant 拒绝；有则只准 1 次；HUMAN 拒绝且不扣；AI 放行不扣；暂停期拒绝、到期恢复 |
| **命令 kind 覆盖（R3）** | `DebuggerCommandKindsEachCostOneUnit` | touch/emulateTouch/pinch/scroll/tap 各扣 1，`last_command_kind` 正确 |
| **window 生命周期（R3）** | `AutomationEventWindowLifecycle` | 截止、5s 封顶、旧 id 不能关新窗、revoke 关对应 session、mode 变更关闭 |
| **命令 + window（controller，R3）** | `AutomationCommandAndWindow` | 拒绝不开窗；授权后扣 1；HUMAN 不扣；AI 不扣 |
| **孤立 release 不投递** | `AssistDroppedPressAlsoDropsRelease` | 未授权按下 → up 也丢 |
| **过期 / 用尽** | `AssistGrantExpiry`、`AssistGrantExhaustion` | 不复活 |
| **human presence 去抖** | `HumanPresenceIsDebounced`、`DebuggerInputIsNotHumanPresence` | 300ms 只影响计数；debugger 不算 presence |
| **mode 版本单调** | `ModeVersionIsMonotonic` | 初始 ASSIST/0 |
| **每 WebContents 一个 controller** | `OneControllerPerWebContents` | 同一实例、初始 ASSIST |

## 9. 未覆盖项

### 9.1 本轮明确不做

1. **扩展 API（K7/K8/K9）** 与真实 `grantInput` 写入路径（chrome.debugger 会话 → `DevToolsSession*` 映射）。
2. **30s 快照过期 / 降级 ASSIST**、`snapshot.stale`。
3. **`Begin/EndDebuggerInputCommand`**（见 §3）。
4. **OOPIF / 子帧的浏览器级验证**（本轮禁止启动可见窗口）。
5. **human_presence 事件桥**、chrome://flags 条目、UMA。
6. **端到端「DOM 计数不变」证据**（需要 browser test）。

### 9.2 已知的注入旁路（本 patch 不覆盖）

| 旁路 | 原因 | 影响 | 建议 |
|---|---|---|---|
| `Runtime.evaluate` / `callFunctionOn` / `DOM.*` | 不是输入事件 | **HUMAN 下仍可改页面** | BrowserPilot 命令层做所有权校验；内核不承诺这层隔离 |
| `DragController` 的拖拽续传（`StartDragging` 之后） | 转发发生在 InputHandler 判决之外的异步回调，没有 scope | ASSIST/HUMAN 下这些事件被 RWHI 丢弃（fail closed），拖拽中断 | 若要支持 AI 拖拽，需把 drag 状态纳入 session 预算 |
| 已启动的合成手势在模式切到 HUMAN 之后 | 内核没有取消 `SyntheticGestureController` 队列的公开 API | 手势可能继续跑完（时长受命令参数限制），其事件按人类输入投递 | 后续可在 `SetOwnership(HUMAN)` 时取消队列 |
| 合成手势内部事件超出 5s window | window 硬上限 5s | 尾部事件被当作人类输入（投递 + 1s 暂停） | 长滚动请拆命令 |
| TouchEmulator 生成的鼠标事件（默认 touch 后端） | 只有命令入口裁决，内部事件无标记 | 落在 window 内的按 automation 处理，之后按人类 | 同上 |
| `synthesizeScrollGesture` 的 `repeat_count × delay` 很长 | window 不随 repeat 续期 | 仅影响 presence/pause 判定，不影响投递 | 可后续在 repeat 时分段续期 |

## 10. 风险

| 风险 | 影响 | 缓解 / 建议 |
|---|---|---|
| window 内真实人类输入不设暂停（≤5s） | 极端并发下人类输入不会立刻压制 AI | window 只由已授权命令开启，HUMAN 下不可能；通常 <1s；可调 `kInputOwnershipAutomationWindow` |
| window 5s 上限 | 长合成手势尾部按人类处理 | 拆分长滚动命令；常量集中，便于调整 |
| HUMAN 切换后已启动手势继续 | HUMAN 语义短时被削弱 | 记录为未覆盖；后续在 mode 切换时取消 gesture 队列 |
| 人类暂停 1s | 快速人工操作时 AI 命令频繁收到 ServerError | 扩展层应把该错误当「人类在场」退避 |
| 鼠标 5s / 键盘 2s 上限 | 慢速拖拽或长按被强制关闭，up 丢弃 | 上限远大于正常自动化手势 |
| `kFromDebugger` 不区分来源（RWHI 层） | 第三方注入无法在该层归属 | 已在 InputHandler 层按 `DevToolsSession*` 分桶；未经过 InputHandler 的路径 fail closed |
| `Runtime.evaluate` 旁路 | HUMAN 仍有非输入类修改途径 | 文档明确边界；命令层负责所有权 |
| 构建期 DLL 被其它 agent 的探针进程占用 | 链接偶发 `permission denied` | 等待后重试；不杀进程、不用 `taskkill /IM` |
| `content_features.h` 变更触发大范围重编 | 构建时间 | 增量后已 no work |

## 11. 复现与回退

```bash
cd /d/c/src && export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
export PATH="/d/c/src/third_party/cpython3/host/bin:$PATH"
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome content_unittests
out/WinDev/content_unittests.exe --gtest_filter=InputOwnership*
```

回退：

```bash
git -C /d/c/src apply -R /d/work/browsercli/chromium/patches/028-input-ownership-arbitration.patch
```

（已在当前树验证 `git apply --check -R` 通过，EXIT=0。）

## 12. 交付物清单

| 项 | 路径 | 状态 |
|---|---|---|
| patch 028 | `D:\work\browsercli\chromium\patches\028-input-ownership-arbitration.patch` | 已重新生成，**2956 行**，17 段，sha256 `c129b42baeef335b62e4f6b0ad684db550e21a72078efae07782446f646ea86e` |
| 本文档 | `D:\work\browsercli\docs\windows-input-arbitration-kernel-2026-10-29.md` | R3 |
| 源码改动 | `D:\c\src` 工作区（12 改 + 5 新增） | 保留，未 commit |
| `patch-manifest.json` | `D:\work\browsercli\chromium\patch-manifest.json` | **未修改** |
| 基线 git status | `build-logs/028d-baseline-status.txt` | 生成 patch 前后一致 |
| 构建日志 | `build-logs/028d-content-unittests-build.log`、`028c-chrome-build.log` | 已保存 |
| 单测日志 | `build-logs/028d-input-ownership-tests.log` | 167 行，40/40 PASS |
| 进程排查记录 | `build-logs/028c-chrome-procs*.txt`、`028c-lock-probe*.txt`、`028d-baseline-status.txt` | 说明 DLL 占用来自其它 agent 的 `out/WinDev/chrome.exe` 探针，未杀进程 |

## 13. Patch 029：生产输入权 API 落地（K10-K14、K16，2026-10-29）

> 本节记录 `chromium/patches/029-input-ownership-api.patch`。它把设计文档
> `D:\work\BrowserPilot\docs\input-ownership-api-2026-10-29.md` 的 K10-K14/K16 落到
> `D:\c\src`：content 生产写入口 + `chrome.browserCliInputOwnership` 扩展 API +
> session 绑定 + 30s stale + 命令 scope + `onHumanPresence`。
> Feature 仍 **`FEATURE_DISABLED_BY_DEFAULT`**；`patch-manifest.json` **未修改**，
> 未 commit / push。patch 001-028 与工作区未 reset/checkout。

### 13.1 落地状态（对照 K10-K18）

| # | 项 | 状态 | 落点 |
|---|---|---|---|
| K10 | `stale` / `last_update` / configured-vs-effective | **已实现** | `InputOwnershipSnapshot` 增加 `effective_mode`/`stale`/`last_update`；`InputOwnershipDecider::Refresh()` 在每次决策前重算，`now-last_update>30s` 时一次性清 grant/scope/window，**不覆写** `mode_`/`version_`；心跳恢复立即回到 configured |
| K11 | 生产 `SetOwnership` 版本语义 | **已实现** | 高 version 接受并清 grant；同 version 同 mode 幂等只刷新 `last_update`；同 version 异 mode → `kVersionConflict`；低 version → `kStaleVersion`；version 0 → `kInvalidArgument` |
| K12 | `GrantDebuggerInput` / `BeginInputCommand` / `EndInputCommand` | **已实现** | grant 按 `DevToolsSession*` 分桶、绝对 `now+ttl`（≤600s）；begin 预占 1 单位并建立 `min(now+kindTtl, grant_deadline)` scope（内核上限 120s）；scope 内同 session 事件不再扣减；同 `commandId` 幂等；`cancelled` 且 scope 内 0 事件才退回 1 单位；到期由 `PurgeExpired` 关闭且单位保持消耗 |
| K13 | `SetHumanPresenceCallback` | **已实现** | content 只提供全局回调（仅 chrome 层注册）；controller 在每次决策前后比较去抖计数（300ms）触发；chrome 层通过 `EventRouter::DispatchEventToExtension` 只投递给受信扩展 |
| K14 | `chrome.browserCliInputOwnership`（顶层） | **已实现** | 新 JSON 文件 `chrome/common/extensions/api/browser_cli_input_ownership.json`（6 方法 + `onHumanPresence`）+ `chrome/browser/extensions/api/browser_cli_input_ownership/`；生成注册已进入 `generated_api_registration.cc`。命名必须顶层，原因见 §13.11 |
| K15 | window → 新 tab 立即继承 | **部分（P1 缺口）** | `setOwnership`/`heartbeat` 都会把窗口快照展开到**当前** tab；heartbeat（≤10s）会把 `setOwnership` 之后新建的 tab 补绑。**未实现** `TabStripModelObserver` 的“创建即继承”，见 §13.6 |
| K16 | 扩展 session 映射 | **已实现（fail closed）** | `ExtensionDevToolsClientHost`（chrome.debugger 的 client 注册表）→ `DevToolsAgentHost::GetInputOwnershipSessionForClient()` → 同一 `DevToolsSession*` token；找不到 attach 时 `grantInput`/`begin` 返回 `NO_DEBUGGER_SESSION`，**不会**全局放行 |
| K17 | InputHandler 来源 session 归因 | 028 已具备 | 028 的 `InputHandler` 已在事件/文本/命令三路把 `session()` 作为 token 传入；029 只新增“由 chrome 解析同一个 token”的出口 |
| K18 | 手势绝对截止 | 028 已具备（数值为 5s/2s/300ms） | 028 决策器已实现鼠标 5s、键盘 2s、文本 burst ≤300ms 且都 cap 到 grant deadline；029 未改这些上限 |

### 13.2 可信调用方校验（每次调用，缺一不可）

`chrome/browser/extensions/api/browser_cli/browser_cli_extension_identity.{h,cc}`：

1. `function->extension()` 非空（不是网页/内容脚本调用）；
2. `source_context_type() == mojom::ContextType::kPrivilegedExtension`；
3. `sha256(extension_id)` 命中编译期哈希表（源码不含明文 ID；
   `nnollghpaggbcdkkgoieneffnlijinio` → `0FC2B72B...C1722`）；
4. `Manifest::Location` ∈ {`kExternalPref`, `kExternalRegistry`}；非 official 构建额外允许
   `kUnpacked` / `kCommandLine`（E2E harness），official 构建拒绝；
5. `browser_context()` 不是 OTR；
6. `views::BrowserCliPresentationPolicy::IsManagedModeEnabled()`（= `--browser-cli-background-presentation`）。

API 存在性由 `_api_features.json` + **delegated availability check** 控制
（`browser_cli_input_ownership_availability.cc`）：只有 `InputOwnershipEnforcement`
开启 **且** managed mode 开启 **且** 调用上下文是 privileged extension 时才可见；
否则命名空间本身不存在（不是“注册后拒绝”）。这与 §2.1 的设计一致。

### 13.3 session 绑定链路（K16）

```
chrome.debugger.attach({tabId})
  → extensions::ExtensionDevToolsClientHost（debugger_api.cc 文件内注册表）
  → 新增 chrome 侧查询 GetExtensionDebuggerClientForTab(context, extension_id, web_contents)
  → content::DevToolsAgentHost::GetForTab(web_contents)
  → 新增虚函数 GetInputOwnershipSessionForClient(client)（DevToolsAgentHostImpl 用
     SessionByClient() 命中同一个 DevToolsSession*）
  → content::GrantDebuggerInput(web_contents, token, ...)
```

- token 只以 `const void*` 出现在 public header；chrome 不解析、扩展也拿不到有效指针
  （响应里的 `sessionToken` 目前**不回填**，避免泄漏地址）。
- 第三方 CDP 会话没有 grant 桶；无法解析调用方 session → `NO_DEBUGGER_SESSION`（fail closed）。
- 请求里的可选 `session` 字段按设计忽略：授权只来自“调用方扩展 + tab”解析出的会话。

### 13.4 内核 API（content）

新增 `content/browser/input_ownership/input_ownership_api.cc`，声明在
`content/public/browser/input_ownership.h`（全部 fail closed）：

```cpp
InputOwnershipApplyResult SetInputOwnership(WebContents*, InputOwnershipMode, uint64_t version);
InputOwnershipApplyResult HeartbeatInputOwnership(WebContents*, uint64_t version);
InputOwnershipGrantInfo GrantDebuggerInput(WebContents*, InputOwnershipSessionToken, int actions, base::TimeDelta ttl);
InputOwnershipCommandScope BeginInputOwnershipCommand(WebContents*, token, command_id, max_duration);
InputOwnershipWriteResult EndInputOwnershipCommand(WebContents*, token, command_id, bool cancelled);
InputOwnershipSnapshot GetInputOwnershipSnapshot(WebContents*);
void SetInputOwnershipHumanPresenceCallback(InputOwnershipHumanPresenceCallback);
```

Feature off 或没有 controller 时：`kFeatureDisabled`（Set/Heartbeat）、`granted=false`、
`Begin.result=kFeatureDisabled`、默认 `kAssist` 快照；**不会创建** WebContentsUserData。

### 13.5 chrome 扩展 API

| 方法/事件 | 说明 |
|---|---|
| `setOwnership({target, mode, version})` | tab 或 window；window 展开到当前 tabs 并记录窗口快照（供 heartbeat 补绑） |
| `heartbeat({target, version})` | 不推进 version、不清 grant；window 目标会把 `version == 0` 的新 tab 补绑到窗口记录，`version` 与窗口记录不一致的 tab（tabId 精确覆盖）保持不动且不使窗口心跳失败 |
| `getOwnership({target})` | 只读；返回 `mode/version/stale/lastUpdateAt`（window 取第一个 tab 的真值） |
| `grantInput({target:{tabId}, actions, ttlMs})` | 解析调用方 session；`actions`/`ttlMs` 由内核夹紧（10000 / 600000ms） |
| `beginInputCommand({target:{tabId}, commandId, kind})` | AI → 直接 ok（不占预算，`grantRemaining=-1`）；ASSIST → 预占 1；HUMAN/无 grant → `GRANT_REQUIRED` |
| `endInputCommand({target:{tabId}, commandId, outcome})` | `outcome` 缺省 = `ok`；未知 command → `closed:false, UNKNOWN_COMMAND` |
| `onHumanPresence` | 300ms 去抖后只投递给受信扩展；`kind` ∈ mouse/wheel/key/text/touch/other |

错误码（稳定字符串）与设计 §1.9 对齐：`INVALID_TARGET`、`INVALID_ARGUMENT`、
`UNKNOWN_TARGET`、`STALE_VERSION`、`VERSION_CONFLICT`、`VERSION_UNKNOWN`、
`NO_DEBUGGER_SESSION`、`GRANT_REQUIRED`、`UNKNOWN_COMMAND`。
权限失败返回 `PERMISSION_DENIED: ...`（正常路径下 API 不可见）。

版本/时间戳限制：生成代码把 JSON `integer` 映射为 32 位 `int`，因此
`version` 上限 `INT_MAX`（超出 `INVALID_ARGUMENT`）；`expiresAt`/`deadline`/`lastUpdateAt`/`at`
在 JSON 中用 `number`（double，精确到 2^53）承载 epoch 毫秒。

### 13.6 K15 缺口与临时行为

- 未实现 `TabStripModelObserver`：`setOwnership({windowId})` 之后新建的 tab **不会立即**继承窗口快照。
- 临时行为：新 tab 保持默认 `kAssist`（fail closed，AI 命令会被内核丢弃但不影响人类），
  下一次 `heartbeat`（扩展每 10s）按窗口记录补绑；`setOwnership` 也会补绑。
- 精确下一步：在 `browser_cli_input_ownership_window_binding.{h,cc}` 中按窗口挂
  `TabStripModelObserver::OnTabStripModelChanged`（inserted → `SetInputOwnership(wc, record.mode,
  record.version)`），用 `BrowserCollectionObserver::OnBrowserClosed` 在窗口销毁前 `RemoveObserver`；
  记录键用 `(BrowserContext*, window_id)` + `BrowserWindowInterface` 弱校验，避免 window id 复用。

### 13.7 构建与单测（原始输出）

```bash
cd /d/c/src
export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
export PATH="/d/c/src/third_party/cpython3/host/bin:$PATH"
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome content_unittests
out/WinDev/content_unittests.exe --gtest_filter=InputOwnership*
third_party/ninja/ninja.exe -C out/WinDev -n chrome content_unittests
```

- `content_unittests`：EXIT=0；`chrome`：EXIT=0（`[432/434] LINK(DLL) chrome.dll`）。
- 最终 `ninja -n`：`ninja: no work to do.`
- `--gtest_filter=InputOwnership*`：**51/51 PASS**（harness 分片执行，各分片均 `PASSED`；
  0 FAILED）。新增覆盖：stale 降级/恢复、版本幂等/冲突/低版本、grant/begin/end 与 scope
  绝对截止、cancel 退费、Feature off 惰性、free-function 贯通、human presence 回调去抖。
- `chrome.browserCliInputOwnership` 的生成注册已出现在
  `gen/chrome/browser/extensions/api/generated_api_registration.cc`（6 个 Function），
  `api_features.cc` 含顶层 `browserCliInputOwnership`。
- 新增 chrome 单测 `browser_cli_extension_identity_unittest.cc` 已编译进 `unit_tests`
  目标对象（`obj/chrome/browser/extensions/unit_tests/...obj`，EXIT=0）；**未链接/运行
  `unit_tests`**（成本高，按任务约定以“可编译 + content 全量单测”为准）。

### 13.8 现在可以跑的真实 E2E（L 的 harness）

前置：`--enable-features=InputOwnershipEnforcement` + `--browser-cli-background-presentation`，
安装 allowlisted 扩展（非 official 构建可用 unpacked）。

- `input_status` 的 `kernel.state === "ready"`、`version >= 1`；
- `setOwnership/heartbeat/getOwnership` 三件套 + 版本冲突回退；
- `grantInput` 在 attach 后可解析 session；未 attach → `NO_DEBUGGER_SESSION`；
- `withKernelInputScope`（AI：begin 返回 ok 不占预算；ASSIST：1 命令 1 grant）；
- HUMAN 下第三方 CDP 注入被丢弃、AI 下放行；
- 30s 停心跳后 effective ASSIST（AI 注入被内核丢弃），恢复心跳后回到 configured；
- `onHumanPresence` 事件到达扩展（300ms 去抖），扩展侧再 500ms 去抖进入 ASSIST。

**仍不可跑**：K15 的“新 tab 立即继承”（需等下一次 heartbeat）；以及未在真实浏览器里验证
service worker 的 `source_context_type` 是否为 `kPrivilegedExtension`（若为 `kUnspecified`，
所有调用会返回 `PERMISSION_DENIED`——这是本轮最需要 E2E 首先确认的点）。

### 13.9 本轮未覆盖 / 新风险

1. **K15**：见 §13.6（P1 缺口）。
2. **SW context type**：`source_context_type()==kPrivilegedExtension` 的假设未在真机验证；
   失败表现为明确的 `PERMISSION_DENIED`（不是放行）。
3. **window 快照账本**是 chrome 进程内静态缓存（上限 256 项），浏览器重启/窗口关闭后自然失效；
   真值仍以每个 WebContents 的 content 状态为准。
4. **`sessionToken` 不回填**：响应里没有可审计 token；扩展侧字段可选，不影响语义。
5. **版本上限**：JSON/C++ `int`，`version > INT_MAX` 会被拒绝（扩展每模式变化 +1，实际不可达）。
6. **`kind` 只用于 TTL/审计**：chrome 端固定用 120s 上限发给内核；扩展侧 10s/60s/120s 的
   细分仍以客户端 `commandTtlFor()` 为准（内核只做上限兜底）。
7. **单测未跑 `unit_tests`**：仅编译对象；chrome 层行为（target 解析、窗口展开、事件路由）
   尚未有自动化覆盖，依赖 L 的 E2E。
8. **`Runtime.evaluate`/`DOM.*` 边界不变**：仍由扩展命令层守卫（§2.5/§7.1）。

### 13.10 交付物（patch 029）

| 项 | 路径 / 值 |
|---|---|
| patch 029 | `D:\work\browsercli\chromium\patches\029-input-ownership-api.patch`，**3611 行**，33 个文件段（+3020/−23） |
| patch sha256 | `b3354b10205adb2d9abba5fc91599fc02f4fbfcc8437526da851a35c058c4f9c` |
| 回退校验 | `git apply --check -R` 通过（EXIT=0） |
| 基线 | 由 patch 001-028 重建到临时树后取 diff，patch 只含本轮 029 改动 |
| 构建日志 | `/tmp/build-content-final.log`、`/tmp/build-rename2.log`（本机临时） |
| 单测日志 | `/tmp/final-input-ownership-tests3.log`，51/51 PASS |
| 真实探针 | `build-logs/029e-probe-input-ownership.js`、`.out`、`029e-probe-*.err` |
| manifest / commit | `patch-manifest.json` **未修改**；未 commit / push；工作区保留 |

### 13.11 真实 E2E 根因与命名修订（2026-10-29 第二轮）

#### 根因（已由真实探针与源码确认）

`extensions/renderer/feature_cache.cc:150-155` 在枚举可安装的 API namespace 时：

```cpp
// Exclude child features (like events or specific functions).
if (api_feature_provider->GetParent(*feature) != nullptr)
  continue;
```

`_api_features.json` 里同时存在父 feature `"browserCli"`（`getCapabilities`）和子 feature
`"browserCli.inputOwnership"`，后者被 `GetParent()` 判定为 `browserCli` 的子 feature，
**BindingAccessor 从未安装**，因此扩展页面/SW 里 `chrome.browserCli.inputOwnership` 永远
`undefined`（`chrome.browserCli.getCapabilities` 正常）。

排除项：
- renderer 命令行确实有 `--enable-features=InputOwnershipEnforcement` 与
  `--browser-cli-background-presentation`（L 的 E2E renderer 证据 + 本轮探针 positive case）；
- delegated availability 回调只在 browser 侧以 `context=kWebUi` 被调用（临时日志），
  说明 extension renderer 根本没走到 availability 计算；
- schema/`generated_api_registration.cc`/`api_features.cc` 都包含该命名空间。

#### 选择：方案 B（顶层命名空间）

- 命名空间改为 **`chrome.browserCliInputOwnership`**（无父 feature），
  `_api_features.json` 键为 `"browserCliInputOwnership"`，保留独立 allowlist +
  `requires_delegated_availability_check`。
- **未修改** `extensions/renderer/feature_cache.cc`（影响所有 child feature，风险大）。
- 扩展客户端 `D:\work\BrowserPilot\src\background\input-ownership-api.ts` 优先探测
  `chrome.browserCliInputOwnership`，并保留旧嵌套名回退；客户端单测已改为新名字并覆盖回退。
- 设计文档 §1.1 的 `browserCli.inputOwnership` 名称与实现不一致，**以本节为准**：
  这是为绕开 Chromium 子 feature 安装规则的实现修订，语义/参数/错误码不变。
- 另外把 `--browser-cli-background-presentation` 加入
  `ChromeContentBrowserClient::AppendExtraCommandLineSwitches` 的 renderer 公共 switch，
  因为 availability 在 renderer 里求值；否则 managed 模式在 renderer 看不到，
  命名空间会被误隐藏。

#### 真实探针（headless + 临时 profile + unpacked 扩展，无可见窗口）

脚本：`build-logs/029e-probe-input-ownership.js`（`--headless=new --in-process-gpu`，
隔离 `--user-data-dir`，只 kill 自己启动的子进程）。原始结果：

| 场景 | 受信扩展 popup | 受信扩展 SW | 非受信扩展 page | 普通网页 |
|---|---|---|---|---|
| feature on + managed | `namespace=object`，7 个方法均 `function`，`onHumanPresence=true`，`legacyIo=false` | `io=true` | — | `undefined` |
| feature off + managed | `undefined`（`browserCli.getCapabilities` 仍在） | `undefined` | — | `undefined` |
| feature on + managed off | `undefined` | `undefined` | — | `undefined` |
| feature on + managed（非受信扩展） | — | — | `namespace=undefined`（连 `browserCli` 也 `undefined`） | `undefined` |

关键断言（受信 popup，feature+managed）：

```
namespace: "object"
setOwnership: "function"    grantInput: "function"
beginInputCommand: "function"  endInputCommand: "function"
heartbeat: "function"       getOwnership: "function"
onHumanPresence: true       legacyIo: false
```

同一轮还验证了：`chrome.browserCli` 顶层（capabilities）仍正常；
受信 SW 也看到新命名空间；非受信扩展与普通网页完全看不到。

#### 最小 chrome 层测试

- 新增 `chrome/browser/extensions/api/browser_cli/browser_cli_input_ownership_availability_unittest.cc`
  （编译进 `unit_tests` 目标对象，EXIT=0）：断言 map 键是顶层
  `browserCliInputOwnership`；feature off / managed off / 非 privileged context 均不可用，
  只有 feature+managed+privileged extension 可用。
- `unit_tests` 全量链接需 4516 步，本轮未链接/运行；功能验证以真实探针为准。

#### P0 回归记录（本轮不修）

managed hidden 窗口的产品截图失败：`fromSurface=true` 8s 超时；`fromSurface=false` 返回
`-32000 Only screenshots from surface are allowed`。同一 tab 的原始 CDP
`Page.captureScreenshot{fromSurface:true}` 成功（10092 bytes），说明内核能出帧，
问题在产品截图路径/超时或 cursor policy。归主代理后续处理，本轮未改截图代码。



