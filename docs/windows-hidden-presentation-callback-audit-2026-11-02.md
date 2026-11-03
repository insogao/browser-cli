# Presentation callback 生命周期审计（A2 复核）

日期：2026-11-02
范围：对 `chromium/patches/036-presentation-callback-lifecycle.patch` 的两项强制正确性
复核：remembered-failure 的 FIFO/顺序缺陷，以及 failed-presentation 回调语义的
caller/consumer 契约审计。
分支：`agent/rc-a2-audit`（base clean PR #9 + patch 036）；共享源码 `D:/c/src`
（HEAD `7d0f28c3` = `chromium/VERSION` 固定 revision，已应用 036 与 P1 037）。
状态：**缺陷已复现并修复；`blink_platform_unittests` 在 out/WinRelease 实测通过**
（28/28，关键用例 `--gtest_repeat=3` 稳定）。

---

## 1. 结论摘要

1. **缺陷 1（FIFO 顺序）已确认并修复。** 原实现把 failed details 按**到达顺序**放进
   `failed_presentation_callbacks_`，成功清理却假设它按 token 有序，只从队首弹出
   `<= success_token` 的条目。构造 `[44, 43]` 后 `success(43)`，队首 44 > 43，
   循环立即停止，token 43 的陈旧失败被保留；随后注册 token 43 的 callback 会被
   **错误地以 failure 完成**（实测 BEFORE：3 个断言失败，见 §2）。
   修复：成功时删除 remembered 表中**所有** `token <= success_token` 的条目，
   并额外记录 `latest_presented_frame_token_` 过滤掉之后才到达的陈旧失败。
2. **缺陷 2（契约）已审计完毕。** 生产上唯一的注册链路
   `WebFrameWidgetImpl::ReportTimeSwapPromise` → `WidgetBase::AddPresentationCallback`
   → `LayerTreeView::AddPresentationCallback` 会用 `RunCallbackAfterPresentation()`
   把 failed details 的空 timestamp 回填成 swap_time；所有下游 consumer
   （PaintTiming、WindowPerformance event timing、same-document navigation、
   AnimationFrameTimingMonitor trace、GPU benchmarking）都只读 timestamp，且既有的
   `DidNotSwap(SWAP_FAILS)` 路径本来就以 swap_time 完成它们，因此**带 `kFailure`
   的完成是可接受的**。
   但审计发现一个真实约束：Paint Timing 的 `pending_presentation_data_` 按注册
   （即 frame token）顺序消费，旧实现“只完成 failed token、不管队首”会把**较新的
   callback 先于较旧的 callback** 运行，造成 LCP/FCP/Element Timing 的
   presentation time 归因错位。**因此修复不只是 FIFO 清理**：失败完成改为
   **只从队首完成“已知失败”的连续前缀**（front-first drain），保持回调按 frame
   token 单调运行。
3. **60 硬上限/DCHECK 未改动。** `kMaxBufferSize = 60`、
   `DCHECK_LE(callbacks.size(), kMaxBufferSize)` 与
   `static_assert(kPresentationCallbackPressureLimit < kMaxBufferSize)` 全部保留；
   没有删除 DCHECK、没有扩大 buffer、没有静默丢弃 callback。

---

## 2. 缺陷 1：`[44, 43]` + `success(43)`（before/after）

### 2.1 复现

单元测试：`LayerTreeViewTest.RememberedFailuresOutOfOrderCannotFailLateCallbackAfterSuccess`
（`layer_tree_view_unittest.cc`）

```text
注册 token 1..30（达到 pressure limit）
DidPresentCompositorFrame(44, failed)   // 未注册 -> 记住 44
DidPresentCompositorFrame(43, failed)   // 未注册 -> 记住 43，此时表内到达序为 [44, 43]
DidPresentCompositorFrame(43, success)  // 成功：1..30 完成，43 应被 supersede
AddPresentationCallback(43, cb)         // 迟到的注册
DidPresentCompositorFrame(45, success)  // 契约：cb 应由后续 success 完成
```

### 2.2 BEFORE（未修复的实现，同一测试二进制）

```text
$ out/WinRelease/blink_platform_unittests.exe \
    --gtest_filter='LayerTreeViewTest.RememberedFailuresOutOfOrderCannotFailLateCallbackAfterSuccess'

layer_tree_view_unittest.cc(619): error: Value of: completions.empty()
  Actual: false
  Expected: true
layer_tree_view_unittest.cc(627): error: Value of: completions[0].failed
  Actual: true
  Expected: false
layer_tree_view_unittest.cc(628): error: Expected equality of these values:
  completions[0].timestamp  -> 489529210955 (failure+1us)
  next_success_timestamp    -> 489529210964 (success(45))
[  FAILED  ] ... (159 ms)
1 FAILED TEST
```

即：迟到的 token 43 callback 被陈旧的 failed details 立即完成，后续 success(45)
不再运行它。

### 2.3 根因

```cpp
// 旧代码：只弹队首，且假设 remembered deque 以 token 递增
while (!failed_presentation_callbacks_.empty() &&
       failed_presentation_callbacks_.front().first <= frame_token) {
  failed_presentation_callbacks_.pop_front();
}
```

remembered 表是**到达顺序**（失败反馈可以乱序到达），不是 token 顺序，所以
`[44, 43]` 的队首 44 阻断了对 43 的清理。

### 2.4 修复

- `ForgetFailedPresentationCallbacksUpTo(token)`：成功时删除**全部**
  `entry.first <= token` 的条目（不再依赖队首顺序）。
- `latest_presented_frame_token_`：记录已成功 present 的最大 token；失败反馈若
  `token <= latest_presented` 则视为陈旧（success 已覆盖其回调），不再 remember。
- `RememberFailedPresentationCallbacks` 对同一 token **去重替换**，避免重复反馈
  占用 bound 或在清理后留下第二条。

### 2.5 AFTER

```text
$ out/WinRelease/blink_platform_unittests.exe \
    --gtest_filter='LayerTreeViewTest.RememberedFailuresOutOfOrderCannotFailLateCallbackAfterSuccess'
[ RUN      ] ...
[       OK ] ... (2 ms)
[  PASSED  ] 1 test.
SUCCESS: all tests passed.
```

---

## 3. 缺陷 2：caller / consumer 契约审计

### 3.1 唯一的生产注册链路

| 层 | 位置 | 说明 |
| --- | --- | --- |
| swap promise | `third_party/blink/renderer/core/frame/web_frame_widget_impl.cc:4004-4035` (`RunCallbackAfterSwap`) | `DidSwap()` 后投递注册任务；只在该路径调用 `WidgetBase::AddPresentationCallback` |
| widget | `third_party/blink/renderer/platform/widget/widget_base.cc:1192` | 直接转发到 `LayerTreeView` |
| LayerTreeView | `.../compositing/layer_tree_view.cc` | 本补丁修改的队列 |

`RunCallbackAfterSwap` 把用户回调包成 `RunCallbackAfterPresentation`（同文件
`:4037-4059`）：

```cpp
bool presentation_time_is_valid =
    !presentation_time.is_null() && (presentation_time > swap_time);
if (presentation_time_is_valid) { ... } else {
  // 用 swap_time 覆盖空/无效 timestamp，flags 原样保留
}
```

所以 LayerTreeView 用 failed details（`PresentationFeedback::Failure()` 的 timestamp
其实是 `Now()`；测试里也带非空 timestamp）完成回调时，下游**总能拿到非空时间**；
这与既有的 `DidNotSwapReason::SWAP_FAILS` 路径
（`ReportSwapAndPresentationFailureOnTaskRunner`，`:4092-4111`）形状完全一致。

### 3.2 consumer 逐个审计

| consumer | 位置 | 对 failed details 的行为 | 是否接受 failure | 顺序假设 |
| --- | --- | --- | --- | --- |
| Paint Timing（FCP/LCP/element/animated） | `core/paint/timing/paint_timing.cc:353, 730`；`FlushPaintTimingsOnFramePresentedCallback` | 只读 `presentation_feedback.timestamp`；不读 `failed()`；按 pipeline 消费 `pending_presentation_data_` | 接受（既有 broken-swap 路径同样以 swap_time 完成） | **是**：`data->id <= id` 且用当前 callback 的 `paint_timing_info` 赋值，要求 id/token 单调递增；`kPaintTimingWaitForPresentationFrameIndex` 新路径虽能缓冲乱序，但默认关闭 |
| WindowPerformance event timing | `core/timing/window_performance.cc:859` (`OnPresentationPromiseResolved`) | 注释明确 "presentation time might be 'fake' when broken swap promise"；只读 timestamp/frame_id | 接受 | 按 frame_index 归因；乱序会推迟归属，不崩溃 |
| Same-document navigation UMA | `core/frame/local_frame_view.cc:1277` → `core/frame/local_frame_client_impl.cc:486` | `timestamp - start` 记 UMA | 接受（wrapper 保证非空） | 单次请求，无顺序依赖 |
| AnimationFrameTimingMonitor trace | `core/frame/animation_frame_timing_monitor.cc:570-590` | 只用 timestamp 发 trace event | 接受 | 单次，无依赖 |
| GPU benchmarking `AddSwapCompletionEventListener` | `content/renderer/gpu_benchmarking_extension.cc:1505-1516` | 忽略 details，只触发 JS 回调（测试 API） | 接受（且失败时完成正是期望） | 无 |
| `WidgetBase::ForceRedraw` | `widget_base.cc:426` | 走 `LayerTreeHost::RequestPresentationTimeForNextFrame`（`cc::PresentationTimeCallbackBuffer`），与本补丁的队列无关 | N/A | N/A |

没有任何 consumer 检查 `presentation_feedback.failed()`，也没有 consumer 对失败
DCHECK；`gfx::PresentationFeedback::Failure()` 提供非空 timestamp。结论：**failure
完成本身可接受**，前提是（a）只用于 one-shot 反馈证明“该 token 永远不可能 present”
的情况，（b）回调仍按 frame token 顺序运行。

### 3.3 one-shot / “确定不可能”的证据

- `components/viz/service/frame_sinks/compositor_frame_sink_support.cc:1141`
  `CHECK(!frame_timing_details_.contains(frame_token));`
  —— 每个 frame token 只会收到一份 `PresentationFeedback`。
- failed feedback 的来源全部是确定性的丢弃：
  `surface.cc:102/1073`（active/pending frame 被替换、surface 销毁）、
  `compositor_frame_sink_support.cc:1170`（frame 被 reject）、
  `display.cc:343`（display 销毁时清空 pending presentation）。
- frame sink 丢失时根本不会再有反馈，因此 `DidLoseLayerTreeFrameSink()` /
  `RequestNewLayerTreeFrameSink()` 直接以 failure 完成所有 pending。

### 3.4 由此产生的实现调整

原补丁在 pressure 状态下完成“**任意** failed token 自己的 callback”，即使它不在队首。
这满足“该 token 确定不可能”，但会乱序运行 callback，破坏 Paint Timing 的归因前提
（§3.2）。修复后：

```text
失败反馈 -> remember(token, details)
         -> 若队列已达到 pressure limit（stalled）:
              从队首开始，只要队首 token 的失败已知，就按顺序完成它；
              遇到第一个“失败未知”的 token 立即停止。
```

这样既只消费 one-shot 证明的失败，又保持 frame token 单调。

---

## 4. 修复后的状态机（patch 036 当前实现）

| 状态/字段 | 语义 |
| --- | --- |
| `presentation_callbacks_` | 按 frame token 递增的 pending 队列（原有不变量） |
| `latest_presented_frame_token_` | 已成功 present 的最大 token；`<=` 它的失败反馈是陈旧数据，不 remember |
| `presentation_callbacks_stalled_` | 队列曾达到 `kPresentationCallbackPressureLimit (30)` 且此后没有 success；置位后失败反馈会被消费，success 清除 |
| `failed_presentation_callbacks_` | `(token, failed details)`，**每次失败反馈都 remember**（不只 pressure 状态），同 token 替换；上限 64；success/ sink loss 清理 |

关键路径：

1. **failed feedback**：`size >= 30` 时置 stalled；token 未被 success 覆盖则 remember；
   stalled 时执行 front-first drain。
2. **success feedback**：完成队列中 `token <= success_token` 的全部条目（原有语义），
   更新 `latest_presented_frame_token_`，清 stalled，删除 remembered 中
   `token <= success_token` 的**全部**条目（不再只看队首）。
3. **late registration**（失败反馈早于注册）：仅当队列为空且 stalled 时立即以 remembered
   failed details 完成（此时不可能乱序）；否则入队，由后续 drain 按序完成。
4. **sink loss / replacement**（`DidLoseLayerTreeFrameSink()` 与
   `RequestNewLayerTreeFrameSink()`）：全部 pending 以 `PresentationFeedback::Failure()`
   完成，清 remembered、`latest_presented_frame_token_` 与 stalled，允许新 sink
   复用 token 序列。

不变量：

- **I1 顺序**：任何路径（success 前缀、failure 前缀、sink loss）都按 frame token
  递增完成 callback；不存在“新 token 先完成”。
- **I2 确定性**：failure 完成只使用该 token 自己的 one-shot 反馈，或 sink 已丢失。
- **I3 契约**：未 reaching pressure 时失败不消费任何 callback，仍由后续 success 完成
  （`RunPresentationCallbackOnSuccess` 与 `FailedPresentationBelowPressureKeepsCallbacks`
  覆盖）。
- **I4 有界**：remembered 去重 + 上限 64；callback 队列上限仍由
  `kMaxBufferSize = 60` + DCHECK 兜底，未修改。
- **I5 token 生命周期**：sink loss/replacement 清空 remembered 与
  `latest_presented_frame_token_`，旧 token 不会污染新 sink。

---

## 5. 测试与结果

### 5.1 构建（out/WinRelease，与既有 WinRelease 配置一致）

```bash
export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
third_party/ninja/ninja.exe -C out/WinRelease blink_platform_unittests
```

结果：`exit 0`（新增测试与实现编译通过；`blink_platform_nocompile_tests` 通过）。

### 5.2 运行

```bash
out/WinRelease/blink_platform_unittests.exe --gtest_filter='LayerTreeView*'
```

结果：**28/28 PASS**（3 个 shard 批次：10 + 10 + 8），`SUCCESS: all tests passed.`

关键用例重复 3 次：

```bash
out/WinRelease/blink_platform_unittests.exe \
  --gtest_filter='LayerTreeViewTest.RememberedFailuresOutOfOrderCannotFailLateCallbackAfterSuccess:\
LayerTreeViewTest.PresentationCallbackQueueBoundedWhenNeverPresented:\
LayerTreeViewTest.RepeatedFrameSinkLossRunsEveryCallbackOnce:\
LayerTreeViewTest.FrameSinkReplacementClearsPendingPresentationCallbacks:\
LayerTreeViewTest.LatePresentationCallbackCompletesWithFailureWhenInOrder' \
  --gtest_repeat=3
```

结果：15/15 PASS（`--gtest_repeat=3` × 5 用例）。

### 5.3 用例矩阵

| 用例 | 覆盖点 | BEFORE | AFTER |
| --- | --- | --- | --- |
| `RunPresentationCallbackOnSuccess`（既有） | failed(1) 后 success(2) 仍运行 token 1（契约不回归） | PASS | PASS |
| `FailedPresentationBelowPressureKeepsCallbacks` | < 30：失败不消费，后续 success 完成 | PASS | PASS |
| `FailedPresentationCompletesKnownFailedPrefixInOrder` | 乱序失败（15→1→3→2）只完成已知失败的连续前缀，顺序 1,2,3；success 覆盖其余 | **FAIL**（先完成 15，乱序） | PASS |
| `RememberedFailuresOutOfOrderCannotFailLateCallbackAfterSuccess` | **`[44,43]` + `success(43)` + late 43**：不得用陈旧失败完成 | **FAIL**（3 断言） | PASS |
| `SuccessSupersedesRememberedFailuresUpToItsToken` | success 清理 `<= token`；之后到达的陈旧失败不 remember | 未测（新用例） | PASS |
| `LatePresentationCallbackCompletesWithFailureWhenInOrder` | 反馈早于注册 + 队列已空 + stalled：立即以失败完成且不重复入队 | 未测（新用例） | PASS |
| `LatePresentationCallbackWaitsForEarlierFrames` | 反馈早于注册但队首仍 pending：入队，随后按序 1..31 完成 | 未测（新用例） | PASS |
| `DuplicateFailedPresentationIsRememberedOnce` | 重复失败去重；success 后不留陈旧条目 | 未测（新用例） | PASS |
| `PresentationCallbackQueueBoundedWhenNeverPresented` | 30 帧突发 + 乱序失败 + 500 帧 1 Hz 无 success：530 个 callback 全部按序完成一次 | 未测（旧二进制被前序用例中断） | PASS |
| `FrameSinkLossCompletesPresentationCallbacks` | sink loss 完成全部 pending；remembered/ token 复用不串味 | PASS | PASS |
| `FrameSinkReplacementClearsPendingPresentationCallbacks` | 线程模式的 `RequestNewLayerTreeFrameSink()` 路径同样清理 | PASS | PASS |
| `RepeatedFrameSinkLossRunsEveryCallbackOnce` | 模拟 rapid show/hide × 5：每轮 token 从 1 重新开始，callback 恰好运行一次 | PASS | PASS |

> BEFORE 一列中 “PASS” 表示该用例在旧实现下实测通过；两个 “FAIL” 用例是本次修复的
> 回归门禁（`FailedPresentationCompletesKnownFailedPrefixInOrder` 在旧实现下先完成
> token 15，`[44,43]` 用例在旧实现下用陈旧失败完成 late 43）。其余新增用例在旧二进制
> 上未完整执行（前序用例触发了测试 helper 的越界断言并中断该 shard），因此以 AFTER
> 结果为准；`FrameSinkLoss…`/`FrameSinkReplacement…`/`RepeatedFrameSinkLoss…` 三个
> 用例在旧二进制上单独运行且 PASS。

### 5.4 关于 60 硬上限

- `kMaxBufferSize = 60`、`AddCallback()` 中的
  `DCHECK_LE(callbacks.size(), cc::PresentationTimeCallbackBuffer::kMaxBufferSize)`
  和 `static_assert(30 < 60)` 均未改动（见 patch diff）。
- out/WinRelease 是 `is_debug=false` 且未设 `dcheck_always_on`，因此 DCHECK 在此
  二进制中被编译掉；“触发 60 即崩溃”的 fail-closed 行为只能在 DCHECK 构建观察到。
  本次实测通过“530 帧无 success 时 callback 全部按序完成”证明队列不再依赖 60 上限
  兜底；60 上限本身保持 fail-closed。

---

## 6. 剩余风险

1. **反馈完全缺失**：若 viz 对某个 token 既不 success 也不 failure，队列仍会在 60 处
   DCHECK（fail-closed，符合要求）。本补丁不伪造成功、不批量丢弃。
2. **单次 BeginFrame 前大量提交**：一分钟内提交 60+ 帧且反馈尚未到达时，仍可能触达
   60 上限；这与 patch 前一致，未扩大上限。
3. **乱序成功**：success 路径本身按 `<= token` 前缀完成，天然单调；新增
   `latest_presented_frame_token_` 只用于过滤之后到达的陈旧失败。
4. **未执行浏览器级矩阵**：本审计只构建/运行 `blink_platform_unittests`；managed
   hidden 180 s、OOPIF、GPU restart 等真实场景仍需在浏览器构建上重跑（原 doc §5.2）。
5. **Apple 路径未改**：`core_animation_error_code_callbacks_` 行为不变；本补丁仅
   Windows 相关场景。

---

## 7. 复核命令清单

```bash
# 1) 构建
third_party/ninja/ninja.exe -C out/WinRelease blink_platform_unittests

# 2) 全量 LayerTreeView suite
out/WinRelease/blink_platform_unittests.exe --gtest_filter='LayerTreeView*'

# 3) 反例（BEFORE 失败、AFTER 通过）
out/WinRelease/blink_platform_unittests.exe \
  --gtest_filter='LayerTreeViewTest.RememberedFailuresOutOfOrderCannotFailLateCallbackAfterSuccess'
```

证据文件：本审计对应提交仅包含
`third_party/blink/renderer/platform/widget/compositing/layer_tree_view.{cc,h}` 与
`layer_tree_view_unittest.cc` 的改动（patch 036 重新生成），共享源码其他文件未修改。
