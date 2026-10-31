# tests/windows-gpu-dcomp

Windows GPU/DComp 黑屏（GPU-B）的诊断工具与 A/B harness。**诊断不是产品修复**；本目录
不改变 Browser CLI 的启动参数或产品行为。

## 结论（2026-10-31）

- `--in-process-gpu` 下 `GpuInit::InitializeInProcess()` 从未调用
  `gl::InitializeDirectComposition()`，因此 `gl::DirectCompositionSupported()` 恒为 false，
  `DCompPresenter` 无法创建，display compositor 退回到 GL/Dawn 输出路径；该路径在
  `--in-process-gpu` 下不向浏览器窗口呈现内容（黑或白），renderer/CDP 内容始终正常。
- 官方 Chrome 156 用同样参数复现**完全相同**的窗口表面（WGC 哈希相同），因此不是
  Browser CLI 窗口 patch 的问题，而是上游 in-process GPU 路径缺失 DComp 初始化。
- 现有 `--in-process-gpu --disable-direct-composition` **不是有效 workaround**：窗口表面
  由黑变白，仍然是空白而不是页面内容。不得再把它当作可见窗口修复。
- 见 `docs/windows-gpu-dcomp-2026-10-31.md` 的完整证据、提议补丁和候选 workaround。

## 工具

- `tools/wgc-capture.cpp` / `tools/build-wgc.ps1`：
  用 Windows Graphics Capture（DWM 支持）抓取真实 HWND 的已合成表面，输出 PNG、窗口/
  子窗口树、style/ex_style、可见性/前台、采样点颜色、颜色直方图、帧时间戳。WGC 对
  **被遮挡窗口**仍有效，不需要抢前台，也不需要把窗口置顶到可见屏幕区域。
- `harness/gpu-dcomp-ab.mjs`：启动一个隔离 profile 的 Chrome/Browser CLI，加载 3×3
  高饱和色块 + 动画页面，采集：
  - CDP `Page.captureScreenshot(fromSurface=true)`（renderer 参考帧）；
  - WGC 顶层窗口帧（visible / 移到 z-order 底部 / 完全隐藏三个阶段）；
  - HWND、window/client rect、`IsWindowVisible`、`GetForegroundWindow`、DWM cloak、
    `WS_EX_NOREDIRECTIONBITMAP`、子窗口（`Intermediate D3D Window`、
    `Chrome_RenderWidgetHostHWND`）；
  - CDP `SystemInfo.getInfo`/`getProcessInfo`（`directComposition`、`inProcessGpu`、
    `skiaBackendType`、GPU 进程）；
  - `--trace-startup=gpu,viz,cc` trace（SurfaceId、CompositorFrame、DrawAndSwap、
    SwapBuffers、`DCompPresenter::Present`、`DCLayerTree::Commit...`、fallback present 事件）。
- `harness/show-window.ps1`：`SW_SHOW` + `SWP_NOACTIVATE`（可选 `-Activate` / `-Hide`）。
  默认不激活、不抢前台。
- `harness/send-to-bottom.ps1`：把自己的窗口移到 z-order 底部（`SWP_NOACTIVATE`），
  用于遮挡/后台路径。
- `harness/window-tree.ps1`：枚举目标进程的顶层/子窗口及 style/cloak/rect。
- `harness/png-sample.ps1`：对 PNG 做 5×5 采样、颜色直方图、空白/纯色判定。
- `harness/analyze-runs.py`：汇总多个 run root，输出 `evidence.json` 和 A/B 表。
- `harness/run-ab.ps1`：批量跑矩阵（每个 variant 独立临时 profile）。

## 构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build-wgc.ps1
```

需要 VS Build Tools（C++ x64）与带 C++/WinRT 头的 Windows SDK；不联网。

## 运行

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File harness/run-ab.ps1 `
  -Bcli D:/c/src/out/WinDev/chrome.exe `
  -Official "C:/Program Files/Google/Chrome/Application/chrome.exe" `
  -OutRoot D:/c/logs/gpu-b2 -Analyze
```

单个 variant：

```powershell
node harness/gpu-dcomp-ab.mjs --variant bcli-dcomp --chrome D:/c/src/out/WinDev/chrome.exe --root D:/c/logs/gpu-b2/bcli-dcomp
```

每个 root 的 `summary.json` 是原始证据；`wgc-visible.json` 是 WGC 帧/子窗口/采样；
`trace-startup.json` 是 Chromium trace；`cdp-*.png` 与 `wgc-*.png` 是帧图。

## Variant 标签

| variant | 含义 |
| --- | --- |
| `official-default` | 官方 Chrome 默认（GPU 进程 + DComp），**工作基准**，窗口彩色 |
| `official-inproc-dcomp` | 官方 Chrome `--in-process-gpu`，**上游复现**，黑 |
| `official-inproc-nodcomp` | 官方 Chrome 加 `--disable-direct-composition`，白（不是内容） |
| `bcli-dcomp` | Browser CLI `--in-process-gpu`，复现黑；与官方 inproc 同哈希 |
| `bcli-nodcomp` | Browser CLI 当前产品 workaround，白；**不显示页面** |
| `bcli-inproc-dawn-d3d12` | 候选 workaround：强制 Graphite Dawn D3D12 swap-chain（子窗口 + `SkiaOutputDeviceDawnSwapChain`），彩色；**不是根修复** |
| `official-inproc-dawn-d3d12` | 同一 workaround 的上游对照，彩色 |
| `official-inproc-software` | `--disable-gpu` 软件合成对照，彩色；诊断用，不能作为产品修复 |
| `*keepredir` | 保留 DWM redirection bitmap 的对照，仍白；排除 redirection bitmap 假设 |
| `*early-show` | 在首帧前显示窗口的对照，仍黑；排除显示时序假设 |

## 安全边界

- 每个 run 使用独立 `--user-data-dir`（`D:/c/logs/gpu-b2/<run>/profile`），不复用用户 profile。
- 不使用 `--no-sandbox`；不修改系统输入法/安全设置；不杀全局进程。
- 不抢前台：`show-window.ps1` 默认 `SWP_NOACTIVATE`，只 `SW_SHOW`；`wgc` 对被遮挡窗口
  仍可采集。日志记录 `foreground=false` 与真实 `foreground_hwnd`。
- Browser CLI 窗口在 managed/延迟展示模式下启动时可能是 hidden；harness 显式 show 是
  为了复现“HWND 可见但表面黑”的原场景，不代表产品启动路径。
- 不修改 `D:/c/src`（共享 Chromium checkout）；提议的 Chromium diff 只放在
  `tests/windows-gpu-dcomp/proposed/`，需主 agent 应用并单独构建验证。
