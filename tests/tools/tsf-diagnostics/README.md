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

## 已知坑（都踩过）

| 现象 | 原因 |
| --- | --- |
| `Couldn't resolve error at 'KERNELBASE!ExitProcess "..."'` | 脚本执行时 KERNELBASE 未加载。改用 `ntdll!RtlExitUserProcess`，或用 `bu` 延迟断点 |
| `.echo ===X===` / `.if { ...; }` 报 Syntax error | cdb 的 `.if` 块需要换行，不能分号串接；`===` 在部分上下文会被误解析。用 `--` 分隔 |
| 只有 `ExitProcess -> chrome!DllMain -> CrashOnProcessDetach` | 这是 `CrashOnProcessDetach()` 的故意空指针写（0x356），是退出过程的副作用，不是崩溃原因 |
| `Application` 日志里没有崩溃事件 | 目标是被 `ExitProcess` 结束的，Windows 记为正常退出，不产生 WER 事件 |
| 想抓 managed 模式 | patch 023 会跳过 TSF，抓不到。必须用 stock 模式复现 |
