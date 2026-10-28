# Windows TSF / 搜狗输入法兼容性调查报告（第一阶段）

日期：2026-10-28
范围：`chromium/patches/023-windows-tsf-managed-skip.patch` 及其根因
状态：**调查完成，未修改任何正式代码**
相关 PR：[browser-cli#1](https://github.com/insogao/browser-cli/pull/1)、[BrowserPilot#1](https://github.com/insogao/BrowserPilot/pull/1)

---

## 1. 结论摘要

1. **启动时 exit 3 不是 Chromium 崩溃，是搜狗主动调用了 `ExitProcess(3)`。** 已取得连续调用栈，调用点位于 `SogouPY.ime` 内部，且在它自己的 C++ 异常处理器里。
2. **与 browser-cli 的移植无关。** 未经任何修改的上游 Chromium 151 在同一台机器上产生**逐帧相同、偏移相同**的调用栈。
3. **此前"签名是根因"的假设已被推翻**（那是我的错误推断，仅基于相关性）。真正与结果同向变化的变量是**沙箱开关**：
   - 我们的构建 + 沙箱 → Sogou 组件一个都不加载 → 抛异常 → `ExitProcess(3)`
   - 我们的构建 + `--no-sandbox` → 加载与 Chrome 相同的 5 个组件 → 正常
   - 官方 Chrome + 沙箱（含便携路径）→ 加载全部组件 → 正常
4. **仍未确定的一环**：为什么沙箱开启时，非官方构建里的搜狗在加载自身组件之前就放弃，而官方 Chrome 不会。这是唯一剩下的未知，第 5 节给出四个候选假设与各自的实验设计。
5. **patch 023 机制有效但耦合错误**：它用一个*展示策略*开关控制*输入法*行为，并且代价是 managed 模式下**完全没有输入法**（不是降级）。第 6 节给出必须修改项。

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

| # | 宿主 | 版本 | 签名 | 沙箱 | 搜狗组件加载数 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | Google Chrome | 156.0.8078.48 | Valid（Google LLC） | ON | 5 | 正常 |
| E2 | Google Chrome（便携副本） | 156.0.8078.48 | Valid | ON | 5 | 正常 |
| E3 | 上游 Chromium（Playwright） | 151.0.7922.34 | NotSigned | ON | 2 | exit 3 |
| E4 | browser-cli（patch 023 前） | 156.0.8067.0 | NotSigned | ON | 2 | exit 3 |
| E5 | browser-cli（patch 023） | 156.0.8067.0 | NotSigned | ON | —（跳过 TSF） | 正常 |
| E6 | browser-cli | 156.0.8067.0 | NotSigned | **OFF** | 5 | 正常 |

判读：

- **E1 vs E4** 排除"Chromium 156 的 TSF 路径本身有问题"。
- **E2 vs E1** 排除安装路径、注册、安装状态。
- **E3 vs E4** 排除 browser-cli 的任何 patch。
- **E6 vs E4** 把变量收敛到沙箱。
- **E1 vs E6** 说明沙箱**单独**不足以解释：官方构建在沙箱下正常。

因此结论只能是：**"沙箱 + 非官方构建"这个组合**才触发。签名只是与"官方构建"高度相关，被 E6 单独推翻（未签名 + 无沙箱 = 正常）。

日志：`tsf-n1.log`、`tsf-ex.log`、`ours-nosandbox.log`、`chrome-under-cdb.log`、`chrome-portable.log`、`pw-exit.log`。脚本：`check-crash-events.ps1`、`compare-mitigations.ps1`（均在 `D:\c\logs`）。

---

## 4. 已被推翻的假设

| 假设 | 推翻依据 |
| --- | --- |
| 签名是根因（本人先前推断） | E6：未签名 + 无沙箱完全正常。签名只是相关变量 |
| 安装路径 / 注册状态 | E2：官方 Chrome 复制到 `D:\c\logs` 仍正常 |
| 进程缓解策略差异 | F8：两边快照一致 |
| Chromium 版本 | E3/E4 两个不同版本同一崩溃点；E1/E4 同为 156 结果相反 |
| browser-cli 的展示策略 / patch 引入 | E3：上游未改动构建同样崩溃 |
| SEH/C++ 捕获写得更"对"就能解决 | F9：退出发生在搜狗自己的处理器里，调用侧没有介入点 |

---

## 5. 尚未确定的一环

**问题**：沙箱开启时，非官方构建里的搜狗为什么在**加载自身组件之前**就放弃，而官方 Chrome 会继续加载？

已知的边界条件很窄：同一用户、同一 IME 版本、同一 mitigation 策略、同一 Chromium 版本区间、同一沙箱代码。差异只可能来自"宿主可被搜狗观测到的属性"或"沙箱在浏览器进程里的实际副作用"。

### H1（推荐优先）官方构建标志差异
`is_official_build` / `dcheck_always_on` / `is_component_build` 会改变编译定义与部分运行时代码路径。
- 实验：以 `is_official_build = true` 构建一次（**不需要证书**），在沙箱下观察搜狗组件是否加载。
- 成本：一次全量构建（数小时）。风险：低（不改代码）。
- 若成立 → P0 级修复：调整构建参数，不动 IME 代码。

### H2 可执行文件签名
- 实验：对现有二进制做**测试签名**后复测。
- **阻塞**：需要创建/安装证书，属"不得在未授权情况下安装证书或修改系统级设置"。**需要你明确授权**，且必须说明测试签名并不等价于 Google 的可信签名，若搜狗校验发布者名称则无效。

### H3 沙箱在浏览器进程中的具体副作用
沙箱关闭会同时关闭多项机制（job 对象、部分 mitigation 安装、`Sandbox::Initialize` 路径）。
- 实验：在 `sandbox::Sandbox::Initialize` / job 分配点逐个插桩（仅本地实验构建，不进 patch 系列），找出哪一项使搜狗放弃。
- 成本：中等；需要 2–3 次实验构建。风险：低（不改产品代码）。

### H4 搜狗内部判据 —— **已执行，收益有限**
- 结果：抛出的对象是搜狗内部异常类（vtable 在 `SogouPY.ime`），**不含消息字符串**，只有两个小整数（0x0e、0x28）。没有 `std::exception::what()` 可用，也没有 HRESULT。
- 结论：**无法从异常对象反推失败原因**。该路径已关闭，不要再投入。
- 副作用收益：确认了旧栈里 `chrome!DllMain+0x30` 是 `CrashOnProcessDetach()`（F13），即"看起来像 Chromium 自己在崩溃"是退出过程的产物，不是原因。

**推荐顺序（已更新）：H1 → H3 → H2（需授权）**。

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
| **P0-A** | 定位沙箱下搜狗放弃的判据（H4→H1→H3），从构建/环境侧修复，不改 IME 代码 | 2–5 天（含 1–2 次全量构建） | 最好：不动 Chromium IME 逻辑 | 低（实验构建不进 patch 系列） | **首选** |
| **P0-B** | 若 H1 成立：调整构建参数（如 `is_official_build`、关闭 `dcheck_always_on`） | 1 次构建 + 验证 | 好 | 中：官方构建标志会影响性能/体积，需评估 | 视 H1 结果 |
| **P1** | `ITfThreadMgrEx::ActivateEx(TF_TMAE_NOACTIVATETIP)` 延迟 TIP 激活 | 3–5 天 | 中 | **高**：崩溃点很可能只是推迟到"首次聚焦输入框"，即用户真正想打字时；必须做焦点后复测 | 备选 |
| **P1'** | 丢弃半初始化 bridge 前显式 `Deactivate()` | 0.5 天 | 好 | 低 | 建议随任一方案一起做 |
| **P2** | 独立开关 `--browser-cli-tsf=default\|deferred\|disabled`（`disabled` 仅诊断/特殊自动化） | 1 天 | 好 | 低 | **中期必备**，但不能替代修复 |

不推荐：移植旧 IMM32（本 revision 已删除该实现，工作量大且回退上游方向）；独立 IME 代理进程（复杂度与稳定性风险远超收益）。

**关于 P1 的预判（需实测确认）**：崩溃点在 `MSCTF!CTip::Activate` 内部。`TF_TMAE_NOACTIVATETIP` 只是把这一步推迟到需要输入法时，届时同样会进入 `CTip::Activate`。因此它大概率**不解决问题，只是把 exit 3 从启动时挪到用户点击输入框时**——那比现在更糟。除非实测证明延迟后搜狗走了不同分支，否则不建议投入。

---

## 8. 下一步实施方案

### 阶段 1（本报告之后，立即可以做的只读实验）

1. ~~**H4**：dump 抛出对象取错误串~~ —— **已执行并关闭**（F14：搜狗内部异常类，无消息）。
2. **H1**：在一次实验构建中加 `is_official_build = true`（`out/WinExp/args.gn`，**不写入 `chromium/args-windows.gn`**），观察：
   - 搜狗组件加载数（cdb ModLoad 计数）
   - 启动退出码
   - 若加载数变为 5 → H1 成立。
3. **H3 的前置**：在一次实验构建里，把 `--no-sandbox` 打开的几项机制分别单独关闭（job 对象 / `Sandbox::Initialize` 子步骤），观察哪一项单独就能恢复搜狗组件加载。这一步能直接把 H3 从"候选"变成"定位"。

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
| H2 签名实验 | 需创建/安装测试证书 | 你的明确授权；且需接受"测试签名 ≠ Google 签名"的局限 |
| 切换系统默认输入法做对照 | 会改用户 IME 配置 | 你的授权，或提供一台可自由改配置的测试机 |
| 干净 Windows VM 复现 | 本机无法排除机器特性 | 一台干净 VM（无第三方 IME / 安全软件） |
| `is_official_build` 实验构建 | 全量构建数小时 + 磁盘 | 无需授权，但需你确认可以占用构建时间 |

---

## 11. 对产品的一句话结论

搜狗在"沙箱 + 非官方构建"下会主动结束宿主进程，这是它的缺陷；但我们不能等它修。在找到那个可被我们控制的触发条件之前，**managed 模式没有中文输入法是不可接受的**，因此这条调查必须继续，不能以"能启动、能跑 CDP"作为收尾。
