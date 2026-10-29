# 遮挡窗口取新帧截图：根因、最终方案与实测（2026-10-28）

范围：让“窗口被完全遮挡”时 `Page.captureScreenshot`（`fromSurface=true`）取到**最新帧**。
状态：**SOLVED**。内核 patch `027-viz-activate-pending-root-frame-for-copy.patch` 已入库清单，
未 commit。遮挡场景 `--repeat 3` 连跑 3 次、`--repeat 5` 压测、3 窗口混合场景全部 PASS；
`hidden` / `minimized` / `normal` / `inactive` 回归 PASS；基线既有的 61>60 崩溃不再复现。

关联：[windows-background-render-2026-10-28.md](windows-background-render-2026-10-28.md)、
[windows-background-ops-p0-2026-10-28.md](windows-background-ops-p0-2026-10-28.md)。

---

## 0. 结论摘要

- **补丁**：`chromium/patches/027-viz-activate-pending-root-frame-for-copy.patch`
  （4 个文件，+48 行，全部在 `components/viz/service/frame_sinks/`）。
- **机制**：viz 收到 `CopyOutputRequest` 后，若嵌入该 surface 的 display 的**根 surface 仍有一个
  pending frame**，就按“deadline 到期”的同一语义强制激活它（`ActivatePendingFrameForDeadline()`）。
  根 surface 激活 → display 正常收到 damage → 调度器订阅 BeginFrame → 真实合成一帧 →
  聚合时 `Surface::TakeCopyOutputRequestsFromClient()` 取出 copy 请求并执行。
- **不改变**窗口可见性、z-order、前台、页面可见性；页面仍 `hidden`、rAF 停、timer ~1 Hz。
- **实测**：遮挡 1×1 `--repeat 3` × 3 次 = **9/9 PASS**（plain 48–153 ms，bump 41–77 ms，
  `frame_ok=True`）；`--repeat 5` = 5/5 PASS（10 次截图，无崩溃）；遮挡 3 窗口 = 3/3 PASS。
- **回归**：hidden 3/3、minimized 2/2、normal 1/1、inactive 2/2，全部 `frame_ok=True`。
- **61>60 崩溃**：补丁后 `--repeat 5` 退出码 0，无 `layer_tree_view.cc:554` FATAL。
  原因：每次 forced-redraw 的帧现在真的被 display 呈现，presentation 回调被消费，不再累积。

---

## 1. 根因（两层，均有运行时证据）

### 1.1 第一层（上一轮已证实）

```
Page.captureScreenshot(fromSurface=true)
  → RequestForceRedraw → renderer 提交新 CompositorFrame
  → DFH::CopyFromCompositingSurfaceInternal
  → HostFrameSinkManager::RequestCopyOfOutput(surface_id, timeout=0)
  → CompositorFrameSinkSupport::RequestCopyOfOutput  // 仅入队
  → 只有一次真实的 display 聚合（SurfaceAggregator::PrewalkSurface）
    才会调用 Surface::TakeCopyOutputRequestsFromClient() 取走请求
```

遮挡窗口的浏览器 UI display 在 16 s 内 **0 次** `AttemptDrawAndSwap`、**0 次**
`TakeCopyOutputRequestsFromClient`；SW_HIDE 下同样代码是 13 次/正常绘制。上一轮的候选
A/B/C1–C4/D 都试图“让 display 画一帧”，但都失败；本轮找到了它们失败的共同原因。

### 1.2 第二层（本轮新增，真正的第一断点）

插桩（临时，已全部移除）显示：遮挡时浏览器窗口的 UI compositor **根 surface 从头到尾没有
active frame**，display 处于 `root_frame_missing=1`，因此：

- `DisplayScheduler::ShouldDraw()` 恒为 false → `StartObservingBeginFrames()` 从未调用
  （日志 0 次），display 连 BeginFrame 都不订阅；
- 根 surface 没有 active frame，`SurfaceAggregator::Aggregate()` 不会发生；
- `DisplayDamageTracker::CheckForDisplayDamage(子 surface)` 恒为 **0**，因为
  `damage_ranges_` 只在聚合时填充，而这个 display 从未聚合过；
- 于是 copy 请求的 `SurfaceModified(has_damage=true)` 也无法唤醒 display。

关键日志（遮挡，未修复，`D:\c\logs\vzdbg-occ7.log`）：

```
VZDBG submit sink=FrameSinkId(0, 2) is_root=1 lsi=LocalSurfaceId(2, 1, ...)
      deadline=12000 deps=2 refs=2          # UI 根 surface 提交一次后永久 pending
VZDBG surface-pending sid=... deadline_in_frames=12000 default_lb=1
      interval_us=16666 sys_default=4 per_dep=1
VZDBG surface-deadline-check ... passed=0    # 每次 BeginFrame 都检查，始终不通过
VZDBG tracker-damaged surface=(7, 3)... has_damage=1 check=0
VZDBG dfh-copy-request ... timeout_ms=0
VZDBG copy-enqueue last=(7, 3)... count_before=0
VZDBG sched-start-observe                    # 0 次
VZDBG sched-obf / sched-attempt / aggregate  # 0 次
```

根 surface 的 pending frame 有 **2 个 surface 同步依赖**（工具栏 WebUI + 页面内容），
其 per-dependency deadline 是 **12000 帧**（60 Hz 下约 200 秒）。页面内容 surface
`(7,3)` 每秒都在正常提交/激活，但工具栏 WebUI surface 在整个遮挡期间**一帧都不提交**
（它的 renderer 同样被遮挡），所以根 frame 永远等不到依赖，也不会在 14 s 内到达 deadline。

### 1.3 为什么 deadline 是 12000 帧：测试构建的 field trial 配置

`--enable-logging --v=1` 可见 `Applying FieldTrialTestingConfig`。该配置中的
`WebUIReloadButtonStudy / EnabledWithSurfaceSync_20260902` 打开了：

```
InitialWebUISurfaceSync (deadline_in_frames=12000)
PerDependencyDeadlines
BypassOutdatedSurfaceActivation
```

调用链：

```
WebUIToolbarWebView::ApplyInitialSurfaceSyncDeadline()
  → SetSurfaceSyncDeadline(12000)
  → RenderWidgetHostViewAura::SetForceSpecifiedDeadline(12000)
  → DelegatedFrameHost::SetForceSpecifiedDeadline(12000)
  → DFH::EmbedSurface(..., cc::DeadlinePolicy::UseSpecifiedDeadline(12000))
  → 工具栏 WebUI 的 cc::SurfaceLayer deadline = 12000
  → 窗口 UI 根 frame 的 metadata.deadline = max(各 SurfaceLayer) = 12000
```

正常情况下，工具栏的 `DidFirstVisuallyNonEmptyPaint()` 会调用
`SetSurfaceSyncDeadline(std::nullopt)` 把 deadline 复位；但窗口整体被遮挡时工具栏 renderer
不产帧，复位永远不会发生。**这就是“遮挡”与“隐藏/最小化”真正的分水岭**（对 hidden/minimized
PASS 的推断，未对 hidden 单独插桩）：SW_HIDE/最小化时捕获路径的 `window_->Show()` 会真正把
窗口变为可见，工具栏 renderer 因而可以产帧，根 frame 的依赖得以解析。

对照实验（未打补丁的构建，`--disable-field-trial-config`）：

```
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario occluded --windows 1 --repeat 1 --screenshot-timeout 6 \
  --extra-switch=--disable-field-trial-config
→ plain=OK 85ms / bump=OK 84ms；PNG marker 16 vs DOM 11
```

即：没有该测试配置时遮挡截图本来就能出帧；harness 报的 STALE 是“PNG 比 probe 更新 5 帧”
（`__bump(5)` 的计数在 probe 之后才落地）造成的 ±4 窗口边界误判，不是旧帧。
生产构建默认关闭 `InitialWebUISurfaceSync`，所以这是**测试配置触发的真实内核缺陷形态**：
只要嵌入 display 的根 frame 因任意 surface-sync 依赖长时间 pending，copy 请求就会挂到超时。

---

## 2. 最终方案（patch 027）

### 2.1 改动点

`components/viz/service/frame_sinks/frame_sink_manager_impl.cc/.h`

```cpp
void FrameSinkManagerImpl::RequestCopyOfOutput(...) {
  ...
  // exact-surface 分支与普通入队分支在返回前都会调用：
  ActivatePendingRootFrameForCopy(surface_id);
}

void FrameSinkManagerImpl::ActivatePendingRootFrameForCopy(
    const SurfaceId& surface_id) {
  const FrameSinkId root_frame_sink_id =
      GetOldestRootCompositorFrameSinkId(surface_id.frame_sink_id());
  if (!root_frame_sink_id.is_valid())
    return;
  RootCompositorFrameSinkImpl* root_frame_sink =
      base::FindPtrOrNull(root_sink_map_, root_frame_sink_id);
  if (!root_frame_sink)
    return;
  root_frame_sink->ActivatePendingRootFrameIfNeeded();
}
```

`components/viz/service/frame_sinks/root_compositor_frame_sink_impl.cc/.h`

```cpp
void RootCompositorFrameSinkImpl::ActivatePendingRootFrameIfNeeded() {
  const SurfaceId& root_surface_id = display_->CurrentSurfaceId();
  if (!root_surface_id.is_valid())
    return;
  Surface* root_surface = support_->frame_sink_manager()
                              ->surface_manager()
                              ->GetSurfaceForId(root_surface_id);
  if (!root_surface || !root_surface->HasPendingFrame())
    return;
  root_surface->ActivatePendingFrameForDeadline();
}
```

### 2.2 为什么放在 viz、放在 `RequestCopyOfOutput`

1. **时序安全**：请求已经同步入队之后才触发激活，不依赖 `ui::Compositor` →
   `DisplayPrivate` 与 `FrameSinkManager` 两个 Mojo 接口之间的跨 pipe 顺序。
2. **不需要新 mojom**，也不改 `DisplayPrivate` / `ui::Compositor` 可见性。
3. **语义与 deadline 到期完全一致**：`ActivatePendingFrameForDeadline()` 会清空未解析的
   `activation_dependencies_`，用 fallback 内容激活——这正是 viz 在 deadline 到期时的既有行为，
   只是把“等 12000 帧”提前到“现在要截图”。
4. **根 surface 选择**：第一版用 `support_->last_activated_surface_id()` 失败（本场景根 surface
   从未激活，值无效）；改用 `display_->CurrentSurfaceId()`（由 `SetNewRootSurface` 维护）后命中。
5. **目标 surface 不受影响**：强制激活的是“嵌入方的根 frame”，被截图的页面 surface 仍是它
   自己最新的 active frame；如果目标 surface 还没激活，copy 请求继续留在 support 队列，
   等它激活后的下一次聚合再取走。

### 2.3 修复后的实际链路（插桩构建，`D:\c\logs\vzdbg-fix2.log`）

```
VZDBG fix-activate-root root=SurfaceId(FrameSinkId(0, 2), ...) found=1 pending=1
VZDBG sched-start-observe shoulddraw=1 needs_draw=1 visible=1 lost=0 root_missing=0
VZDBG sched-obf n=1 valid=1 inside_deadline=0
VZDBG sched-attempt n=1 shoulddraw=1 ... args_valid=1
VZDBG aggregate n=1 root=SurfaceId(FrameSinkId(0, 2), ...)
VZDBG take-copy-from-client surface=SurfaceId(FrameSinkId(7, 7), ...)
VZDBG take-copy-taken
...
VZDBG sched-attempt n=15 shoulddraw=0 needs_draw=0 ...
VZDBG sched-stop-observe
```

一次 1×1 采样（2 次截图）的计数：`fix-activate-root` 2 次（第一次 `pending=1` 并激活；
第二次 `pending=0`，已不需要再激活）、`aggregate` 10 次、`take-copy-from-client` 19 次
（含根 surface 的例行取队列）、`take-copy-taken` 2 次、`AttemptDrawAndSwap` 16 次，
随后 `needs_draw=0`、`StopObservingBeginFrames`，display 回到空闲。
即：**按需一帧（约 15 帧的收敛窗口，~250–350 ms）后恢复后台节流**，不是永久全速渲染。

---

## 3. 原始数据（最终构建，无插桩、无 `--enable-logging`）

### 3.1 遮挡 1 窗口 × `--repeat 3`，跑 3 次

命令：

```sh
python tests/tools/background-render-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario occluded --windows 1 --repeat 3 --screenshot-timeout 6 \
  --result-json D:/c/logs/fix-occ-3a.json
```

| run | sample | vis | raf | plain ms | PNG marker | DOM expected | bump ms | bump marker | bump expected | frame_ok |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3a | p0#0 | hidden | +0 | 149 | `[4,0,255]` | `[0,0,255]` | 48 | `[16,0,255]` | `[13,0,255]` | True |
| 3a | p0#1 | hidden | +0 | 67 | `[21,0,255]` | `[17,0,255]` | 60 | `[31,0,255]` | `[27,0,255]` | True |
| 3a | p0#2 | hidden | +0 | 48 | `[35,0,255]` | `[32,0,255]` | 71 | `[45,0,255]` | `[41,0,255]` | True |
| 3b | p0#0 | hidden | +0 | 153 | `[4,0,255]` | `[0,0,255]` | 63 | `[16,0,255]` | `[12,0,255]` | True |
| 3b | p0#1 | hidden | +0 | 68 | `[21,0,255]` | `[17,0,255]` | 42 | `[30,0,255]` | `[27,0,255]` | True |
| 3b | p0#2 | hidden | +0 | 54 | `[34,0,255]` | `[31,0,255]` | 53 | `[43,0,255]` | `[40,0,255]` | True |
| 3c | p0#0 | hidden | +0 | 129 | `[4,0,255]` | `[0,0,255]` | 64 | `[16,0,255]` | `[12,0,255]` | True |
| 3c | p0#1 | hidden | +0 | 48 | `[20,0,255]` | `[17,0,255]` | 41 | `[29,0,255]` | `[26,0,255]` | True |
| 3c | p0#2 | hidden | +0 | 72 | `[34,0,255]` | `[30,0,255]` | 77 | `[44,0,255]` | `[40,0,255]` | True |

3 次均 `VERDICT PASS`，`fresh_after_repaint=3`，`timeouts=0`，`black=0`。
marker 低字节比 probe 早读的 DOM 值大 3–4（`__bump(5)` 的计数在 probe 后落地），
但 bump 后的 PNG 相对 plain 明确前进了 9–12 帧，证明不是旧帧。

### 3.2 遮挡压测与多窗口

| 场景 | 结果 | 明细（plain / bump ms，全部 frame_ok=True） |
| --- | --- | --- |
| occluded 1×1 `--repeat 5`（10 次截图） | **PASS 5/5，exit 0** | 138/62、50/59、67/41、54/68、71/70 |
| occluded 3 窗口 `--repeat 1` | **PASS 3/3** | p0 145/61、p1 68/66、p2 76/66 |

`--repeat 5` 是 61>60 崩溃的直接回归：基线在 `--repeat 3` 即 FATAL
（`layer_tree_view.cc:554 DCHECK callbacks.size() <= kMaxBufferSize (61 vs 60)`），
补丁后 10 次截图无任何 FATAL，浏览器进程正常退出。

### 3.3 回归

| 场景 | 结果 | 明细（plain / bump ms，全部 frame_ok=True） |
| --- | --- | --- |
| hidden 1×3 | PASS 3/3 | 164/2966、3148/3969、3144/2972 |
| minimized 1×2 | PASS 2/2 | 148/3154、3849/3126 |
| normal 1×1 | PASS 1/1 | 153/77（本会话仍无法真正前置，`vis=hidden`，但截图正常） |
| inactive 1×2 | PASS 2/2 | 94/2850、3096/1120 |

hidden/minimized 的 3 s 级耗时与基线同量级（捕获路径的 1/6 s 延迟 + 可见性切换），
无新增超时。

### 3.4 z-order / 前台

在 harness 副本（`D:\c\logs\bgcheck-zorder.py`，仅测试用，未入库）中，每次截图前后采样
`GetWindow(hwnd, GW_HWNDPREV)` 与 `GetForegroundWindow()`：

```
z={'above': 38079762, 'foreground': 42077964} -> 同值   （3 次采样全部相同）
```

窗口矩形 `[40,40,1100,800]`、`visible=True`、`iconic=False` 前后不变；
页面 `visibilityState=hidden`、`raf+0`、`timer+1~2`。补丁本身只改 viz，不触碰
aura/窗口/可见性/焦点代码。

---

## 4. 上一轮候选为什么全部失败（补充解释）

上一轮的 C1–C4 都只做了“让 display 重画/订阅 BeginFrame”，但当时根 surface 仍是 pending
（`root_frame_missing=1`），`ShouldDraw()` 恒 false，所以 `AttemptDrawAndSwap` 仍为 0。
C1 在开 stderr 日志时“偶然通过”，最可能的解释是日志拖慢了启动/遮挡判定，让工具栏在窗口被
遮挡前完成了首帧并复位 deadline，根 frame 得以激活——属于时序巧合，不是方案（未单独插桩证实）。
A/B/D（evictor/隐藏分支）同样不触及“根 frame pending”这一层。

---

## 5. 风险与未覆盖

1. **fallback 语义**：强制激活会让根 frame 用 fallback 内容补齐未就绪的嵌入 surface。
   截图目标（页面内容）通常是 active 的最新帧；但浏览器 UI chrome（如工具栏）可能取到旧/空
   fallback。只在“copy 请求到达且根 frame pending”时发生，属于可接受的降级。
2. **额外帧**：每次 copy 请求会带来一次约 15 帧的收敛窗口（~250–350 ms），随后
   `needs_draw=0` 并停止订阅 BeginFrame。根 frame 激活后，display 恢复为标准行为：
   子 surface 的新帧会正常造成 damage 并触发一次绘制（本 fixture 的页面 timer 约 1 Hz，
   因此遮挡窗口此后约 1 fps 合成），这不是全速渲染；空闲页面不会产生帧。
3. **多父/多 display**：`GetOldestRootCompositorFrameSinkId()` 取最老的父根；同一 surface 被多个
   根嵌入（tab 拖拽等）时只会唤醒最老的根，其他根的 copy 请求若也 pending 可能仍需各自唤醒。
   本轮未构造该场景。
4. **平台**：只在 Windows `out/WinDev`（`--in-process-gpu`）验证；macOS/Linux 未跑。
5. **生产配置**：生产构建默认关闭 `InitialWebUISurfaceSync`，本 bug 的触发条件弱得多；
   补丁在该配置下是惰性的（根 frame 通常已 active），未观察到行为变化。
6. **61>60 崩溃**：本次修复让 forced-redraw 帧真正被呈现，回调被消费，因而不再累积；
   没有额外增加“未呈现回调上限”的防护。若未来出现别的“提交了但永不呈现”的路径，
   该 DCHECK 仍可能被触发，建议产品侧继续保留截图超时/退避。
7. 未验证：tab 悬停预览、真实用户桌面（非自托管会话）、遮挡状态下长时间（分钟级）反复截图。

---

## 6. 复现与验证命令

```sh
# 主验收（遮挡，3 次采样）
python tests/tools/background-render-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario occluded --windows 1 --repeat 3 --screenshot-timeout 6 \
  --result-json D:/c/logs/fix-occ-3a.json

# 崩溃回归（10 次截图）
python tests/tools/background-render-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario occluded --windows 1 --repeat 5 --screenshot-timeout 6

# 回归
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario hidden --windows 1 --repeat 3 --screenshot-timeout 8
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario minimized --windows 1 --repeat 2 --screenshot-timeout 8
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario normal --windows 1 --repeat 1 --screenshot-timeout 8

# 根因对照（未打补丁时应 PASS；本机为测试 field trial 触发）
python tests/tools/background-render-cdp-check.py --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario occluded --windows 1 --repeat 1 --screenshot-timeout 6 \
  --extra-switch=--disable-field-trial-config
```

原始 JSON：`D:\c\logs\fix-occ-3a.json`、`fix-occ-3b.json`、`fix-occ-3c.json`、
`fix-occ-5.json`、`fix-occ-3w.json`、`fix-hidden-3.json`、`fix-min-2.json`、
`fix-normal-1.json`、`fix-inactive-2.json`、`fix-occ-zorder.json`；
诊断日志：`vzdbg-occ7.log`（未修复）、`vzdbg-fix2.log`（修复后，含插桩）。

---

## 7. 主代理独立复测（2026-10-28 晚）

A2 交付后，主代理用同一 harness 独立复跑（最终构建，无插桩、无 `--enable-logging`）：

- `apply-patches.py --check`：27/27 PASS，`STATE applied`；`ninja -n chrome`：no work。
- 遮挡 1 窗口 × 3 次：150/68、65/67、54/71 ms，`frame_ok=True` ×3，`vis=hidden`，`raf+0`。
- 遮挡 1 窗口 × 5 次（10 张截图）：全部 `frame_ok=True`，无 `layer_tree_view.cc:554` FATAL，PASS。
- hidden / minimized / inactive / normal 各 1 窗口 × 2 次：全部 PASS，`frame_ok=True`。
- 说明：normal 场景在本机仍报 `vis=hidden`（窗口未被前台激活），实际走的是遮挡路径；
  正常可见路径由 cursor 浏览器测试与 native E2E 覆盖。
- 结论：遮挡新帧与连续截图崩溃在最终构建上均可独立复现为 PASS。

### 7.1 过程事故披露（必须记录）

诊断早期执行过一次 `taskkill /IM chrome.exe /F`。该命令会终止机器上所有 `chrome.exe`，
可能包括用户正在使用的 Chrome（Program Files 版本）。当前机器上没有 `chrome.exe` 进程；
无法确认用户窗口是否由该命令终止，也未再使用进程级 kill。后续实验只允许终止自己启动的
进程树（按 PID/命令行匹配），禁止 `taskkill /IM`。

---

## 8. patch 027 主代理代码审核（提交前）

审核范围：`components/viz/service/frame_sinks/frame_sink_manager_impl.{cc,h}`、
`root_compositor_frame_sink_impl.{cc,h}`，共 4 文件 +48 行。

- **调用路径**：只在 `FrameSinkManagerImpl::RequestCopyOfOutput` 内触发；正常渲染、
  非截图路径不经过该函数。没有 pending root frame 时 `HasPendingFrame()` 直接返回，
  是安全 no-op。
- **依赖语义**：`Surface::ActivatePendingFrameForDeadline()` 会清空
  `activation_dependencies_` / `view_transition_dependencies_`，然后按 deadline fallback
  语义激活 pending frame。这是 Chromium 已有的 deadline 到期行为，只是提前到“有 copy
  请求”时触发；pending frame 的 `referenced_surfaces` 不会被删除，后续新 frame 会重新
  建立依赖，因此不是永久清除依赖，但当前帧对未就绪依赖会使用 fallback（例如遮挡窗口的
  工具栏可能取旧内容）。目标页面 surface 在截图场景下已是 active 帧，copy 请求由它取出。
- **iframe / 多层 Surface**：根 frame 激活后由 `SurfaceAggregator` 聚合所有引用 surface；
  目标 iframe 的 surface 若已 active，则 copy 正常；缺失依赖走 fallback。产品级 E2E
  必须覆盖 iframe/Canvas/WebGL。
- **tab 预览 / 资源生命周期**：走正常 `Surface::ActivateFrame` 路径——旧 copy request 会
  重新挂到新 root pass，上一帧资源按原逻辑 unref；没有额外 evict，tab 预览仍使用 active
  frame。强制激活只影响“何时激活”，不改变激活后的资源管理。
- **多父 root**：`GetOldestRootCompositorFrameSinkId` 只唤醒最老父根；tab 拖拽等多父场景
  未覆盖，记录为残余风险。
- **结论**：未发现阻塞性缺陷；依赖提前清除是 deadline 语义的已知代价，且只发生在 copy
  请求路径。允许在进程安全整改通过后提交 patch 027；产品级 E2E 通过后才把遮挡截图 P0
  标为 PASS。
