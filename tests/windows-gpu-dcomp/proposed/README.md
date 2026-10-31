# patch 034 说明（已构建验证）

`chromium/patches/034-gpu-inprocess-direct-composition.patch`

- 目标：`gpu/ipc/service/gpu_init.cc`，在 `GpuInit::InitializeInProcess()` 内初始化
  DirectComposition，与 `GpuInit::Initialize()`（独立 GPU 进程）保持一致。
- 依据：`GpuInit::Initialize()` 在 `ui/gl/direct_composition_support.cc` 中设置
  `g_dcomp_device`；`DirectCompositionSupported()` 只返回该全局；in-process 从不调用它，
  因此 `ImageTransportSurface::CreatePresenter()` 永远返回 null，display 退回
  GL/Dawn 输出路径。
- 交叉证据：Chromium 自己的 in-process 测试 GPU 服务
  `components/viz/test/test_gpu_service_holder.cc:301` 明确在 GPU 线程调用
  `gl::QueryD3D11DeviceObjectFromANGLE()` + `gl::InitializeDirectComposition()`；
  `ui/compositor/test/in_process_context_factory.cc:299` 也走同一初始化。生产 in-process
  路径与测试 in-process 路径不一致。
- 已由主代理在共享 checkout 临时应用并构建验证；实际 patch 在
  `chromium/patches/034-gpu-inprocess-direct-composition.patch`。验证步骤：
  1. `git apply` 到 Chromium checkout；
  2. `ninja -C out/WinDev chrome`；
  3. 重跑 `harness/run-ab.ps1`，确认 `official-inproc-dcomp`/`bcli-dcomp` 的 WGC 变为彩色
     （与 `official-default` 同色块），`DCompPresenter::Present`/`DCLayerTree::Commit...`
     事件出现；
  4. 回归 `cdp_hidden`（patch 027）与遮挡/最小化/正常三态，仍为彩色且窗口不上屏；
  5. 与 `--disable-direct-composition` 组合时确认不会回退为白屏（DComp 初始化后该 flag
     仍会禁用 DComp，因此不应再依赖它）。
- 顺序注意：`InitializePlatformOverlaySettings()` 在本补丁之前执行，`overlay_info.direct_composition`
  可能仍为 false；主平面 DComp 不受影响（`CreatePresenter()` 直接查
  `DirectCompositionSupported()`），但 video overlay 信息会偏保守。若需 overlay 信息正确，
  把 `InitializePlatformOverlaySettings()` 移到本补丁之后。
- 风险：in-process 时 `GpuHostImpl::SetProcessId(GetCurrentProcId())`，DComp 子窗口由
  浏览器进程的 `ChildWindowWin` 创建，`RenderingWindowManager::RegisterChild` 的
  PID 检查可通过。该子窗口 reparent 路径已被候选 workaround
  （Dawn D3D12 `SkiaOutputDeviceDawnSwapChain`）在 in-process 下实际验证：WGC 可见
  `Intermediate D3D Window` 子窗口（pid == 浏览器 pid）且窗口彩色。
