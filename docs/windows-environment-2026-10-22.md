# Windows 环境与基线报告（2026-10-22）

## 结论

**环境阻塞已于 2026-10-23 解除。** Chromium 固定 revision 156.0.8067.0 要求的
VS2026 18.x、Windows SDK `10.0.28000.0`、ATL/MFC 与 Debugging Tools for Windows 已全部安装；
`build/vs_toolchain.py get_toolchain_dir` 正确解析出 `vs_version = "2026"`、
`sdk_version = "10.0.28000.0"`，GN 重新生成成功，zlib 目标真实编译+链接通过，完整 `chrome`
构建已启动。M0 仍以实际构建与运行证据为准，不因环境就绪而标记 PASS。

## 2026-10-23 工具链安装结果

| 组件 | 版本/路径 |
| --- | --- |
| Visual Studio BuildTools 2026 | 18.11.1（`C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools`） |
| MSVC | 14.51.36231（VC145），含 `atlmfc` |
| Windows SDK | 10.0.28000.0（Include/Lib 与 26100 并存） |
| Debugging Tools | `Windows Kits\10\Debuggers\x64\dbghelp.dll`、`dbgcore.dll` |
| 安装方式 | winget `Microsoft.VisualStudio.BuildTools`（VCTools + Recommended + ATLMFC）；SDK 用官方 `winsdksetup.exe`，features 含 `OptionId.WindowsDesktopDebuggers` |

**构建环境变量（每次 gn/ninja 都必须设置）：**

```bat
set DEPOT_TOOLS_WIN_TOOLCHAIN=0
set "vs2026_install=C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
```

原因：Chromium 的 `_GenerateCandidatePaths('2026')` 只查 `%ProgramFiles%\Microsoft Visual
Studio\18`，而 VS 安装器实际落在 `%ProgramFiles(x86)%`；`vs2026_install` 是
`build/vs_toolchain.py` 支持的官方覆盖入口。

安装器提示过一次重启（属于附加组件），`vswhere` 报告 `isRebootRequired=false`；工具链已验证
可编译，未重启。

## 并行度与内存（2026-10-23 实测）

`-j 12` 在 6400 目标处失败，错误为 `LLVM ERROR: out of memory`（clang 前端 OOM），
不是代码错误也不是工具链缺失。本机 32 GB 物理内存，构建时可用内存仅剩 7–8 GB。
后续统一使用 `-j 6` 续跑；ninja 不会重做已完成目标。

## 首个 Windows 代码阻塞与修复（2026-10-23）

**缺陷**：`ui/views/BUILD.gn` 的 `source_set("browser_cli_presentation_policy")` 没有
`VIEWS_IMPLEMENTATION`，而 `browser_cli_presentation_policy.h` / `browser_cli_foreground_lease.h`
的公开 API 用 `VIEWS_EXPORT` 标注。`is_component_build=true` 下
`ui/views/views_export.h` 把 `VIEWS_EXPORT` 展开为 `__declspec(dllimport)`，导致策略自己的
`.cc` 把定义编译成 dllimport 重声明。

实测错误（`D:\c\logs\policy-dllimport.log`）：

```text
browser_cli_presentation_policy.cc(121,18): error: 'views::CurrentForegroundOrigin'
  redeclared without 'dllimport' attribute: 'dllexport' attribute added
  [-Werror,-Winconsistent-dllimport]
... 20 errors generated.
```

**修复**（两处 GN，未改任何 C++）：

1. `source_set("browser_cli_presentation_policy")` 增加 `defines = [ "VIEWS_IMPLEMENTATION" ]`。
2. 两个策略单测目标同样增加该 define，因为单测把 source_set 静态链入自己的可执行文件；
   否则测试 TU 仍按 dllimport 引用本地已定义符号，链接失败
   `LNK4217: locally defined symbol imported`（这一失败已实测并复现）。

**Windows 运行证据**：

| 测试 | 结果 |
| --- | --- |
| `browser_cli_presentation_policy_unittests.exe` | 32/32 PASS，exit 0 |
| `browser_cli_foreground_lease_unittests.exe` | 8/8 PASS，exit 0 |

这两项是**共用 C++ 策略在 Windows 上的首次真实运行通过**，证明策略/lease 语义跨平台成立；
它们不证明窗口展示行为，Windows 原生 gateway 仍未实现。

## 第二个构建阻塞：`python3` 被 Microsoft Store 别名截获（2026-10-23）

`content/browser/resources/traces_internals:config_proto_gen` 调用的
`tools/protoc_wrapper/protoc-gen-ts_proto.bat` 执行裸 `python3`。本机 PATH 中
`python3` 先命中 `C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\python3`
（Microsoft Store 占位程序），输出：

```text
Python was not found; run without arguments to install from the Microsoft Store...
--ts_proto_out: protoc-gen-ts_proto: Plugin failed with status code 9009.
```

这不是代码缺陷。修复方式：构建时把 checkout 自带的
`third_party/cpython3/host/bin` 前置到 PATH（该目录有真实的 `python3.exe`）。
`scripts/build-windows.sh` 已加入该处理并给出缺失告警。不要依赖系统 Python，也不要
安装 Store 版 Python。

## M0 首次全量构建成功（2026-10-23）

`ninja -C out/WinDev -j 6 chrome` 最终 **28115/28115，exit 0**，零失败。
产物：

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `out/WinDev/chrome.exe` | 1.7 MB | 启动器 |
| `out/WinDev/chrome.dll` | 167 MB | 浏览器主体 |
| `out/WinDev/ui_views.dll` | 5.7 MB | views 组件（含共用策略） |

**符号证据（patch 015 状态，实测）：**

| 符号/字符串 | `ui_views.dll` | `chrome.dll` | 结论 |
| --- | --- | --- | --- |
| `BrowserCliPresentationPolicy` | 20 | 0 | 共用策略已编译进 Windows views 组件 |
| `BROWSER_CLI_PRESENTATION` | 2 | 0 | 策略日志字符串存在 |
| `ForegroundAssist` | 19 | 0 | lease 代码存在 |
| `AllowProgrammaticWindowChangeFromCurrentOrigin` | 0 | 0 | Windows 定义尚未实现（M1） |
| `ArbitrateBrowserCliCommandLineIntent` | 0 | 0 | browser_cli producer 尚未接入 Windows（W-04/M1） |
| `BROWSER_CLI_USER_EXPLICIT_SHOW` | 0 | 0 | 同上 |

**运行证据（无可见窗口，隔离 profile）：**

```text
chrome.exe --headless=new --no-sandbox --disable-gpu \
  --user-data-dir=<mktemp> --dump-dom "data:text/html,<h1>BrowserCLI-Win</h1>"
-> exit 0，输出 <html><head></head><body><h1>BrowserCLI-Win</h1></body></html>
```

**两个 Windows 行为差异（必须记录，不是本项目的缺陷）：**

1. `chrome.exe --version` 在 Windows 上不打印版本，会正常启动浏览器。
   根因：`HandleVersionSwitches` 位于 `chrome_main_delegate.cc:1255` 的
   `#if BUILDFLAG(IS_POSIX)` 内。所有 Windows smoke 必须用 `--headless=new`，
   禁止用 `--version` 作为启动检查。
2. 本机不加 `--no-sandbox` 时 headless 退出码 3（`RESULT_CODE_KILLED_BAD_MESSAGE`），
   加 `--no-sandbox` 才能运行。`--no-sandbox` 仅限本机诊断，生产与验收不得使用；
   沙箱初始化失败需单独排查（疑似本机安全策略/杀软），未解决前不能宣称 Windows
   runtime 验收通过。

## M1 Slice 1（patch 016）

Windows 原生展示 gateway 与命令行 producer 接线的实现、证据与限制见
[`windows-m1-gateway-2026-10-23.md`](windows-m1-gateway-2026-10-23.md)。
简要结论：共享策略已在真实 Windows 进程中执行并 fail closed；assist 展示、
扩展 origin scope、用户菜单/taskbar 入口、会话恢复 scope 仍未实现。

## 原始阻塞证据（保留）

Chromium 固定 revision 156.0.8067.0 要求 Windows SDK `10.0.28000.0`；本机原只有 VS2022
BuildTools 17.14 + SDK `10.0.26100.0`，且缺少 ATL/MFC 与 Debugging Tools for Windows。
hermetic toolchain 下载被 Google 401 拒绝（无匿名访问）。

## 已完成

| 项 | 结果 |
| --- | --- |
| depot_tools | `D:\c\depot_tools`，clone 26s，约 554MB |
| Chromium 源码 | Gitiles archive `src-7d0f28c.tar.gz`，1.4GB，sha256 `7997af005cb399a89b2ab967d0f874458b3f45aac0b113085dac04dd5063a881` |
| git checkout | `git fetch --depth=1 --filter=blob:none`（61s/19MB）+ real commit/trees |
| HEAD / tree | `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940` / `abb974d2f95d2858237827db4aad78aa32b44d04`（与 Gitiles commit JSON 一致） |
| 工作区 | 5.1GB，约 507k 文件；普通文件与 tree 一致；21 个 symlink 为 skip-worktree 平台降级（`D:\c\logs\symlink-paths.txt`） |
| 依赖 | `gclient sync --no-history --nohooks -j 8` + `gclient runhooks` 完成；gn.exe/ninja.exe/clang-cl.exe 就位 |
| patch stack | 001-014 在真实 checkout 上 `--check` pristine → `--apply` → `STATE applied`；sha256 全部匹配 |
| GN | patched source `gn gen out/WinDev` 成功（36095 targets，使用诊断性 SDK 26100 参数） |

## 阻塞证据

1. `build/vs_toolchain.py` / `build/toolchain/win/setup_toolchain.py`：
   `SDK_VERSION = '10.0.28000.0'`。
2. 本机 SDK：只有 `C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0`。
3. ATL/MFC：`VC\Tools\MSVC\14.44.35207\atlmfc` 不存在。
4. Debugging Tools：`Windows Kits\10\Debuggers\x64\dbghelp.dll` 不存在。
5. hermetic toolchain：
   `gs://chrome-wintoolchain` → `401 Anonymous caller does not have storage.objects.list access`。
6. 诊断性 SDK 26100 构建（已回退）：
   - abseil `low_level_alloc.cc` 编译失败：
     `fileapi.h/winbase.h: unknown type name 'FILE_INFO_BY_HANDLE_CLASS'`（`NTDDI_WIN11_BR` 在
     26100 头文件中不可用）；
   - `copy_dlls` 报 `dbghelp.dll not found ... Debugging Tools for Windows`。

## 需要安装（需管理员/UAC）

- Visual Studio 2026 Build Tools（或更新后的 VS2022），工作负载：
  - Desktop development with C++
  - MSVC v143/v14x x64/x86 build tools
  - Windows 11 SDK **10.0.28000.0**
  - C++ ATL for latest build tools
  - C++ MFC（如 Chromium target 需要）
  - Debugging Tools for Windows（Windows SDK 组件）
- 或者：为 gsutil 配置可访问 `chrome-wintoolchain` 的 Google 账号，然后
  `DEPOT_TOOLS_WIN_TOOLCHAIN=1 python3 build/vs_toolchain.py update --force`。
- 安装后不要重新下载源码；直接：
  `gn gen out/WinDev` + `third_party\ninja\ninja.exe -C out\WinDev -j 12 chrome`。

## 当前磁盘/进程状态

- `D:\` 可用约 329GB。
- 无运行中的 gclient/ninja 构建；无可见浏览器进程。
- Chromium checkout 处于 001-014 applied（38 个 tracked 修改 + 新增文件）；工具链脚本已回退，
  `build/vs_toolchain.py` / `setup_toolchain.py` 与 HEAD 一致。
- `D:\work\browsercli` 未 commit；变更：`.gitattributes`、`chromium/patch-manifest.json`、
  `scripts/apply-patches.py`、`tests/tools/test_apply_patches.py`、`chromium/patches/README.md`。

## 沙箱初始化失败诊断（2026-10-24）

**现象**：`chrome.exe --headless=new`（不加 `--no-sandbox`）在
`PowerStateChange: Off battery` 之后立即 exit 3；stderr 约 89KB，但没有任何
`sandbox`/`FATAL`/`ERROR` 行；无 WER 报告，Application 事件日志无 1000/1001 记录。
可见模式同样 exit 3。加 `--no-sandbox` 后 headless 与可见模式都正常。

**已排除**：

- 系统 Exploit Protection：`Get-ProcessMitigation -System` 全部为 `NOTSET`。
- 新沙箱特性：`--disable-features=WinSboxCsrssLockdown,WinSboxNoFakeGdiInit,WinIsolatedProcess`
  无效，仍 exit 3。
- 退出码不是 sandbox fatal：`SBOX_FATAL_*` 是 7006–7014；3 对应 content 的
  `RESULT_CODE_KILLED_BAD_MESSAGE`。

**已定位（2026-10-26）：搜狗输入法 TSF 服务在启动期抛未捕获 C++ 异常。**

用 cdb `sxe eh` 抓到完整的 C++ 异常栈，14 层全部落在搜狗 DLL 内：

```text
KERNELBASE!RaiseException                       <- e06d7363, 参数 c0000604
SogouPY!ImeExtension+0x10b8610
SogouPY!ImeExtension+0xa2734a
SogouPY!ImeExtension+0xa27399
SogouPY!ImeExtension+0xa29628
SogouPY!ImeExtension+0xa1aa71
...（共 14 层，全部 SogouPY!ImeExtension）
```

触发链：

1. `content/browser/browser_main_runner_impl.cc:139` 调用 `ui::InitializeInputMethod()`；
2. `ui/base/ime/init/input_method_initializer.cc:25` → `TSFBridge::Initialize()`；
3. `ui/base/ime/win/tsf_bridge.cc:220` → `thread_manager_->Activate(&client_id_)`，
   系统在此加载当前中文 IME（搜狗 `SogouPY.ime`）；
4. 搜狗在进程内抛 C++ 异常，异常穿透到 `ExitProcess`；
5. `base/win/dllmain.cc` 的 `CrashOnProcessDetach` 故意写空指针 0x356，
   掩盖了真实退出路径，最终表现为 exit 3。

**已排除（均为实测）**：

- 不是父进程/job 对象：改用 WMI `Win32_Process::Create` 独立启动，同样 exit 3。
- 不是系统 Exploit Protection：`Get-ProcessMitigation -System` 全部 `NOTSET`。
- 不是某个 sandbox feature：分别关闭
  `WinSboxCsrssLockdown`、`WinSboxNoFakeGdiInit`、`WinIsolatedProcess`、
  `RendererAppContainer`、`NetworkServiceSandbox` 均无效。
- 不是键盘布局注册表：临时移除中文 Preload（备份
  `D:\c\logs\preload-backup.reg`，已恢复）仍 exit 3。
- 退出码不是 sandbox fatal：`SBOX_FATAL_*` 是 7006–7014；3 对应
  `RESULT_CODE_KILLED_BAD_MESSAGE`。

**结论与处置**：这是本机第三方 IME 的缺陷，不是本项目代码问题；沙箱失败发生在
Chromium 加载 IME 阶段，早于任何 Browser CLI patch 的执行路径。
**生产与验收不得使用 `--no-sandbox`**；本机可见验收中的 `--no-sandbox` 仅作诊断。

三条可选处置（需用户决定）：

1. 用户在本机移除/替换搜狗输入法（或其 TSF 服务）后复测，这是对自己机器的配置改动；
2. 接受当前限制，把 `--no-sandbox` 记为“本机已知环境限制”，生产用干净机器；
3. 新增 patch 在 `TSFBridgeImpl::Initialize()` 外做异常保护并降级为“无 IME”，
   属于新行为，需评审且仍标 REVIEW。

## 临时兼容方案（2026-10-28）

`023-windows-tsf-managed-skip.patch` 的"managed 模式跳过 TSF"已**撤销**：它等于没有输入法，
而 managed 窗口随时可能交给用户。现在 TSF 始终启用，`--browser-cli-tsf=disabled` 仅作诊断；
Windows 开发/验收运行需带 `--in-process-gpu`（临时兼容，发布配置不使用）。
完整验收证据、测量陷阱与 GPU 进程隔离风险见
`docs/tsf-sogou-investigation-2026-10-28.md` §12。

## exit 3 的对照实验（2026-10-28）

当时有一个合理的质疑：如果这是普遍问题，网上应该有大量记录。为回答它，在同一台机器、
同一会话、同一搜狗版本下用四个宿主做对照（全部 `--headless=new`、独立临时 profile）：

| 宿主 | 版本 | 代码签名 | 沙箱 | 结果 |
| --- | --- | --- | --- | --- |
| Google Chrome | 156.0.8078.48 | Valid（Google LLC） | 开 | **exit 0** |
| 上游 Chromium（Playwright 构建） | 151.0.7922.34 | NotSigned | 开 | exit 3 |
| Browser CLI（patch 023 之前） | 156.0.8067.0 | NotSigned | 开 | exit 3 |
| Browser CLI（patch 023 之后） | 156.0.8067.0 | NotSigned | 开 | exit 0（跳过 TSF） |
| Browser CLI | 156.0.8067.0 | NotSigned | 关 | exit 0 |

**结论**：与我们的移植无关（未改动一行的上游 Chromium 同样失败），也与 Chromium 版本
无关（同为 156 的官方 Chrome 正常）。本机上唯一与结果同向变化的变量是**是否由 Google
签名并正式安装**。这只说明相关性，机制仍未知（Sogou TIP 可能在不受信任的宿主里主动
放弃初始化）；要证明因果需要一个签名后的构建复测，属 Phase 9 范围。

**为什么网上搜不到**（四点，均在本机验证过）：

1. 受影响人群很小：只有“在装有该版本搜狗的机器上自行编译 Chromium”的开发者；
2. 症状是静默的：搜狗自己调用 `ExitProcess`，Windows 记录为正常退出。
   实测 `Application` 日志在该时间窗内 **Application Error / Windows Error Reporting
   事件数为 0**，既没有崩溃弹窗也没有 dump；
3. 默认调试看不到真实路径：`cdb -G` 只显示
   `ExitProcess → chrome!DllMain → CrashOnProcessDetach`，指向 Chromium 自己的 DllMain，
   极易误判成“构建坏了”；必须 `sxe eh` 才能看到 14 层 `SogouPY!ImeExtension`；
4. 中文 IME 厂商的这类缺陷很少进 crbug，搜索只会命中下载页与营销页面。

本轮检索（Bing / 中文 Bing）确实没有找到描述该症状的公开记录；这与上述人群规模一致，
不能作为“问题不存在”的证据。

## cdb 补充证据（2026-10-24）

以下是最初用 `cdb -G` 抓到的退出栈，用于说明 exit 3 不是普通 sandbox 返回值：

```text
chrome!DllMain+0x30  mov dword ptr [0], 0x356   <- base/win/dllmain.cc CrashOnProcessDetach
chrome!DllMain+0x17
chrome!dllmain_dispatch
ntdll!LdrpCallInitRoutineInternal
ntdll!LdrpCallInitRoutine
ntdll!LdrShutdownProcess
ntdll!RtlExitUserProcess
KERNEL32!ExitProcessImplementation
```

含义：进程走了 `ExitProcess`，但没有清除 `ShouldCrashOnProcessDetach`
（`chrome/app/chrome_main.cc:145` 在正常返回时才会重置），于是
`DllMain(DLL_PROCESS_DETACH)` 故意写空指针 0x356，把真实退出路径掩盖成访问冲突。
`--no-sandbox` 只是绕过了触发条件（不加载 IME 的沙箱初始化路径）。
