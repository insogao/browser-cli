# tests/windows-rc-security

Windows RC 安全基线验证：进程缓解策略对照、`disable_fieldtrial_testing_config`
影响、patch 034（in-process DirectComposition）生命周期与后台截图回归。

**这些是诊断/验证工具，不是产品修复。** 默认不启动可见窗口，不使用
`--no-sandbox`，不复用用户 profile，不全局杀进程；只终止本次启动的进程树。
结论与逐项 PASS/FAIL/NOT-TESTED 见
`docs/windows-rc-security-2026-11-01.md`。

## 目录

- `harness/process-mitigations.ps1`
  对给定 PID/进程名读取全部 `PROCESS_MITIGATION_POLICY`（`GetProcessMitigationPolicy`）、
  令牌完整性级别/AppContainer、句柄与 GDI/USER 计数、主 exe 的 PE
  `DllCharacteristics`/load-config/CFG/SEHOP 元数据、Authenticode、模块数与安装目录
  模块数。查询失败记为 `UNAVAILABLE(Win32 error)`，与“策略未应用（bit=0）”严格区分。
  `-QuickCounters` 只取计数，用于生命周期泄漏检查。
- `harness/run-mitigation-matrix.ps1`
  以隔离 profile、本地 HTTP 页、`--headless=new` 启动 Browser CLI / 官方 Chrome /
  官方 Edge 的 11 个场景，抓取每个进程的缓解快照。场景包含默认、`--in-process-gpu`、
  `--single-process`、`--enable-features=BrowserDynamicCodeDisabled`、
  `--enable-field-trial-config`、`--enable-features=NetworkServiceSandbox`。
- `harness/summarize-mitigations.py`
  把矩阵输出汇总为“每进程类一行”的 Markdown/JSON；缺失快照（早期退出）显式标注。
- `harness/acg-window.ps1`
  以 10 ms 轮询 `ProcessDynamicCodePolicy`，在进程退出前捕获 ACG 的实际值。
  用于本机 Sogou TSF 在 ACG 下 `ExitProcess(3)` 的短命进程。
- `harness/dcomp-lifecycle.mjs`
  patch 034 生命周期：隐藏窗口下的 in-process/GPU 进程 DComp 子窗口、`GPUInfo`
  overlay 字段（CDP `SystemInfo.getInfo`）、隐藏窗口
  `Page.captureScreenshot(fromSurface=true)`（patch 027 copy 路径）、
  `Browser.close`/关闭最后一个 tab/`WM_CLOSE` 隐藏窗口的干净退出、崩溃转储计数。默认
  不创建新窗口、不调用任何 ShowWindow。
- `harness/close-window.ps1`
  只对给定 HWND `PostMessage(WM_CLOSE)`（不 Show），用于“关闭隐藏窗口”的生命周期边界。
- `proposed/035-gpu-inprocess-overlay-info-after-dcomp.patch`
  提议补丁（未应用、未进 `chromium/patch-manifest.json`）：把
  `InitializePlatformOverlaySettings()` 移到 in-process DComp 初始化之后，修复
  `overlay_info.direct_composition=false`。该 patch 已用
  `git apply --check` 在 `gpu_init.cc` 副本上验证可应用。

## 复现

```powershell
# 1. 进程缓解矩阵（约 3-4 分钟；11 个场景）
powershell -NoProfile -ExecutionPolicy Bypass -File `
  tests/windows-rc-security/harness/run-mitigation-matrix.ps1 `
  -OutRoot D:\c\logs\rc-sec

# 2. 汇总
python tests/windows-rc-security/harness/summarize-mitigations.py `
  D:\c\logs\rc-sec --json D:\c\logs\rc-sec\summary.json

# 3. ACG 窗口（field trial 两个安全 study 的实际效果）
powershell -NoProfile -ExecutionPolicy Bypass -File `
  tests/windows-rc-security/harness/acg-window.ps1

# 4. patch 034 生命周期（隐藏窗口，两次独立启动用于 restart 证据）
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs `
  --mode inproc --root D:\c\logs\rc-sec\dcomp-inproc-final
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs `
  --mode gpuproc --root D:\c\logs\rc-sec\dcomp-gpuproc-final

# 5. 关闭最后一个 tab 的退出路径（仍不显示窗口）
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs `
  --mode inproc --close-tab --root D:\c\logs\rc-sec\dcomp-close-tab

# 6. WM_CLOSE 关闭隐藏顶层窗口的退出路径（仍不显示窗口）
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs `
  --mode inproc --close-window --root D:\c\logs\rc-sec\dcomp-close-window
```

## 安全边界与已知偏差

- 每个场景独立 `--user-data-dir=D:\c\logs\rc-sec\<run>\profile-*`（`fs.mkdtempSync`
  创建，Node 的 mktemp 等价物；PowerShell matrix 用每次清空的独立目录），不使用真实
  profile，不使用 `--no-sandbox`。
- 只停止本次启动的根进程与经镜像路径/创建时间复核的后代；不使用
  `taskkill /IM`、`Stop-Process -Name` 或任何全局 kill。
- `process-mitigations.ps1` 只读目标进程，不注入、不写内存。
- **历史偏差（2026-11-01，已修复）**：早期 `dcomp-lifecycle.mjs` 的
  `Target.createTarget(newWindow:true)` 窗口创建/关闭循环会让 Browser CLI 窗口短暂
  `visible=true`。该步骤已从默认路径移除，只在显式 `--cycles` 下保留并在运行时打印
  警告；默认路径经过 12 s 采样确认窗口始终 `visible=false`。该次数据不用于
  “干净 PASS”，详见文档的 deviation 小节。
- 官方 Chrome/Edge 的服务器端 variations seed 不在本仓库控制范围内；本目录只测
  未注入 seed 的隔离 profile。因此不能从“默认值相同”推断“线上安全等价”。
