# 遮挡窗口取新帧截图：根因与候选实测（2026-10-28）

范围：让“窗口被完全遮挡”时 `Page.captureScreenshot`（`fromSurface=true`）也能取到**最新帧**。
结论：**BLOCKED —— 未产出内核 patch，Chromium 工作树已回到基线（内容目录零改动）。**
本文记录已证实的挂起链路、已实现并实测的候选（全部失败）、可复现命令与原始数据，供后续接手。

关联：[windows-background-render-2026-10-28.md](windows-background-render-2026-10-28.md)（测量矩阵）、
[windows-background-ops-p0-2026-10-28.md](windows-background-ops-p0-2026-10-28.md)（产品侧降级）。

---

## 1. 已证实的挂起链路（有运行时证据）

```
Page.captureScreenshot
  → PageHandler::CaptureScreenshot
      WebContents::IncrementCapturerCount(stay_hidden=true, stay_awake=true)
        → hidden_capturer_count_ > 0
        → CalculatePageVisibilityState() = kHiddenButPainting   // 页面仍 hidden，但允许产帧
        → UpdateVisibilityAndNotifyPageAndView()
             → SetPrimaryMainFrameViewVisibility(OCCLUDED)
                  page_visibility = kHiddenButPainting ≠ kHidden ⇒ view_is_visible = true
                  → RenderWidgetHostViewAura::ShowWithVisibility(kHiddenButPainting)
                       window_->Show(); ShowImpl() → OnShowWithPageVisibility()
                       host()->WasShown(); DFH::WasShown(); DFH::EmbedSurface()
      RenderWidgetHostImpl::GetSnapshotFromBrowser(from_surface=true)
        → RequestForceRedraw() → renderer WidgetBase::ForceRedraw()
             LayerTreeHost()->RequestPresentationTimeForNextFrame(cb);
             LayerTreeHost()->SetNeedsCommitWithForcedRedraw();
        → GotResponseToForceRedraw()            // 渲染进程“已呈现”后才回调
        → RenderWidgetHostViewAura::CopyFromSurface()
        → DelegatedFrameHost::CopyFromCompositingSurface()
        → DelegatedFrameHost::CopyFromCompositingSurfaceInternal()
             HostFrameSinkManager::RequestCopyOfOutput(
                 surface_id, capture_exact_surface_id=false, timeout=0)
```

关键一步在 viz：

```
CompositorFrameSinkSupport::RequestCopyOfOutput()
  → copy_output_requests_.push_back(request);          // 只是入队，不附着到 surface
  → 只有 SurfaceAggregator::PrewalkSurface()
       → Surface::TakeCopyOutputRequestsFromClient()   // 唯一取出口
       → Surface::RequestCopyOfOutput() → 挂到 active frame 的 render pass
       → 该帧被 display 真正 draw 时 copy 才执行
```

即：**copy request 必须由“之后发生的一次合成帧聚合”来取出**。若嵌入该 surface 的合成器
（这里是浏览器窗口的 `ui::Compositor`）此后不再产生任何帧，请求永远停在队列里，
`Page.captureScreenshot` 一直等到 CDP 超时 —— 与实测现象一致。

### 1.1 实测证据（遮挡 vs 隐藏，同一份代码，仅窗口状态不同）

插桩（临时，已全部回退）：
`DelegatedFrameHost::CopyFromCompositingSurface(Internal)`、`Surface::TakeCopyOutputRequestsFromClient`、
`DisplayScheduler::AttemptDrawAndSwap`、`cc::Scheduler` 动作、`WindowTreeHost` 遮挡/可见性。

| 观测点 | 遮挡（occluded） | 隐藏（hidden，SW_HIDE） |
| --- | --- | --- |
| `DFH::CopyFromCompositingSurfaceInternal` | 到达，`can_copy=1 evictor_visible=1 has_surface=1 dip=1084x705 timeout_ms=0` | 到达，同左 |
| `RequestCopyOfOutput` 入队 | 发生 | 发生 |
| `Surface::TakeCopyOutputRequestsFromClient`（16s 内） | **0 次** | **13 次**（该 surface），全部聚合共 46 次 |
| 请求入队 → 取出 | **从未** | **12 ms** |
| 请求入队 → PNG | **超时（16014 ms）** | **78 ms** |
| `DisplayScheduler::AttemptDrawAndSwap`（16s 内） | **0 次** | 有（含 `should_draw=1 needs_draw=1`） |
| 页面 `document.visibilityState` | hidden（rAF 停，timer ~1Hz） | hidden（rAF 停，timer ~1Hz） |
| 帧新鲜度（PNG marker vs DOM 帧号） | —（无图） | **一致** |

结论：**遮挡窗口的浏览器 UI 合成器在整个捕获窗口内一帧都不产**（不是“产帧太慢”，也不是
“copy 结果为空”）。隐藏/最小化之所以成功，是因为捕获路径里的 `window_->Show()` /
合成器可见性切换在请求入队后约 12 ms 触发了一次真实的 display 帧。

### 1.2 遮挡时 `WasHidden(kOccluded)` 早退不是直接原因

`delegated_frame_host.cc:140-149` 的 `#if BUILDFLAG(IS_WIN)` 早退只影响
`frame_evictor_->SetVisible(false)`（surface 是否被 evict、是否为 tab 预览保留），
**不改变“谁来产帧”**。实测遮挡时 `evictor_visible=1 / has_surface=1`，与隐藏路径
（捕获时同样 `evictor_visible=1`）在 DFH 侧完全一致；差异发生在合成器侧。

### 1.3 未证实的最后一环（重要）

遮挡窗口的 `ui::Compositor` 为什么“完全冻结”，只做到以下边界，**没有拿到 cc 内部证据**：

- `compositor_->IsVisible() == 1`（cc host 可见），`size=1084x792`，`window_->IsVisible() == 1`；
- `window_->GetOcclusionState()` 返回 `UNKNOWN`（不是 OCCLUDED），所以不能用它做判定；
- 试图在 cc 内落文件日志时，`base::AppendToFile` 在启动任务阶段触发
  `base::DisallowUnresponsiveTasks` 的阻塞 IO DCHECK（`thread_restrictions.cc:62`）直接 FATAL，
  说明该插桩方式会改变时序/直接崩溃；`--trace-startup` 输出为 perfetto protobuf，未在本轮解析。

因此“cc 的 `SingleThreadProxy::commit_requested_` 粘住”或“`ShouldSubscribeToBeginFrames()` 条件不满足”
都只是**假设**，不作为结论。

---

## 2. 候选方案：实现 + 实测（全部失败，均已回退）

所有候选都只改一个点，增量构建 `ninja -C out/WinDev -j 6 chrome`，用同一 harness 实测。
“无日志配置”= 不传 `--enable-logging=stderr`（harness 默认，stderr 到 DEVNULL）。

| # | 候选 | 实现位置 | 结果 |
| --- | --- | --- | --- |
| A | 记录 pending capture，occluded 时让 frame evictor 走隐藏分支 | — | **未实施**：分析即否决。evict 只会让 DFH 层不再引用该 surface，`TakeCopyOutputRequestsFromClient` 更不会被调用；而 capture 时的 `WasShown/EmbedSurface` 又把 surface 重新引用回同一 LSI，不产生新帧 |
| B | 移除 `WasHidden` 的 Windows occluded 早退 | — | **未实施**：与 A 同一理由；且会全局破坏上游为 tab 预览保留 surface 的行为 |
| C1 | `DelegatedFrameHost::CopyFromCompositingSurface` 末尾加 `compositor_->ScheduleFullRedraw()` | `content/browser/renderer_host/delegated_frame_host.cc` | **失败（依赖时序）**：开 `--enable-logging=stderr` 的诊断构建下 3/3 通过（84 ms / 71 ms / 71 ms，`frame_ok=True`），**关掉日志立刻回到 16 s 超时**（`DS-attempt`/`TakeCopyOutputRequests` 均为 0 次）。说明该调用只是“碰巧”在合成器仍活跃时有效 |
| C2 | `RenderWidgetHostViewAura::CopyFromSurface` 里 `window_->SchedulePaintInRect(bounds)` | `content/browser/renderer_host/render_widget_host_view_aura.cc` | **失败**：仍 0 次聚合。`--repeat 3` 时渲染进程 FATAL（`layer_tree_view.cc:554 DCHECK callbacks.size() <= kMaxBufferSize (61 vs 60)`），但该崩溃在**未改动的基线构建**上同样可复现，不是本改动引入（见 §4.1） |
| C3 | C2 + `compositor->ScheduleFullRedraw()` | 同上 | **失败**：实测 `comp_vis=1 size=1084x792`，仍 0 次聚合 |
| C4 | C3 再叠加合成器可见性切换 `SetVisible(false); SetVisible(true);`（仅作用于该窗口的 `ui::Compositor`，不动原生窗口） | 同上 | **失败**：仍 0 次聚合，16 s 超时 |
| D | 仅在 `hidden_capturer_count_ > 0` 时把 occluded 视作可捕获 | — | 未实施：与 A/B 同一前提（需要一次真实聚合），且没有可用的“capturer count”下传通道到 DFH |
| E | `fromSurface=false` | 0 改动 | 已知返回旧帧（docs 已记录），**不能作为验收通过**，只作对照 |

反证（说明 harness 与插桩本身没问题）：
遮挡场景加 `--disable-features=CalculateNativeWinOcclusion` → **PASS 63 ms**，
`TakeCopyOutputRequestsFromClient` 记录 **1011 次**，`frame_ok=True`，`vis=visible`。

---

## 3. 回归（当前基线构建，未做任何改动）

命令（全部 `D:\c\src\out\WinDev\chrome.exe`）：

```sh
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario hidden --windows 1 --repeat 3 --screenshot-timeout 8
```

| 场景 | 结果 | 明细 |
| --- | --- | --- |
| hidden 1×3 | **PASS 3/3** | plain 156 / 2108 / 2114 ms，bump 3973 / 2117 / 2116 ms，`frame_ok=True`×3，`vis=hidden`，`raf+0`，`timer+1` |
| minimized 1×2 | **PASS 2/2** | plain 153 / 2799 ms，bump 3991 / 2122 ms，`frame_ok=True`×2，`vis=hidden`，`raf+0` |
| normal 1×1 | BLOCKED（**环境限制**） | `vis=hidden` + 16 s 超时：本会话无法把测试窗口提到前台（`SetForegroundWindow` 被拒），窗口实际仍被遮挡。加 `--disable-features=CalculateNativeWinOcclusion` 后同一条命令 `vis=visible`、**PASS 103 ms**，`raf+72` |
| occluded 1×1（基线复现） | BLOCKED | plain 16014 ms 超时、bump 16005 ms 超时，`vis=hidden`，`raf+0`，`timer+1` |
| occluded 1×2（基线复现） | BLOCKED | #0 plain 16000 ms / bump 16010 ms；#1 plain 16008 ms / bump 13286 ms；均超时、无图 |
| occluded 1×3（基线复现） | **渲染进程崩溃** | 前两轮超时后，渲染进程 FATAL `layer_tree_view.cc:554 (61 vs 60)`，浏览器进程 0xC0000005、CDP 连接被重置（详见 §4.1） |

结论：hidden / minimized 主路径未受影响；遮挡路径维持原状（BLOCKED），且**基线本身**在多次遮挡截图后会崩溃。

---

## 4. 为什么没有产出 `027-*.patch`

- 唯一“能过”的改动（C1）只在开启 stderr 日志时通过，关日志必挂 —— 属于时序巧合，不可交付；
- C2/C3/C4 均无效，且 C2 会触发渲染进程 DCHECK 崩溃（presentation 回调缓冲 61>60）；
- 依 AGENTS.md“fail closed、不得先跑起来再说”，**未生成 patch、未改 `chromium/patch-manifest.json`**，
  `git status` 中 `content/ cc/ components/viz/ ui/aura/` 均无本地改动（与 26 个既有 patch 的基线一致）。

### 4.1 附带发现：遮挡窗口多次截图会让渲染进程崩溃（基线既有，P0）

未做任何改动的基线构建上即可复现：

```sh
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario occluded --windows 1 --repeat 3 --screenshot-timeout 8
# → 前两轮超时；随后渲染进程 FATAL，浏览器进程退出码 0xC0000005
```

```
FATAL:third_party\blink\renderer\platform\widget\compositing\layer_tree_view.cc:554]
DCHECK failed: callbacks.size() <= cc::PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60).
```

机理：每次 `Page.captureScreenshot` 都会在渲染进程里
`LayerTreeHost()->RequestPresentationTimeForNextFrame(cb)`，回调只在帧**被呈现**时消费；
遮挡窗口一帧都不呈现，回调按帧 token 累积到 60 上限后 DCHECK（本构建 `DCHECK_ALWAYS_ON=1`）直接 abort。
`--repeat 2` 仍在阈值内（正常超时），`--repeat 3` 越界。
**这不是本轮改动引入的**，产品侧对遮挡窗口反复截图同样会命中，建议单独开一条 P0 处理
（例如内核侧限制未呈现回调数量，或产品侧对同一遮挡窗口做截图节流/失败后退避）。

---

## 5. 后续建议（按可行性排序）

1. **viz 层“按需一帧”**（最有希望）：给 `viz::mojom::DisplayPrivate` 增加一个
   “强制一帧并 needs_draw”的方法（等价于 `Display::scheduler_->SetNeedsOneBeginFrame(args, needs_draw=true)`
   + `ForceImmediateDrawAndSwapIfPossible()`），由 `DelegatedFrameHost::CopyFromCompositingSurface`
   在 surface 未被绘制时调用。这样不依赖 cc 的 BeginFrame 调度，也不改变窗口/页面可见性。
2. **让 `RequestCopyOfOutput` 的 `SurfaceModified(has_damage=true)` 能唤醒嵌入 display**：
   现状 `DisplayDamageTracker::OnSurfaceDamaged()` 只在
   `aggregator_->CheckForDisplayDamage(surface_id)` 为真时才认为 display 受损；需要确认
   delegated surface 在遮挡后是否仍被算作“对本 display 有贡献”。若否，补上注册即可。
3. **确认 `--in-process-gpu` 是否放大问题**：harness 固定带该开关（测试用）。本机该构建
   **不带 `--in-process-gpu` 无法启动**（`chrome.exe` 立即退出，退出码 3，无日志），
   因此无法在本轮排除它；若产品路径（不带该开关）表现不同，优先级会大幅下降。
4. 产品层继续使用 `fromSurface=false` + `possiblyStale` 兜底（`docs/windows-background-ops-p0-2026-10-28.md` §2），
   并明确“被遮挡窗口的截图不保证实时”。

---

## 6. 新增风险 / 未覆盖

1. **（基线既有，P0）遮挡窗口反复截图会累积 presentation 回调**：见 §4.1。遮挡窗口
   `--repeat 3` 即触发渲染进程 DCHECK 崩溃（`layer_tree_view.cc:554`，61>60），
   与任何候选无关；任何“让遮挡窗口产帧但不呈现”的方案都会放大该风险。
2. **`RequestCopyOfOutput` 的 timeout 为 0**（`DelegatedFrameHost` 传 `base::TimeDelta()`），
   即内核侧永不超时；挂起完全依赖调用方（CDP/产品层）超时。若上层忘记超时会永久挂住。
3. **`window_->GetOcclusionState()` 在捕获时返回 `UNKNOWN`**，不能用它做“是否遮挡”的判定；
   可用的信号只有 `WebContentsImpl::visibility_`（捕获期间仍为 OCCLUDED）或 DFH 记录过的
   `HiddenCause`。
4. 未验证：tab 悬停预览是否受任何候选影响（因为没有任何候选进入候选交付状态）；
   未验证 3/8 窗口规模的遮挡场景（环境无法稳定构造多个被遮挡窗口）；
   未验证真实用户桌面（非自托管会话）下的遮挡判定时序。
