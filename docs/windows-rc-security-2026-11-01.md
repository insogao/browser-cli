# Windows RC 安全基线：进程缓解对照、field trial 配置与 patch 034 生命周期

状态：**Draft / 本地验证完成，不得据此宣称发布安全等价**。
日期：2026-11-01。证据根目录：`D:\c\logs\rc-sec\`（本机，不在仓库内）。
关联：`chromium/patches/034-gpu-inprocess-direct-composition.patch`、
`tests/windows-rc-security/`、`docs/windows-gpu-dcomp-2026-10-31.md`。

---

## 0. 结论摘要

1. **策略位（policy bits）层面，Browser CLI 与官方 Chrome 在 browser / GPU /
   renderer / storage utility / 默认 network utility 上逐项相同**；与 Edge 相比只发现
   Edge 自己的两处强化（renderer AppContainer、renderer force-relocate ASLR），不是
   Browser CLI 的回归。
2. **但“缓解位相同”不等于“安全等价”**。已测到的实质差异：
   - Browser CLI 构建为 **component build + 未签名**：browser 进程加载 631 个模块
     （493 个来自安装目录），renderer 513 个（495 个来自安装目录）；官方 Chrome 为
     141/5 与 20/3，官方 Edge 为 164/8 与 21/4。主 `chrome.dll` 为 `NotSigned`，
     Chrome/Edge 的 exe 为 `Valid`。
   - component build 下 `chrome_content_browser_client.cc` 的 **启动前 CIG**
     整段被 `#if !defined(COMPONENT_BUILD)` 排除；renderer 仍通过 delayed mitigation
     得到 `MicrosoftSignedOnly=1`，但 component DLL 在进程启动期加载，启动前 CIG 缺失；
     模块加载与 CIG 生效的精确时序未测（逐模块签名亦未验证）。该差异是**时序/覆盖
     范围**差异，`GetProcessMitigationPolicy` 的稳态 bit 看不出来。
   - `--in-process-gpu` 不是安全等价的 fallback：GPU 工作进入 browser 进程后，进程
     仍是 Medium IL、无 CIG/ACG、无 Win32k/extension-point/child-process/font/image-load
     限制，且加载 648 个模块；独立 GPU 进程则是 Low IL + CIG + 上述限制。正常模式使用
     独立 GPU 进程，因此这是 fallback 风险，不是当前正常路径。
3. **`disable_fieldtrial_testing_config=true` 的真实效果已量化**：该 build 的
   `FIELDTRIAL_TESTING_ENABLED=0`。测试配置里只有 **2 个** Windows 安全相关 study：
   `BrowserDynamicCodeDisabled`（browser ACG）与 `NetworkServiceSandboxWindows`
   （network service sandbox）。显式强制两者时测得：
   - browser ACG raw `0x3`（ProhibitDynamicCode + AllowThreadOptOut）在 123–329 ms 内
     出现；本机 Sogou TSF 随后以 **exit code 3** 终止 browser（与
     `chromium/args-windows.gn` 记录的机制一致）。
   - network utility 从“未沙箱、Medium IL、无策略”变为 **Low IL + AppContainer=1 +
     ACG/CIG/ExtPt/Child/Font/ImageLoad**，Browser CLI 与官方 Chrome 完全一致。
   因此关掉测试配置会同时关掉 **browser ACG** 和 **network service sandbox** 两个真实
   安全缓解。官方 Chrome 默认（无 seed 的隔离 profile）同样两者皆关，但它在线上的
   server-side variations 可能打开两者；**本仓库无法验证 seed 后的状态，不能推断等价**。
4. **patch 034 功能 PASS、报告字段 FAIL、生命周期基本 PASS**：
   - in-process 下 DComp 已初始化：隐藏窗口出现 `Intermediate D3D Window` 子窗口，
     owner pid = browser pid；out-of-process 时 owner pid = GPU 进程。
   - 隐藏窗口 `Page.captureScreenshot(fromSurface=true)`（patch 027 copy 路径）在
     in-process 与 out-of-process 均连续彩色（4/4 帧、9–10 种采样色），窗口 12 s
     采样始终 `visible=false`。
   - 干净退出：5 次独立启动（in-process 3 次、out-of-process 2 次）+ 1 次“关闭最后一个
     tab”均为 exit 0、无 crash dump、无挂起。
   - **FAIL**：`GPUInfo.overlay_info.direct_composition` / `supportsOverlays` 在
     in-process 下为 `false/false`（out-of-process 为 `true/true`），因为
     `InitializePlatformOverlaySettings()` 在 DComp 初始化之前执行。提议 patch 035
     已写好并验证可应用（未构建、未进 manifest）。
   - **FAIL/NOT-EXERCISED**：`--single-process` 在 `extensions_client.cc:38`
     `Check failed: !g_client` 处 FATAL（0x80000003），发生在 GPU 初始化之前，patch 034
     未被触达。
   - **风险**：`InitializeDirectComposition()` 有 `CHECK(!g_dcomp_device)`；正常退出
     不调用 `ShutdownDirectComposition()`（上游 GPU 进程正常退出同样不调用）。当前
     配置下 in-process GPU 线程只初始化一次，但任何未来的“同进程 GPU 线程重启”都会
     直接 CHECK 崩溃。

**不要**把本报告的任何一行解读为“Browser CLI 与官方 Chrome 安全等价”。

---

## 1. 范围、构建与基线

### 1.1 被测对象

| 项 | 值 |
| --- | --- |
| Browser CLI 构建 | `D:\c\src\out\WinDev\chrome.exe`，Chromium 156.0.8067.0，patch 001–034，`is_component_build=true`，`is_debug=false`，`symbol_level=0`，**NotSigned** |
| 官方 Chrome | `C:\Program Files\Google\Chrome\Application\chrome.exe`，156.0.8078.48，Google Authenticode 有效 |
| 官方 Edge | `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`，156.0.4314.50，Microsoft Authenticode 有效 |
| OS | Windows 10.0.26200（Windows 11，x64），Windows PowerShell 5.1.26100 |
| patch 034 | 共享 checkout `D:\c\src` 的 `gpu/ipc/service/gpu_init.cc` 已包含（本 worktree 只读该 checkout，未修改共享源码） |

`disable_fieldtrial_testing_config=true` 是共享 build 的 `args.gn` 当前值；本 worktree
的 `chromium/args-windows.gn` **没有**该行（base `cbe40b4` 不含此改动），因此本报告把
“当前二进制实际编译值”和“仓库将采用的配置”分开记录，见 §3。

### 1.2 测试隔离

- 每个场景独立 `--user-data-dir=D:\c\logs\rc-sec\<run>\profile-*`（DComp harness 用
  `fs.mkdtempSync`，Node 的 mktemp 等价物；matrix 用每次清空的独立目录），不使用真实
  用户 profile。
- 不使用 `--no-sandbox`，不使用 `--use-mock-keychain`（Windows 无此需求），不启动任何
  “先显示再隐藏”的窗口作为通过条件。
- 只终止本次启动的根进程及其后代，且后代必须同时满足：镜像路径在本次安装目录内、
  创建时间不早于本次启动、PID 在“根进程仍存活时抓取的进程表”中。**没有**全局
  `taskkill /IM` / `Stop-Process -Name`。
- 所有 matrix 场景使用 `--headless=new`，无窗口；DComp 场景使用 Browser CLI 的
  managed presentation（窗口创建后保持 hidden），并用 12 s 采样确认 `visible=false`。
- 官方 Chrome/Edge 的 variations seed 未注入：所有“官方默认”结论都是**无 seed 的
  隔离 profile**结论。

### 1.3 方法

- `GetProcessMitigationPolicy()`：逐项查询 `PROCESS_MITIGATION_POLICY` 全部索引
  （DEP / ASLR / DynamicCode / StrictHandle / SystemCallDisable /
  MitigationOptionsMask / ExtensionPointDisable / ControlFlowGuard / Signature /
  FontDisable / ImageLoad / SystemCallFilter / PayloadRestriction / ChildProcess /
  SideChannelIsolation / UserShadowStack / RedirectionTrust / UserPointer）。
- 字段语义（贯穿全文）：
  - **APPLIED**：查询成功且 bit=1。
  - **NOT-APPLIED**：查询成功且 bit=0（策略对该进程未生效，不是查询失败）。
  - **UNAVAILABLE(n)**：查询失败（Win32 error n），值未知；**绝不当作 0**。
  - **NO-API**：OS 没有暴露该字段的查询 API（本次只有 SEHOP）。
  - `MitigationOptionsMask` 返回的是 opaque/deprecated 64-bit mask，只记录 raw。
- 令牌：`TokenIntegrityLevel` → Untrusted/Low/Medium/High/System；
  `TokenIsAppContainer` → 0/1（本报告已修复早期查询 bug，见 §2.6）。
- 镜像：主 exe 的 PE `DllCharacteristics`（DYNAMICBASE / HIGH_ENTROPY_VA / NX_COMPAT /
  GUARD_CF / FORCE_INTEGRITY）、load-config（SEH handler table、GuardFlags、
  CFG instrumented）、Authenticode 状态。
- 模块面：`Process.Modules` 的模块总数、来自安装目录的模块数。
- GPU/overlay：CDP `SystemInfo.getInfo` 的 `gpu.auxAttributes`
  （`directComposition`、`supportsOverlays`、`inProcessGpu` 等）；
  `SystemInfo.getProcessInfo` 的进程类型/PID。
- 窗口/DComp：真实 HWND 枚举（`EnumWindows`/`EnumChildWindows`）、
  `Intermediate D3D Window` 的 owner pid、可见性、12 s 采样；隐藏窗口 CDP
  `Page.captureScreenshot(fromSurface=true)` + PNG 采样（patch 027 copy 路径）。

---

## 2. 进程缓解对照

### 2.1 默认场景总表（headless，无 variations seed）

以下全部来自 `D:\c\logs\rc-sec\summary.json` 与各场景 `mitigations.json`。
`ACG/CIG/...` 均为 `GetProcessMitigationPolicy` 实测值；`—` 表示该场景没有该进程。

| 进程（场景/PID） | IL | AC | ACG | CIG | CFG | DEP | ASLR-BU | ASLR-RELOC | ASLR-HI | EXT-PT | CHILD | FONT | IMG-NOREMOTE | IMG-NOLOW | WIN32K | SSP | 模块/安装目录 | 签名 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BCLI browser (bcli-default/32512) | Medium | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 631/493 | NotSigned |
| Chrome browser (chrome-default/40616) | Medium | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 141/5 | Valid |
| Edge browser (edge-default/11388) | Medium | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 164/8 | Valid |
| BCLI GPU (bcli-default/2800) | Low | 0 | 0 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 559/497 | NotSigned |
| Chrome GPU (chrome-default/34868) | Low | 0 | 0 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 69/6 | Valid |
| Edge GPU (edge-default/24204) | Low | 0 | 0 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 70/4 | Valid |
| BCLI renderer (bcli-default/17868) | Untrusted | 0 | 0 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 513/495 | NotSigned |
| Chrome renderer (chrome-default/3412) | Untrusted | 0 | 0 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 20/3 | Valid |
| Edge renderer (edge-default/25176) | Untrusted | **1** | 0 | 1 | 1 | 1 | 1 | **1** | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 21/4 | Valid |
| BCLI network util (bcli-default/30816) | Medium | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 538/495 | NotSigned |
| Chrome network util (chrome-default/36504) | Medium | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 44/3 | Valid |
| Edge network util (edge-default/24132) | Medium | 0 | 0 | 0 | 1 | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 45/3 | Valid |
| BCLI storage util (bcli-default/24332) | Untrusted | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 513/495 | NotSigned |
| Chrome storage util (chrome-default/37248) | Untrusted | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 19/3 | Valid |
| Edge storage util (edge-default/24560) | Untrusted | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 20/3 | Valid |

补充字段（与上表同一批快照）：`STRICT-H`（strict handle checks）在
GPU/renderer/storage（以及 sandboxed network）为 1，browser 与默认 network utility 为 0；
`SideChannelIsolation` raw=`0x1`（SmtBranchTargetIsolation）同样只出现在
GPU/renderer/storage；`SystemCallFilter` / `PayloadRestriction`（EAF/IAT/ROP）/
`RedirectionTrust` 在以上所有进程均为 0；`SSP` 为 1 的进程见上表。`UserPointer` 对所有
进程都是 `UNAVAILABLE(50, ERROR_NOT_SUPPORTED)`；`SEHOP` 为 `NO-API`（见 §2.5）。
BCLI、Chrome、Edge 在这些补充字段上也逐项一致。

### 2.2 network service sandbox 打开后的总表

`--enable-features=NetworkServiceSandbox`（默认 `FEATURE_DISABLED_BY_DEFAULT`，
见 `sandbox/policy/features.cc:20`）：

| 进程（场景/PID） | IL | AC | ACG | CIG | EXT-PT | CHILD | FONT | IMG-NOREMOTE | WIN32K | ASLR-RELOC | 模块/安装目录 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BCLI network (bcli-netsandbox/40052) | Low | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 532/495 |
| Chrome network (chrome-netsandbox/32212) | Low | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 0 | 1 | 39/3 |

两列策略位逐项一致。**默认场景下 network utility 未沙箱**（Medium IL、无策略、
`--service-sandbox-type=none`），三者（BCLI/Chrome/Edge）都一样，原因是该 sandbox 由
field trial / feature 控制且默认关闭，而不是 Browser CLI 独有回归。

### 2.3 in-process GPU（fallback）的缓解面

`bcli-inproc`：browser PID 25580，**没有 GPU 进程**；renderer 25860 / network 4212 /
storage 32468 的策略与默认场景相同。browser 进程本身的值为：

| IL | AC | ACG | CIG | CFG | DEP | ASLR-BU/RELOC/HI | EXT-PT | CHILD | FONT | IMG | WIN32K | SSP | 模块 | 签名 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Medium | 0 | 0 | 0 | 1 | 1 | 1/0/1 | 0 | 0 | 0 | 0 | 0 | 1 | 648 | NotSigned |

对比独立 GPU 进程（Low IL、CIG=1、EXT-PT/CHILD/FONT/IMG=1、Win32k=0），in-process
把 GPU 解析器/驱动攻击面放进了限制更少的 browser 进程。**正常模式使用独立 GPU 进程**
（见任务背景），因此这是 `--in-process-gpu` / `--single-process` fallback 的风险，
patch 034 只修复这些 fallback 的呈现，不改变该安全事实。

### 2.4 与官方 Chrome/Edge 的逐项判断

**相同（实测）**：browser、GPU、renderer、storage utility、默认 network utility 的
ACG/CIG/CFG/DEP/ASLR-BU/ASLR-HI/EXT-PT/CHILD/FONT/IMG-NOREMOTE/IMG-NOLOW/WIN32K/SSP
全部相同；network sandbox 打开后也全部相同。没有发现 Browser CLI 因 patch 001–034
导致某个策略位缺失。

**不同（实测）**：

1. **签名**：Browser CLI `chrome.exe` 与 `chrome.dll` 均 `NotSigned`；Chrome/Edge exe
   `Valid`（Google/Microsoft）。影响 SmartScreen/AppLocker/EDR 信誉、基于发布者的
   CIG 白名单、企业策略，不可用进程缓解位表达。
2. **模块面（component build）**：browser 631 vs 141/164；renderer 513 vs 20/21；
   安装目录模块 493/495 vs 5/3、8/4。主 `chrome.dll` 已确认 `NotSigned`，但 495 个
   安装目录模块未逐个验签；CIG=1 的稳态值与官方 Chrome 相同，差异在启动前 CIG 缺失与
   模块加载时序（NOT-TESTED）。
3. **Edge 独有强化**：renderer `AppContainer=1`（Chrome/BCLI 为 0）与
   `ASLR-RELOC=1`（Chrome/BCLI 为 0）。这是 Edge 比 Chrome/BCLI 更严，不是 BCLI
   相对 Chrome 的回归。
4. **CIG 时序（代码级）**：`chrome/browser/chrome_content_browser_client.cc:5384`
   的启动前 CIG 整段在 `#if !defined(COMPONENT_BUILD)` 内；Browser CLI 是 component
   build，只有 `sandbox/policy/win/sandbox_win.cc:505` 的 delayed
   `MITIGATION_FORCE_MS_SIGNED_BINS`。官方 Chrome（非 component）两者都有。该差异
   无法用 `GetProcessMitigationPolicy` 的稳态 bit 区分，标为 **code-verified,
   runtime-timing NOT-TESTED**。
5. **in-process GPU**：见 §2.3。

### 2.5 不可用 / 不可比字段

| 字段 | 状态 | 说明 |
| --- | --- | --- |
| SEHOP | **NO-API** | `GetProcessMitigationPolicy` 无 SEHOP 项。x64 镜像 load-config 的 SEH handler table 为 0（`img-sehop=false`），不能证明启用或未启用。Chromium 对沙箱子进程使用 pre-start `MITIGATION_SEHOP`；实际值本次**未测**。 |
| `ProcessUserPointer` | **UNAVAILABLE(50)** | 本 OS build 返回 ERROR_NOT_SUPPORTED；不是“未应用”。 |
| `ProcessMitigationOptionsMask` | **opaque** | 查询成功（如 browser raw `0x33333303`），但该 64-bit mask 已 deprecated，未解码，不作为结论。 |
| `ProcessSystemCallFilter` | raw 0 | 未解码，未宣称。 |
| 逐模块签名 | **NOT-TESTED** | 只验证了 exe 与主 `chrome.dll`；495 个安装目录模块未逐个校验签名。 |
| AppContainer（早期） | **已修复** | 首版 C# 在完整性级别分支提前 `return`，AC 恒为 -1；已改为同一 token 上继续查询 `TokenIsAppContainer`，最终数据见 §2.1（BCLI/Chrome renderer=0，Edge renderer=1，sandboxed network=1）。 |

### 2.6 复现与工具

- 采集：`tests/windows-rc-security/harness/process-mitigations.ps1`
- 矩阵：`tests/windows-rc-security/harness/run-mitigation-matrix.ps1`
  （11 场景：bcli-default / bcli-inproc / bcli-single / bcli-dynacode /
  bcli-testconfig / bcli-netsandbox / chrome-default / chrome-dynacode /
  chrome-testconfig / chrome-netsandbox / edge-default）
- 汇总：`tests/windows-rc-security/harness/summarize-mitigations.py`
  （输出 `D:\c\logs\rc-sec\summary.json` / `summary.md`）

---

## 3. `disable_fieldtrial_testing_config=true` 的显式验证

### 3.1 编译期证据

| 证据 | 值 |
| --- | --- |
| `D:\c\src\out\WinDev\args.gn` | `disable_fieldtrial_testing_config = true` |
| `gen/components/variations/service/buildflags.h` | `BUILDFLAG_INTERNAL_FIELDTRIAL_TESTING_ENABLED() (0)` |
| 代码路径 | `components/variations/service/variations_field_trial_creator.cc:329-345`：`#else` 分支只接受 `--enable-field-trial-config` 并调用 `ExitWithMessage`；不再应用配置 |
| 生效条件 | `components/variations/service/BUILD.gn:26`：`force_enable || (!disable && !(is_android && is_chrome_branded))` |

结论：当前二进制**完全不包含**测试 field trial 配置；`--enable-field-trial-config`
不会启用任何 study。

### 3.2 测试配置中的 Windows 安全 study

对 `testing/variations/fieldtrial_testing_config.json` 做结构化过滤（622 个含 Windows
的 group），命中“会改变 OS 进程缓解策略”的 feature 恰好 2 个：

| study | experiment | feature | 影响 |
| --- | --- | --- | --- |
| `BrowserDynamicCodeDisabled` | `Enabled` | `BrowserDynamicCodeDisabled` | `chrome_browser_main_win.cc:411` → browser 进程 ACG（`MITIGATION_DYNAMIC_CODE_DISABLE_WITH_OPT_OUT`） |
| `NetworkServiceSandboxWindows` | `Enabled_20231025` | `NetworkServiceSandbox` | network utility 进入 AppContainer 沙箱，ACG/CIG/ExtPt/Child/Font/ImageLoad 全部打开 |

其余 620 个 Windows group 未做逐项安全评审；本报告只声明“**与 Windows OS 进程缓解
策略直接接线**的 study 恰好这两个”。`RendererAppContainer`、`WinSboxDisableExtensionPoints`、
`NetworkServiceCodeIntegrity`、`WinSboxStrictHandleChecks`、`WinSboxModuleTamperingProtection`
**不在**测试配置中（grep 无命中）。

### 3.3 运行时：ACG 的实际值与退出码

`tests/windows-rc-security/harness/acg-window.ps1`（10 ms 轮询，进程存活期间记录
第一个非零 raw）：

| 场景 | PID | ACG raw | 首次出现 | watch 退出码 | 最终进程退出码 |
| --- | --- | --- | --- | --- | --- |
| bcli-default | 3340 | `0x00000000` | 从未 | 0（存活） | -1（测试结束主动终止） |
| bcli `--enable-features=BrowserDynamicCodeDisabled` | 33028 | `0x00000003` | 329 ms | 3 | 3 |
| bcli `--enable-field-trial-config` | 44712 | `0x00000000` | 从未 | 1（瞬时） | **-1073741819 (0xC0000005)** |
| chrome-default | 44556 | `0x00000000` | 从未 | 0（存活） | -1 |
| chrome `--enable-features=BrowserDynamicCodeDisabled` | 30292 | `0x00000003` | 123 ms | 3 | 3 |
| chrome `--enable-field-trial-config` | 39888 | `0x00000003` | 123 ms | 3 | 3 |

`0x3` = `ProhibitDynamicCode | AllowThreadOptOut`。exit code 3 与本机
`chromium/args-windows.gn` 记录的 Sogou TSF 行为一致（ACG 下 Xbyak JIT 失败，
SogouPY 调 `ExitProcess(3)`）。

**关键对照**：

- 官方 Chrome 的 `--enable-field-trial-config` **确实应用**了测试配置：ACG 在 123 ms
  出现，随后 exit 3。
- Browser CLI 的同名 flag **不应用**配置（ACG 恒 0），但在“配置已被排除”的错误路径上
  最终以 0xC0000005 结束，而代码本意是 `puts(message); exit(1);`
  （`variations_service_client.cc:97`）。watch 观察到瞬时的退出码 1，最终状态是
  access violation；没有任何 message 输出到 stdout/stderr。**这是独立于 patch 034 的
  错误路径缺陷**，记为 FAIL（§5 第 9 行）。

### 3.4 运行时：network service sandbox 的实际值

默认（无 flag）：BCLI/Chrome/Edge 的 network utility 都是 `--service-sandbox-type=none`、
Medium IL、ACG/CIG/ExtPt/Child/Font/Img 全 0。
`--enable-features=NetworkServiceSandbox` 后：

- BCLI network (40052)：Low IL、AC=1、ACG/CIG/ExtPt/Child/Font/ImgNoRemote/ImgNoLow=1、
  WIN32K=0、ASLR-RELOC=1；
- Chrome network (32212)：逐项相同。

这证明测试配置里的 `NetworkServiceSandboxWindows` 不是“只影响测试”的开关，而是真实
安全缓解；`disable_fieldtrial_testing_config=true` 在未注入 variations seed 的 profile
里会让 network service 保持未沙箱。

### 3.5 哪些是“只是测试配置差异”，哪些是真实差异

- **只是测试配置差异**：Browser CLI 之前（非品牌 build 默认应用测试配置）会打开
  `BrowserDynamicCodeDisabled` 与 `NetworkServiceSandbox`；官方 Chrome 只在显式 flag
  或 server-side seed 下打开。关掉配置后，browser ACG 与 network sandbox 都回到
  “未应用”。
- **真实差异，不是测试配置能解释的**：签名、component build 的模块面、启动前 CIG
  缺失、in-process GPU 的 browser 进程攻击面（§2.4）。
- **仍未验证**：官方 Chrome 线上 server-side variations 是否启用上述两个 study；
  企业策略 `DynamicCodeSettings`；SmartScreen/AppLocker/EDR 行为；逐模块签名；
  network service 在真实 seed 下的 LPAC（`WinSboxNetworkServiceSandboxIsLPAC`）状态。
  因此**不能**声明“BROWSER CLI 默认与官方 Chrome 安全等价”。

---

## 4. patch 034 生命周期验证

### 4.1 代码事实

- `ui/gl/direct_composition_support.cc:710` `InitializeDirectComposition()`：
  `CHECK(!g_dcomp_device); CHECK(!GetSolidColorPoolFactoryRef());`，设置全局
  `g_dcomp_device` / `g_d3d11_device` / `g_d3d12_command_queue` / solid-color factory。
- `ui/gl/direct_composition_support.cc:825` `ShutdownDirectComposition()`：释放上述全局。
- 唯一常规调用点是 `ui/gl/init/gl_initializer_win.cc:194` 的 `ShutdownGLPlatform()`
  （经 `gl::init::ShutdownGL`）；`gpu_init.cc` 中 `ShutdownGL` 只出现在 SwiftShader
  fallback（591/661/1136/1184）和初始化失败路径，**正常进程退出不调用**。
- `GpuInit::~GpuInit()`（`gpu_init.cc:366`）只调用 `StopForceDiscreteGPU()`；
  `InProcessGpuThread::CleanUp()`（`content/gpu/in_process_gpu_thread.cc`）只 reset
  `gpu_process_`。因此 in-process 路径与上游 GPU 进程一样，把 DComp 全局留给 OS 在
  进程退出时回收；**没有显式 shutdown 配对**。
- 每窗口资源：`SkiaOutputDeviceDComp::~SkiaOutputDeviceDComp()`
  （`components/viz/service/display_embedder/skia_output_device_dcomp.cc:194`）调用
  `presenter_->DestroyDCLayerTree()`；`DCompPresenter` 是 per-output-surface 的
  refcounted 对象，析构时 `ChildWindowWin` 析构会把 `Intermediate D3D Window` 的
  `DestroyWindow` post 到 window owner thread（`ui/gl/child_window_win.cc:177`）。
  全局 DComp device 不随窗口关闭重建；per-window 资源跟随 presenter 释放。进程退出
  前的全局 device 生命周期依赖 OS 回收，**未显式 shutdown**。

**风险**：`CHECK(!g_dcomp_device)` 使得“同进程第二次 `InitializeInProcess`”直接 FATAL。
当前只有 `InProcessGpuThread::Init()` 调用它，且一个 browser 进程只创建一个
in-process GPU thread；`Browser.crashGpuProcess` 在 in-process 下会
`GpuServiceImpl::Crash()` → `ImmediateCrash`（整个 browser 退出），不会走“重启 GPU
线程再初始化”。因此该路径目前不可达，但属于未来改动必须守住的约束。

### 4.2 干净测试结果（默认不显示窗口）

证据目录：`D:\c\logs\rc-sec\dcomp-inproc-final`、`dcomp-gpuproc-final`、
`dcomp-close-tab2`、`dcomp-inproc4`、`dcomp-inproc5`、`dcomp-gpuproc2`。

| 运行 | 模式 | browser PID | GPU PID | DComp 子窗口 owner | 窗口 visible（12 s 采样） | overlay directComposition / supportsOverlays | 隐藏截图 | 退出 | crash dump |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dcomp-inproc-final | `--in-process-gpu` | 26536 | 无（同进程） | 26536 | false/false/false | **false / false** | 4/4 彩色（10/9/10/10 色） | 0 | 0 |
| dcomp-gpuproc-final | 默认 GPU 进程 | 7860 | 35824 | 35824 | false/false/false | **true / true** | 4/4 彩色（10/10/10/10 色） | 0 | 0 |
| dcomp-inproc4 | `--in-process-gpu` | 2104 | 无 | 2104 | false/false/false | false / false | 4/4 彩色 | 0 | 0 |
| dcomp-inproc5 | `--in-process-gpu` | 42960 | 无 | 42960 | false/false/false | false / false | 4/4 彩色 | 0 | 0 |
| dcomp-gpuproc2 | 默认 GPU 进程 | 45052 | 32412 | 32412 | false/false/false | true / true | 4/4 彩色 | 0 | 0 |
| dcomp-close-tab2 | `--in-process-gpu`，关闭最后一个 tab | 22764 | 无 | 进程已退出 | false（关闭前 12 s） | false / false | 4/4 彩色（10 色） | 0 | 0 |

解读：
- **PASS**：in-process 下 DComp 已初始化（隐藏窗口也有 `Intermediate D3D Window`，
  owner = browser pid），且隐藏窗口截图连续 4 帧彩色 → patch 034 的呈现修复有效，
  patch 027 copy 路径未回归。out-of-process 的 DComp 子窗口 owner 是 GPU 进程，
  两者行为对齐。
- **PASS**：三次独立 in-process 启动（2104、42960、26536）+ 两次 out-of-process 启动，
  均 exit 0、0 crash dump、无挂起；关闭最后一个 tab 也是 exit 0。
- **FAIL**：in-process 的 `GPUInfo.overlay_info` 报告 `false/false`，而同一页面
  out-of-process 报告 `true/true`。DComp 实际工作（子窗口/截图），因此这是**报告字段
  的顺序缺陷**，不是功能失败。根因：`InitializeInProcess()` 在 DComp 块之前调用
  `InitializePlatformOverlaySettings()`，而 `CollectHardwareOverlayInfo()` 读取
  `gl::DirectCompositionSupported()`；out-of-process `Initialize()` 的顺序是
  DComp → overlay（`gpu_init.cc:973` → `:979`）。
- **提议修复**：`tests/windows-rc-security/proposed/035-gpu-inprocess-overlay-info-after-dcomp.patch`
  把 `InitializePlatformOverlaySettings()` 移到 in-process DComp 块之后、
  `SetSkiaBackendType()` 之前。已在 `gpu_init.cc` 副本上 `git apply --check` 通过；
  **未构建、未改共享源码、未进 patch-manifest**，所以修复后的 overlay 值仍是
  NOT-TESTED。

### 4.3 `--single-process` 与其他复用 `InitializeInProcess` 的路径

- `InitializeInProcess()` 的唯一调用点是 `content/gpu/in_process_gpu_thread.cc:102`，
  即 `--in-process-gpu` 与 `--single-process` 共用。
- `bcli-single`（PID 33884）：1.2 s 内以 **0x80000003 STATUS_BREAKPOINT** 退出；
  `chrome.log` FATAL 为
  `extensions\common\extensions_client.cc:38] Check failed: !g_client.`，
  调用栈在 `BrowserProcessImpl::Init` → `ChromeBrowserMainParts::PreCreateThreads`。
  该崩溃发生在 GPU 初始化之前，**patch 034 未被执行**。这是上游 single-process
  限制（component build + extensions client），不是 034 引入的。
- 结果：`--single-process` **FAIL（模式本身不可用）**；patch 034 在该模式下
  **NOT-TESTED/NOT-EXERCISED**。不得用 `--single-process` 作为 fallback。

### 4.4 窗口创建/关闭循环（hygiene deviation）

早期 `dcomp-lifecycle.mjs` 使用 `Target.createTarget(newWindow:true)` 做了 3 轮
“新窗口 → 关闭”循环（`dcomp-inproc2`）。数据本身显示：窗口 1→2→1、DComp 子窗口
1→2→1、句柄数在关闭后回到基线（2471→2520→2464 等），说明 per-window DComp 资源
随窗口释放。**但该步骤让窗口出现 `visible=true`**，违反仓库“不得为测试让窗口短暂
上屏”的规则；该数据**不计入干净 PASS**，仅作为方向性证据。默认 harness 已删除该
步骤（只在显式 `--cycles` 下保留并打印警告），默认路径经过 12 s 采样确认
`visible=false`。

因此：**“窗口关闭时 DComp 子窗口/visual/swapchain 在进程仍存活时被释放” = NOT-TESTED
（干净条件）**；可用的干净证据是“关闭最后一个 tab → 整个进程 exit 0、子窗口随进程
销毁、0 crash dump”。

### 4.5 后台截图（patch 027 copy 路径）

在窗口 `visible=false`、`document.visibilityState=hidden` 的条件下，
`Page.captureScreenshot(fromSurface=true)`：

- `dcomp-inproc-final`：4/4 帧彩色，采样 unique 9–10，saturated 19–20；
- `dcomp-gpuproc-final`：4/4 帧彩色，unique 10，saturated 19；
- `dcomp-close-tab2`：关闭前 4/4 帧彩色。

**PASS**：DComp（in-process 与 GPU 进程）不会让 patch 027 的 copy-output 后台截图
回归；页面动画 tick 在隐藏时仍推进（最终 tick 23–25/47）。

遮挡（visible 但被其他窗口遮挡）场景**本轮 NOT-TESTED**：需要显示窗口，按 hygiene
规则不做；既有证据只在 `docs/windows-occluded-capture-2026-10-28.md`。

### 4.6 其余风险

1. **无显式 teardown**：in-process browser 进程退出前不调用
   `ShutdownDirectComposition()`；与上游 GPU 进程正常退出一致，依赖 OS 回收。
   若未来引入“GPU 线程重启/同进程重新初始化”，`CHECK(!g_dcomp_device)` 会 FATAL。
   建议后续给 in-process 路径增加显式 shutdown 或在 `InitializeInProcess` 前允许
   idempotent 重新初始化。
2. **D3D12 / Graphite-D3D12**：patch 034 在 `dawn_context_provider_` 存在且
   `kDCompOnD3D12` 时走 Dawn solid-color factory；该组合未在本次矩阵中单独构建/验证
   （NOT-TESTED）。`docs/windows-gpu-dcomp-2026-10-31.md` 已记录其 validation risk。
3. **overlay 信息偏低**：除了 `directComposition=false` 导致 UI/诊断误报，还可能让
   video overlay 相关决策偏保守；主平面呈现不受影响。提议 patch 035 需在
   有显示环境中回归 `chrome://gpu`、视频 overlay、HDR。
4. **in-process GPU 的安全降级**：即使 patch 034 修好呈现，`--in-process-gpu` 仍把
   GPU 攻击面放进 Medium IL browser 进程；生产不得把它当作与独立 GPU 进程等价的安全
   配置。

---

## 5. PASS / FAIL / NOT-TESTED 明细

| # | 行 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | browser 进程缓解枚举（BCLI/Chrome/Edge） | **PASS** | §2.1；`summary.json` |
| 2 | GPU 进程缓解枚举 | **PASS** | §2.1，GPU 行 |
| 3 | renderer 进程缓解枚举 | **PASS** | §2.1，renderer 行 |
| 4 | utility（network/storage）缓解枚举 | **PASS** | §2.1 + §2.2 |
| 5 | `--in-process-gpu` 复用路径缓解枚举 | **PASS** | §2.3，browser 25580，无 GPU 进程 |
| 6 | 与官方 Chrome 策略位逐项对照 | **PASS**（策略位相同；构建面不同） | §2.4 |
| 7 | 与官方 Edge 策略位逐项对照 | **PASS**（Edge 独有 AppContainer/RELOC） | §2.4 |
| 8 | SEHOP 有效值 | **NOT-TESTED / NO-API** | §2.5；x64 `img-sehop=false`，不能判定 |
| 9 | `--enable-field-trial-config` 在 BCLI 上被干净拒绝 | **FAIL** | ACG 恒 0；瞬时 exit 1，最终 0xC0000005，无 message（acg-window run，PID 44712） |
| 10 | 官方 Chrome 的 `--enable-field-trial-config` 应用测试配置 | **PASS** | ACG `0x3` @123 ms，exit 3（PID 39888） |
| 11 | `BrowserDynamicCodeDisabled` 实际打开 browser ACG | **PASS** | BCLI `0x3` @329 ms（33028）；Chrome `0x3` @123 ms（30292） |
| 12 | `NetworkServiceSandbox` 实际打开 network 沙箱 | **PASS** | BCLI 40052 / Chrome 32212：Low IL、AC=1、全策略 |
| 13 | `disable_fieldtrial_testing_config=true` 的编译期生效 | **PASS** | buildflag 0；`args.gn` 行；§3.1 |
| 14 | 测试配置中“安全相关 study 只有两个” | **PASS**（限定于 OS 缓解接线） | §3.2；其余 620 group 未逐项安全评审 |
| 15 | 官方 Chrome server-side variations 等价性 | **NOT-TESTED** | 无 seed；§3.5 |
| 16 | patch 034 in-process DComp 初始化 | **PASS** | 隐藏窗口 DComp 子窗口 owner=26536 等，截图彩色 |
| 17 | patch 034 Init/Shutdown 配对 | **PASS（按上游惯例）/ 风险** | 无显式 shutdown；exit 0、0 crash；§4.1 |
| 18 | `GPUInfo.overlay_info.direct_composition` 顺序 | **FAIL** | in-process false/false vs out-of-process true/true；提议 035 |
| 19 | overlay 修复后的值 | **NOT-TESTED** | patch 035 仅 `git apply --check`，未构建 |
| 20 | `--single-process` + patch 034 | **FAIL（模式）** | 0x80000003，`extensions_client.cc:38`；034 未触达 |
| 21 | 进程重启（两次独立启动） | **PASS** | inproc 2104/42960/26536，均 exit 0 |
| 22 | 关闭最后一个 tab 的退出 | **PASS** | dcomp-close-tab2 exit 0，0 crash |
| 23 | per-window DComp 释放（进程存活时） | **NOT-TESTED（干净）** | 仅 deviation 数据；默认 harness 不再创建窗口 |
| 24 | 同一进程二次 `InitializeInProcess` | **NOT-TESTED** | 当前不可达；`CHECK(!g_dcomp_device)` 风险 |
| 25 | 隐藏窗口后台截图（patch 027） | **PASS** | inproc/gpuproc 各 4/4 彩色；12 s hidden |
| 26 | 遮挡窗口后台截图 | **NOT-TESTED** | 需要显示窗口；既有 10-28 证据未复跑 |
| 27 | 无泄漏/无残留状态（进程退出） | **PASS** | 5 次独立启动 + 1 次关最后一个 tab，均 exit 0、0 crash dump、无挂起 |
| 28 | 测试过程不上屏（默认路径） | **PASS** | 12 s 采样 visible=false；§4.2 |
| 29 | 测试过程不上屏（历史 cycles 步骤） | **DEVIATION / 已修复** | `dcomp-inproc2` 窗口 visible=true；已移除默认步骤 |
| 30 | 逐模块签名 | **NOT-TESTED** | 只验证 exe 与 chrome.dll |

---

## 6. Hygiene deviation 与处置

- **事实**：2026-11-01 的 `dcomp-inproc2` 运行在
  `Target.createTarget(newWindow:true)` 的 3 轮窗口循环中让 Browser CLI 窗口
  `visible=true`（toplevel HWND 1972696/2365912/2562520 等）。harness 没有调用
  `ShowWindow`，是 CDP 新建窗口导致；但这仍属于“测试窗口上屏”，按仓库规则必须
  fail closed、报告并停止。
- **处置**：
  1. 默认 harness 删除窗口循环，改为两次独立进程启动覆盖 restart；
  2. 默认路径不创建任何 target，不关闭/打开新窗口，只对已存在的隐藏页面执行
     `Page.captureScreenshot`；
  3. 12 s 可见性采样确认 toplevel `visible=false`（inproc4/5/final、gpuproc2/final）；
  4. 关闭最后一个 tab 的路径直接让进程退出，不创建新窗口；
  5. `--cycles` 仅保留为显式 opt-in，并打印 hygiene 警告；本次之后未再运行。
- `dcomp-inproc2` 的窗口计数/句柄数据只作为方向性证据，**不计入 PASS**。

---

## 7. 复现命令

```powershell
# 缓解矩阵（11 场景，约 3-4 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File `
  tests/windows-rc-security/harness/run-mitigation-matrix.ps1 `
  -OutRoot D:\c\logs\rc-sec

python tests/windows-rc-security/harness/summarize-mitigations.py `
  D:\c\logs\rc-sec --json D:\c\logs\rc-sec\summary.json

# ACG / field trial 两 study 的实际效果
powershell -NoProfile -ExecutionPolicy Bypass -File `
  tests/windows-rc-security/harness/acg-window.ps1

# patch 034 隐藏窗口生命周期（in-process + out-of-process + 第二次启动）
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs --mode inproc  --root D:\c\logs\rc-sec\dcomp-inproc-final
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs --mode gpuproc --root D:\c\logs\rc-sec\dcomp-gpuproc-final
node tests/windows-rc-security/harness/dcomp-lifecycle.mjs --mode inproc --close-tab --root D:\c\logs\rc-sec\dcomp-close-tab
```

### 7.1 静态门禁

- `tests/check-browser-launch-hygiene.sh`：**OK**（18 个入口）。两个 Windows-only
  `.mjs` harness（本目录与 `tests/windows-gpu-dcomp/harness/gpu-dcomp-ab.mjs`）显式声明
  `browser-launch-hygiene: windows-only`（Windows 无 login keychain），profile 改为
  `fs.mkdtempSync`（Node 的 mktemp 等价物），不再固定复用 `profile/` 路径。
- `tests/run-script-tests.sh`：本机 **FAIL（162 passed / 60 failed）**，但 60 项全部是
  `python3` 被解析到 Microsoft Store stub（rc=49，“Python was not found”），没有任何
  失败提到本目录；需要装有 `python3` 的 macOS/CI 环境复跑。本机记为 **NOT-TESTED**，
  不把环境缺失误报为本改动的失败。
- `tests/windows-rc-security/proposed/035-...patch`：在 `gpu_init.cc` 副本上
  `git apply --check` **OK**；未构建（共享 checkout 不改）。

---

## 8. 结论（Draft）

- 进程缓解**策略位**层面，Browser CLI 在本次测到的 browser/GPU/renderer/utility 上
  与官方 Chrome 一致；与 Edge 的差异是 Edge 更严（AppContainer + force-relocate）。
- 真正的安全差距在**构建形态**（未签名、component build 的模块面、启动前 CIG 缺失）
  和 **variations/field trial 配置**，而不是 patch 001–034 的进程策略。
- `disable_fieldtrial_testing_config=true` 同时移除了 browser ACG 与 network service
  sandbox 两个真实缓解；是否与官方 Chrome 线上等价**未验证**，不得宣称等价。
- patch 034 的呈现修复有效（隐藏窗口 DComp + 后台截图 + 干净退出），但 overlay 报告
  顺序是明确 FAIL，需落地提议 patch 035 并回归；`--single-process` 不可用；
  per-window 释放与二次初始化在干净条件下仍未测。
- **保持 Draft，不做发布安全声明。**
