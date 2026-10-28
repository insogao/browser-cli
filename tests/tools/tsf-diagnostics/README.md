# TSF / IME 启动调查脚本

配合 [docs/tsf-sogou-investigation-2026-10-28.md](../../../docs/tsf-sogou-investigation-2026-10-28.md) 使用。
这些是 **cdb 命令脚本**，不含任何浏览器启动入口；启动参数由调用者给出。

只读诊断：不修改系统设置、不安装证书、不触碰真实 profile。请始终使用独立临时
`--user-data-dir`，且**不要**加 `--no-sandbox`（它本身就是被调查的变量）。

## 前提

- Debugging Tools for Windows（`cdb.exe`），本机在
  `C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe`。
- 目标进程能复现问题。注意：patch 023 之后 **managed 模式会跳过 TSF**，
  因此要用 **stock 模式**（不加 `--browser-cli-background-presentation`）复现。

## 01-exit-stack.cdb —— 谁在退出进程

```sh
cdb -G -cf 01-exit-stack.cdb chrome.exe --headless=new \
  --user-data-dir=<temp> --no-first-run --no-default-browser-check about:blank
```

输出形如：

```text
ntdll!RtlExitUserProcess
KERNEL32!ExitProcessImplementation+0xb
<SogouPY!ImeExtension+...>        <- 真正的调用者
```

要点：必须用 `ntdll!RtlExitUserProcess`，不能用 `KERNELBASE!ExitProcess`——
脚本执行时 KERNELBASE 尚未加载，`bp` 会报 `Couldn't resolve error`。

## 02-exception.cdb —— 异常第一现场

```sh
cdb -G -cf 02-exception.cdb chrome.exe --headless=new ...
```

`.exr -1` 的 `Parameter[3]` 是抛出模块的基址，可与 `ModLoad` 行对照确认是谁抛的。
本机的搜狗用例中参数为 `19930520 / <对象指针> / <ThrowInfo> / <SogouPY 基址>`，
且对象内没有消息字符串，无法据此得到失败原因。

## 03-dll-loads.cdb —— 组件是否被尝试加载

```sh
cdb -G -cf 03-dll-loads.cdb chrome.exe --headless=new ...
```

用它对比"崩溃"与"正常"两种宿主：官方 Chrome 会加载 `SogouInput\Components\*`
共 5 个 DLL，而崩溃的非官方构建一个都不会尝试——说明搜狗在更早处就放弃了，
不是某个 DLL 加载失败。

## 04-module-load-stack.cdb —— 某个模块是谁加载的

```sh
cdb -G -cf 04-module-load-stack.cdb chrome.exe --headless=new ...
```

脚本里把 `ld:<module>` 换成目标模块名（例如 `ld:kbdus`、`ld:imm32`），
在模块加载的第一时间打印调用栈。本机用它确认了 `KBDUS.DLL` 出自
`USER32!CommonCreateWindowStation`（沙箱子进程会创建独立窗口站）。

## snapshot-process-context.ps1 + freeze-at-tsf.sh —— 安全上下文快照

```sh
tests/tools/tsf-diagnostics/freeze-at-tsf.sh ON
tests/tools/tsf-diagnostics/freeze-at-tsf.sh OFF --no-sandbox
```

在 `ui_base_ime_win!ui::TSFBridge::Initialize` 停住浏览器，然后从外部读取
目标进程的完整性级别、AppContainer、ElevationType、受限 SID 数、**特权名称与状态**、
是否在 Job、缓解策略。冻结在 TSF 入口是为了避免崩溃过程中途改变进程状态。

本机结论：沙箱 ON 与 OFF 的上述各项**逐项相同**，因此"沙箱"在这里不是权限差异。

注意两个实现坑（都踩过）：

- `GetSidSubAuthorityCount` 返回的是 `PUCHAR`（指向计数的指针），按值声明会读到野指针；
- 子进程会继承未知开关，所以按 `--browser-cli-diag-target` 找进程时必须排除含 `--type=`
  的项，否则抓到的是渲染/GPU 子进程（完整性级别 Untrusted，看起来像"浏览器被沙箱化了"）。

## 已用这些脚本得出的结论（避免重复劳动）

| 结论 | 依据 |
| --- | --- |
| 退出由 `SogouPY.ime` 调用 `ExitProcess(3)` | 01 的栈 |
| 异常由搜狗自己抛出、自己处理 | 02 的 `.exr -1`（Parameter[3] = SogouPY 基址） |
| 搜狗在沙箱下**从未尝试**加载自身组件 | 03 的 LDR 日志里没有 isgpet/picface/ichat 等 |
| 签名不是门槛 | 破坏签名后的官方 Chrome 仍正常 |
| 浏览器进程安全上下文与沙箱开关无关 | snapshot 的 ON/OFF 对比 |
| 独立 GPU 进程是必要条件 | 沙箱 ON 下 `--in-process-gpu` 通过，其余 GPU 开关均无效 |

## 已知坑（都踩过）

| 现象 | 原因 |
| --- | --- |
| `Couldn't resolve error at 'KERNELBASE!ExitProcess "..."'` | 脚本执行时 KERNELBASE 未加载。改用 `ntdll!RtlExitUserProcess`，或用 `bu` 延迟断点 |
| `.echo ===X===` / `.if { ...; }` 报 Syntax error | cdb 的 `.if` 块需要换行，不能分号串接；`===` 在部分上下文会被误解析。用 `--` 分隔 |
| 只有 `ExitProcess -> chrome!DllMain -> CrashOnProcessDetach` | 这是 `CrashOnProcessDetach()` 的故意空指针写（0x356），是退出过程的副作用，不是崩溃原因 |
| `Application` 日志里没有崩溃事件 | 目标是被 `ExitProcess` 结束的，Windows 记为正常退出，不产生 WER 事件 |
| 想抓 managed 模式 | patch 023 会跳过 TSF，抓不到。必须用 stock 模式复现 |
