# Windows 长驻隐藏窗口：presentation feedback 生命周期（A2，route 2）

日期：2026-11-01
范围：managed 窗口“模型可见、原生不呈现”时，renderer 的 presentation callback
累积到 61/60 并触发 DCHECK 的生命周期分析、最小安全补丁、不变量与测试矩阵。
配套补丁：`chromium/patches/036-presentation-callback-lifecycle.patch`
（3 文件；仅 Blink，无 Viz 改动）。

状态：**提案已落盘；本 worktree 已在 WinRelease 构建并验证**。共享源码 `D:/c/src` 只读，
未修改、未应用补丁。分支 `agent/rc-hidden-a2`，基线 `833b880`（browser-cli PR #1）。

---

## 0. 结论摘要

1. **61/60 DCHECK 不在 `cc::PresentationTimeCallbackBuffer` 里。**
   崩溃点是 Blink 的 `LayerTreeView::presentation_callbacks_` 双端队列：
   `third_party/blink/renderer/platform/widget/compositing/layer_tree_view.cc:553`
   `DCHECK_LE(callbacks.size(), cc::PresentationTimeCallbackBuffer::kMaxBufferSize)`。
   它只是**复用**了 `cc::PresentationTimeCallbackBuffer::kMaxBufferSize = 60`
   （`cc/trees/presentation_time_callback_buffer.h:43`）这个常量。

2. **Viz 已经在发送 failed presentation feedback。**
   被 supersede 的帧在 `Surface::ActivateFrame()` 里被
   `UnrefFrameResourcesAndRunCallbacks()` 回收；若该帧从未被 display 取走
   presentation 通知，就同步产生 `gfx::PresentationFeedback::Failure()`
   （`components/viz/service/surfaces/surface.cc:717`、`:1070`）。pending 帧被替换、
   frame 被 reject、surface 销毁也走同一条路径。renderer 在下一次
   `OnBeginFrame`/`DidNotProduceFrame` 时通过 `AsyncLayerTreeFrameSink::ExportFrameTiming()`
   收到它们。

3. **真正的缺口在 Blink：它完全忽略 failed feedback。**
   `LayerTreeView::DidPresentCompositorFrame()` 在
   `frame_timing_details.presentation_feedback.failed()` 时直接 `return`
   （`layer_tree_view.cc:421-424`），既不 run、也不删除对应条目。于是隐藏窗口每提交
   一帧就 +1 条，viz 的 undrawn-frame 节流又把提交压到约 1 Hz，约 60 s 后 DCHECK。

4. **不能“收到 failed feedback 就把 `<= token` 的回调全清掉”。**
   回调契约是“在 `frame_token` **或其后任一帧**首次成功 present 时运行”
   （`layer_tree_view.h:137-141`）；且 failed feedback 可能相对 success 乱序到达
   （已 drawn 的旧帧的 success 可能晚于新帧的 failure）。清掉 `<= T` 会把“之后会成功
   的帧”变成假失败，也会破坏上游单测
   `LayerTreeViewTest.RunPresentationCallbackOnSuccess`。

5. **最小安全补丁（只改 Blink）：**
   - 低于压力阈值（30）：**保持上游语义**，failed feedback 不消费任何回调；
   - 达到压力阈值：只完成**与 failed feedback 同一 token** 的回调，且传入**带
     `kFailure` 的 failed `FrameTimingDetails`**；其它 token 一律不动；
   - 若失败先于回调注册（反馈/注册竞态），用有界 FIFO 记住该 token，注册时立即以同一
     failed details 完成；
   - frame sink 丢失/替换：所有 pending 回调以 failed feedback 完成（此后不可能再有反馈），
     并清空 remembered 表，避免新 sink 的 token 与旧条目串味；
   - **不改** `kMaxBufferSize`、**不删** DCHECK、不批量丢弃、不把失败伪装成成功。

6. **不建议在 Viz 为“undrawn 但未 superseded”的最新帧伪造 failed feedback。**
   该帧在窗口重新显示/被聚合时仍可能成功 present；提前失败就是假失败。Viz 侧无需新增
   失败路径。

---

## 1. 代码路径（`D:/c/src`，只读）

### 1.1 Viz 何时确定性地产生 failed feedback

| 路径 | 代码 | 说明 |
| --- | --- | --- |
| active frame 被新帧替换 | `components/viz/service/surfaces/surface.cc:717` → `UnrefFrameResourcesAndRunCallbacks()`（`:1053`） | 若 `will_be_notified_of_presentation == false`，`:1070-1074` 发送 `PresentationFeedback::Failure()` |
| pending frame 被替换 | `surface.cc:384` | 同上 |
| surface 销毁 | `surface.cc:143-149` | pending + active 都回收 |
| frame 被 reject | `components/viz/service/frame_sinks/compositor_frame_sink_support.cc:1154-1171` | `DidRejectCompositorFrame()` 直接以 Failure 调 `DidPresentCompositorFrame()` |
| drawn 后 swap 失败 | `Surface::PresentationHelper::~PresentationHelper()`（`surface.cc:97-103`） | 未被 `DidPresent()` 就析构时发 Failure |

`CompositorFrameSinkSupport::DidPresentCompositorFrame()`（`compositor_frame_sink_support.cc:1097`）
要求 token 仍在 `pending_received_frame_times_` 中，并把 details 放进
`frame_timing_details_`；每个 token **只允许一份反馈**（`CHECK(!frame_timing_details_.contains(...))`，
以及 erase 后的 membership CHECK）。因此：

> **Viz 不变量 V1**：被 supersede / replace / reject / destroy 的 frame token，
> 会且只会收到一份失败反馈；token 不会被重复反馈。

### 1.2 反馈如何到达 renderer（以及为什么是 ~1 Hz）

```
CompositorFrameSinkSupport::DidPresentCompositorFrame()
  -> frame_timing_details_（每个 token 一条）
  -> OnBeginFrame() 的 timing_details 参数（compositor_frame_sink_support.cc:1250）
  -> AsyncLayerTreeFrameSink::OnBeginFrame()（cc/mojo_embedder/async_layer_tree_frame_sink.cc:357）
  -> ExportFrameTiming()（:304-309）-> LayerTreeHostImpl::DidPresentCompositorFrame()
```

`CompositorFrameSinkSupport::ShouldSendBeginFrame()`（`:1505`）在
`active_frame_index - last_drawn_frame_index > kUndrawnFrameLimit (3)`
且 `may_throttle_if_undrawn_frames` 时返回 `ThrottleUndrawnFrames`；反馈投递与
BeginFrame 绑定，因此隐藏窗口的失败反馈也被压到约 1 次/秒。这正是 60 条约 60 s 的原因
（实测 59–70 s）。

### 1.3 Blink 侧队列与逐帧注册来源

- `LayerTreeView::presentation_callbacks_`：
  `base::circular_deque<pair<frame_token, vector<OnceCallback<void(FrameTimingDetails)>>>>`。
- 注册入口 `AddPresentationCallback()`（`layer_tree_view.cc:518`）←
  `WidgetBase::AddPresentationCallback()`（`widget_base.cc:1192`）←
  `WebFrameWidgetImpl` 的 `ReportTimeSwapPromise::RunCallbackAfterSwap()`
  （`web_frame_widget_impl.cc:4004-4035`）。每个 swap 的 `DidSwap()` 投递一次注册。
- 生产上的逐帧注册来源：Paint Timing 的
  `PaintTiming::OnPaintFinished()`（`third_party/blink/renderer/core/paint/timing/paint_timing.cc:311-366`）
  为每次有 paint timing 记录的 rendering update 调
  `RegisterNotifyPresentationTime()`；另有 LCP / same-document navigation /
  unbounded surface dismissal / LoAF trace 等一次性请求。
- 成功路径只弹出 `front->first <= frame_token` 的条目；失败路径直接 return。

### 1.4 崩溃证据（已有，未重跑）

`D:/c/logs/hs-repro-raf-1/chrome.log:5223`：

```text
FATAL:...layer_tree_view.cc:554] DCHECK failed:
callbacks.size() <= cc::PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60)

blink_platform!blink::LayerTreeView::AddCallback<...OnceCallback<viz::FrameTimingDetails const&>>
blink_platform!blink::LayerTreeView::AddPresentationCallback
blink_platform!blink::WidgetBase::AddPresentationCallback
blink_core!blink::ReportTimeSwapPromise::RunCallbackAfterSwap
...
Task trace: blink::ReportTimeSwapPromise::DidSwap
```

同目录 `result.json`：`rafCount=61`、`paintCount=62`、`native.visible=false`、
`visibilityState=visible`；`summary.txt`：65.063 s `target-crashed`。
`D:/c/logs/hs-trace-repro/trace.json`：`Surface::CommitFrame` 68、
`Surface::ActivateFrame` 68、`FramePresented` 仅 2。即每一帧都有 supersede 失败反馈，
但 Blink 一条都没消费。

---

## 2. 为什么“失败就清 `<= token`”不安全

1. **契约**：`AddPresentationCallback()` 的注释是
   “run on the first successful presentation for `frame_token` **or a following frame**”。
   一帧自己失败，不代表它的回调不能再由更大的 token 成功完成。
2. **乱序**：`PresentationHelper` 的 success 在 swap 完成后才产生；一个已 drawn 的旧帧
   的 success 完全可能晚于一个已被 supersede 的新帧的 failure。
   `cc::PresentationTimeCallbackBuffer` 的注释也明确写了
   “failed presentation feedbacks can arrive out of order”。
   清 `<= T` 会把仍可能成功的 `T-1` 判成失败（假失败）。
3. **既有单测**：`RunPresentationCallbackOnSuccess` 断言
   failed(1) 之后 success(2) 仍然运行 token 1 的回调。
   无条件消费失败会直接破坏这个契约与测试。

结论：**只能清 `== T`，且只能在该 token 已由 one-shot 反馈证明永远不会成功时清。**

---

## 3. 补丁：`036-presentation-callback-lifecycle.patch`

只改 Blink 三个文件：

| 文件 | 改动 |
| --- | --- |
| `third_party/blink/renderer/platform/widget/compositing/layer_tree_view.h` | 公开常量 `kPresentationCallbackPressureLimit = 30`；4 个私有方法；有界 remembered FIFO |
| `.../layer_tree_view.cc` | 失败反馈压力路径、late-registration 完成、frame sink loss 完成、`static_assert(limit < kMaxBufferSize)` |
| `.../layer_tree_view_unittest.cc` | 5 个新单测 + 保留既有成功路径测试 |

大小：577 行 diff（约 +300 行，其中大部分是注释与测试）。

### 3.1 关键逻辑

```cpp
// DidPresentCompositorFrame()
if (frame_timing_details.presentation_feedback.failed()) {
  if (presentation_callbacks_.size() >=
      kPresentationCallbackPressureLimit) {
    CompleteFailedPresentationCallbacks(frame_token, frame_timing_details);
  }
  return;   // 低于阈值：保持上游“等下一次 success”的语义
}
```

```cpp
// CompleteFailedPresentationCallbacks()
// 只在 deque 中查找 token == frame_token 的条目；
// 找到：把回调 move 出来、erase 条目、用 failed_details 运行；
// 没找到：把 (token, failed_details) 放进有界 FIFO。
```

```cpp
// AddPresentationCallback()
// 若 token 已在 remembered FIFO 中（失败反馈早于注册），
// 立即以 remembered failed_details 运行，不再入队。
```

```cpp
// RequestNewLayerTreeFrameSink() / DidLoseLayerTreeFrameSink()
// 旧 sink 不可能再产生反馈：所有 pending 回调以
// gfx::PresentationFeedback::Failure() 完成；清空 remembered FIFO。
```

### 3.2 语义边界

- **不是把失败当成功**：传入的 details 保留
  `presentation_feedback.flags & kFailure`；timestamp 是
  `PresentationFeedback` 约定的“失败时间”（`ui/gfx/presentation_feedback.h:60-62`），
  不是伪造的展示时间。`RunCallbackAfterPresentation()` 只在 timestamp 为空或早于
  swap_time 时才回填 swap_time，并保留 flags。
- **不批量丢弃**：每个回调都恰好运行一次；没有用“静默 delete”换取内存。
- **不改上限**：`kMaxBufferSize` 与 DCHECK 原样保留；`static_assert` 保证
  30 < 60，留出在途帧余量。若 Viz 反馈真的缺失，仍会按 fail-closed 触发 DCHECK。
- **阈值语义**：<30 时行为与上游完全一致（包括乱序失败后仍等 success）；
  ≥30 说明已经很久没有任何成功 present（隐藏/不呈现状态），此时对“已经确定失败的
  token”完成回调是唯一既能保持 truthful、又能阻止无限增长的选择。

### 3.3 为什么没有 Viz 补丁

- superseded/replaced/rejected/destroyed 的失败反馈**已经存在**（§1.1），
  加代码只会重复发送。
- 唯一没有反馈的是“最新、已 active、但 display 没 draw”的帧。它在窗口重新可见后仍可能
  成功 present，因此不能失败；否则就是任务禁止的 false failure。
- `Display::DrawAndSwap()` 在 `should_swap == false` 时不会取 `PresentationHelper`，
  这正是“未 present”的来源，但也正是“可能稍后 present”的来源。

---

## 4. 不变量

- **I1（Viz one-shot）**：每个提交到 `CompositorFrameSinkSupport` 的 token，
  要么 success、要么在 supersede/reject/destroy 时 failure，最多一份。
- **I2（失败即该 token 永不成功）**：token `T` 的 failure 反馈证明该 CompositorFrame
  不会再成功 present；token 在该 frame sink 生命周期内不复用。
- **I3（回调契约）**：token `T` 的回调在第一个 `token >= T` 的成功 present 时运行；
  `T` 的 failure 本身不授权清掉 `T-1` 或任何其它 token 的回调。
- **I4（有界生命周期）**：没有成功 present 时，每个 widget 的 pending 回调数必须
  `< kMaxBufferSize`；完成必须携带失败语义，不能伪造成功时间。
- **I5（乱序安全）**：failure 只能影响 `token == T` 的条目；不得影响 `< T` 或 `> T`
  的条目。
- **I6（sink 生命周期）**：frame sink 丢失/替换后，旧 token 的回调必须在接收新 sink 的
  token 前全部完成（failure）并清空 remembered 表；防止 token 重启/串味。
- **I7（门禁不变）**：`kMaxBufferSize = 60` 与 `DCHECK` 保持；`kMaxBufferSize` 不放大。

---

## 5. 测试

### 5.1 单元测试（补丁内，需构建 `blink_platform_unittests`）

```sh
# 在应用 001–034 的 Chromium out 目录
autoninja -C out/WinDev blink_platform_unittests
out/WinDev/blink_platform_unittests.exe --gtest_filter='LayerTreeViewTest.*'
```

| 测试 | 断言 |
| --- | --- |
| `RunPresentationCallbackOnSuccess`（既有） | failed(1) 后 success(2) 仍运行 token 1 回调（契约不回归） |
| `FailedPresentationBelowPressureKeepsCallbacks` | 低于阈值：failure 不运行任何回调；后续 success 全部运行 |
| `FailedPresentationAtPressureCompletesOnlyFailedFrame` | 阈值上：只完成失败 token 自己；其它 token 仍等 success |
| `LatePresentationCallbackAfterFailureCompletesWithFailure` | 反馈先于注册：注册时立即以 failed details 完成，且不会重复入队 |
| `PresentationCallbackQueueBoundedWhenNeverPresented` | 连续 500 帧“只失败、不成功”：队列稳定在阈值，不触发 60 DCHECK |
| `FrameSinkLossCompletesPresentationCallbacks` | sink 丢失：全部以 failure 完成；新 sink 的 token 不冲突 |

### 5.2 浏览器级矩阵（真实构建 + 隔离 profile）

复用 `tests/tools/hidden-stability-cdp-check.py`（在 `bc-rc-hidden` 分支/PR #3 中；
本 worktree 未包含）。所有运行要求：独立 `tempfile.mkdtemp` profile、**不使用**
`--no-sandbox`、进程用 Job Object 回收、不操作真实 profile/钥匙串。

| # | 场景 | 期望 | 判定证据 |
| --- | --- | --- | --- |
| 1 | normal visible（未被遮挡） | 无 FATAL；screenshot fresh；`FramePresented` 增长 | 既有 visible 基线不回归 |
| 2 | normal-hidden（先 show 再 hide，真模型隐藏） | 180 s 稳定；rAF 0；截图仍 fresh | `hs-long-stable` 对照 |
| 3 | managed-default（模型 visible + 原生隐藏）动态内容 × 6（text/raf/canvas/webgl/extension/OOPIF） | **180 s 稳定**；无 `layer_tree_view.cc:554` | 修复前 57–70 s 必崩；修复后 callback 完成数 == 注册数 |
| 4 | occluded（原生可见、被完全遮挡，模型 hidden） | 60 s 稳定；`patch 027` 的按需截图仍 fresh | 遮挡不进入“永不 present”状态 |
| 5 | minimized | 60 s 稳定；截图 fresh | `hs-ctl-normal-minimized` / managed-minimized |
| 6 | rapid show/hide（≥100 次用户显式 show 后立即 hide） | 无 FATAL；每次 show 的首次 present 时间有记录；无 callback 泄漏 | 压力路径 + frame sink 生命周期 |
| 7 | continuous screenshots（每 100 ms，连续 600 次，hidden 窗口） | 无 FATAL；截图新鲜度 marker 匹配；不出现 `fromSurface` 永久挂起 | 截图强制 draw 会自然 drain 队列，但不得依赖它 |
| 8 | OOPIF（跨站 iframe，父子 renderer 各自动态） | 父子两个 renderer 都稳定；各自队列有界 | 父子 renderer 独立 LayerTreeView |
| 9 | GPU process restart（kill GPU 进程 ≥5 次，每次后继续重绘） | 无 FATAL；框架恢复后 present 恢复；sink loss 时旧回调全部完成 | `RequestNewLayerTreeFrameSink()` 清空路径 |

建议在 trace 中统计三个新事件（补丁已加）并断言：

```text
PresentationCallbacksCompletedOnFailure        (pressure, per failed token)
PresentationCallbackCompletedLateFailure       (feedback-before-registration)
PresentationCallbacksCompletedOnFrameSinkLoss  (sink loss)
```

判定“没有伪造成功”：上述完成事件的 details 必须
`presentation_feedback.failed() == true`；不得出现“失败 token 以 success 时间完成”。

### 5.3 本 worktree 不能测的

- 不能编译：`D:/c/src/out/WinDev` 是既有构建产物，本 worktree 按要求不修改共享源码，
  因此单测与真实浏览器矩阵都未运行。
- 不能测量 patch 后的队列稳态、`PresentationCallbacksCompletedOnFailure` 计数、
  以及 `layer_tree_view.cc:554` 是否消失。
- 不能测 GPU restart 下的真实 token 重启行为（需要构建）。

---

## 6. 基线证据（已有，未重跑）

- `D:/c/logs/hs-repro-raf-1/`：59.9–65.1 s FATAL，`rafCount=61`，崩溃栈完整
  （`ReportTimeSwapPromise` → `LayerTreeView::AddCallback`）。
- `D:/c/logs/hs-trace-repro/trace.json`：68 commit / 68 activate / 只有 2 FramePresented。
- 触发矩阵（`docs/windows-hidden-presentation-stability-2026-11-01.md`）：
  text/raf/canvas/webgl/extension/iframe 在 57–70 s 崩溃；static/no-paint 稳定；
  真模型隐藏稳定；8 窗口压力下动态窗口全崩、浏览器进程存活。
- `D:/c/logs/hs-repro-screenshot-driven/`：每 10 s 截图 70 s 不崩（workaround，
  不是修复；截图强制 display draw → success 反馈 → 队列被 drain）。

这些证据说明：**失败反馈并不缺失，Blink 的消费缺失才是 61/60 的直接原因**；
但无法证明补丁后的稳态，必须构建后重跑。

---

## 7. 风险与未覆盖项

1. **callback 语义变化（仅压力状态）**：>30 个 pending 时，失败 token 的回调不再等
   后续 success，而是立即收到 failed details。正常可见窗口（队列通常 1–3 条）不受影响；
   上游 `RunPresentationCallbackOnSuccess` 语义保留。指标类消费者（Paint Timing /
   Event Timing / same-document navigation）会记录失败时间而不是后续 success 时间。
2. **反馈/注册乱序**：已用有界（32 条）remembered FIFO 覆盖“失败先到、回调后注册”；
   若 FIFO 被挤掉（极端长延迟），该回调会留在队列里，最终由 DCHECK fail-closed。
   该竞态要求失败反馈快于同线程的 swap 回调投递，正常几乎不可能。
3. **token 复用**：frame sink 丢失时清空 remembered 与 pending；新 sink 的 token
   不会命中旧条目。`LayerTreeHostImpl::next_frame_token_` 本身跨 sink 单调递增，
   但这不构成依赖。
4. **false failure**：只在 token 自己收到 failure 时完成；不会因为“新 token 失败”而
   完成旧 token。低于阈值完全不做。Viz 的 undrawn 最新帧不被失败。
5. **screenshot / copy 路径**：`patch 027` 的 `ActivatePendingRootFrameForCopy()` 会让
   display 真正聚合一次并产生 success，从而自然 drain 队列；补丁不改变该行为。
   周期截图仍只能算 workaround，不作为稳定性证明。
6. **`cc::PresentationTimeCallbackBuffer` 的同类风险（未改）**：它同样在 failure 时
   保留 successful callbacks（`presentation_time_callback_buffer.cc:107-122`）。
   本崩溃的栈不在它里面，且它的调用方（tab-switch 指标、scroll latency、
   `ui::Compositor`）注册频率低。若要硬化，需要同样的“exact token + 压力阈值”
   策略，并改 `PopPendingCallbacks` 的签名以携带 details；属于后续独立补丁。
7. **Apple**：`core_animation_error_code_callbacks_` 未改；macOS 的 managed 窗口是
   真模型隐藏，且该队列只在 Apple 编译。Windows 补丁不影响它。
8. **门禁**：DCHECK 保留即保留 fail-closed 行为；若未来 Viz 出现新的“永不反馈”漏洞，
   renderer 仍会以 DCHECK 暴露，而不是静默增长。
9. **补丁编号**：本 worktree 使用 order 34。GPU-B 的
   `034-gpu-inprocess-direct-composition.patch`（PR #2）与本补丁落在同一 PR 链时，
   需要重新编号（建议 GPU 侧改为 035）；合并前必须重跑
   `python scripts/apply-patches.py --check --checkout <src>`。

---

## 8. 剩余工作

1. 构建 `blink_platform_unittests`，跑 §5.1（必须先过）。
2. 构建 `chrome`，用 §5.2 的 9 个场景真实跑一遍；重点：
   managed-default 180 s、OOPIF 父子、rapid show/hide、GPU restart。
3. 在 trace 中核对失败完成事件全部 `failed() == true`，且没有成功时间伪造。
4. 若上游化：把“压力完成”策略拆成独立 UMA（如
   `Renderer.PresentationCallbacks.CompletedOnFailure`），用真实可见场景确认
   阈值 30 不会误触发（正常窗口 pending 应 <5）。
5. 与 route 1（产品层模型可见性）合并评估：若 managed 窗口改为真正 model-hidden，
   本补丁仍应保留作为内核侧兜底，但触发概率会降到 0。
