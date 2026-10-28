# Windows TSF / 搜狗输入法兼容性调查报告（第一阶段）

日期：2026-10-28
范围：`chromium/patches/023-windows-tsf-managed-skip.patch` 及其根因
状态：**调查完成，未修改任何正式代码**
相关 PR：[browser-cli#1](https://github.com/insogao/browser-cli/pull/1)、[BrowserPilot#1](https://github.com/insogao/BrowserPilot/pull/1)

---

## 1. 结论摘要

1. **启动时 exit 3 不是 Chromium 崩溃，是搜狗主动调用了 `ExitProcess(3)`。** 已取得连续调用栈，调用点位于 `SogouPY.ime` 内部，且在它自己的 C++ 异常处理器里。
2. **与 browser-cli 的移植无关。** 未经任何修改的上游 Chromium 151 在同一台机器上产生**逐帧相同、偏移相同**的调用栈。
3. **"签名是根因"的假设已被推翻**（那是我的错误推断，仅基于相关性），而且是用零成本方式推翻的：给官方 Chrome 副本尾部追加 27 字节使 Authenticode 失效、代码一字未改，它**照样加载全部搜狗组件**。**因此不需要申请或安装任何证书。**
4. **沙箱在这里不是"权限"，而是"进程拓扑"的代理变量。** 冻结在 `TSFBridge::Initialize` 实测：浏览器主进程在沙箱 ON/OFF 下的安全上下文**逐项相同**（Medium 完整性、非 AppContainer、0 个受限 SID、同样 5 个特权、不在 Job、同样的 mitigation、TSF 前模块集仅差 `KBDUS.DLL`）。真正与结果同向变化的内部差异是：**是否存在独立的 GPU 进程**——
   - 沙箱 ON + 独立 GPU 进程 → exit 3（5/5）
   - 沙箱 ON + `--in-process-gpu` → 正常（3/3）
   - 沙箱 OFF（任意变体）→ 正常（5/5）
   - `--disable-gpu` / `--disable-gpu-sandbox` / `--disable-gpu-compositing` / `--single-process` 均无效
5. **仍未确定的一环**：在安全上下文完全相同的前提下，为什么"独立 GPU 进程 + 全局沙箱"会让搜狗在加载自身组件前放弃。第 5 节给出 H5（对比 `--in-process-gpu` 与基线的进程内部状态）与 H1（`is_official_build` 实验构建）两个候选，H5 优先。
6. **patch 023 机制有效但耦合错误**：它用一个*展示策略*开关控制*输入法*行为，并且代价是 managed 模式下**完全没有输入法**（不是降级）。第 6 节给出必须修改项。

---

## 2. 已证实的事实

| 编号 | 事实 | 证据 |
| --- | --- | --- |
| F1 | 退出由 `SogouPY.ime` 代码调用 `ExitProcess(3)` 发起 | `D:\c\logs\tsf-n1.log`，`ntdll!RtlExitUserProcess` 断点处的连续栈（见 §2.1） |
| F2 | 调用发生在**搜狗自己的异常处理期间**：栈中出现 `RtlpExecuteHandlerForException → RtlDispatchException → KiUserExceptionDispatch` | 同上 |
| F3 | 异常由 `SogouPY.ime` 抛出（`RaiseException` + `e06d7363`，`Parameter[3]` = SogouPY 基址 `0x7ffae10f0000`） | `D:\c\logs\tsf-ex.log` |
| F4 | 上游 Chromium 151 在沙箱下产生**同一偏移**的退出调用点 `SogouPY!ImeExtension+0x10ce3c9` | `D:\c\logs\pw-exit.log` |
| F5 | 沙箱关闭时，我们的构建加载与 Chrome **完全相同的 5 个搜狗组件**且不崩 | `D:\c\logs\ours-nosandbox.log` |
| F6 | 官方 Chrome 在沙箱下加载 SogouTSF/SogouPY **加 5 个组件**后正常运行 | `D:\c\logs\chrome-under-cdb.log` |
| F7 | 官方 Chrome **便携运行**（复制到 `D:\c\logs\chrome-portable`）同样正常并加载全部组件 | `D:\c\logs\chrome-portable.log` |
| F8 | 两个进程的进程缓解策略快照**一致**（`ExtensionPoint=0x3333333333333303`，其余 `UNSUPPORTED`） | `D:\c\logs\compare-mitigations.ps1` 输出 |
| F9 | 吞掉该退出的机制不存在：`ExitProcess` 在搜狗处理器内部被调用，任何调用侧 SEH/C++ 捕获都无法介入 | 栈结构（F2）+ patch 023 的三次失败尝试 |
| F10 | 退出**不产生崩溃事件**：`Application` 日志中 `Application Error` / `Windows Error Reporting` 事件数为 0 | `D:\c\logs\check-crash-events.ps1` |
| F11 | `TSFBridgeImpl` **从不调用 `ITfThreadMgr::Deactivate()`**（全文件无该调用），部分初始化丢弃后线程仍处于 TSF 激活状态 | `grep Deactivate() ui/base/ime/win/tsf_bridge.cc` → 无匹配 |
| F12 | 本 revision **没有 IMM32 实现**；`InputMethodWinTSF` 的每个入口都以 `if (!TSFBridge::GetInstance())` 提前返回 → bridge 缺席时**完全没有输入法**，不是降级 | `ls ui/base/ime/win/`（无 `imm32_manager`）；`grep -c` 7 处空指针守卫 |
| F13 | 旧栈里那个 `chrome!DllMain+0x30` 是 Chromium 自己的 `CrashOnProcessDetach()`：`ExitProcess` 触发 DLL detach → `ShouldCrashOnProcessDetach()` 仍为 true → 故意空指针写入 0x356。它**是退出过程中的副作用，不是崩溃原因**，也正是它把真实调用链掩盖了一层 | `D:\c\logs\tsf-throw2.log`；`base/win/dllmain.cc` |
| F14 | 抛出的 C++ 对象是搜狗内部类型：vtable 位于 `SogouPY.ime` 地址区间（`0x7ffae262f198`），对象内**没有消息字符串**，只有两个小整数（0x0e、0x28） | `D:\c\logs\tsf-throw2.log`（`---obj---` 段） |
| F15 | **签名不是门槛**：官方 Chrome 副本尾部追加 27 字节使 Authenticode 失效（`Status: NotSigned`）、代码未改，仍加载全部 5 个搜狗组件且正常 | `D:\c\logs\chrome-tampered.log` |
| F16 | **浏览器进程安全上下文与沙箱开关无关**：冻结在 `TSFBridge::Initialize` 实测 ON/OFF 的完整性级别、AppContainer、受限 SID、5 项特权名称与状态、Job、mitigation 全部相同 | `D:\c\logs\snap-ON.log`、`snap-OFF.log`；`snapshot-tsf.ps1` |
| F17 | **独立 GPU 进程是必要条件**：沙箱 ON 时 `--in-process-gpu` 稳定通过（3/3），而 `--disable-gpu`、`--disable-gpu-sandbox`、`--disable-gpu-compositing`、`--disable-software-rasterizer`、`--use-gl=disabled`、`--single-process`、`--no-zygote` 均仍 exit 3 | 见 3.3 |
| F18 | Microsoft Edge 156（微软签名、非 Google 品牌、产品名不同）在沙箱下加载 **6** 个搜狗组件且正常，排除"Google 专有逻辑"与"产品名白名单中的单一名字" | `D:\c\logs\edge.log` |

### 2.1 退出调用栈（原文摘录，`D:\c\logs\tsf-n1.log`）

```text
ntdll!RtlExitUserProcess
KERNEL32!ExitProcessImplementation+0xb
SogouPY!ImeExtension+0x10ce3c9          <-- ExitProcess 调用点
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
KERNELBASE!RaiseException+0x8a              <-- 搜狗抛出 C++ 异常
SogouPY!ImeExtension+0x10b8610
...
SogouPY!ImeDestroy+0xf0                     <-- IMM32 入口
SogouTSF!DllRegisterServer+0x3ade
MSCTF!CTip::Activate+0x121                  <-- TIP 激活
MSCTF!CThreadInputMgr::_ActivateTip+0x113
MSCTF!CThreadInputMgr::ActivateInputProfile+0x372
MSCTF!CThreadInputMgr::EnsureTIPsActivated+0x74
MSCTF!CThreadInputMgr::ActivateEx_P+0x16b
MSCTF!CThreadInputMgr::Activate+0x22
ui_base_ime_win!ui::TSFBridge::Initialize+0x576
ui_base_ime_init!ui::InitializeInputMethod+0x39
content!content::BrowserMainRunnerImpl::Initialize+0x23f
```

读法：搜狗在 TIP 激活过程中启动其 IMM32 侧（`SogouPY`），某一步失败并抛出 C++ 异常；**它自己的异常处理器**随后调用 `ExitProcess(3)`。Chromium 只是触发者，不是决策者。

### 2.2 关键修正

旧文档曾把 exit 3 解释为 `RESULT_CODE_KILLED_BAD_MESSAGE`。**该解释错误**：3 是搜狗传给 `ExitProcess` 的参数，与 Chromium 的结果码无关。因此"这不是沙箱 fatal"的旧结论虽然结论正确，理由却是巧合。

---

## 3. 对照实验矩阵

全部为 headless + 独立临时 `--user-data-dir`；同一 Windows 用户、同一会话、同一搜狗版本（16.6.0.4207）。

### 3.1 浏览器身份维度

| # | 宿主 | 版本 | 签名 | 官方构建 | 沙箱 | 搜狗组件 | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | Google Chrome | 156.0.8078.48 | Google，有效 | 是 | ON | 5 | 正常 |
| E2 | Google Chrome（复制到 `D:\c\logs` 便携运行） | 同上 | 有效 | 是 | ON | 5 | 正常 |
| E3 | Google Chrome（**末尾追加 27 字节，签名失效**） | 同上 | **NotSigned** | 是 | ON | 5 | **正常** |
| E4 | Microsoft Edge | 156.0.4314.42 | Microsoft，有效 | 是 | ON | 6 | 正常 |
| E5 | 上游 Chromium（Playwright） | 151.0.7922.34 | NotSigned | 否 | ON | 0 | exit 3 |
| E6 | browser-cli（patch 023 前） | 156.0.8067.0 | NotSigned | 否 | ON | 0 | exit 3 |
| E7 | browser-cli（patch 023） | 156.0.8067.0 | NotSigned | 否 | ON | —（跳过 TSF） | 正常 |
| E8 | browser-cli | 156.0.8067.0 | NotSigned | 否 | **OFF** | 5 | 正常 |

### 3.2 浏览器进程安全上下文（冻结在 `TSFBridge::Initialize` 时测量）

| 维度 | 沙箱 ON | 沙箱 OFF |
| --- | --- | --- |
| 完整性级别 | Medium (8192) | Medium (8192) |
| AppContainer | 否 | 否 |
| ElevationType | 3 (Default) | 3 (Default) |
| 受限 SID 数 | 0 | 0 |
| 特权列表 | `SeShutdownPrivilege=off, SeChangeNotifyPrivilege=on, SeUndockPrivilege=off, SeIncreaseWorkingSetPrivilege=off, SeTimeZonePrivilege=off` | **逐项相同** |
| 是否在 Job（调用者视角） | 否 | 否 |
| 缓解策略 | 仅 `ExtensionPoint=0x3333333333333303` | 相同 |
| TSF 初始化前已加载模块 | 550 | 549（差异仅 `KBDUS.DLL`） |
| 窗口站创建 vs TSF 初始化顺序 | 前者在前 | 无窗口站创建 |

### 3.3 GPU 进程拓扑（browser-cli，沙箱 ON）

| 变体 | 结果 |
| --- | --- |
| 基线 | exit 3（5/5 确定性） |
| `--disable-gpu` | exit 3 |
| `--disable-gpu-sandbox` | exit 3 |
| `--disable-gpu-compositing` | exit 3 |
| `--disable-software-rasterizer` | exit 3 |
| `--use-gl=disabled` | exit 3 |
| `--disable-features=Vulkan` | exit 3 |
| `--single-process` | exit 3 |
| `--no-zygote` | exit 3 |
| **`--in-process-gpu`** | **rc=0（3/3 确定性）** |
| 沙箱 OFF（任意变体） | rc=0（5/5 确定性） |

判读：

- **E1 vs E6 / E5** 排除"Chromium 的 TSF 路径本身有问题"。
- **E2** 排除安装路径、注册、安装状态。
- **E3（关键）** 排除**代码签名**：同一份官方二进制只追加尾部字节使 Authenticode 失效，代码未变，仍然正常。**H2 因此不需要证书即可关闭。**
- **E4** 排除发布者、产品名与 Google 专有逻辑：Edge 与 Chrome 的产品名、发布者、品牌均不同，都正常。
- **E8 vs E6** 把差异收敛到"沙箱开关"；但 3.2 表明浏览器进程的安全上下文**完全没变**，所以沙箱在这里不是"权限"，而是**内部代码路径/进程拓扑**的代理变量。
- **3.3** 把机制进一步收敛：**只要 GPU 服务跑在独立进程里且全局沙箱开启就会崩；把 GPU 放进浏览器进程（`--in-process-gpu`）就正常**。单独取消 GPU 进程的沙箱（`--disable-gpu-sandbox`）无效，说明不是 GPU 子进程的权限问题。

日志：`tsf-n1.log`、`tsf-ex.log`、`tsf-throw2.log`、`ours-nosandbox.log`、`chrome-under-cdb.log`、`chrome-portable.log`、`chrome-tampered.log`、`edge.log`、`pw-exit.log`、`imm32-ON/OFF.log`、`kbdus-ON/chrome.log`、`snap-ON/OFF.log`。脚本：`check-crash-events.ps1`、`compare-mitigations.ps1`、`snapshot-tsf.ps1`、`freeze-snapshot.sh`（均在 `D:\c\logs`）。

---

## 4. 已被推翻的假设

| 假设 | 推翻依据 |
| --- | --- |
| 签名是根因（本人先前推断） | **E3**：官方 Chrome 只追加尾部字节使签名失效，代码未变，仍然正常加载全部组件 |
| 安装路径 / 注册状态 | **E2**：复制到 `D:\c\logs` 便携运行仍正常 |
| 发布者 / 产品名 / Google 专有逻辑 | **E4**：Edge（微软签名、不同产品名、不同品牌）同样正常 |
| **进程令牌 / 完整性级别 / AppContainer / 受限 SID / 特权** | **3.2**：ON 与 OFF 逐项相同 |
| **Job 对象** | **3.2**：两者均不在调用者 Job 中；源码中 `AssignProcessToJobObject` 只用于子进程 |
| **进程缓解策略** | **3.2**：两者仅 `ExtensionPoint`，值相同 |
| **模块加载环境** | **3.2**：TSF 初始化前 550 vs 549，唯一差异 `KBDUS.DLL`；而 Chrome/Edge 也加载它 |
| **初始化顺序（窗口站 vs TSF）** | Chrome/Edge 与我们同为"窗口站在前"，结果相反 |
| **GPU 子进程的沙箱** | `--disable-gpu-sandbox` 仍 exit 3 |
| Chromium 版本 | E5/E6 两个不同版本同一崩溃点；E1/E6 同为 156 结果相反 |
| browser-cli 的展示策略 / patch 引入 | E5：上游未改动构建同样崩溃 |
| SEH/C++ 捕获写得更"对"就能解决 | F9：退出发生在搜狗自己的处理器里，调用侧没有介入点 |
| 只是竞态/偶发 | 沙箱 ON 5/5 exit 3，OFF 5/5 正常；`--in-process-gpu` 3/3 正常 |

## 5. 尚未确定的一环

**问题**：在**同一个、安全上下文完全相同**的浏览器进程里，为什么"有独立 GPU 进程 + 全局沙箱开启"会让搜狗在加载自身组件之前放弃，而官方 Chrome/Edge 不会？

现在已经能确定它不是权限、不是策略、不是签名、不是产品身份、不是模块集合、不是窗口站顺序。它随**进程拓扑与内部初始化**变化（`--in-process-gpu` 可稳定规避）。

### H5（下一步首选，成本低）GPU 进程存在时，浏览器进程内部多了什么
`--in-process-gpu` 是唯一有效的规避手段，因此对比它与基线的**进程内部状态**最有信息量：
- TSF 初始化时主线程度量：COM apartment（`CoGetApartmentType`）、当前线程桌面/窗口站、主线程序列化状态；
- 句柄表差异（尤其是命名对象、`ALPC` 端口、共享内存节）；
- 是否已创建隐藏窗口（`EnumThreadWindows`）。

判据：找出只在"独立 GPU 进程"情形下存在、且与 TSF/IMM32 相关的状态对象。

### H1（成本高，但现在是唯一能解释"官方构建"差异的实验）
官方构建（Chrome/Edge）与非官方构建（Playwright/browser-cli）在 `is_official_build` 上不同，而签名、品牌、发布者已被排除。构建侧核查结果：
- `chrome_pgo_phase` 默认 **0**，因此**不需要 PGO profile**；
- `BUILDCONFIG.gn:175` 只禁止 `is_debug && is_official_build` 组合；
- 未发现强制静态的 assert，但官方构建惯例为静态 + thin-LTO，链接成本高（本机此前静态构建曾触发页文件不足）。

建议先做 H5；若 H5 仍不能定位，再做 `is_official_build=true` 的实验构建（`out/WinExp`，**不写入** `chromium/args-windows.gn`）。

### H2 签名 —— **已用零成本方式关闭，无需证书**
E3 证明破坏签名不影响结果。除非后续证据反转，不再需要测试证书授权。

**推荐顺序：H5 → H1**。

已关闭：H4（dump 抛出对象）——见 F14，搜狗内部异常类且无消息，无法反推原因。

---

## 6. patch 023 评审

### 6.1 实际执行路径（现文件 `ui/base/ime/win/tsf_bridge.cc:739`）

```cpp
HRESULT TSFBridge::Initialize() {
  if (!base::CurrentUIThread::IsSet()) return E_FAIL;
  if (GetThreadLocalTSFBridge().Get()) return S_OK;          // 幂等
  // ← patch 023：managed 模式在此早退
  if (HasSwitch("browser-cli-background-presentation")) { LOG(...); return E_FAIL; }
  auto delegate = std::make_unique<TSFBridgeImpl>();
  HRESULT hr = InitializeTsfBridgeGuarded(delegate.get(), &ime_threw);  // SEH
  if (ime_threw) { GetThreadLocalTSFBridge().Set(nullptr); return hr; }
  if (SUCCEEDED(hr)) ReplaceThreadLocalTSFBridge(std::move(delegate));
  return hr;
}
```

`TSFBridgeImpl::Initialize()` 的序列：`CoCreateInstance(TF_InputProcessorProfiles)` →
`CoCreateInstance(TF_ThreadMgr)` → **`thread_manager_->Activate(&client_id_)`（崩溃点）** →
`InitializeDocumentMapInternal()`（9 个 `TextInputType`，每个建 `TSFTextStore` + `ITfDocumentMgr`）。

### 6.2 必须修改

1. **耦合错误（P0 级设计缺陷）**：用 `--browser-cli-background-presentation`（展示策略开关）控制输入法行为。两者正交，任何一方调整都会误伤另一方。应改为独立开关（见 §7 P2）。
2. **默认值方向错误**：当前"默认无输入法"，等于把诊断模式当作产品默认。目标状态应是"默认有输入法，仅在诊断/特定场景关闭"。
3. **`verify-windows.sh` 的成功判据要跟着改**：现在把 `TSF skip` 日志当作 managed smoke 的 PASS 条件，修复后必须替换（见 §9）。

### 6.3 可暂留

1. **SEH 包装 `InitializeTsfBridgeGuarded()`**：对本例无效（F9），但对"抛异常但自己不复位的第三方 TIP"仍有价值，成本低、语义清晰，建议保留。
2. **`ime_threw` 时不发布 bridge**：方向正确（避免半初始化对象对外可见）。但它**没有解决线程级残留**：F11 表明 `TSFBridgeImpl` 从不 `Deactivate`，因此若 `Activate()` 成功而后续步骤抛异常，线程仍留在 TSF 激活态。修复路径应为"丢弃前显式 `Deactivate()`"，属于 P1 的正确性改进，可独立于根因修复。

### 6.4 需要复核

`ui/base/ime/win/BUILD.gn` 里给**整个 component** 打开 `//build/config/compiler:exceptions`。影响面是该组件所有编译单元的代码生成（unwind 表、体积、可能的代码布局变化），而实际只需一个文件使用 `__try`。
- 建议：做一次"去掉该 config 是否仍能编译"的实验；若 `__try` 不需要 `/EHsc`，应移除或收窄到单文件 config，减少无关改动面。

### 6.5 `InputMethodWinTSF` 在 bridge 缺席时的行为（已核实）

7 处 `if (!ui::TSFBridge::GetInstance())` 早退，注释写明 "TSFBridge can be null for tests"。本 revision 无 IMM32 回退（F12）。因此：

> **managed 模式 = 完全没有输入法。ASCII 直输可用，中文等组合输入不可用，HUMAN/ASSIST 窗口内同样不可用。**

这与"输入法功能降级"不是一回事，必须在产品文档里如实描述。

---

## 7. 修复路线排序

| 优先级 | 方案 | 预估成本 | 兼容性 | 风险 | 推荐 |
| --- | --- | --- | --- | --- | --- |
| **P0-A** | H5：对比"独立 GPU 进程 vs `--in-process-gpu`"时浏览器进程的内部状态，定位搜狗真正读到的信号 | 1–2 天 | 最好：目标是不改 IME 代码 | 低 | **首选** |
| **P0-B** | H1：`is_official_build=true` 实验构建（无需 PGO），验证官方构建是否根因 | 1 次全量静态+LTO 构建 | 好 | 中：构建时间长、本机页文件曾不足 | H5 无效时执行 |
| **P1** | `ITfThreadMgrEx::ActivateEx(TF_TMAE_NOACTIVATETIP)` 延迟 TIP 激活 | 3–5 天 | 中 | **高**：崩溃点很可能只是推迟到"首次聚焦输入框"，即用户真正想打字时；必须做焦点后复测 | 备选 |
| **P1'** | 丢弃半初始化 bridge 前显式 `Deactivate()` | 0.5 天 | 好 | 低 | 建议随任一方案一起做 |
| **P2** | 独立开关 `--browser-cli-tsf=default\|deferred\|disabled`（`disabled` 仅诊断） | 1 天 | 好 | 低 | **中期必备**，但不能替代修复 |

**明确不作为修复手段**：

- `--no-sandbox`：你已明确禁止，且本轮证明它与问题无因果（浏览器进程安全上下文不变）。
- `--in-process-gpu`：可稳定规避（3/3），但它把 GPU 服务搬进浏览器进程，改变渲染架构与稳定性边界，只能作为**诊断杠杆**，不能作为产品默认。

不推荐：移植旧 IMM32（本 revision 已删除该实现）；独立 IME 代理进程。

---

## 8. 下一步实施方案

### 阶段 1（本报告之后，立即可以做的只读实验）

1. ~~**H4**：dump 抛出对象取错误串~~ —— **已执行并关闭**（F14：搜狗内部异常类，无消息）。
2. **H5（首选）**：对比"基线（崩）"与 `--in-process-gpu`（正常）在 `TSFBridge::Initialize` 时刻的进程内部状态：COM apartment、当前线程桌面/窗口站、隐藏窗口、句柄表（命名对象 / ALPC / 共享节）。目标不是猜，而是找出**只在独立 GPU 进程存在时出现、且与 TSF/IMM32 相关**的对象。
3. **H1（H5 无效时）**：`is_official_build = true` 实验构建（`out/WinExp/args.gn`，**不写入** `chromium/args-windows.gn`；`chrome_pgo_phase=0` 已默认，无需 PGO profile），观察搜狗组件加载数与退出码。

### 阶段 2（按实验结果二选一）

- 若 H1/H3 成立 → 改构建参数或在沙箱初始化处做**最小、可解释**的调整；**不改** `tsf_bridge.cc` 的正常路径；随后用 §9 验收，最后删除 patch 023 的早退。
- 若 H4 给出明确失败原因（例如某个 API 返回 `E_ACCESSDENIED`）→ 评估是否属于 sandbox 策略误伤，按 P0 处理。

### 阶段 3（无论哪条路都要做）

1. 把 patch 023 的早退改为独立开关，默认 **`default`（启用 TSF）**；`disabled` 仅用于诊断。
2. 加 `Deactivate()` 到半初始化清理路径。
3. 复核 `BUILD.gn` 的 `exceptions` config 是否可移除。

### 涉及的具体文件（阶段 2/3）

| 文件 | 预期改动 |
| --- | --- |
| `chromium/patches/023-*.patch` → 新 patch | 早退改为 `--browser-cli-tsf` 开关；默认值反转；记录决策日志 |
| `ui/base/ime/win/tsf_bridge.cc` | 半初始化清理调用 `thread_manager_->Deactivate()` |
| `ui/base/ime/win/BUILD.gn` | 视实验结果收窄或移除 `exceptions` |
| `scripts/verify-windows.sh` | 第 4 步判据从"必须出现 TSF skip"改为"IME 可用性验收" |
| BrowserPilot `src/background/capabilities.ts` | 增加独立 `ime` 状态字段，不与 `enhanced` 混为一谈 |

---

## 9. 验收测试要求（新增，不得用 headless 代替）

必须**真实可见窗口 + 真实 IME 输入**；禁止用 DOM 注入或 CDP `Input.insertText` 冒充。

| # | 场景 | 判定 |
| --- | --- | --- |
| 1 | 沙箱开启，带 managed 开关启动 | 进程存活 ≥ 60s，无 `ExitProcess` 断点命中 |
| 2 | 搜狗拼音：输入 `nihao`，空格上屏，退格，候选翻页 | 文本正确进入 DOM，候选窗可见 |
| 3 | 微软拼音同上 | 同上 |
| 4 | 地址栏 / `<input>` / `<textarea>` / `contenteditable` 四处 | 各自可组合输入 |
| 5 | CLI `show` 后人工接管（HUMAN）再输入中文 | 可输入 |
| 6 | ASSIST 授权前台后输入中文 | 可输入 |
| 7 | 两个后台窗口 + 其中一个前台接管 | 后台窗口无退出，接管窗口可输入 |
| 8 | 全程沙箱开启 | `--no-sandbox` 出现即 FAIL |

配套记录：窗口枚举证明"后台零窗口"、进程存活、`Application` 日志零崩溃事件、IME 模块加载列表（应含 5 个搜狗组件）。

---

## 10. 阻塞项与需要授权的事项

| 项 | 阻塞原因 | 需要什么 |
| --- | --- | --- |
| 切换系统默认输入法做对照 | 会改用户 IME 配置 | 你的授权，或一台可自由改配置的测试机 |
| 干净 Windows VM 复现 | 本机无法排除机器特性 | 一台干净 VM（无第三方 IME / 安全软件） |
| H1 实验构建 | 全量静态 + thin-LTO，数小时；本机曾因页文件不足失败 | 你的确认（不改任何产品文件，产物在 `out/WinExp`） |

**已解除**：H2 签名实验不再需要——E3 用"破坏签名的官方 Chrome"零成本推翻了签名假设，因此**不需要测试证书，也不需要授权安装证书**。

---

## 11. 对产品的一句话结论

搜狗在"沙箱 + 非官方构建"下会主动结束宿主进程，这是它的缺陷；但我们不能等它修。在找到那个可被我们控制的触发条件之前，**managed 模式没有中文输入法是不可接受的**，因此这条调查必须继续，不能以"能启动、能跑 CDP"作为收尾。
