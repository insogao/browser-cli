# GPU P0 跟踪：ExitProcess(3) 与 DComp 黑屏（2026-10-31）

状态：**GPU-A 已定位实际退出调用方；GPU-B 进行中，尚未修复。**
构建：`out/WinDev`，Chromium 156.0.8067.0，patch 001–033 applied。
安全边界：独立临时 `--user-data-dir`；未使用 `--no-sandbox`；未禁用/修改系统输入法；未修改注册表/安全配置。

---

## 1. GPU-A：ExitProcess(3) 的实际调用栈

### 1.1 复现命令

```text
cdb.exe -G -cf <commands> -logo D:\c\logs\gpu-a-20261031-120925\cdb.log
  D:\c\src\out\WinDev\chrome.exe
  --headless=new
  --user-data-dir=D:\c\logs\gpu-a-20261031-120925
  --no-first-run --no-default-browser-check
  about:blank
```

断点：

```text
bp ntdll!RtlExitUserProcess "kb 60; .echo ===PROC===; |; .echo ===THREAD===; ~; q"
```

### 1.2 进程 / 线程 / 调用方

`D:\c\logs\gpu-a-20261031-120925\cdb.log`：

```text
.  0 id: 349c create name: chrome.exe
.  0 Id: 349c.260 Suspend: 1 ... Unfrozen "CrBrowserMain"
```

- 进程：`chrome.exe` 浏览器主进程（PID 349c），不是 `--type=gpu-process`；
- 线程：`CrBrowserMain`（TID 260），即浏览器主线程；
- 退出参数：`ExitProcessImplementation` 的 `Arg 0 = 3`；
- 退出发生在搜狗自己的 C++ 异常处理路径中，不是 Chromium fatal。

### 1.3 连续栈（关键帧）

```text
ntdll!RtlExitUserProcess
KERNEL32!ExitProcessImplementation+0xb
SogouPY!ImeExtension+0x10ce3c9          <-- ExitProcess(3) 调用点
SogouPY!ImeExtension+0x10ce394
SogouPY!ImeExtension+0x10cc8f5
SogouPY!ImeExtension+0x10df33d
SogouPY!ImeExtension+0x10baf84
SogouPY!ImeExtension+0x10bbbc0
SogouPY!ImeExtension+0x10bbc2e
SogouPY!ImeExtension+0x10b7a35
ntdll!RtlpExecuteHandlerForException+0xf   <-- 正在执行异常处理器
ntdll!RtlDispatchException+0x5f5
ntdll!KiUserExceptionDispatch+0x2e
KERNELBASE!RaiseException+0x8a              <-- 搜狗抛出 C++ 异常 e06d7363
SogouPY!ImeExtension+0x10b8610
...
SogouPY!ImeDestroy+0xf0                     <-- IMM32 入口
SogouPY!ImeInquire+0x19
SogouTSF!DllRegisterServer+0xc5117
SogouTSF!DllRegisterServer+0x3ade
MSCTF!CTip::Activate+0x121                  <-- TIP 激活
MSCTF!CThreadInputMgr::_ActivateTip+0x113
MSCTF!CThreadInputMgr::ActivateInputProfile+0x372
MSCTF!CThreadInputMgr::EnsureTIPsActivated+0x74
MSCTF!CThreadInputMgr::ActivateEx_P+0x16b
MSCTF!CThreadInputMgr::Activate+0x22
ui_base_ime_win!ui::TSFBridge::Initialize+0x616
ui_base_ime_win!ui::TSFBridge::Initialize+0x205
ui_base_ime_init!ui::InitializeInputMethod+0x39
content!content::BrowserMainRunnerImpl::Initialize+0x23f
content!content::BrowserMain+0x99
...
chrome!ChromeMain+0x365
chrome_exe!wWinMain+0xc01
```

### 1.4 判读

1. **实际调用方是 SogouPY，不是 Chromium/GPU。** `ExitProcess(3)` 的返回地址在
   `SogouPY!ImeExtension+0x10ce3c9`；栈上方是 `RtlpExecuteHandlerForException`，
   下方是 `RaiseException`（`e06d7363`）和 `SogouTSF`/`MSCTF` TIP 激活。
2. **不是 GPU 进程退出。** TSF 初始化时只有浏览器主进程/主线程；没有 GPU 子进程或
   GPU 服务参与这个栈。`--in-process-gpu` 改变的是浏览器进程内部初始化时序/早期线程，
   不是“修复 GPU 进程崩溃”。
3. **patch 023 只在 managed 模式跳过 TSF**，因此普通模式仍会命中该路径；这正是当前
   `manual-browser.cmd` 仍需要 `--in-process-gpu` 的原因。
4. 这与 2026-10-28 的上游 Chromium 151 / stock Chrome / Edge 对照结论一致：
   - 上游 Chromium 151 产生相同偏移的 `SogouPY` 退出；
   - 官方 Chrome 156 与 Edge 156 在同一台机器正常加载搜狗组件；
   - 签名、安装路径、产品名、进程令牌/完整性/Job/mitigation 均已被排除；
   - TSF 时刻存在稳定差异：`--in-process-gpu` 多 1 条线程 / 7 个句柄（5 Thread + 2 Event），
     尚未定位是谁改变了搜狗的时序/内部判断。

### 1.5 GPU-A 结论

**GPU-A 的问题不是“GPU 进程崩溃”，而是 SogouPY 在 TSF TIP 激活期间主动调用
`ExitProcess(3)`；`--in-process-gpu` 只是改变了进入 TSF 前的进程内部时序并规避了它。**
下一步仍是 H5：定位那 +1 线程/7 句柄对应的对象，以及 TSF 激活期间线程调度差异；
不能用禁用输入法、改安全配置或伪造进程身份来“修复”。

---

## 2. GPU-B：DComp 黑屏（进行中，已拿到第一组对照）

### 2.1 测试页与采集方法

测试页是本地 HTTP 页面：3×3 高饱和色块（RED/GREEN/BLUE/YELLOW/CYAN/MAGENTA/ORANGE/
VIOLET/WHITE）+ 大号文字 + 2s 位移动画。不是空白页，也不是单白点判定。

采集方法：

- CDP `Page.captureScreenshot(fromSurface=true)` 作为 renderer 参考帧；
- `PrintWindow(PW_RENDERFULLCONTENT)` 采样窗口自身表面；
- `CopyFromScreen` 采样窗口屏幕区域（环境有全屏窗口时会被遮挡，需结合 foreground 判断）；
- 记录真实 HWND、window rect、client origin、`IsWindowVisible`、`GetForegroundWindow`；
- `Page.setWebLifecycleState("active")` + `Target.activateTarget` 后才接受 visibility 结果；
- 未使用 `--no-sandbox`，使用独立临时 profile。

### 2.2 第一组结果

DComp 路径（`--in-process-gpu`，不加 `--disable-direct-composition`）：

| 项目 | 值 |
| --- | --- |
| 构建 | `out/WinDev` 156.0.8067.0 |
| HWND | **4526636**（900×700，`visible=True`、`GetForegroundWindow()==HWND`） |
| renderer 参考 | CDP 彩色 3×3 色块/文字正常 |
| 窗口表面采样 | `PrintWindow` 与 `CopyFromScreen` 各 9 点均为 `RGB≈31,32,35`（7 个近黑变体，整体黑） |
| 判定 | **DComp 路径黑屏复现**；不是 renderer 没内容 |

`--disable-direct-composition` 路径：

| 项目 | 值 |
| --- | --- |
| renderer 参考 | CDP 彩色 3×3 色块/文字正常 |
| visibility | `Page.setWebLifecycleState("active")` 后为 `visible` |
| 窗口表面采样 | 同一 HWND（12587316）`PrintWindow`/屏幕均返回白色，**不是彩色页面**；仍不能作为通过判据 |
| 判定 | 尚未取得非遮挡的彩色屏幕帧；不能把白色/空白当 PASS |

### 2.3 当前结论与下一步

- 已确认 DComp 路径的**窗口表面**是黑的，而 renderer/CDP 内容是彩色的；问题在
  DComp/SwapChain/Present/Commit 或窗口 surface 绑定，不在 HTML 内容。
- `--disable-direct-composition` 的屏幕比较仍受测试环境窗口遮挡影响；下一轮改用
  DWM thumbnail / Windows Graphics Capture（或专用可见桌面）取得非遮挡帧。
- 本轮 trace：stock Chrome 的 `gpu,viz,cc` trace 可采到事件；Browser CLI 的
  CDP `Tracing.start` 当前返回 0 事件，需改用命令行 `--trace-startup` 或检查
  in-process GPU 的 tracing 初始化。
- 还没有拿到 Surface ID / `DCLayerTree::CommitAndClearPendingOverlays` / `DCompPresenter::Present`
  的 Browser CLI 事件；这是下一轮 GPU-B 的第一优先级。
- 修复必须同时回归后台遮挡截图（patch 027 的 copy/activation 路径），不能在修 DComp
  时让隐藏页截图重新变旧。

实验产物：`D:\c\logs\gpu-b-dcomp-2026-10-31T04-23-49-009Z`（HWND 4526636，黑）、`D:\c\logs\gpu-b-nodcomp-2026-10-31T04-24-22-096Z`（HWND 12587316，白）；
本文件只记录结论和证据路径。

---

## 3. GPU-B 更新（2026-10-31，WGC trusted A/B）

后续轮次见 [windows-gpu-dcomp-2026-10-31.md](windows-gpu-dcomp-2026-10-31.md)。要点：

- 用 Windows Graphics Capture 在窗口被遮挡、`foreground=false` 时取得可信 A/B：
  DComp 路径 9 点 `#1F2023` 黑，`--disable-direct-composition` 9 点 `#FFFFFF` 白；
  CDP `fromSurface=true` 始终彩色。**白不是内容**，原 workaround 不成立。
- 官方 Chrome 156 加 `--in-process-gpu` 复现**逐字节相同**的黑表面
  （WGC hash `14864142134207728000`），证明不是 Browser CLI 窗口 patch。
- 根因：`GpuInit::InitializeInProcess()` 未调用 `gl::InitializeDirectComposition()`，
  `DirectCompositionSupported()` 恒 false，`DCompPresenter` 无法创建；trace 中
  `DCompPresenter::Present`/`DCLayerTree::CommitAndClearPendingOverlays` 为 0。
- 提议补丁：`tests/windows-gpu-dcomp/proposed/0001-gpu-init-inprocess-direct-composition.patch`
  （未构建验证）。候选 workaround：强制 Graphite Dawn D3D12 swap chain，实测彩色，
  但仍标注为 workaround。
- patch 027 回归：上述所有路径的 `cdp_hidden`/`cdp_bottom` 仍彩色。

