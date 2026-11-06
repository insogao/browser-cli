# Windows TSF / 搜狗 `ExitProcess(3)` 根因报告（ACG + Xbyak 阶段）

日期：2026-10-31
范围：001–033 构建（Chromium 156.0.8067.0，`out/WinDev`，component，非 official）
分支：`agent/tsf-exit`（worktree `D:\work\wt\bc-tsf-exit`），基点 `dbb560f`
状态：**根因已定位、因果已用对照实验证明；本分支未修任何产品代码，不声称已修复。**
前序报告：`docs/tsf-sogou-investigation-2026-10-28.md`（F1–F18 / H5-A）

---

## 0. 一句话结论

Browser CLI 的 Windows 构建**不是 Google 品牌**，因此在 Chromium 里
`ShouldUseFieldTrialTestingConfig()` 对非 Chrome 品牌构建**默认返回 true**，
自动应用了 `testing/variations/fieldtrial_testing_config.json`；该文件在 Windows 上启用
`BrowserDynamicCodeDisabled`。结果：浏览器进程在 TSF 初始化**之前**被 Chromium 自己设上
**ACG（Arbitrary Code Guard，禁止动态代码）**（`ProcessDynamicCodePolicy`，
`MITIGATION_DYNAMIC_CODE_DISABLE_WITH_OPT_OUT`）。

随后 TSF 初始化时，搜狗 TIP 的 `ActivateEx` → `SogouPY!ImeInquire` → 搜狗内置的
神经网络/JIT 运行时（`uintel::xnet` + Xbyak）对一块 32 KB 的 JIT 缓冲调用
`VirtualProtect(..., PAGE_EXECUTE_READWRITE)`。该调用被 ACG 拒绝
（Win32 `0x677 ERROR_DYNAMIC_CODE_BLOCKED` / NTSTATUS `0xC0000604`），
Xbyak 于是抛出 `Xbyak::Error(ERR_CANT_PROTECT = 14)`；**搜狗自己的 C++ 异常处理器**
捕获后调用 `ExitProcess(3)`。

官方 Chrome 156 / Edge 156 不应用该测试配置（Chrome 是 Google 品牌；Edge 实测同样没有 ACG），
因此同一条搜狗调用成功，TIP 激活正常返回。

> 这不是权限、签名、产品名、沙箱令牌或线程数问题。唯一的开关是
> **浏览器进程是否带 ACG**，而 ACG 来自一个**测试专用**的 field-trial 配置。

---

## 1. 环境与方法

| 项 | 值 |
| --- | --- |
| 被测构建 | `D:\c\src\out\WinDev\chrome.exe` / `chrome.dll`，Chromium 156.0.8067.0 |
| 构建形态 | `is_component_build=true`、`is_debug=false`、`symbol_level=0`（本地仍有 PDB）、`is_official_build=false`、非 Google 品牌 |
| 对比宿主 | Google Chrome 156.0.8078.48；Microsoft Edge 156.0.4314.50 |
| 调试器 | cdb 10.0.28000.2705，`-G -cf <script>`，本地符号缓存 `C:\ProgramData\dbg\sym` |
| 启动方式 | `--headless=new --user-data-dir=<独立临时目录> --no-first-run --no-default-browser-check about:blank` |
| 约束 | 未用 `--no-sandbox`；未改动 `D:\c\src`；只在我们自己启动的测试进程上设断点/终止；未改 Sogou DLL、未拦截 `ExitProcess`、未关闭 TSF |
| 原始日志 | `D:\c\logs\tsfexit\`（文件名见各节；cdb 脚本同目录） |

所有计数/状态均在 `TSFBridge::Initialize` 入口或首个 `e06d7363` 处冻结取得；每个配置至少一次运行，
关键结论均有第二次独立运行或对照运行支持。

---

## 2. 第一个 C++ 异常（任务 1）

### 2.1 断点与命中

脚本：`D:\c\logs\tsfexit\exc-c.cdb`（在 `KERNELBASE!RaiseException` 上按 `@rcx==0xe06d7363` 过滤，
同时 `sxe eh` 记录 first-chance，`bp ntdll!RtlExitUserProcess` 记录退出栈）。
日志：`D:\c\logs\tsfexit\exc-c_81d4_2026-10-31_13-46-18-416.log`。

- 线程：`CrBrowserMain`（浏览器主线程）。
- 该进程内 **`e06d7363` 只出现一次**（脚本命令回显之外的 `==CPP-EH-RAISE==` 仅 1 次；
  `(pid.tid): C++ EH exception - code e06d7363 (first chance)` 仅 1 次）。
- **first-chance，无 second-chance**：cdb 只报告 `(first chance)`；日志中
  `(second chance)` 出现 0 次。异常被搜狗自己的处理器消费，进程随即经
  `ExitProcess(3)` 结束，因此该异常不会以 second-chance 形式再次上报。

> 说明：在该 first-chance 断点上 `.ecxr` 返回
> `Unable to get exception context, HRESULT 0x8000FFFF`（日志 `ecxr_8790_*.log`）。
> 因此本文的寄存器状态取自 `RaiseException` 入口的 `r`（即抛出点上下文），
> 异常记录取自 `.exr -1`，栈取自同一次断点的 `kb`；三者一致且可交叉验证。

### 2.2 异常记录（`.exr -1`）

```text
ExceptionAddress: 00007ffb7e18408a (KERNELBASE!RaiseException+0x8a)
   ExceptionCode: e06d7363 (C++ EH exception)
  ExceptionFlags: 00000081
NumberParameters: 4
   Parameter[0]: 0000000019930520   (MSVC C++ EH magic)
   Parameter[1]: 000000cebadf7ab0   (thrown object, on stack)
   Parameter[2]: 00007ffae29ca740   (ThrowInfo, SogouPY + 0x18da740)
   Parameter[3]: 00007ffae10f0000   (SogouPY.ime image base)
```

`RaiseException` 入口处 `rdx=1`（`EXCEPTION_NONCONTINUABLE`），`rbx` 指向 ThrowInfo，
`rdi=0x19930520`，`rax=SogouPY` 基址——与 MSVC `_CxxThrowException` 的调用约定一致。

### 2.3 异常对象与类型

对象在栈上（`0x...7ab0`），内存布局：

```text
+0x00  00007ffa`e262f198    vtable（SogouPY + 0x153f198）
+0x08  00000000`00000000
+0x10  00000000`00000000
+0x18  00000000`0000000e    int err_ = 14
```

- vtable[-1] → `CompleteObjectLocator` @ `SogouPY + 0x16c5280`；
  `pTypeDescriptor` RVA `0x01919ff0` → 名称字符串：
  **`.?AVError@Xbyak@@`**，即 **`class Xbyak::Error`**。
- `ThrowInfo` 的 `pCatchableTypeArray` 有 **2** 项，类型分别是
  **`.?AVError@Xbyak@@`** 和 **`.?AVexception@std@@`**（`Xbyak::Error : std::exception`）。
- 因此对象第 4 个字段 `+0x18 = 14` 是 Xbyak 的错误码：
  **`ERR_CANT_PROTECT`（14，含义 “can't protect”）**。
  这不是靠“猜枚举”：`SogouPY.ime` 自带的 Xbyak 错误字符串表就在文件偏移
  `22273896` 起，按枚举顺序排列——`bad addressing`(1), `code is too big`(2), …,
  `label is not found`(11), … , `bad parameter`(13), **`can't protect`(14)**,
  … , `can't alloc`(22)。即 DLL 自己的字符串表第 14 项就是 “can't protect”。
  （同一区域还含 `Xbyak::CodeGenerator`、`Xbyak::CodeArray`、
  `uintel::xnet::X64JITCodeGenerator`、`ImplX64BLASGEMMPackingAJITKernel`、`OnnxModel`
  等 RTTI/字符串，说明这是搜狗的 NN/GEMM JIT 内核生成器。）

### 2.4 抛出点栈（截取）

```text
KERNELBASE!RaiseException+0x8a
SogouPY!ImeExtension+0x10b8610            <-- throw Xbyak::Error(14)
SogouPY!ImeExtension+0xa2734a             <-- VirtualProtect 返回后检查
SogouPY!ImeExtension+0xa27399
SogouPY!ImeExtension+0xa29628
...
SogouPY!ImeDestroy+0xf0
SogouPY!ImeInquire+0x19                   <-- IMM32 IME 导出入口
SogouTSF!DllRegisterServer+0xc5117        <-- SogouTSF TIP 内部（符号为最近导出）
SogouTSF!DllRegisterServer+0xc4dce
SogouTSF!DllRegisterServer+0x3ade         <-- TIP 的 ActivateEx 实现
MSCTF!CTip::Activate+0x121
MSCTF!CThreadInputMgr::_ActivateTip+0x113
MSCTF!CThreadInputMgr::ActivateInputProfile+0x372
MSCTF!CThreadInputMgr::OnActiveProfileChange+0x396
MSCTF!CThreadInputMgr::EnsureTIPsActivated+0x74
MSCTF!CThreadInputMgr::ActivateEx_P+0x16b
MSCTF!CThreadInputMgr::Activate+0x22
ui_base_ime_win!ui::TSFBridge::Initialize+0x616
ui_base_ime_init!ui::InitializeInputMethod+0x39
content!content::BrowserMainRunnerImpl::Initialize+0x23f
```

（`SogouTSF!DllRegisterServer+...`、`SogouPY!ImeDestroy+...` 是“最近导出符号 + 偏移”的
无 PDB 符号化结果，实际为内部函数；`SogouPY!ImeInquire+0x19` 表示从 `ImeInquire`
内部调用到下一层。）

### 2.5 退出点栈（同一异常，handler 内）

`exc-c_*.log` 的 `==RTLEXIT==`：

```text
ntdll!RtlExitUserProcess
KERNEL32!ExitProcessImplementation+0xb
SogouPY!ImeExtension+0x10ce3c9            <-- ExitProcess(3) 调用点（异常 0x3）
SogouPY!ImeExtension+0x10ce394
SogouPY!ImeExtension+0x10cc8f5
SogouPY!ImeExtension+0x10df33d
SogouPY!ImeExtension+0x10baf84
SogouPY!ImeExtension+0x10bbbc0
SogouPY!ImeExtension+0x10bbc2e
SogouPY!ImeExtension+0x10b7a35
ntdll!RtlpExecuteHandlerForException+0xf  <-- 正在执行搜狗自己的异常处理器
ntdll!RtlDispatchException+0x5f5
ntdll!KiUserExceptionDispatch+0x2e
KERNELBASE!RaiseException+0x8a             <-- 就是 2.2 的那次 throw
SogouPY!ImeExtension+0x10b8610
...
```

结论：**同一个 first-chance 异常**（`Xbyak::Error(14)`）在搜狗处理器中被当作致命错误，
处理器调用 `ExitProcess(3)`。外部 SEH/C++ 捕获没有介入点（前序 F9 依然成立）。

---

## 3. 抛出时 Sogou 所处的 TSF / IME 状态（任务 2）

### 3.1 TSF 调用位置

`MSCTF!CTip::Activate+0x121` 的指令（日志 `exc-a_*.log` 的 `==CTIP-CODE==`）：

```text
mov rcx,[rbp-10h]
mov rax,[rcx]
mov rax,[rax+28h]
call MSCTF!guard_dispatch_icall$...      <-- 对 TIP 对象的虚表 +0x28 的间接调用
```

`+0x28` 是 **`ITfTextInputProcessorEx::ActivateEx`** 槽位（`IUnknown` 3 槽 + `Activate` +
`Deactivate` 之后）。即：**MSCTF 正在激活当前输入配置对应的 TIP，调用其 `ActivateEx`**；
被调对象是刚加载的 `SogouTSF.ime`。

触发路径（自下而上）：

```text
BrowserMainRunnerImpl::Initialize
→ ui::InitializeInputMethod
→ TSFBridge::Initialize
→ ITfThreadMgr::Activate
→ CThreadInputMgr::ActivateEx_P / EnsureTIPsActivated
→ CThreadInputMgr::OnActiveProfileChange / ActivateInputProfile
→ CThreadInputMgr::_ActivateTip
→ CTip::Activate
→ SogouTSF TIP::ActivateEx
→ SogouPY!ImeInquire
→ 搜狗 NN/JIT 初始化
→ VirtualProtect(PAGE_EXECUTE_READWRITE) 失败
→ Xbyak::Error(14)
```

### 3.2 初始化状态（事实）

| 维度 | 抛出时的状态 | 证据 |
| --- | --- | --- |
| TSF bridge | `TSFBridgeImpl` 尚未发布；正处于 `TSFBridge::Initialize` 的 guarded 调用内 | 栈 `TSFBridge::Initialize+0x616` |
| Thread manager | `ITfThreadMgr::Activate(&client_id_)` 尚未返回（崩溃发生在它内部） | 栈；源码 `tsf_bridge.cc:221` |
| Document manager / TextStore | **尚未创建**。`InitializeDocumentMapInternal()` 在 `thread_manager_->Activate` 之后才调用 | 源码 `tsf_bridge.cc:221→227` |
| TIP | 正在激活 SogouTSF TIP，调用 `ITfTextInputProcessorEx::ActivateEx` | `CTip::Activate+0x121` + 虚表 `+0x28` |
| IMM32 侧 | SogouTSF 在 ActivateEx 内调用 `SogouPY!ImeInquire`（传统 IMM32 IME 入口），由其完成搜狗内部初始化 | 栈；`SogouPY!ImeInquire+0x19` |
| COM apartment | 浏览器 UI 线程是 **STA**：`CoInitializeEx(NULL, COINIT_APARTMENTTHREADED=0x2)`，调用点在 `content::BrowserMainRunnerImpl::Initialize+0x137`；线程池线程另有 `0x6` 的 STA 初始化 | `D:\c\logs\tsfexit\com2_79e4_*.log` |
| 线程 | `CrBrowserMain` | 同上 |

因此，抛出点**不是**文档管理器/TextStore 阶段，也**不是**键盘布局或输入配置查询阶段；
它发生在 **TIP 激活 → 搜狗 IMM32 兼容核心 `ImeInquire` → 搜狗 NN/JIT 内核生成**这一段，
此时 Chromium 的 TSF bridge 仍是半初始化状态。

> 本 revision 没有 IMM32 回退（前序 F12）。但本次问题不是“bridge 缺席”，而是 TIP 自己在
> `ActivateEx` 里 JIT 失败后主动结束宿主进程。

---

## 4. 失败的具体 API（任务 1/2 的收口）

在 `SogouPY!ImeInquire` 之后、抛出之前，共捕获 4 次 `VirtualProtect`（日志
`D:\c\logs\tsfexit\vprot2_6ef8_2026-10-31_14-08-24-125.log`）。前三次保护属性分别为
`0x4`、`0x4`、`0x2`（`PAGE_READWRITE`/`PAGE_READONLY`，不涉及执行权限），
**第四次**为：

```text
KERNELBASE!VirtualProtect
  rcx = 0000026c`ae4b0000      lpAddress   （JIT 代码缓冲）
  rdx = 00000000`00008000      dwSize      （32 KB）
  r8  = 00000000`00000040      flNewProtect = PAGE_EXECUTE_READWRITE
  r9  = 000000cf`5cbf8050      lpflOldProtect
RetAddr:
  SogouPY!ImeExtension+0xa27305
↓ 紧接着
  SogouPY!ImeExtension+0xa2734a / +0xa27399 / ... → RaiseException
```

抛出点的 `!gle`：

```text
LastErrorValue: (Win32) 0x677 (1655)   = ERROR_DYNAMIC_CODE_BLOCKED
LastStatusValue: (NTSTATUS) 0xc0000604 = STATUS_DYNAMIC_CODE_BLOCKED
```

`ERROR_DYNAMIC_CODE_BLOCKED` 的定义（Windows SDK `winerror.h`）：
“The operation was blocked as the process prohibits dynamic code generation.”
`STATUS_DYNAMIC_CODE_BLOCKED` 同理（`ntstatus.h`）。

Xbyak 的语义：`CodeArray::protect()` 在 `ProtectMemory()`（Windows 上即
`VirtualProtect`）失败时抛出 `ERR_CANT_PROTECT`（14）。这与对象里的 `err_ = 14`
以及“失败调用后立刻 throw”完全吻合。

**结论：搜狗 JIT 的 `VirtualProtect(PAGE_EXECUTE_READWRITE)` 被进程的 ACG 拒绝，
Xbyak 抛 `Error(ERR_CANT_PROTECT)`，搜狗处理器随后 `ExitProcess(3)`。**

---

## 5. ACG 是谁打开的（任务 2/4 的关键）

### 5.1 调用栈：Chromium 自己

脚本：`D:\c\logs\tsfexit\mit.cdb`；日志
`D:\c\logs\tsfexit\mit_8694_2026-10-31_13-48-59-803.log`。

```text
KERNELBASE!SetProcessMitigationPolicy
  rcx = 2                 (ProcessDynamicCodePolicy)
  rdx -> DWORD = 0x3      (ProhibitDynamicCode=1, AllowThreadOptOut=1)
chrome!sandbox::CanSetProcessMitigationsPostStartup+0x36c   (ApplyProcessMitigationsToCurrentProcess 内联)
chrome!sandbox::RatchetDownSecurityMitigations+0x43
chrome!ChromeBrowserMainPartsWin::PostEarlyInitialization+0x10b
content!content::BrowserMainLoop::EarlyInitialization+0x211
content!content::BrowserMainRunnerImpl::Initialize+0x204
```

`NtSetInformationProcess` 侧同样可见：class `0x34`（`ProcessMitigationPolicy`），
buffer `{Policy=2, Flags=3}`。

即：ACG 是 **Chromium 在 `PostEarlyInitialization` 里主动设置**的，
早于 `ui::InitializeInputMethod()` / TSF 初始化。

### 5.2 源码路径

`chrome/browser/chrome_browser_main_win.cc`：

```cpp
void MaybeBlockDynamicCodeForBrowserProcess() {            // :397
  // 单进程 / in-process GPU / --no-sandbox 时直接返回
  const char* const kUnsupportedSwitches[] = {
      switches::kSingleProcess, switches::kInProcessGPU,
      sandbox::policy::switches::kNoSandbox};
  ...
  bool block_dynamic_code =
      base::FeatureList::IsEnabled(features::kBrowserDynamicCodeDisabled);  // :411
  ...
  if (block_dynamic_code) {
    sandbox::SandboxFactory::GetBrokerServices()
        ->RatchetDownSecurityMitigations(
            sandbox::MITIGATION_DYNAMIC_CODE_DISABLE_WITH_OPT_OUT);         // :425
  }
}
```

`sandbox/win/src/process_mitigations.cc:231`（以及 `:306` 的 opt-out 变体）把
`PROCESS_MITIGATION_DYNAMIC_CODE_POLICY.ProhibitDynamicCode = true`。

### 5.3 为什么我们的构建里这个 feature 是开的

`chrome/browser/browser_features.cc:184`：

```cpp
BASE_FEATURE(kBrowserDynamicCodeDisabled, base::FEATURE_DISABLED_BY_DEFAULT);
```

默认关闭。真正的开关是 **field-trial testing config**：
`testing/variations/fieldtrial_testing_config.json:3499` 在 **windows** 平台启用
`BrowserDynamicCodeDisabled`。

而是否应用该测试配置，由
`components/variations/service/variations_field_trial_creator.cc:143`
的 `ShouldUseFieldTrialTestingConfig()` 决定：

```cpp
#if BUILDFLAG(GOOGLE_CHROME_BRANDING)
  return is_enable_switch_set;                      // 品牌构建：默认不应用
#else
  return is_enable_switch_set ||
         (!command_line->HasSwitch(kDisableFieldTrialTestingConfig) &&
          !command_line->HasSwitch(kVariationsServerURL));  // 非品牌：默认应用
#endif
```

Browser CLI 的 `GOOGLE_CHROME_BRANDING` 为 false（自定义 BRANDING），命令行也没有
`--disable-field-trial-config` / `--variations-server-url`，因此**默认应用测试配置**，
`BrowserDynamicCodeDisabled` 被打开 → 浏览器进程被设上 ACG。

### 5.4 排除其他来源（已实测）

| 可能来源 | 结论 | 证据 |
| --- | --- | --- |
| 企业策略 `DynamicCodeSettings` | **排除**：HKLM/HKCU `Software\Policies` 下没有 Chromium/Chrome/Edge 策略键，也没有 `DynamicCodeSettings` | `reg query` 枚举（本机仅有 `Policies\Microsoft\...`） |
| Variations seed（服务器实验） | **排除**：临时 profile 没有 seed；`--disable-field-trial-config`（只影响测试配置）能移除 ACG | `state_7c34_*.log` |
| 命令行 `--enable-features` | **排除**：`!peb` 显示命令行只有 headless/user-data-dir/no-first-run/about:blank | `feat2_*.log`、`feat3_*.log` |
| Sogou 自己设置 | **排除**：调用栈在 `chrome!ChromeBrowserMainPartsWin::PostEarlyInitialization`，不在 Sogou 模块 | `mit_8694_*.log` |
| 仅“非 official 构建”本身 | **不是**：`is_official_build` 不直接控制该 feature；控制点是 `GOOGLE_CHROME_BRANDING` + 测试配置 | 5.2/5.3 |

### 5.5 因果验证（4 组）

| 运行 | ACG 设置 | 首个 C++ 异常 | TIP ActivateEx | 结果 |
| --- | --- | --- | --- | --- |
| 基线（sandbox ON，独立 GPU 进程） | **是** | `Xbyak::Error(14)` | 抛出 | `ExitProcess(3)` |
| `--disable-features=BrowserDynamicCodeDisabled` | **否** | 无 | 正常返回 | 存活（`causal_90b4_*.log`） |
| `--disable-field-trial-config` | **否** | 无 | 正常返回 | 存活（`state_7c34_*.log`） |
| `--in-process-gpu` | **否** | 无 | 正常返回 | 存活（`state_8518_*.log`） |
| 对照：`--disable-features=SomeUnrelatedFeatureXYZ` | **是** | `Xbyak::Error(14)` | 抛出 | `ExitProcess(3)`（`causal_6a84_*.log`） |

后两组对照排除了“随便加一个 `--disable-features` 就好了”的偶然性：
**只有针对 `BrowserDynamicCodeDisabled` 或测试配置本身，ACG 才消失。**

---

## 6. 官方 Chrome / Edge 对照（任务 3）

统一脚本：`D:\c\logs\tsfexit\generic.cdb`
（`SetProcessMitigationPolicy` + `RaiseException(e06d7363)` + `MSCTF!CTip::Activate` 入口/返回 + 退出）。

| 宿主 | 版本 | 观察到 `SetProcessMitigationPolicy(ProcessDynamicCodePolicy)` | `SogouPY!ImeInquire` | 同一条 JIT `VirtualProtect(0x8000, 0x40)` | TIP `ActivateEx` 返回 | 首个 C++ EH |
| --- | --- | --- | --- | --- | --- | --- |
| Browser CLI | 156.0.8067.0 | **是**（`+0x3` flags） | 是 | 是（失败 `0x677`） | **未返回**（进程退出） | `Xbyak::Error(14)` |
| Google Chrome | 156.0.8078.48 | 否 | 是 | 是（同一 `SogouPY+0xa27305` 调用点，继续 JIT） | 是 | 无 |
| Microsoft Edge | 156.0.4314.50 | 否 | 是 | —（同一 TIP 路径，未逐帧抓 VPROT） | 是 | 无 |

Chrome 的日志：`D:\c\logs\tsfexit\generic_5c14_2026-10-31_13-59-43-575.log`（无 `==SETMIT==`，
`==CTIP-ENTER==` → `==CTIP-RETURNED==`）；
`D:\c\logs\tsfexit\vprot2_802c_2026-10-31_14-08-38-341.log` 显示 Chrome 在
`SogouPY!ImeExtension+0xa27305` 处发出**相同**的
`VirtualProtect(0x2e9d0000, 0x8000, PAGE_EXECUTE_READWRITE)`，随后继续第二次 JIT，
最后 TIP 正常返回，**没有** `e06d7363`。

Edge 的日志：`D:\c\logs\tsfexit\generic_8790_2026-10-31_14-06-59-528.log`（无 `==SETMIT==`，
`==CTIP-ENTER==` → `==CTIP-RETURNED==`）。

Edge 同为非 Google 品牌却不受影响，最可能是其构建设置了
`disable_fieldtrial_testing_config=true`（或启动器传了 `--disable-field-trial-config`）；
这一点我们**没有**从 Edge 的构建配置中直接核实，只报告观测结果（无 ACG、TIP 正常）。

---

## 7. `--in-process-gpu` 的真实差异（任务 4）

### 7.1 功能差异（这才是原因）

`MaybeBlockDynamicCodeForBrowserProcess()` 的“不支持开关”列表明确包含
`--in-process-gpu`、`--single-process`、`--no-sandbox`（源码 `chrome_browser_main_win.cc:399-405`）。
因此：

- 基线：`SetProcessMitigationPolicy(ProcessDynamicCodePolicy, flags=3)` **发生**；
- `--in-process-gpu`：**不发生**（实测 `state_8518_*.log` 无 `==SETMIT==`），
  TIP 正常返回；
- `--no-sandbox`：同理（前序 E8 已证），但本轮按约束**没有**再跑 `--no-sandbox`。

**这是 `--in-process-gpu` 与基线之间唯一与 TSF 结果相关的功能差异。**
它不是“多了一个线程”，而是跳过了一整段浏览器进程安全策略设置。

### 7.2 线程/句柄计数（不是原因）

在 `TSFBridge::Initialize` 入口实测（`state.cdb`）：

| 配置 | 线程数 | 句柄总数 | Thread 句柄 | Event 句柄 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 基线 | 21 | 274 | 50 | 97 | exit 3 |
| `--in-process-gpu` | 22 | 280 | 54 | 99 | 正常 |
| `--disable-field-trial-config` | 20 | 268 | — | — | 正常（**仍是独立 GPU 进程**） |

`~*` 的线程名逐项对比：`--in-process-gpu` 只多出一个 **`ThreadPoolForegroundWorker`**
（线程池 worker 的调度数量，本身随运行波动；`--disable-field-trial-config` 运行是 20 条）。
没有新增 ALPC 端口、Section、Desktop、WindowStation 或命名对象。

**因果测试**：`--disable-field-trial-config` 保持“sandbox ON + 独立 GPU 进程 + 基线级线程/句柄拓扑”，
只是不应用测试配置，就完全不崩。因此：

> 前序 H5-A 观测到的 “+1 线程 / +7 句柄” 是 `--in-process-gpu` 的**伴随现象**，
> 不是原因；把它当作根因会误导修复方向。

### 7.3 为什么 `--single-process` 仍 exit 3（前序 3.3）

`--single-process` 同样跳过 ACG（在 unsupported 列表里），所以它本不该因 ACG 崩溃。
前序记录它 rc=3，可能是该模式自身的其它早期失败（`--single-process` 不受支持）；
本轮未重复该实验，不应把它当作 ACG 的反例。若需要，应单独用 cdb 复测其 `ExitProcess` 调用点
是否仍是 `SogouPY!ImeExtension+0x10ce3c9`。

---

## 8. 触发链总结（hypothesis → confirmed mechanism）

```text
[构建] 非 Google 品牌 (GOOGLE_CHROME_BRANDING=false)
   ↓
[启动] ShouldUseFieldTrialTestingConfig() = true（非品牌默认应用测试配置）
   ↓
[fieldtrial_testing_config.json] Windows 启用 BrowserDynamicCodeDisabled
   ↓
[PostEarlyInitialization] MaybeBlockDynamicCodeForBrowserProcess()
   → RatchetDownSecurityMitigations(MITIGATION_DYNAMIC_CODE_DISABLE_WITH_OPT_OUT)
   → ProcessDynamicCodePolicy: ProhibitDynamicCode=1, AllowThreadOptOut=1
   ↓
[TSF 初始化] InitializeInputMethod → TSFBridge::Initialize → ITfThreadMgr::Activate
   → MSCTF!CTip::Activate → SogouTSF TIP::ActivateEx
   ↓
[Sogou] SogouPY!ImeInquire → 内置 xnet/Xbyak GEMM JIT
   → VirtualProtect(jitBuf, 0x8000, PAGE_EXECUTE_READWRITE)
   → FALSE, ERROR_DYNAMIC_CODE_BLOCKED (0x677) / STATUS_DYNAMIC_CODE_BLOCKED (0xC0000604)
   ↓
[Xbyak] throw Xbyak::Error(ERR_CANT_PROTECT=14)   [first chance]
   ↓
[搜狗自己的 handler] RtlpExecuteHandlerForException → ExitProcess(3)
   ↓
[Chrome] 浏览器进程退出（rc=3，无 WER 崩溃事件）
```

官方 Chrome/Edge：品牌构建/关闭测试配置 → 不设 ACG → 同一条 `VirtualProtect` 成功 →
TIP 激活完成 → 正常。

---

## 9. 最小安全修复建议（未应用，未声称已修复）

### 9.1 推荐：构建期关闭“测试专用 field-trial 配置”

在 `chromium/args-windows.gn`（以及 `out/WinDev/args.gn`）加入一行：

```diff
--- a/chromium/args-windows.gn
+++ b/chromium/args-windows.gn
@@
 use_remoteexec = false
+# 测试专用 field-trial 配置不应驱动产品行为。非 Google 品牌构建默认会应用
+# testing/variations/fieldtrial_testing_config.json，其中 Windows 的
+# BrowserDynamicCodeDisabled 会在浏览器进程上启用 ACG，导致搜狗等第三方 IME 的
+# JIT 无法 VirtualProtect(PAGE_EXECUTE_READWRITE)，从而被搜狗 ExitProcess(3)。
+# 设为 true 后行为与官方品牌构建一致（需要测试配置的测试显式加
+# --enable-field-trial-config）。
+disable_fieldtrial_testing_config = true
```

- 该 gn arg 由 `components/variations/service/BUILD.gn:12-31` 定义，生成
  `FIELDTRIAL_TESTING_ENABLED=0`。
- **不修改任何 Chromium 源码**，不触碰 ACG 实现本身；renderer/child 的 ACG 策略不变。
- 与官方 Chrome 的默认行为一致：品牌构建本来就不应用该测试配置。
- 运行时等价验证（已做）：`--disable-field-trial-config` →
  无 ACG、TIP 正常返回（`state_7c34_*.log`）。
- 注意：若有浏览器测试依赖该测试配置里的其它 feature，需要显式加
  `--enable-field-trial-config`；这属于测试侧调整，不是产品行为。

### 9.2 备选（仅作临时/诊断，不建议作为产品默认）

- 启动参数 `--disable-features=BrowserDynamicCodeDisabled`：已实测可绕过
  （`causal_90b4_*.log`）。它直接关闭一个安全缓解，语义上不如 9.1 干净，
  只适合作为确认/临时手段。
- 不要：`--no-sandbox`、拦截 `ExitProcess`、改 Sogou DLL、关闭 TSF、
  二进制 patch、在 Chromium 里删除 ACG 代码。这些要么违反约束，要么把安全边界改坏。

### 9.3 回归/验收

1. 用 9.1 重新构建后，在 `TSFBridge::Initialize` 时刻断言浏览器进程
   `GetProcessMitigationPolicy(ProcessDynamicCodePolicy).ProhibitDynamicCode == 0`
   （或 cdb 下不再命中 `SetProcessMitigationPolicy(2, flags=3)`）。
2. 重跑 `docs/windows-ime-acceptance-runbook-2026-10-28.md` 的真实 GUI 中文输入矩阵
   （stock / managed+HUMAN / ASSIST / 地址栏 / `<input>` / `<textarea>` / contenteditable）。
3. 保留一次“有 ACG + 无搜狗 JIT”的对照诊断（例如临时加
   `--enable-features=BrowserDynamicCodeDisabled` 并用一个不做 JIT 的 TIP/输入法），
   以确认修复不是靠“绕过 TSF”。

---

## 10. 证据索引（原始日志，均在 `D:\c\logs\tsfexit\`）

| 用途 | 文件 |
| --- | --- |
| 首个 `e06d7363`：对象/RTTI/ThrowInfo/首末栈 | `exc-c_81d4_2026-10-31_13-46-18-416.log`；脚本 `exc-c.cdb` |
| CTip::Activate 入口、虚表 `+0x28` 反汇编、CTip 对象 | `exc-a_a1d8_2026-10-31_13-43-59-344.log`；脚本 `exc-a.cdb` |
| `.ecxr` 不可用（HRESULT 0x8000FFFF） | `ecxr_8790_2026-10-31_14-09-42-899.log`；脚本 `ecxr.cdb` |
| ACG 设置调用栈 + policy/flags | `mit_8694_2026-10-31_13-48-59-803.log`；脚本 `mit.cdb` |
| `--disable-features=BrowserDynamicCodeDisabled` 修复验证 | `causal_90b4_2026-10-31_13-54-26-920.log` |
| 无关 feature 对照（仍崩） | `causal_6a84_2026-10-31_13-58-44-889.log`；脚本 `causal.cdb` |
| 基线 TSF 入口：21 线程 / 274 句柄 + ACG + 崩溃 | `state_8620_2026-10-31_14-07-25-575.log`；脚本 `state.cdb` |
| `--in-process-gpu`：22 线程 / 280 句柄、无 ACG、正常 | `state_8518_2026-10-31_14-07-12-989.log` |
| `--disable-field-trial-config`：无 ACG、正常 | `state_7c34_2026-10-31_14-07-35-312.log` |
| 基线 JIT `VirtualProtect(...,0x40)` + 立即 throw | `vprot2_6ef8_2026-10-31_14-08-24-125.log`；脚本 `vprot2.cdb` |
| Chrome 同一 JIT `VirtualProtect` + TIP 返回 | `vprot2_802c_2026-10-31_14-08-38-341.log` |
| Chrome：无 ACG、TIP 返回 | `generic_5c14_2026-10-31_13-59-43-575.log`；脚本 `generic.cdb` |
| Edge：无 ACG、TIP 返回 | `generic_8790_2026-10-31_14-06-59-528.log` |
| COM apartment（UI 线程 `0x2` STA） | `com2_79e4_2026-10-31_14-11-58-934.log`；脚本 `com2.cdb`（早先一次 `com_849c_*.log` 亦一致） |
| Xbyak 错误字符串表（DLL 内顺序，第 14 项 = `can't protect`） | 直接读 `C:\Windows\System32\SogouPY.ime`；对照 `xbyak.h` |

## 11. 参考源码（`D:\c\src`，只读）

- `chrome/browser/chrome_browser_main_win.cc:397-430, 661-664`
- `components/variations/service/variations_field_trial_creator.cc:143-155`
- `components/variations/service/BUILD.gn:12-31`
- `testing/variations/fieldtrial_testing_config.json:3499-3512`
- `chrome/browser/browser_features.cc:184`
- `sandbox/win/src/process_mitigations.cc:226-236, 300-312`
- `ui/base/ime/win/tsf_bridge.cc:199-258`（`thread_manager_->Activate` 在
  `InitializeDocumentMapInternal` 之前）

---

## 12. 状态与边界

- **已确认**：首个异常类型/位置、调用链、失败 API 与错误码、ACG 的设置者与来源、
  Chrome/Edge 差异、`--in-process-gpu` 的真实差异、线程/句柄非因果。
- **未做**：未重新构建验证 9.1 的 args 改动；未修改任何产品代码/配置；
  未在 Edge 构建配置中直接核实其 `disable_fieldtrial_testing_config`；
  未重跑完整 GUI 中文输入验收。
- **不声称已修复**。下一步是在一个受控的 Windows 构建上应用 9.1 并重跑
  第 9.3 节验收。

---

## 附录 A：关键复现脚本（cdb）

### A.1 首个 C++ 异常 + 退出栈（`exc-c.cdb`）

```text
.logopen /t D:\c\logs\tsfexit\exc-c.log
bp KERNELBASE!RaiseException ".if (@rcx == 0xe06d7363) { .echo ==CPP-EH-RAISE==; .echo ==ARGS==; dq @r9 L4; r @$t4=poi(@r9+0x18); .echo ==OBJ==; r @$t2=poi(@r9+0x8); dq @$t2 L6; .echo ==VTABLE-MINUS8==; r @$t5=poi(@$t2); dq @$t5-0x8 L2; r @$t6=poi(@$t5-0x8); .echo ==COL==; dq @$t6 L4; .echo ==OBJ-RTTI-NAME==; da (@$t4 + dwo(@$t6+0xc) + 0x10); .echo ==THROWINFO==; r @$t3=poi(@r9+0x10); dd @$t3 L4; .echo ==CTA==; r @$t7=@$t4+dwo(@$t3+0xc); dd @$t7 L3; .echo ==CT1==; r @$t8=@$t4+dwo(@$t7+4); dd @$t8 L7; .echo ==CT1-NAME==; da (@$t4 + dwo(@$t8+4) + 0x10); .echo ==CT2==; r @$t9=@$t4+dwo(@$t7+8); dd @$t9 L7; .echo ==CT2-NAME==; da (@$t4 + dwo(@$t9+4) + 0x10); .echo ==GLE==; !gle; .echo ==THREADS==; ~; gc } .else { gc }"
sxe -c ".echo ==EH-FIRSTCHANCE==; .exr -1; .ecxr; kb 14; gc" eh
bp ntdll!RtlExitUserProcess ".echo ==RTLEXIT==; kb 30; q"
g
```

### A.2 ACG 设置来源（`mit.cdb`）

```text
.logopen /t D:\c\logs\tsfexit\mit.log
bp KERNELBASE!SetProcessMitigationPolicy ".echo ==SETMIT==; r; kb 14; .echo ==MITBUF==; dd @rdx L4; gc"
bu ntdll!NtSetInformationProcess ".if (@rdx == 0x34) { .echo ==NTSETMIT==; r; kb 14; .echo ==MITBUF==; dd @r8 L4; gc } .else { gc }"
bu ui_base_ime_win!ui::TSFBridge::Initialize ".echo ==TSF-ENTER==; kb 4; gc"
bp KERNELBASE!RaiseException ".if (@rcx == 0xe06d7363) { .echo ==CPP-EH==; kb 8; gc } .else { gc }"
bp ntdll!RtlExitUserProcess ".echo ==RTLEXIT==; q"
g
```

### A.3 TSF 时刻状态 + 结果（`state.cdb`）

```text
.logopen /t D:\c\logs\tsfexit\state.log
bp KERNELBASE!SetProcessMitigationPolicy ".echo ==SETMIT==; kb 8; gc"
bu ui_base_ime_win!ui::TSFBridge::Initialize ".echo ==TSF-ENTER==; ~*; .echo ==HANDLES==; !handle 0 0; gc"
bp KERNELBASE!RaiseException ".if (@rcx == 0xe06d7363) { .echo ==CPP-EH==; kb 12; gc } .else { gc }"
bp MSCTF!CTip::Activate ".echo ==CTIP-ENTER==; gu; .echo ==CTIP-RETURNED==; r rax; q"
bp ntdll!RtlExitUserProcess ".echo ==RTLEXIT==; kb 20; q"
g
```

### A.4 失败 API（`vprot2.cdb`）

```text
.logopen /t D:\c\logs\tsfexit\vprot2.log
bp KERNELBASE!VirtualProtect ".if (@$t0==1) { .echo ==VPROT==; r; kb 8; gc } .else { gc }"
bu SogouPY!ImeInquire ".echo ==IMEINQUIRE==; r @$t0=1; gc"
bp KERNELBASE!RaiseException ".if (@rcx == 0xe06d7363) { .echo ==CPP-EH==; kb 8; gc } .else { gc }"
bp MSCTF!CTip::Activate ".echo ==CTIP-ENTER==; gu; .echo ==CTIP-RETURNED==; q"
bp ntdll!RtlExitUserProcess ".echo ==RTLEXIT==; kb 12; q"
g
```

### A.5 无 Chrome 专用符号的通用对照（`generic.cdb`，用于 Chrome/Edge）

```text
.logopen /t D:\c\logs\tsfexit\generic.log
bp KERNELBASE!SetProcessMitigationPolicy ".echo ==SETMIT==; .echo ==CALLER==; kb 8; gc"
bp KERNELBASE!RaiseException ".if (@rcx == 0xe06d7363) { .echo ==CPP-EH==; kb 12; gc } .else { gc }"
bp MSCTF!CTip::Activate ".echo ==CTIP-ENTER==; gu; .echo ==CTIP-RETURNED==; r rax; q"
bp ntdll!RtlExitUserProcess ".echo ==RTLEXIT==; kb 20; q"
g
```

调用方式（示例）：

```text
cdb -y "C:\ProgramData\dbg\sym" -G -cf D:\c\logs\tsfexit\<script>.cdb \
  D:\c\src\out\WinDev\chrome.exe --headless=new \
  --user-data-dir=<独立临时目录> --no-first-run --no-default-browser-check about:blank
```

注意：不要加 `-g`（它会让目标先跑起来，断点可能来不及生效）；不要加 `-o`
（避免连带调试子进程）；用 `-y` 指向本地符号缓存可避免网络符号超时。

