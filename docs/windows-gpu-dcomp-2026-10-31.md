# Windows GPU-B：DComp 黑屏的 trusted A/B、首分歧与根因（2026-10-31）

状态：**黑屏已用可信窗口捕获复现；根因已定位到 in-process GPU 未初始化 DirectComposition；
提议补丁未构建验证。未修复，不宣称修复。** PR 保持 Draft。

范围：Browser CLI `--in-process-gpu` 默认 DComp 路径；真实 HWND 900×700、可见但表面黑，
CDP renderer 内容彩色。构建 `D:/c/src/out/WinDev`（Chromium 156.0.8067.0，patch 001–033）；
官方 Chrome 156.0.8078.48 作为上游基线。

关联：`tests/windows-gpu-dcomp/`（WGC 工具 + A/B harness + 汇总脚本）、
`tests/windows-gpu-dcomp/proposed/0001-gpu-init-inprocess-direct-composition.patch`。

---

## 0. 结论摘要

1. **可信 A/B 成立**：Windows Graphics Capture（WGC，DWM 支持）在窗口 `visible=true`、
   `foreground=false`（被前置窗口遮挡）时抓取到：
   - Browser CLI DComp 路径：9 点均 `RGB≈31,32,35`（`#1F2023` 等 4 个近黑变体），
     `unique_quantized_colors=6`，帧 hash `14864142134207728000`；
   - 同一 3×3 彩色页面的 CDP `Page.captureScreenshot(fromSurface=true)`：
     9 个采样点 9 色、21/25 高饱和点，内容始终彩色。
2. **官方 Chrome 156 用同一组参数复现完全相同的黑表面**：
   `--in-process-gpu` 下 WGC hash 与 Browser CLI **逐字节相同**
   （`14864142134207728000`），窗口树同样没有 `Intermediate D3D Window`，trace 同样
   没有 `DCompPresenter::Present` / `DCLayerTree::CommitAndClearPendingOverlays`。
   → 不是 Browser CLI 001–033 窗口 patch，而是**上游 in-process GPU 路径**。
3. **`--disable-direct-composition` 不是有效 workaround**：窗口表面从黑变成纯白
   （9 点 `#FFFFFF`，hash `14905162914385234000`），仍然没有页面内容。当前
   `--in-process-gpu --disable-direct-composition` 只能让“黑屏”变成“白屏”，不得再作为
   可见窗口修复。
4. **根因**：`GpuInit::InitializeInProcess()`（`gpu/ipc/service/gpu_init.cc:1030`）从不调用
   `gl::InitializeDirectComposition()`；该调用只存在于独立 GPU 进程的
   `GpuInit::Initialize()`（同文件 `:973`）。于是 `DirectCompositionSupported()` 恒 false，
   `DCompPresenter` 无法创建，display compositor 退回 GL/Dawn 输出路径；这些 fallback 在
   in-process 下执行了 `PostSubBuffer` / `Present1`，但窗口表面没有内容。
5. **一个能真正显示页面的候选 workaround**（**不是根修复**）：
   `--in-process-gpu --enable-features=SkiaGraphite --skia-graphite-dawn-backend=d3d12`
   走 `SkiaOutputDeviceDawnSwapChain`（自建 `Intermediate D3D Window` 子窗口并 reparent），
   WGC 抓到彩色 3×3 页面；官方 Chrome 同样有效。见 §6。
6. **提议根修复**：在 `GpuInit::InitializeInProcess()` 中补上 DirectComposition 初始化
   （diff 见 §7），预期使 in-process 与独立 GPU 进程一样使用 `DCompPresenter`。**本 worktree
   不能修改共享 `D:/c/src`，因此该 diff 未构建、未运行验证。**

---

## 1. 测试页与采集方法

### 1.1 测试页

本地 HTTP 页面（每个 run 随机端口）：

- 3×3 高饱和色块：RED / GREEN / BLUE / YELLOW / CYAN / MAGENTA / ORANGE / VIOLET / WHITE；
- 大号文字；
- 绿色方块滑动动画（2 s alternate）+ 100 ms 计数器；
- 不是空白页，不是单白点。

### 1.2 可信窗口捕获：WGC

`tests/windows-gpu-dcomp/tools/wgc-capture.cpp` 使用
`Windows.Graphics.Capture` + D3D11：

- 通过 `IGraphicsCaptureItemInterop::CreateForWindow(HWND)` 捕获真实顶层 HWND；
- DWM 合成表面，**与窗口是否被其他窗口遮挡无关**；不需要前台、不需要把窗口移到屏幕外；
- 每帧输出 PNG，记录 `SystemRelativeTime` + `QueryPerformanceCounter` 时间戳；
- 记录 HWND / PID / TID / 类名 / 标题 / `GetWindowRect` / `GetClientRect` /
  `ClientToScreen` / `IsWindowVisible` / `IsIconic` / `IsZoomed` / `GetForegroundWindow` /
  `DWMWA_CLOAKED` / `DWMWA_EXTENDED_FRAME_BOUNDS` / `GWL_STYLE` / `GWL_EXSTYLE`
  （含 `WS_EX_NOREDIRECTIONBITMAP` / `WS_EX_LAYERED`）/ DPI；
- 枚举子窗口（含 `Intermediate D3D Window`、`Chrome_RenderWidgetHostHWND`）的
  hwnd/parent/root/pid/rect/visible；
- 在 client 区域归一化 9 点采样 + 4 px 步长颜色普查 + FNV-1a 帧 hash。

WGC 的 PNG 是 DWM 对该窗口的合成结果，因此“窗口表面黑、CDP 彩色”是窗口呈现问题，
不是 renderer 没画。

### 1.3 隔离与前台

- 每个 variant 独立 `--user-data-dir=D:/c/logs/gpu-b2/<run>/profile`；
- 不使用 `--no-sandbox`；不使用真实用户 profile / 钥匙串；
- `show-window.ps1` 使用 `SW_SHOW` + `SetWindowPos(HWND_TOP, SWP_NOACTIVATE)`，**不调用
  `SetForegroundWindow`**；`show_state` 记录 `activated=false`；
- 采集时 `foreground=false`，真实前台 HWND 是 ZCode/Chrome 等其他窗口；
- 随后 `send-to-bottom.ps1` 用 `HWND_BOTTOM | SWP_NOACTIVATE` 把测试窗口移到 z-order 底部，
  再做第二轮 WGC；再用 `SW_HIDE` 做第三轮 CDP 截图（patch 027 copy 路径回归）。
- 未抢 Agent A 的前台；未改变任何其他进程的 z-order/可见性。

### 1.4 变体

| variant | 参数 | 目的 |
| --- | --- | --- |
| `official-default` | 官方 Chrome，无额外 flag | 上游工作基准（GPU 进程 + DComp） |
| `official-inproc-dcomp` | `--in-process-gpu` | 上游复现 |
| `official-inproc-nodcomp` | `--in-process-gpu --disable-direct-composition` | 当前 workaround 的上游对照 |
| `official-inproc-keepredir` | `--in-process-gpu --disable-features=RemoveRedirectionBitmap` | 排除 redirection bitmap 假设 |
| `official-inproc-early-show` | `--in-process-gpu`，首帧前显示窗口 | 排除“先隐藏后显示”时序假设 |
| `official-inproc-software` | `--in-process-gpu --disable-gpu` | 软件合成对照（诊断用） |
| `official-inproc-dawn-d3d12` | `--in-process-gpu --enable-features=SkiaGraphite --skia-graphite-dawn-backend=d3d12` | 候选 workaround 的上游对照 |
| `bcli-dcomp` | Browser CLI `--in-process-gpu` | Browser CLI 复现 |
| `bcli-nodcomp` | Browser CLI `--in-process-gpu --disable-direct-composition` | 当前产品 workaround |
| `bcli-inproc-dawn-d3d12` | Browser CLI + Dawn D3D12 | 候选 workaround |
| `bcli-inproc-dawn-d3d12-nodcomp` | 候选 workaround + 现有 flag | 组合验证 |

---

## 2. A/B 颜色证据（WGC，可信）

所有 WGC 都在窗口 `visible=true`、`foreground=false`（被前置窗口遮挡）时采集；
`window_rect=(60,60)-(960,760)`，`client=884×692`，`capture_item=(886,693)`。

| run | HWND / PID | ex_style | WGC 9 点（首帧） | unique | hash | CDP visible/bottom/hidden |
| --- | --- | --- | --- | --- | --- | --- |
| 官方默认（工作） | 3414482 / 5116 | `0x200100`（NOREDIR） | `#00FFFF #3C3C3C #282828 #3C3C3C #FFFF00 #FF00FF #FF8000 #8000FF #FFFFFF` | 241 | `11014233202856770000` | 彩色/彩色/彩色 |
| 官方 `--in-process-gpu`（复现） | 1116068 / 16832 | `0x200100` | `#1F2023 #1F2023 #1C2126 #1F2023 #202021 #202020 #202021 #202021 #202021` | 6 | **`14864142134207728000`** | 彩色/彩色/未跑 |
| 官方 `--in-process-gpu --disable-direct-composition` | 18287586 / 17292 | `0x100` | 9×`#FFFFFF` | 6 | **`14905162914385234000`** | 彩色/彩色/未跑 |
| Browser CLI `--in-process-gpu` | 17832294 / 22912 | `0x200100` | 与官方 inproc 逐点相同 | 6 | **`14864142134207728000`** | 彩色/彩色/彩色 |
| Browser CLI `--in-process-gpu --disable-direct-composition` | 4130352 / 35324 | `0x100` | 9×`#FFFFFF` | 6 | **`14905162914385234000`** | 彩色/彩色/彩色 |
| 官方 `--in-process-gpu --disable-features=RemoveRedirectionBitmap` | 722814 / 32156 | `0x100` | 9×`#FFFFFF` | 6 | `14905162914385234000` | 彩色/彩色/未跑 |
| 官方 `--in-process-gpu` + 首帧前显示 | 83168498 / 35404 | `0x200100` | 与 inproc 逐点相同 | 6 | `14864142134207728000` | 彩色/彩色/未跑 |
| 官方 `--in-process-gpu --disable-gpu`（软件） | 989596 / 4064 | `0x200100` | 8 个彩色点（含 `#00FFFF/#FFFF00/#FF00FF/#FF8000/#8000FF/#FFFFFF`） | 240 | `5624787317646769000` | 彩色/彩色/未跑 |
| Browser CLI Dawn D3D12（候选 workaround） | 83496178 / 28828 | `0x200100` | `#00FFFF #363840 #22242A #363840 #FFFF00 #FF00FF #FF8000 #8000FF #FFFFFF` | 251 | `7707043950038049000` | 彩色/彩色/彩色 |
| 官方 Dawn D3D12（候选 workaround） | 1513902 / 36960 | `0x200100` | `#00FFFF #3C3C3C #282828 #3C3C3C #FFFF00 #FF00FF #FF8000 #8000FF #FFFFFF` | 239 | `1383535079872823800` | 彩色/彩色/未跑 |

关键点：

- **黑表面 hash 在官方 Chrome 156 与 Browser CLI 之间完全相同**，且黑/白两态各自与
  fallback 路径一一对应；这排除了“Browser CLI 窗口 patch 导致黑屏”的假设。
- 白表面 hash 在 `official-inproc-nodcomp`、`official-inproc-keepredir`、
  `bcli-nodcomp` 三者也完全相同。`--disable-direct-composition` 让窗口保留
  DWM redirection bitmap，观察到白色表面；**不是页面内容**。所以“白=工作”是误判。
- 颜色采样中顶行 `#3C3C3C/#282828`（或 Browser CLI 的 `#363840/#22242A`）是浏览器
  工具栏/标签栏，不是页面；页面色块在中心/中/下部 6 个采样点，均为高饱和色。
- 黑/白 run 的 WGC 整帧是**空的 client 区域**：连标签栏/工具栏都没有（官方默认的
  WGC 帧能清楚看到 tab strip、地址栏和页面）。因此失败发生在 display compositor 的
  **输出/呈现**，不是 WebContents/HTML，也不只是页面 surface。
- 官方默认与 Dawn D3D12 的 WGC 在窗口被 `send-to-bottom` 完全遮挡后仍连续产出 3 帧，
  且 hash 不同（动画在推进）；说明 DWM 合成表面是活的，不是陈旧帧。

`unique_quantized_colors` 的 6 不是“只有 6 种颜色”的页面判定：黑/白 run 的窗口表面
只有系统 backdrop/背景刷 + 标题栏；CDP 参考帧始终是 9 色调色板。

---

## 3. 窗口 / Surface / 子窗口绑定证据

### 3.1 工作路径（官方默认）

```text
HWND 3414482  class=Chrome_WidgetWin_1  pid=5116
  window=(60,60)-(960,760) 900x700  client=884x692  visible=true  foreground=false
  ex_style=0x200100  WS_EX_NOREDIRECTIONBITMAP=true  dwm_cloaked=0
  children:
    Chrome_RenderWidgetHostHWND hwnd=15077956 pid=5116  visible=true  (68,147)-(952,752)
    Intermediate D3D Window       hwnd=3542144  pid=10184 visible=true  (68,60)-(952,752)
```

`Intermediate D3D Window` 由 GPU 进程（pid 10184）创建，`parent == 顶层 HWND`，
覆盖整个 client 区域；这是 `DCompPresenter` 的 `ChildWindowWin` 被
`RenderingWindowManager::RegisterChild` reparent 的结果。

### 3.2 复现路径（官方/Browser CLI `--in-process-gpu`）

```text
HWND 17832294  class=Chrome_WidgetWin_1  pid=22912（Browser CLI）
  window=(60,60)-(960,760) 900x700  client=884x692  visible=true  foreground=false
  ex_style=0x200100  WS_EX_NOREDIRECTIONBITMAP=true
  children:
    Chrome_RenderWidgetHostHWND hwnd=7213866 pid=22912 visible=true (68,147)-(952,752)
    Chrome_RenderWidgetHostHWND hwnd=7343246 pid=22912 visible=false
    Chrome_RenderWidgetHostHWND hwnd=1968216 pid=22912 visible=false
  → 没有 Intermediate D3D Window
```

`Chrome_RenderWidgetHostHWND`（68,147)-(952,752) 正好是 884×605 的页面 viewport，
说明 BrowserView/渲染控件绑定存在；但顶层窗口没有 DComp 子窗口，窗口表面是系统
backdrop（黑）或背景刷（白）。

官方 `--in-process-gpu` 的窗口树同样是 3 个 `Chrome_RenderWidgetHostHWND`，没有
`Intermediate D3D Window`；`--disable-gpu` 软件路径则有一个
`Intermediate D3D Window`（pid 4064 == 浏览器 pid，parent 顶层），且窗口彩色——这是
`SoftwareOutputDeviceWinSwapChain` 自己创建 DComp target/子窗口，不依赖
`g_dcomp_device`。

---

## 4. 首分歧：presentation chain

### 4.1 Surface / CompositorFrame（两种模式都存在）

两种模式都有同一棵 frame sink 树：

```text
浏览器 UI root: SurfaceId(FrameSinkId(0, 1), LocalSurfaceId(2, 1, <guid>))
页面内容:       SurfaceId(FrameSinkId(6, 3), LocalSurfaceId(4, 1, <guid>))
```

官方默认（run `official-default-h`）：

```text
Surface::CommitFrame                      ts=334840170629 µs
Surface::ActivateFrame                    ts=334840228919
Display::DrawAndSwap                      ts=334840229039
Surface::CommitFrame (content)            ts=334840233848
Surface::ActivateFrame (content)          ts=334840233850
SkiaOutputSurfaceImplOnGpu::SwapBuffers   ts=334840272096
SkiaOutputDeviceDComp::BeginOverlayAccess ts=334840272135
    args: debug_label="RenderPassBacking", image_size="884x692",
          mailbox="84:55:A2:7E:3B:11:4F:F6:97:75:A6:B2:4C:56:AC:55"
DCompPresenter::Present                   ts=334840274892
DCLayerTree::CommitAndClearPendingOverlays{num_overlays:1} ts=334840274896
```

`BeginOverlayAccess` 的 mailbox 是这次 DComp 主平面使用的 SharedImage 标识；随后
`DCompPresenter::Present` 把 `DCLayerTree` 的 1 个 overlay（主平面）提交给
`IDCompositionDevice::Commit`。in-process run 中该事件和 mailbox 都不存在——不是
“DComp surface 创建了但没提交”，而是**根本没有创建 DComp presenter/overlay surface**。

官方 `--in-process-gpu`（run `official-inproc-dcomp2`）：

```text
Surface::CommitFrame                      ts=334136628559 µs
Surface::ActivateFrame                    ts=334136684863
Display::DrawAndSwap                      ts=334136684936
Surface::CommitFrame (content)            ts=334136689348
Surface::ActivateFrame (content)          ts=334136689350
SkiaOutputSurfaceImplOnGpu::SwapBuffers   ts=334136743403
Display::DrawAndSwap                      ts=334136774617   ← 下一次合成，中间没有 DComp Present/Commit
```

计数（同一 15 s startup trace）：

| 事件 | 官方默认 | 官方 inproc | Browser CLI inproc |
| --- | ---: | ---: | ---: |
| `DCompPresenter::Present` | 547 | 0 | 0 |
| `DCLayerTree::CommitAndClearPendingOverlays` | 547 | 0 | 0 |
| `SkiaOutputDevice->ScheduleOverlays` | 547 | 0 | 0 |
| `NativeViewGLSurfaceEGL:PostSubBuffer`（GaneshGL fallback） | 0 | 253 | 0 |
| `SkiaOutputDeviceDawnD3D11BltMode::Present1`（Graphite D3D11 fallback） | 0 | 0 | 283 |
| `SkiaOutputSurfaceImplOnGpu::SwapBuffers` | 547 | 253 | 283 |
| `Display::DrawAndSwap` | 550 | 253 | 283 |
| `FramePresented` | 23 | 20 | 3 |

**第一处分歧**在 `SkiaOutputSurfaceImplOnGpu::SwapBuffers` 之后：
- 工作路径进入 `SkiaOutputDeviceDComp::BeginOverlayAccess` → `DCompPresenter::Present`
  → `DCLayerTree::CommitAndClearPendingOverlays`，把主平面提交给 DComp；
- in-process 路径没有任何 DComp 事件，直接进入下一次 `DrawAndSwap`；fallback
  `PostSubBuffer`/`Present1` 虽然在执行，但不产生可见内容。

注意：`FramePresented` 在黑/白 run 里仍然触发（20/3 次），它只表示 VSync 上的
presentation callback 被消费，**不能作为“已经呈现内容”的证据**。本报告的 PASS/FAIL
判定只使用 WGC 颜色 + CDP 参考帧 + DComp/fallback 事件。

### 4.2 WGC 时间戳

- 官方默认：`SystemRelativeTime` 334847315.724 ms / 334847782.389 / 334848249.054，
  QPC 334847308/334847774/334848239 ms；
- Browser CLI inproc（黑）：单帧 `SystemRelativeTime=334545766.931 ms`，QPC 334545759 ms；
- Browser CLI Dawn D3D12（workaround）：334506433.754 / 334506900.419 / 334507367.084 ms，
  QPC 334506430/334506893/334507362 ms，三帧 hash 不同。

`SystemRelativeTime` 是每进程相对时间，跨进程不可直接比较；QPC 是系统级，可比较。
WGC 帧时间与 trace 的 `ts` 都是系统时间轴，量级一致。

### 4.3 `directComposition` / `inProcessGpu`（CDP SystemInfo）

| run | displayType | skiaBackendType | directComposition | inProcessGpu | GPU 进程 |
| --- | --- | --- | --- | --- | --- |
| 官方默认 | `ANGLE_D3D11` | `GaneshGL` | **true** | false | 独立 `GPU` pid 10184 |
| 官方 `--in-process-gpu` | `ANGLE_D3D11` | `GaneshGL` | **false** | true | 无独立 GPU 进程 |
| Browser CLI `--in-process-gpu` | `ANGLE_D3D11` | `GraphiteDawnD3D11` | **false** | true | 无独立 GPU 进程 |
| Browser CLI Dawn D3D12 | `ANGLE_D3D11` | `GraphiteDawnD3D12` | false | true | 无独立 GPU 进程 |
| 官方 `--disable-gpu` | `ANGLE_D3D11_WARP` | None | false | true | 无独立 GPU 进程 |

---

## 5. 根因链（代码 + 运行时证据）

```text
--in-process-gpu
  → GpuProcessHost::Init() 创建 InProcessGpuThread
  → GpuInit::InitializeInProcess()            gpu/ipc/service/gpu_init.cc:1030
      （没有调用 gl::InitializeDirectComposition）
  → gl::DirectCompositionSupported() == false ui/gl/direct_composition_support.cc:855
      （只返回 g_dcomp_device；g_dcomp_device 只在 InitializeDirectComposition()
        中赋值，而该函数只在 GpuInit::Initialize() 中调用，gpu_init.cc:973）
  → ImageTransportSurface::CreatePresenter() == nullptr
                                              gpu/ipc/service/image_transport_surface_win.cc:50
  → SkiaOutputSurfaceImplOnGpu::InitializeForGL()（:1975）/
     InitializeForDawn()（:2161）
       presenter 为空 → CreateNativeGLSurface / Dawn 输出设备
  → 每帧照常 DrawAndSwap + SwapBuffers
  → fallback 输出设备执行：
       GaneshGL: NativeViewGLSurfaceEGL::PostSubBuffer（253 次，884×692）
       Graphite D3D11: SkiaOutputDeviceDawnD3D11BltMode::Present1（283 次，884×692）
  → 顶层 HWND 的 DWM 表面没有页面内容：
       无 --disable-direct-composition → WS_EX_NOREDIRECTIONBITMAP，窗口没有
         DComp visual / redirection surface → 观察到黑/系统 backdrop #1F2023；
       有 --disable-direct-composition → 保留 redirection bitmap →
         观察到白 #FFFFFF。
       黑/白两态的具体绘制来源尚未逐一确认；已确认的是两者都没有页面内容，
       CDP 参考帧始终彩色。
```

补充证据：

- `components/viz/common/features.cc:507` `ShouldRemoveRedirectionBitmap()`：
  Win11 22H2+ 默认移除 redirection bitmap；带 `--disable-direct-composition` 时返回 false
  （保留 bitmap）。这解释了黑/白两态，但**不改变“页面内容没有呈现”这一事实**。
- Chromium 自己的 in-process 测试 GPU 服务
  `components/viz/test/test_gpu_service_holder.cc:301` 明确在 GPU 线程执行
  `gl::QueryD3D11DeviceObjectFromANGLE()` + `gl::InitializeDirectComposition()`；
  `ui/compositor/test/in_process_context_factory.cc:299` 也走同一路径。
  **生产 in-process 路径与测试 in-process 路径不一致**，这是上游缺陷的直接旁证。
- 官方 Chrome 156 与 Browser CLI 黑表面 WGC hash 完全相同，说明 Browser CLI 的窗口
  创建/show/BrowserView patch（001–033）没有引入该差异。

---

## 6. 候选 workaround（标注：workaround，不是根修复）

### 6.1 当前产品 workaround 判定

`--in-process-gpu --disable-direct-composition`：

- 窗口表面 **白色**，不是页面；CDP 内容彩色；
- 与官方 Chrome 同参数 hash 完全相同（`14905162914385234000`）；
- patch 027 的 hidden/bottom CDP 截图仍然彩色，说明 copy 路径没问题；
- 结论：**不成立**。不应继续把它描述为“normal mode 工作”或“可见窗口修复”。
  如果产品必须在此构建保持可用，需要替换该 workaround。

### 6.2 能显示页面的 workaround：强制 Graphite Dawn D3D12 swap chain

参数：

```text
--in-process-gpu --enable-features=SkiaGraphite --skia-graphite-dawn-backend=d3d12
```

机制：`DirectCompositionSupported()` 仍为 false，但 Dawn 后端为 D3D12 时
`SkiaOutputDeviceDawn::Create()` 选择 `SkiaOutputDeviceDawnSwapChain`（不是 D3D11 blt
模式），它通过 `gl::ChildWindowWin` 创建自己的 `Intermediate D3D Window`，再由
`AddChildWindowToBrowser` reparent 到浏览器顶层窗口；Dawn 的 swap chain 呈现到该子窗口。

实测（Browser CLI，run `bcli-inproc-dawn-d3d12-h`）：

```text
HWND 83496178 pid 28828
  ex_style=0x200100 (NOREDIR)  visible=true  foreground=false
  children:
    Chrome_RenderWidgetHostHWND hwnd=10553750 pid=28828 visible=true (68,147)-(952,752)
    Intermediate D3D Window       hwnd=5577328  pid=28828 visible=true (68,60)-(952,752)
  WGC visible: 251 unique colors, 彩色 3×3（#00FFFF/#FFFF00/#FF00FF/#FF8000/#8000FF/#FFFFFF）
  WGC bottom : 遮挡后 3 帧仍彩色，hash 不同
  CDP visible/bottom/hidden: 全部彩色（9 色/21 饱和）
  trace: SkiaOutputDeviceDawnSwapChain::PresentImpl ×307
  SystemInfo: skiaBackendType=GraphiteDawnD3D12, directComposition=false, inProcessGpu=true
```

官方 Chrome 同参数（run `official-inproc-dawn-d3d12`）同样彩色
（`Intermediate D3D Window` hwnd=106499708 pid=36960 可见，WGC 239 unique，hash
`1383535079872823800`）；加上 `--disable-direct-composition` 也仍然彩色
（run `bcli-inproc-dawn-d3d12-nodcomp`，WGC 255 unique）。

**风险/限制（必须保留标签）**：

- 强制 D3D12/Graphite 后端，不是上游默认路径；需要目标机 D3D12 支持与更广的
  WebGL/WebGPU/video/overlay 回归。
- 仍需验证多显示器/不同 DPI/睡眠唤醒/驱动更新；本机为 RTX 4060 Laptop + Win11 26200。
- 它证明 `ChildWindowWin` + `RegisterChild` 在 in-process 下是可行的，从而支持 §7 的
  DComp 修复；但**它本身不是根修复**，不应写进产品默认参数而不加标注。

### 6.3 诊断用对照：软件合成

`--in-process-gpu --disable-gpu` 窗口彩色（run `official-inproc-software`，
`Intermediate D3D Window` pid == 浏览器 pid，WGC 240 unique，hash
`5624787317646769000`），但这是软件合成，不能作为产品修复。它证明窗口/展示链路本身
没问题，问题在 GPU 输出路径。

---

## 7. 提议根修复（未构建验证）

补丁：`tests/windows-gpu-dcomp/proposed/0001-gpu-init-inprocess-direct-composition.patch`

内容：在 `GpuInit::InitializeInProcess()` 的 Dawn 初始化之后、`SetSkiaBackendType()` 之前，
加入与 `GpuInit::Initialize()` 相同的 DComp 初始化块：

```cpp
#if BUILDFLAG(IS_WIN)
  {
    Microsoft::WRL::ComPtr<ID3D11Device> d3d11_device;
    Microsoft::WRL::ComPtr<ID3D12CommandQueue> d3d12_command_queue;
    gl::SolidColorPoolFactory solid_color_factory;
    if (dawn_context_provider_) {
      d3d11_device = dawn_context_provider_->GetD3D11Device();
      d3d12_command_queue = dawn_context_provider_->GetD3D12CommandQueue();
#if BUILDFLAG(SKIA_USE_DAWN)
      if (d3d12_command_queue &&
          base::FeatureList::IsEnabled(features::kDCompOnD3D12)) {
        solid_color_factory = CreateDawnTextureSolidColorPoolFactory(
            dawn_context_provider_->GetDevice(), d3d12_command_queue);
      }
#endif
    } else {
      d3d11_device = gl::QueryD3D11DeviceObjectFromANGLE();
    }
    if (!solid_color_factory) {
      solid_color_factory =
          gl::CreateDCSurfaceSolidColorPoolFactory(d3d11_device);
    }
    gl::InitializeDirectComposition(std::move(d3d11_device),
                                    std::move(d3d12_command_queue),
                                    std::move(solid_color_factory));
  }
#endif
```

预期：

- `DirectCompositionSupported()` → true；
- `ImageTransportSurface::CreatePresenter()` → `DCompPresenter`；
- `DCompPresenter` 的 `ChildWindowWin` 在浏览器进程创建子窗口，`GpuHostImpl::pid_` 在
  in-process 时是 `GetCurrentProcId()`（`content/browser/gpu/gpu_process_host.cc:1020`），
  `RenderingWindowManager::RegisterChild` 的 PID 检查应通过；
- WGC 顶层窗口应出现 `Intermediate D3D Window` 且变为彩色；trace 应出现
  `DCompPresenter::Present` / `DCLayerTree::CommitAndClearPendingOverlays`。

未验证原因：本 worktree 禁止修改共享 `D:/c/src`（Chromium checkout），也没有独立构建目录；
补丁只做了 `git -C D:/c/src apply --check`（**只读**，rc=0），未编译/未运行。

注意：`InitializePlatformOverlaySettings()` 在 `InitializeInProcess()` 中位于本补丁之前
（`:1163`），因此 `GPUInfo::overlay_info.direct_composition` 可能仍记录为 false；
这不影响主平面 `DCompPresenter`（它在 `GpuInit` 完成后由
`DirectCompositionSupported()` 决定），但视频 overlay 支持信息会偏保守。若上游要求
overlay 信息也正确，应把 `InitializePlatformOverlaySettings()` 移到本补丁之后（同时保持
`CollectGpuExtraInfo` 的独立性）。本提议为最小改动，暂不包含这个移动。

验证要求（主 agent）：

1. 在独立 Chromium worktree 应用 diff，`ninja -C out/WinDev chrome`；
2. 跑 `bcli-dcomp` / `official-inproc-dcomp`，确认 WGC 变为彩色、出现 DComp 事件；
3. 跑 `cdp_hidden` / `cdp_bottom`，确认 patch 027 copy 路径仍彩色；
4. 跑官方默认基线，确认无回归；
5. 明确去掉 `--disable-direct-composition` 后可见窗口正常；不要保留“白屏 workaround”。

---

## 8. patch 027 / 后台操作回归

本报告的所有 run 都在 **窗口不在前台** 的情况下采集；`cdp_*` 三态截图结果：

| run | CDP visible | CDP bottom（z-order 底部，遮挡） | CDP hidden（SW_HIDE） |
| --- | --- | --- | --- |
| 官方默认（DComp 工作） | 彩色 | 彩色 | 彩色 |
| Browser CLI `--in-process-gpu`（黑） | 彩色 | 彩色 | 彩色 |
| Browser CLI `--disable-direct-composition`（白） | 彩色 | 彩色 | 彩色 |
| Browser CLI Dawn D3D12（候选 workaround） | 彩色 | 彩色 | 彩色 |
| Browser CLI Dawn D3D12 + nodcomp | 彩色 | 彩色 | 彩色 |

→ patch 027 的 copy/activation 路径与窗口呈现路径相互独立；当前黑/白屏不是截图路径
退化。任何修复都必须以这组 hidden/bottom CDP 彩色为回归门槛。

---

## 9. 产物

诊断产物在 `D:/c/logs/gpu-b2/`（不提交）：

- `official-default-h/`：工作基准（DComp + hidden 三态）；
- `official-inproc-dcomp2/`：官方 Chrome 复现；
- `official-inproc-nodcomp/`：官方白屏对照；
- `official-inproc-keepredir/`、`official-inproc-dcomp-earlyshow/`：两个控制实验；
- `official-inproc-software/`：软件合成对照；
- `official-inproc-dawn-d3d12/`：Dawn D3D12 上游对照；
- `bcli-dcomp-h/`：Browser CLI 黑屏（含 hidden）；
- `bcli-nodcomp-h/`：Browser CLI 白屏（含 hidden）；
- `bcli-inproc-dawn-d3d12-h/`、`bcli-inproc-dawn-d3d12-nodcomp/`：候选 workaround；
- `evidence-all.json`、`analyze4.txt`：汇总证据与表。

每个 run 含：`summary.json`、`wgc-visible.json`、`wgc-visible.*.png`、
`wgc-bottom*.png`、`cdp-visible.png`、`cdp-bottom.png`、`cdp-hidden.png`、
`window-tree-*.json`、`trace-startup.json`、`evidence.json`、`chrome.log`。

本 worktree 内：

- `tests/windows-gpu-dcomp/tools/`：WGC 工具与构建脚本；
- `tests/windows-gpu-dcomp/harness/`：A/B harness、窗口/PNG 辅助、汇总脚本、批跑脚本；
- `tests/windows-gpu-dcomp/proposed/`：提议补丁与说明；
- 本文档。

---

## 10. 未决问题 / 下一步

1. **构建验证 §7 diff**：这是唯一可能同时去掉 `--in-process-gpu` 黑屏和
   `--disable-direct-composition` 白屏的根修复候选。需主 agent 在独立 worktree 构建并
   重跑本 harness。
2. in-process GPU 线程的 COM apartment：`DCompositionCreateDevice3` 通常不需要显式
   COM 初始化，但 patch 构建后应在 `InitializeInProcess` 验证；若失败，考虑在
   `InProcessGpuThread::Init` 增加 MTA 初始化并单独评估。
3. 为什么 GL `PostSubBuffer` / Dawn D3D11 blt 在 in-process 下即使有 redirection bitmap
   也不显示内容：这是 fallback 的第二个上游缺陷；DComp 修复落地后可作为独立问题记录，
   不影响根修复判定。
4. D3D12 + `kDCompOnD3D12` 组合；`SkiaOutputDeviceDawnSwapChain` workaround 的硬件/
   驱动矩阵与产品风险。DComp 初始化后，D3D12 Graphite 也会优先走 `DCompPresenter`；
   需确认 `DCompPresenter` 对 `IsGraphiteDawnD3D12` 的 DCHECK/`kDCompOnD3D12` 条件。
5. `GpuInit::InitializeInProcess` 也被 `--single-process` 等模式复用；补丁需单独回归这些
   模式，不能只看 `--in-process-gpu`。
5. 如果上游不接受 in-process DComp 初始化，产品侧只能选择：
   - 修复 GPU-A（Sogou TSF `ExitProcess(3)`）后移除 `--in-process-gpu`；或
   - 使用 §6.2 workaround 并承担后端切换风险；或
   - 保持后台/不可见窗口模式，不把窗口呈现当作已修复。
