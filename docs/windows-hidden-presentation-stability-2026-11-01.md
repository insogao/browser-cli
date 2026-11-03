# Windows 长驻后台/隐藏页面稳定性：presentation-callback 61>60 根因与复现

日期：2026-11-01（运行时间戳见 `D:\c\logs\hs-*`）
范围：managed 后台窗口长期驻留时，页面持续重绘导致 renderer 因
`layer_tree_view.cc:554` DCHECK（`PresentationTimeCallbackBuffer` 61 vs. 60）崩溃。
关联：[windows-background-render-2026-10-28.md](windows-background-render-2026-10-28.md)、
[windows-occluded-capture-2026-10-28.md](windows-occluded-capture-2026-10-28.md)、
[windows-background-ops-p0-2026-10-28.md](windows-background-ops-p0-2026-10-28.md)、
[windows-regression-2026-10-31.md](windows-regression-2026-10-31.md)。

---

## 0. 结论摘要

- **REPRODUCED（真实产品形态）**：用 `D:\work\wt\bp-a-r1r2\dist` 的真实
  BrowserPilot popup（`popup/index.html`，扩展 ID `nnollghpaggbcdkkgoieneffnlijinio`）在
  managed-default 窗口中 1 Hz 刷新，**59.842 s** 触发精确 DCHECK：

  ```text
  [pid:tid:...:FATAL:third_party\blink\renderer\platform\widget\compositing\layer_tree_view.cc:554]
  DCHECK failed: callbacks.size() <= cc::PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60).
  ```

  renderer 死亡；浏览器进程、DevTools 端点、其它窗口仍存活。这与 2026-10-31 R2 首故障
  （长期驻留 popup 在 managed-hidden 窗口持续重绘）一致。**回收 popup 只是 harness 规避，
  不是根因修复**。
- **根因**：managed 窗口的 `SWP_SHOWWINDOW` 在原生层被剥离，但 `WebContents`/renderer 仍是
  **逻辑可见**（`document.visibilityState=visible`、`hasFocus=true`、rAF/定时器运行）。渲染进程
  持续提交/激活 CompositorFrame，而 display 永不 present；每一帧注册的 presentation callback
  永不消费。viz 又因“未绘制帧过多”把 BeginFrame 节流到约 1 次/秒，所以 60 条回调需要约
  60–70 s 才攒满。
- **窗口状态区分**：危险组合是 **“模型可见 + 原生不呈现”**，不是“原生可见”。对从未 show 的
  managed 窗口调用 `SW_HIDE`/`SW_MINIMIZE` **不会改变模型可见性**，因此**仍然复现**；真正的
  模型隐藏对照（先 show 再 hide）90 s/180 s 稳定。
- **未修复**：本轮只做根因定位、触发矩阵、harness 与修复提案；**没有**修改共享 Chromium 源码，
  也没有构建/验证任何 fix。按任务口径：**不宣称 solved**。
- **修复方向**：
  1. **产品层根修（推荐先做）**：managed 窗口在原生呈现被拒绝时，把 `WebContents` 也置为
     hidden（模型与实际一致），从源头停止产生“永不 present”的帧；截图仍在按需重绘路径可用
     （`normal-hidden` 180 s 后截图仍 fresh）。
  2. **Chromium presentation-chain 硬化**：viz 对确定不会再 present 的 undrawn frame 发送
     failed presentation feedback；renderer 在 failed feedback 时消费/丢弃对应回调，并加
     buffer 上限防护。这样即使保留“模型可见但不呈现”的语义也不会杀 renderer。
  3. **只算 workaround**：每 case 回收 popup、周期截图（实测 70 s 不崩但属掩盖）、
     `Page.setWebLifecycleState(frozen)`、把 popup 生命周期压到 60 s 内。**都不是根修**。

---

## 1. 构建、模式与卫生

- 二进制：`D:\c\src\out\WinDev\chrome.exe`，Chromium `156.0.8067.0-devel`，
  `disable_fieldtrial_testing_config=true`，patch 001–033（含 027）。
- 所有运行使用独立 `tempfile.mkdtemp` profile；**无** `--no-sandbox`；**无**真实用户 profile/
  钥匙串；进程只通过 `win_job`（Job Object，`KILL_ON_JOB_CLOSE`）回收；**没有**任何
  `taskkill /IM`、`pkill`、`killall` 或按镜像名清理。
- 两种模式严格分开：
  - `--mode release-like`：只带产品/内容所需的 flag（managed、extension、`--no-startup-window`、
    remote debugging），不写日志、不 trace；崩溃只靠 CDP 事件。
  - `--mode diagnostic`：额外 `--enable-logging --log-file=<evidence>/chrome.log [--v=1]`，
    可选 `--trace-categories viz,cc,gpu`。诊断 flag 会在结果 `config.flags` 中逐条记录。
- 本机测试会话没有可用的“真正可见且未被遮挡”的前台窗口：`normal-visible` 三个窗口
  `IsWindowVisible=true`，但 Chromium 判定为 occluded、`visibilityState=hidden`。因此
  **visible 基线在本机只到“原生可见 + 模型被遮挡”**；没有用 `SetForegroundWindow`/topmost
  等抢前台手段去伪造 visible。

---

## 2. Harness 与内容

新增：

- `tests/tools/hidden-stability-cdp-check.py`
- `tests/fixtures/hidden-stability/dynamic.html`
- `tests/fixtures/hidden-stability/child.html`
- `tests/tools/extensions/browser-cli-hidden-stability/{manifest.json,page.html,page.js,sw.js}`

内容类型（每窗口一个）：

| kind | 行为 | 目的 |
| --- | --- | --- |
| `text` | 每 1 s 更新可见文本 + marker 色块 | 模拟 popup `refresh()`（R2 形态） |
| `raf` | `requestAnimationFrame` 每帧更新可见文本/画布 | 连续动画页面 |
| `canvas` | 2D canvas 每帧动画 + 文本计数 | Canvas 动画 |
| `webgl` | WebGL 每帧动画 + 文本计数 | WebGL 动画 |
| `extension` | MV3 扩展页每 1 s `chrome.runtime.sendMessage` 并重绘 | 扩展页面（真实 popup 用 `--extension-page popup/index.html`） |
| `iframe` | 父页跨站嵌入 `http://localhost:<port>/child`，子帧每次 tick 重绘并 postMessage | OOPIF |
| `static` | 只做初始绘制，无定时器 | 对照 |
| `no-paint` | 定时器只改 JS 变量，不改 DOM/画布 | 证明“定时器本身”不触发 |

场景：

| scenario | 原生操作 | 说明 |
| --- | --- | --- |
| `managed-default` | 无（managed flag 让 show 被策略拒绝） | **模型可见 + 原生隐藏**（危险态） |
| `managed-hidden` | `ShowWindow(SW_HIDE)` | 窗口从未 show，**模型仍可见**；不是模型隐藏对照 |
| `managed-minimized` | `ShowWindow(SW_MINIMIZE)` | `iconic=true`，**模型仍可见** |
| `normal-visible` | 非 managed，`SetWindowPos(SWP_SHOWWINDOW\|SWP_NOACTIVATE)` | 本机被桌面遮挡，模型 hidden |
| `normal-hidden` | 非 managed，先创建可见再 `SW_HIDE` | **真模型隐藏**对照 |
| `normal-minimized` | 非 managed，先可见再 `SW_MINIMIZE` | 模型隐藏对照 |
| `normal-occluded` | 非 managed，三窗同 rect 叠加 | 原生可见 + 模型 occluded |

测量：

- 每 `--sample-interval` 秒：页面 `__hsProbe()`（visibilityState/hasFocus/raf/tick/paint/
  markerRect）、native `IsWindowVisible/IsIconic/GetWindowRect/GW_HWNDPREV`、进程树
  （browser/gpu/renderer/utility 的 working set/private bytes）。
- 崩溃：`Target.targetCrashed`（browser session，auto-attach）、`Inspector.targetCrashed`
  （page session）、probe 连续超时、renderer 进程从进程树消失、以及 diagnostic log 的
  精确 DCHECK 行。
- 截图新鲜度：页面把 `paintCount` 编成 24×24 marker 色块（R=低字节，G=高字节，B=255）；
  `__hsBump(5)` 后立即 `Page.captureScreenshot(fromSurface=true)`，解码 PNG 比对 marker；
  同时要求 `distinct_colours>=8`、`dark_ratio<0.98`。**白图/单像素/旧帧都不算成功**。
- 不重启页面、不 reload、不 kill/reload 定时器；进程只在整轮结束时由 Job Object 回收。

---

## 3. 触发矩阵（实测）

判定格式：native = `hidden|visible|iconic`；model = `document.visibilityState`；
crash = FATAL 时间（log） / 首次崩溃事件时间（CDP）。所有动态内容均为 managed-default，
除非注明。

| # | 内容 × 窗口 | native | model | 结果 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | `text` × 1 | hidden | visible | **FATAL 69.919 s**；crash 事件 79.077 s | `hs-repro-text-1` |
| 2 | `text` × 1（trace） | hidden | visible | **FATAL 65.626 s**；最后一次成功 probe 65.159 s / paint 68 | `hs-trace-repro` |
| 3 | `raf` × 1 | hidden | visible | **FATAL 59.903 s**；raf=61 | `hs-repro-raf-1` |
| 4 | `canvas` × 1 | hidden | visible | **FATAL 59.872 s**；raf=61 | `hs-repro-canvas-1` |
| 5 | `webgl` × 1 | hidden | visible | **FATAL 59.886 s**；raf=61；WebGL context=true | `hs-repro-webgl-1` |
| 6 | `extension`（fixture）× 1 | hidden | visible | **FATAL 66.638 s**；crash 75.016 s；paint 67 | `hs-repro-extension-1` |
| 7 | `extension`（真实 BrowserPilot popup）× 1 | hidden | visible | **FATAL 59.842 s**；crash 65.104 s | `hs-repro-real-popup` |
| 8 | `iframe`（OOPIF）× 1 | hidden | visible | **两个 FATAL：58.993 s（父） / 59.065 s（子）**；OOPIF target type=iframe | `hs-repro-iframe-1` |
| 9 | `text,static,no-paint` × 3 | hidden | visible | text **FATAL 62.463 s**；static/no-paint **95 s 存活**，paint=1 | `hs-ctl-managed-default` |
| 10 | 6 种动态内容 × 6 | hidden | visible | **7 条 FATAL**（57.4–66.8 s）；全部 target crash（事件 81 s 才到） | `hs-ctl-managed-hidden`（SW_HIDE） |
| 11 | 6 种动态内容 × 6 | hidden+iconic | visible | **7 条 FATAL**（57.3–69.7 s）；全部 target crash | `hs-ctl-managed-minimized` |
| 12 | 6 种动态内容 × 6 | hidden | **hidden** | **STABLE 90 s**；raf 0 | `hs-ctl-normal-hidden` |
| 13 | `text,canvas` × 2 | hidden | **hidden** | **STABLE 180 s**；内存平坦；180 s 后截图 fresh | `hs-long-stable` |
| 14 | `text,raf,canvas` × 3 | visible | **hidden（被桌面遮挡）** | STABLE 60 s | `hs-ctl-normal-visible` |
| 15 | `text,raf,canvas` × 3 | visible（同 rect 叠加） | **hidden** | STABLE 60 s | `hs-ctl-normal-occluded` |
| 16 | 6 种动态内容 × 6 release-like | hidden | visible | 6/6 crash 事件（80.95–85.78 s），无日志 | `hs-repro-mixed-release` |
| 17 | 6 动态 + `static` + `no-paint` × 8 release-like | hidden | visible | 6 个动态全 crash；static/no-paint 存活；浏览器存活 | `hs-repro-8w-release` |
| 18 | `text` × 1，每 10 s 截图 | hidden | visible | **NOT-REPRODUCED（70 s 存活）**；6/6 截图 fresh | `hs-repro-screenshot-driven` |

**结论**：

- 触发条件不是“定时器”或“原生隐藏”本身，而是 **renderer 认为可见并持续产生 main-frame
  rendering update，而对应 display 永不 present**。因此 `static`、`no-paint` 稳定；
  `text/raf/canvas/webgl/extension/iframe` 都触发。
- 对从未 show 的 managed 窗口，`SW_HIDE`/`SW_MINIMIZE` 只改原生状态，**不改模型可见性**，
  所以仍触发（#10/#11）。真正的模型隐藏（#12/#13，先 show 再 hide）稳定。
- 原生可见但被遮挡（#14/#15）模型 hidden、rAF 停，稳定；这与旧文档的 occluded 结论一致。
- 8 窗口压力下，动态窗口全部命中；静态/无绘制对照不受影响，浏览器进程不崩。
- 周期截图（#18）会刷新/消费 presentation 链，**掩盖**了自然重绘的崩溃；只能算 workaround，
  不能当修复（且截图本身有 8 s 级成本和语义影响）。
- #10 故意用 `--expect stable` 运行，结果 `VERDICT CRASHED`；这正是“native hide 不等于
  model hide”的判定证据，#11 同理。

---

## 4. Presentation-chain 证据（trace + 源码）

### 4.1 定量 trace（`hs-trace-repro`，`viz,cc,gpu`，80 s）

| 事件 | 次数 | 说明 |
| --- | --- | --- |
| `Surface::CommitFrame` | 68 | 总提交；内容 surface `(6,3)` **66** 次，root UI `(0,1)` 2 次 |
| `Surface::ActivateFrame` | 68 | 内容 surface **66** 次 |
| `DisplayScheduler::DrawAndSwap` | 68 | display 有 draw 尝试 |
| `Display::DrawAndSwap` | 68 | 同上 |
| `FramePresented` | **2** | 只有 2 次真正 presentation；且来自 browser UI |
| `DisplayScheduler::BeginFrame` | 237 | 约 3/s（root UI + 被节流的内容） |
| FATAL | 1 | 65.626 s，61 vs. 60 |
| 最后一次成功 probe | — | 65.159 s，`paintCount=68` |

即：内容 surface 的 66 次 commit/activate 并没有换来 66 次 presentation；只有 2 次
`FramePresented`。renderer 侧 `presentation_callbacks_` 因此单调增长。

### 4.2 为什么是约 1 Hz、约 60 s

`components/viz/service/frame_sinks/compositor_frame_sink_support.h:86`：

```cpp
static constexpr uint32_t kUndrawnFrameLimit = 3;
```

`compositor_frame_sink_support.cc:1540-1565`：当
`active_frame_index - last_drawn_frame_index > kUndrawnFrameLimit` 且
`may_throttle_if_undrawn_frames` 为 true 时，`ShouldSendBeginFrame()` 返回
`ThrottleUndrawnFrames`；并且 `frame_time - last_frame_time < 1s` 时才允许节流，
所以隐藏窗口的 BeginFrame 被压到 **约 1 次/秒**。页面本来想 60 Hz，实际每秒只拿到一帧；
60 条回调因此需要约 60 s（实测 59–70 s）。

### 4.3 源码链路

```text
ui/views/win/hwnd_message_handler.cc:3266
  BrowserCliShouldSuppressWindowPosChange() -> 剥掉 SWP_SHOWWINDOW
      // 只改原生 HWND；没有调用 Widget::Hide()/WebContents WasHidden()

content/browser/renderer_host/render_widget_host_view_aura.cc:1075
  ShowWithVisibility() -> window_->Show() (aura 模型可见) -> ShowImpl()
      :712 NotifyHostAndDelegateOnWasShown()
          visibility_ = VISIBLE; host()->WasShown();   // renderer 收到 WasShown
      :751 HideImpl() 只有在真正 Hide() 时才 host()->WasHidden() / DFH::WasHidden()

third_party/blink/renderer/platform/widget/widget_base.cc:1574
  SetHidden(false) -> SetCompositorVisible(true)   // 模型一直 visible

third_party/blink/renderer/core/frame/web_frame_widget_impl.cc:528
  if (!IsHidden()) animation_frame_timing_monitor_ = ...   // 创建 monitor
  :3042 BeginMainFrame() -> monitor->BeginMainFrame()
  :5610 WasShown() -> 若 monitor 为空则重新创建

third_party/blink/renderer/core/paint/timing/paint_timing.cc:353
  每次有 paint timing 记录的 rendering update:
      ChromeClient::NotifyPresentationTime(...) -> QueueSwapPromise(ReportTimeSwapPromise)

third_party/blink/renderer/core/frame/web_frame_widget_impl.cc:3940
  ReportTimeSwapPromise::DidSwap() -> AddPresentationCallback(frame_token, ...)

third_party/blink/renderer/platform/widget/compositing/layer_tree_view.cc:553
  AddCallback(): DCHECK_LE(callbacks.size(), kMaxBufferSize)   // 61 > 60 -> FATAL
  :413 DidPresentCompositorFrame(): 只有成功 presentation 才清理；
       failed feedback 直接 return（连 failed 也不消费）
```

关键代码摘录：

```cpp
// layer_tree_view.cc:421-433
if (frame_timing_details.presentation_feedback.failed()) {
  return;                     // <- failed feedback 不清理 callbacks
}
while (!presentation_callbacks_.empty()) {
  const auto& front = presentation_callbacks_.begin();
  if (front->first > frame_token) break;
  for (auto& callback : front->second) std::move(callback).Run(frame_timing_details);
  presentation_callbacks_.erase(front);
}
```

```cpp
// cc/trees/presentation_time_callback_buffer.h:43
static constexpr size_t kMaxBufferSize = 60u;
```

因此：**native show 被拒绝并没有让 Chromium 的 visibility 变成 hidden**；`WebContents`/renderer
仍处于 visible，帧持续产生；display 不 present；viz 因 undrawn frames 节流到 1 Hz；
`presentation_callbacks_` 每秒 +1，60 s 后 DCHECK。

---

## 5. 内存 / 进程 / 目标活性

| 运行 | 起始 | 结束 | 行为 |
| --- | --- | --- | --- |
| `hs-repro-8w-release`（8 窗，6 动态崩） | 11 renderer / 3.769 GB WS / 1.348 GB private | 4 renderer / 2.097 GB WS / 0.885 GB private | 6 个动态 target crash；static/no-paint 存活；浏览器进程存活 |
| `hs-long-stable`（normal-hidden 2 窗 180 s） | 4 renderer / 1.916 GB WS / 700 MB private | 4 renderer / 1.917 GB WS / 682 MB private | 无崩溃；内存平坦（无增长趋势） |
| `hs-repro-text-1`（managed text 1 窗） | 3 renderer / 1.606 GB WS | 2 renderer / 1.400 GB WS | 崩溃后 renderer 减少 1，内存下降 |
| `hs-repro-screenshot-driven`（managed text 1 窗 70 s + 周期截图） | 3 renderer / 1.621 GB WS | 3 renderer / 1.644 GB WS | 无崩溃；截图掩盖 |

- 崩溃时 **browser process 不退出**；target 仍可能出现在 `/json/list`，但
  `Inspector.targetCrashed`/probe 超时/进程树中 renderer 消失三者可判定。
- 8 窗口压力下 6 个动态窗口全部命中；静态/无绘制对照不受影响，证明不是“进程级随机崩溃”。

---

## 6. 截图新鲜度（before/after）

判定不是“有 PNG”：marker 色块与 DOM `paintCount` 比对（允许 ±4 帧），并要求非空白。

| 运行 | 场景 | 结果 | 耗时 |
| --- | --- | --- | --- |
| `hs-freshness-managed` | managed-default，6 内容，before+after | **12/12 fresh**，marker 全匹配 | 2.5–4.1 s |
| `hs-freshness-normal-hidden` | normal-hidden，3 内容，before+after | **6/6 fresh** | 0.26–3.25 s |
| `hs-freshness-normal-occluded` | normal-occluded，text，before | **1/1 fresh**（patch 027 后不再挂起） | 319 ms |
| `hs-long-stable` | normal-hidden 180 s 后 after | **2/2 fresh**；text marker 196 vs paint 195；canvas 20 vs 16 | 0.6 / 2.3 s |
| `hs-repro-screenshot-driven` | managed-default 每 10 s 截图 | **6/6 fresh**，且 70 s 不崩（掩盖） | 2.7–3.0 s |

- **截图新鲜不等于“用户可见”**：这些都是 compositor copy 路径；managed-default 的窗口
  原生仍是隐藏的。
- 截图的强制重绘会改变 presentation 链行为：在 patch 027 之后，周期截图**没有**复现
  61>60，反而让页面存活；因此不能用“周期截图”证明稳定性，也不能作为根修。
- 模型隐藏窗口在 180 s 后仍能取到新帧，说明“按需重绘 + 截图”是产品可用的后台语义。

---

## 7. 根因结论

**一句话**：managed 窗口的原生呈现被策略拒绝，但 Chromium 的模型可见性没有同步改变；
renderer 继续产生 main-frame 帧，display 不 present；viz 因 undrawn frames 把 BeginFrame
节流到 1 Hz；每帧注册的 presentation callback 永不消费，60 条约 60 s 后触发 Blink DCHECK，
renderer 被杀。

**触发必要条件**（全部满足）：

1. 窗口属于 managed 后台（`--browser-cli-background-presentation`），原生 show 被拒绝；
2. `WebContents`/renderer 仍为 VISIBLE（`document.visibilityState=visible`）；
3. 页面产生 main-frame rendering update（文本/Canvas/WebGL/扩展页/OOPIF 子帧均可）；
4. display 不 present（原生 HWND 隐藏/被抑制），且没有别的路径消费 presentation feedback。

**不触发**：`static`（无 update）、`no-paint`（定时器不产生 update）、真正模型隐藏
（`normal-hidden`，rAF 停）、原生可见但被遮挡（模型 hidden）。**`SW_HIDE`/`SW_MINIMIZE`
对从未 show 的 managed 窗口不是模型隐藏**，这是 R2 之前判断“managed-hidden”时的关键盲点。

---

## 8. 修复提案（未构建、未验证；不是 solved）

### 8.1 产品层根修（推荐先做）

**原则**：让“原生不可呈现”与“模型可见”一致，从源头停止产生永不 present 的帧。

- 在 managed 窗口创建/首次 show 被 presentation policy 拒绝时，把对应 `WebContents`
  置为 HIDDEN（等价于 `RenderWidgetHostViewAura::Hide()` / `WebContents::WasHidden()`），
  而不是只剥 `SWP_SHOWWINDOW`。`ensure_visible`/USER_EXPLICIT show 时再切回 VISIBLE。
- 可选实现位置（按侵入性排序）：
  1. Browser CLI 的 managed window 创建路径：初始 `show_state` 直接为 hidden；
     用户显式 show 才走 presentation gateway。
  2. `ui/views/widget/browser_cli_presentation_win.cc` / `hwnd_message_handler.cc`：在
     `OnWindowPosChanging` 剥掉 `SWP_SHOWWINDOW` 后，通知 widget/browser 层“本次 show
     未生效”，由 browser 层对该 WebContents 调 `WasHidden()`。不要把 `HWNDMessageHandler`
     直接变成 WebContents 依赖。
- 代价：后台页面的 rAF 停止、定时器约 1 Hz（与 `normal-hidden` 一致）；这正是“后台”
  应有语义。按需截图仍 fresh（180 s 证据）。若产品确实需要后台 60 Hz 动画，则应走 8.2，
  而不是保留当前不一致状态。
- 回归：
  - `hidden-stability-cdp-check.py --scenario managed-default --content text --duration 180
    --expect stable`（修复后必须 stable）；
  - 截图 before/after fresh；
  - `normal-hidden` 不回归；
  - BrowserPilot R2 popup 不再在 60 s 死亡。

### 8.2 Chromium presentation-chain 硬化（上游质量；保留“模型可见但不呈现”时必需）

目标：任何“不可能 present”的 frame token 都必须收到 failed feedback，renderer 必须消费
failed feedback；buffer 再加硬上限作为最后防线。

**A. renderer：failed feedback 也要消费回调**

`third_party/blink/renderer/platform/widget/compositing/layer_tree_view.cc`：

```diff
 void LayerTreeView::DidPresentCompositorFrame(
     uint32_t frame_token,
     const viz::FrameTimingDetails& frame_timing_details) {
   ...
-  // Only run callbacks on successful presentations.
-  if (frame_timing_details.presentation_feedback.failed()) {
-    return;
-  }
+  // A failed feedback still means these tokens will never be presented.
+  // Consume them so the buffer cannot grow without bound.
+  const bool failed =
+      frame_timing_details.presentation_feedback.failed();
   while (!presentation_callbacks_.empty()) {
     const auto& front = presentation_callbacks_.begin();
     if (front->first > frame_token) break;
-    for (auto& callback : front->second)
-      std::move(callback).Run(frame_timing_details);
+    if (!failed) {
+      for (auto& callback : front->second)
+        std::move(callback).Run(frame_timing_details);
+    }
     presentation_callbacks_.erase(front);
   }
```

（`core_animation_error_code_callbacks_` 在 Apple 上同理；Windows 不受影响。）

**B. viz：对 undrawn / 不可呈现的 frame 主动发 failed feedback**

`components/viz/service/frame_sinks/compositor_frame_sink_support.{h,cc}` 已有
`pending_received_frame_times_`、`last_drawn_frame_index_`、`kUndrawnFrameLimit`、
`should_throttle_undrawn_frames`。建议：

```diff
 // compositor_frame_sink_support.h
+  // Fail presentation feedback for frames that have been superseded and can
+  // never be drawn/presented. Keeps LayerTreeView's callback buffer bounded.
+  void FailSupersededUndrawnFrames();
```

在 `ShouldSendBeginFrame()` 判定 `should_throttle_undrawn_frames == true` 时（或
`RootCompositorFrameSinkImpl` 收到 display hidden 通知时）调用：

```cpp
void CompositorFrameSinkSupport::FailSupersededUndrawnFrames() {
  const uint32_t active =
      last_activated_surface_id_.is_valid()
          ? surface_manager_->GetSurfaceForId(last_activated_surface_id_)
                ->GetActiveFrameIndex()
          : kInvalidFrameToken;
  if (active == kInvalidFrameToken) return;
  std::vector<uint32_t> tokens;
  for (const auto& entry : pending_received_frame_times_) {
    if (entry.first < active &&
        !frame_timing_details_.contains(entry.first)) {
      tokens.push_back(entry.first);
    }
  }
  for (uint32_t token : tokens) {
    DidPresentCompositorFrame(token, base::TimeTicks(), gfx::SwapTimings(),
                              gfx::PresentationFeedback::Failure());
  }
}
```

注意：
- 只 fail **严格早于当前 active frame** 的 token；当前 active frame 仍可能在可见后 present。
- `DidPresentCompositorFrame` 会 `CHECK(pending_received_frame_times_.find(token) != end())`，
  必须只对仍在该 map 中的 token 调用；先收集再调用，避免遍历时 erase。
- 若 `frame_timing_details_` 已含该 token，跳过，避免 `CHECK(!contains)`。
- display 从 hidden 变 visible 时，旧 token 已被新帧取代，也可以安全 fail。

**C. 最后防线（独立可测，但不能单独称为根修）**

在 `LayerTreeView::AddCallback` 达到 `kMaxBufferSize` 时丢弃最旧 entry（或 fail 整个
widget），避免任何遗漏的 feedback 路径杀死 renderer。这是 hardening，不是根因修复；
必须与 A/B 一起审。

**D. 回归测试**

- `layer_tree_view_unittest.cc`：加入 token 1/2 的 callback；用
  `gfx::PresentationFeedback::Failure()` 调 `DidPresentCompositorFrame(2, ...)`；断言
  deque 清空、callback 不被成功执行、后续 AddCallback 不 DCHECK。
- `compositor_frame_sink_support_unittest.cc` / `display_unittest.cc`：提交
  `kUndrawnFrameLimit+2` 帧且 display hidden；断言被 supersede 的 token 收到
  `kFailure`，当前 active token 未失败；恢复 visible 后 active 帧可正常 present。
- 本 harness：`managed-default --content text --duration 180 --expect stable` +
  `--freshness both`；`normal-hidden` 180 s 不回归；真实 popup 不再死亡。

### 8.3 Workaround（必须标注，不是根修）

| workaround | 实测 | 评价 |
| --- | --- | --- |
| 每个 case 回收 popup（BrowserPilot PR #1 `89531ea`） | R2 通过 | 只换了一个新 renderer；popup 生命周期 <60 s 才有效 |
| 周期 `Page.captureScreenshot` | 70 s 存活，6/6 fresh | **掩盖**；截图强制重绘改变了 presentation 链，成本高，语义变化 |
| `Page.setWebLifecycleState(frozen)` | 未跑本轮 | 停止渲染，属于冻结页面，不是修复 |
| 把 popup 生命周期压到 <60 s | 逻辑上有效 | 与“长期驻留”产品目标冲突 |
| 关闭遮挡计算 / 抢前台 / 临时 show | 不采用 | 违反不打扰用户、且不解决模型可见性问题 |

**明确不做**：周期 page restart、timer kill/reload、watchdog 事后隐藏。本轮 harness 也没有
使用这些手段；所有崩溃都是自然重绘达到阈值后发生的。

---

## 9. 复现/验收命令

```sh
# 1) 单窗口、1 Hz 文本，自然重绘复现（diagnostic，含精确 FATAL）
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default --content text --windows 1 \
  --duration 80 --mode diagnostic --expect crash --freshness none \
  --evidence-dir D:/c/logs/hs-repro-text-1

# 2) 真实 BrowserPilot popup（只读另一 worktree 的 dist）
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default --content extension --windows 1 \
  --duration 90 --mode diagnostic --expect crash \
  --extension-dir D:/work/wt/bp-a-r1r2/dist --extension-page popup/index.html \
  --evidence-dir D:/c/logs/hs-repro-real-popup

# 3) 真模型隐藏对照：90 s + 前后截图
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario normal-hidden --content text,raf,canvas --windows 3 \
  --duration 90 --mode release-like --expect stable --freshness both \
  --evidence-dir D:/c/logs/hs-freshness-normal-hidden

# 4) 长跑 180 s + 内存 + 截图
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario normal-hidden --content text,canvas --windows 2 \
  --duration 180 --sample-interval 10 --mode release-like --expect stable \
  --freshness both --evidence-dir D:/c/logs/hs-long-stable

# 5) Surface/Undrawn 证据（诊断 trace；不要加 devtools.timeline）
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default --content text --windows 1 \
  --duration 80 --mode diagnostic --expect crash --freshness none \
  --trace-categories viz,cc,gpu --evidence-dir D:/c/logs/hs-trace-repro

# 6) 8 窗口压力（release-like）
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default \
  --window-content text,raf,canvas,webgl,extension,iframe,static,no-paint \
  --windows 8 --duration 90 --mode release-like --expect crash --freshness none \
  --evidence-dir D:/c/logs/hs-repro-8w-release
```

静态门禁：

```sh
tests/check-browser-launch-hygiene.sh          # 17 entry points, all OK
tests/run-script-tests.sh                     # 第 31 组 13 项 PASS
```

本机 `run-script-tests.sh` 的其余失败来自 Windows 宿主/Git Bash 的 `python3` alias 与
macOS-only 组，不是本切片新增；第 31 组全部 PASS。

---

## 10. 证据目录

| 目录 | 内容 |
| --- | --- |
| `D:\c\logs\hs-repro-real-popup` | 真实 BrowserPilot popup，FATAL 59.842 s，extension ID |
| `D:\c\logs\hs-repro-text-1` | 单窗 text 自然重绘，FATAL 69.919 s |
| `D:\c\logs\hs-trace-repro` | trace：66 次内容 surface activate / 2 次 FramePresented，FATAL 65.626 s |
| `D:\c\logs\hs-repro-raf-1` / `hs-repro-canvas-1` / `hs-repro-webgl-1` | 单内容复现 |
| `D:\c\logs\hs-repro-extension-1` | 自建 MV3 扩展页复现 |
| `D:\c\logs\hs-repro-iframe-1` | OOPIF 父子两个 renderer 各触发一次 FATAL |
| `D:\c\logs\hs-ctl-managed-default` | text 崩、static/no-paint 存活 |
| `D:\c\logs\hs-ctl-managed-hidden` / `hs-ctl-managed-minimized` | SW_HIDE / SW_MINIMIZE 仍模型可见、仍崩 |
| `D:\c\logs\hs-ctl-normal-hidden` / `hs-ctl-normal-visible` / `hs-ctl-normal-occluded` | 模型隐藏/遮挡对照，90/60 s 稳定 |
| `D:\c\logs\hs-long-stable` | 180 s 稳定 + 内存平坦 + 180 s 后截图 fresh |
| `D:\c\logs\hs-freshness-managed` | 6 内容 before/after 12/12 fresh |
| `D:\c\logs\hs-freshness-normal-hidden` / `hs-freshness-normal-occluded` | 模型隐藏/遮挡截图 fresh |
| `D:\c\logs\hs-repro-mixed-release` / `hs-repro-8w-release` | release-like 多窗口/8 窗口压力 |
| `D:\c\logs\hs-repro-screenshot-driven` | 周期截图掩盖自然崩溃（workaround 证据） |
| `D:\c\logs\hs-trace-smoke` | trace 管道自检 |

每个目录包含 `result.json`、`samples.jsonl`、`events.jsonl`、`chrome.log`（diagnostic）、
`screenshots/`、`summary.txt`；`screenshots/*.png` 是原始 PNG，marker 可独立复核。

---

## 11. 限制与未覆盖

1. **未改/未建 Chromium**：8.2 的 diff 只是提案；没有编译、没有单测、没有产品验证。
   因此按任务口径 **不宣称 solved**。
2. **visible 基线不完整**：本机无法在不抢前台的情况下得到“真正可见且未被遮挡”的窗口；
   `normal-visible` 实际是“原生 visible + 模型 occluded”。真正的 visible 60 Hz 行为由既有
   文档/可见 UI 测试覆盖，本切片不重复。
3. **trace 开销**：`hs-trace-repro` 的 FATAL 时间（65.6 s）与无 trace 的 59.9–69.9 s 有差异；
   trace 只用于定性/计数，阈值判断以自然重绘运行为准。
4. **真实 popup 依赖另一 worktree**：`D:\work\wt\bp-a-r1r2\dist` 只读加载，未复制进本仓库；
   harness 默认仍用本仓库 fixture。
5. **短于 1 分钟的窗口**：未覆盖“popup 生命周期 <60 s”的产品矩阵；本切片目标是长期驻留。
6. **macOS/Linux**：未跑；本机为 Windows `out/WinDev`，默认 GPU 进程。
7. **多父 root / tab 拖拽 / 悬停预览**：未覆盖；它们也可能走“不 present”的路径，建议在
   8.2 的 failed-feedback 方案下补回归。
8. **内存**：180 s 平坦，但未做小时级；崩溃时内存下降是 renderer 被杀的结果，不是泄漏修复。

---

## 12. 与 R2 的关系

- R2 的 popup 由 BrowserPilot `open_space` 默认创建：`chrome.windows.create({focused:false,
  state:"normal"})`，即**模型可见、原生 show 被 managed policy 拒绝**；popup 每 1 s
  `refresh()` 更新可见状态文本。这正是本切片 #7 的真实形态。
- 2026-10-31 的 harness 修复（记录 `Target.targetCrashed`、`ensurePopup()` 检测死亡后回收、
  每 case 回收 popup）能让 E2E 通过，但只是把 renderer 生命周期压到 60 s 内；**不是根因修复**。
- 本切片证明：即使不截图、不 reload、不重启，只要 popup 持续重绘，renderer 仍会在约
  60 s 死亡；回收 popup 必须继续标注为 workaround，直到 8.1 或 8.2 落地并验证。
