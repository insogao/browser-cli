# Windows non-component release-shape build（2026-10-28，out/WinRelease）

状态：**内部未签名测试构建（internal unsigned test build）**。
不是已签名发布产物，不是 release-ready，不得据此宣称安全等价或可公开分发。
配套：`docs/windows-release-packaging-preflight-2026-10-28.md`、
`config/windows-release-runtime-flags.env`、`tests/windows-release/`。

本轮目标：在 `D:\c\src\out\WinRelease` 建立**真实 non-component** 配置并构建，验证
`is_component_build=false` 对进程缓解、模块面、启动前 CIG 的影响，并在不放宽沙箱的
前提下评估 `NetworkServiceSandbox`。WinDev（component）与官方 Chrome 作为对照。

---

## 1. 构建语系与来源

| 项 | 值 |
| --- | --- |
| 源码根 | `D:\c\src`（共享 checkout；HEAD `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940` = `chromium/VERSION`） |
| 分支 | `D:\work\wt\bc-winrelease`，`agent/rc-winrelease`，base `833b880`（browser-cli PR #1） |
| patch 系列 | 本分支 `chromium/patch-manifest.json` 001–033 **+** 共享 checkout 已应用的 034（`sha256 1185dd0b…5d57c4`，in-process DComp init）与 035（`sha256 2108bcb2…95cb2`，overlay info after DComp），来自 `bc-gpu-dcomp` / `bc-rc-security` 工作树 |
| GN args | `chromium/args-windows-release.gn`（SHA-256 `bb9e8887…e476194`），无 WinDev 改动 |
| 输出 | `D:\c\src\out\WinRelease`（`out/WinDev` 未触碰） |
| 编译器 | clang `llvmorg-24-init-7747-g62397f8b-29`，VS BuildTools 2026 14.51.36231，Windows SDK 10.0.28000.0 |
| 配置命令 | `bash scripts/configure-windows-release.sh --checkout D:/c/src`（等价 `gn gen out/WinRelease`；本机实际 `gn.exe gen out/WinRelease` 用时 30.6 s，生成 36,075 targets） |
| 构建命令 | 正式入口 `scripts/build-windows-release.sh --checkout D:/c/src --jobs 6`；本轮因会话后台任务 60 分钟上限，用 `timeout 3300 … --jobs {6,8,12,16,20} --allow-more-jobs` 分块续跑，最终 `ninja -C out/WinRelease -j 20 chrome` exit 0 |

> 可复现性说明：共享 checkout 的 patch 状态是 001–033 + 034/035；034/035 不在本分支
> manifest 内，属于其它 RC 工作流的 working-tree 修改。构建日志与源码状态必须一起保存，
> 不能只用本分支 commit 复现该二进制。

### 1.1 与 WinDev 的唯一配置差异

`chromium/args-windows-release.gn` 只相对 `chromium/args-windows.gn` 改变
`is_component_build`（true → false），并显式声明 `is_official_build = false`
（WinDev 默认也是 false，但这里写成显式值，避免 future default 漂移）。
其余保持一致：`is_debug=false`、`symbol_level=0`、`blink_symbol_level=0`、`target_cpu="x64"`、
`use_remoteexec=false`、`branding_file_path`、`browser_cli_win_icon_dir`、
`disable_fieldtrial_testing_config=true`。

关键约束：

- **不启用** `BrowserDynamicCodeDisabled`（浏览器 ACG 会让 Sogou Xbyak JIT 失败，
  `ExitProcess(3)`）；本轮不通过释放 ACG 换取启动率。
- **不使用** `--in-process-gpu` / `--single-process` / `--no-sandbox` /
  `--disable-direct-composition`；GPU 走默认独立进程。
- **不是** `is_official_build=true`：没有 PGO/ThinLTO/orderfile，不能作为公开产物。

---

## 2. 编译期证据

| 证据 | WinRelease | WinDev |
| --- | --- | --- |
| `out/…/args.gn` 中 `is_component_build` | `false` | `true` |
| `gn desc //chrome/browser:browser defines` 中 `COMPONENT_BUILD` | 0 | 1 |
| `obj/chrome/chrome_dll.ninja` 编译行 `-DCOMPONENT_BUILD` | 无 | 有 |
| `IS_COMPONENT_BUILD`（partition_alloc buildflags） | `false` | `true` |
| `FIELDTRIAL_TESTING_ENABLED`（`gen/components/variations/service/buildflags.h`） | `0` | `0` |
| `dcheck_always_on` | `true` | `true` |
| `is_official_build` / `chrome_pgo_phase` | `false` / `0` | `false` / `0` |

`FIELDTRIAL_TESTING_ENABLED=0` 表示测试 field-trial 配置（含
`BrowserDynamicCodeDisabled` 与 `NetworkServiceSandboxWindows`）完全不进入该二进制；
`--enable-field-trial-config` 也不会应用任何 study。因此 `NetworkServiceSandbox`
必须由产品入口显式开启（见 §3），不能依赖 field trial。

---

## 3. NetworkServiceSandbox 评估与启用方式

### 3.1 事实

- 定义：`sandbox/policy/features.cc`，`BASE_FEATURE(kNetworkServiceSandbox,
  FEATURE_DISABLED_BY_DEFAULT)`；受 `IsNetworkSandboxSupported()`（Win10 20H1+、
  非 ARM64）与 local-state 策略 `NetworkServiceSandboxEnabled` 约束。
- 该版本 Chromium **没有** `enable_features` GN arg（`gn args --list` 无此项），
  本仓库又禁止为发布构建改 Chromium 源码，因此**构建期没有合法的“默认开启”开关**。
- 默认未注入 variations seed 时，官方 Chrome/Edge 与 WinDev/WinRelease 的 network utility
  都是 `--service-sandbox-type=none`、Medium IL、无策略（实测见 §5）。

### 3.2 本轮采用

在产品启动入口显式传递：

```text
--enable-features=NetworkServiceSandbox
```

（记录于 `config/windows-release-runtime-flags.env`；矩阵与 TSF smoke 都使用它。）
实测效果：network utility 变为 **Low IL + AppContainer=1**，`Signature`(CIG)、
`DynamicCode`(ACG, `0x5`)、`ExtensionPointDisable`、`ChildProcess`、`FontDisable`、
`ImageLoad(NoRemote/NoLowLabel)` 全部打开、`SystemCallFilter`(Win32k)=0，
与官方 Chrome 同 flag 下的逐项策略位一致。

### 3.3 边界与未决

- 这是**运行时开关**：直接双击 `out/WinRelease/chrome.exe`（不带该 flag）仍然不沙箱。
  公开产物必须在 launcher/更新配置中保证该 switch 始终存在，或提交一个经安全评审的
  Chromium patch 把 Windows 默认值改为 enabled；当前仓库两者都没有，属于 release blocker。
- `NetworkServiceCodeIntegrity`（network service 启动前 CIG）**已评估但未启用**：它只在
  non-component/non-ASAN 且 network sandbox 开启时有效；本轮不把它偷偷打开，需要单独的
  代理/证书/下载兼容性回归。
- 不使用企业策略 `NetworkServiceSandboxEnabled` 作为消费级产品默认（需要机器策略）。
- 用户在同一命令行追加 `--disable-features=NetworkServiceSandbox` 时可以关闭；launcher
  的 append 顺序不是安全边界。

### 3.4 为什么不改用测试 field-trial 配置

`disable_fieldtrial_testing_config=true` 是 Sogou TSF 的硬前提：测试配置会打开
browser ACG（`BrowserDynamicCodeDisabled`），使 SogouPY 的 Xbyak JIT 失败并以
`ExitProcess(3)` 结束。不能用“恢复测试配置 + 关闭 ACG study”的方式换取
`NetworkServiceSandboxWindows`，因为配置是否应用不由产品控制；显式运行时 flag 是唯一
可审计的路径。

---

## 4. 构建结果

| 项 | 值 |
| --- | --- |
| 开始 | 2026-11-01 15:34:30（本机） |
| 完成 | 2026-11-01 21:03:56 |
| 墙钟 | **5 h 29 m 26 s** |
| 最终 ninja | `ninja -C out/WinRelease -j 20 chrome` **exit 0**；`build-windows OK` |
| 图规模 | 58,343 条 command / 47,647 个 CXX object（`chrome` 目标闭包） |
| `chrome.dll` | 461,359,104 B（约 440 MiB），2026-11-01 21:03 |
| `chrome.exe` | 4,192,768 B，2026-11-01 21:03 |
| `chrome_elf.dll` | 2,068,480 B，2026-11-01 21:02 |
| SHA-256 | exe `0441080d13dcab32a87b66b3e3ee90d0122db5a64ccf8e6ed9ef04a097067bb1`；dll `23197bf6df27765c23e2415cd94d1d5a72a0bd90a817518c9021dca92700677d`；elf `3f8831db87a711ae03894a8612db158e663f0ef9d0d9161c129108e8d2cca87b` |
| 日志 | `D:\c\logs\winrelease\build-windows-*.log`、`build-runner-chunk*.log`、`gn-gen.log` |

**为什么是分块构建**：本会话的后台任务在 60 分钟被外部终止。chunk1 从 15:34:30 跑到
16:34:34，最后一条是普通 `Scalarizer.cpp` 编译，**没有 clang 诊断、没有 LLVM OOM 行、
没有 clang crash dump**；重启后同一对象正常编译。这是外部终止，不是源码/工具链失败。
后续每次用 `timeout 3300`（≤55 分钟）续跑，chunk 边界不改变产物。

**jobs**：默认仍是 `-j 6`；为压缩本会话墙钟，chunk 依次提高到 `-j 8/-j 12/-j 16/-j 20`
（`--allow-more-jobs`）。全程未出现 clang OOM；最低空闲内存约 4 GiB（-j20 的 Blink 阶段），
随后回升到 12–20 GiB。这个选择只影响墙钟，不影响编译产物或测试结论。

**失败项**：最终构建 0 个真实编译失败。唯一一次 `FAILED:` 是 chunk1 被外部终止时的
`Scalarizer.obj`（无编译器输出）；没有把它当作代码失败，也没有改任何 flag 绕过。

**WinDev 未被触碰**：`out/WinDev/chrome.exe` 仍是 2026-10-27、`chrome.dll` 仍是
2026-10-31 的时间戳；`out/WinRelease` 是独立目录。

---

## 5. 安全对照：进程缓解 / 模块面 / CIG / 启动

方法：`tests/windows-release/harness/release-security-compare.mjs`，6 个场景 × 3 次 =
**18/18 run 全部 ok，exit 0**。每个场景使用独立 `--user-data-dir`（`fs.mkdtempSync`），
headless 仅用于免窗口测量；不使用 `--no-sandbox`/`--in-process-gpu`/`--single-process`；
只终止本次 root PID 的后代（exe + ancestry + creation time 校验）。原始数据：
`D:\c\logs\winrelease\matrix\summary.json`（每个 run 另有 `run.json`/`mitigations.json`）。

### 5.1 进程缓解与启动（3 次运行的中位数）

| 场景 | 进程数 | browser | GPU | renderer | network | storage | 模块 b/g/r/n | DevTools ms | page ready ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WinRelease 默认 | 8–9 | Medium AC0 CIG0 ACG0 | Low CIG0x5 ACG0 | Untrusted CIG0x5 ACG0 | Medium AC0 CIG0 ACG0 | Untrusted CIG0x5 ACG1 | 141/3, 70/7, 23/5, 48/5 | 384 | 780 |
| WinRelease + NetworkServiceSandbox | 8–9 | Medium AC0 CIG0 ACG0 | Low CIG0x5 ACG0 | Untrusted CIG0x5 ACG0 | **Low AC1 CIG0x5 ACG1** | Untrusted CIG0x5 ACG1 | 142/3, 70/7, 23/5, 42/5 | 402 | 785 |
| WinDev 默认（component） | 9–10 | Medium AC0 CIG0 ACG0 | Low CIG0x5 ACG0 | Untrusted CIG0x5 ACG0 | Medium AC0 CIG0 ACG0 | Untrusted CIG0x5 ACG1 | 631/493, 559/497, 513/495, 538/495 | 629 | 1254 |
| WinDev + NetworkServiceSandbox | 8–11 | Medium AC0 CIG0 ACG0 | Low CIG0x5 ACG0 | Untrusted CIG0x5 ACG0 | Low AC1 CIG0x5 ACG1 | Untrusted CIG0x5 ACG1 | 631/493, 559/497, 513/495, 532/495 | 612 | 1264 |
| 官方 Chrome 默认 | 10–13 | Medium AC0 CIG0 ACG0 | Low CIG0x5 ACG0 | Untrusted CIG0x5 ACG0 | Medium AC0 CIG0 ACG0 | Untrusted CIG0x5 ACG1 | 141/5, 69/6, 20/3, 44/3 | 338 | 579 |
| 官方 Chrome + NetworkServiceSandbox | 12–13 | Medium AC0 CIG0 ACG0 | Low CIG0x5 ACG0 | Untrusted CIG0x5 ACG0 | Low AC1 CIG0x5 ACG1 | Untrusted CIG0x5 ACG1 | 142/5, 69/6, 20/3, 39/3 | 323 | 646 |

“模块 b/g/r/n” = 进程加载模块总数/来自安装目录的模块数（browser/GPU/renderer/network）。
CIG/ACG 为 `GetProcessMitigationPolicy` 的 raw 值（`0x5` = MicrosoftSignedOnly +
MitigationOptIn；ACG `0x1` = ProhibitDynamicCode）。

**逐字段对照**：脚本把 `GetProcessMitigationPolicy` 的全部 18 个策略索引、每一个解码
bit、token Integrity Level 与 AppContainer 状态逐项对比。结果：

- `winrelease-default` vs `chrome-default`：browser / GPU / renderer / network / storage
  **0 处差异**；
- `winrelease-netsandbox` vs `chrome-netsandbox`：同样 **0 处差异**。

这是**策略位层面**的等价，不等于安全等价：签名、构建形态、模块来源、启动前 CIG 时序
仍不同（见 5.2/5.3）。

### 5.2 模块面：non-component 把 component 的 493/495 个安装目录模块降到 3–5 个

| 进程 | WinDev（component） | WinRelease（non-component） | 官方 Chrome |
| --- | --- | --- | --- |
| browser | 631 / 493 | **141 / 3** | 141 / 5 |
| GPU | 559 / 497 | **70 / 7** | 69 / 6 |
| renderer | 513 / 495 | **23 / 5** | 20 / 3 |
| network（默认） | 538 / 495 | **48 / 5** | 44 / 3 |
| network（sandbox） | 532 / 495 | **42 / 5** | 39 / 3 |
| storage | 513 / 495 | **23 / 5** | 19 / 3 |

WinRelease 安装目录模块的逐文件签名（`process-mitigations.ps1` 的 `installDirModules`）：

- browser（3）：`chrome.dll` NotSigned、`chrome.exe` NotSigned、`chrome_elf.dll` NotSigned；
- renderer（5）：上述 3 个 + `dbghelp.dll` Valid、`msdia140.dll` Valid（Microsoft 签名，
  pre-start CIG 允许加载）；
- GPU（7）：上述 5 个 + `D3DCompiler_47.dll` Valid、**`dxcompiler.dll` NotSigned**；
- network（5）：chrome.dll/exe/elf + dbghelp.dll Valid + msdia140.dll Valid。

对比官方 Chrome：所有安装目录模块均 `Valid`（Google 签名），包括 `dxcompiler.dll`。
因此 non-component 解决了“493 个未签名 component DLL 全部进进程”的攻击面问题，但
**public 产物仍必须签名 chrome.dll/chrome_elf.dll/chrome.exe 与 dxcompiler.dll**；
`dbghelp.dll`/`msdia140.dll` 是 Microsoft 签名，可加载，但正式打包应确认是否随包分发。

### 5.3 CIG 在进程出生时的状态（pre-start vs delayed）

| 场景 | renderer | storage | GPU | network |
| --- | --- | --- | --- | --- |
| WinRelease 默认 | **首次采样即 0x5**（birth） | **birth 0x5** | 0 → 0x5（+203 ms） | 0（未沙箱） |
| WinRelease + NetworkServiceSandbox | **birth 0x5** | **birth 0x5** | 0 → 0x5（+152 ms） | **birth 0x5** |
| WinDev 默认（component） | 0 → 0x5（+292 ms） | 0 → 0x5 | 0 → 0x5（+514 ms） | 0（未沙箱） |
| WinDev + NetworkServiceSandbox | 0 → 0x5（+292 ms） | 0 → 0x5（+305 ms） | 0 → 0x5（+514 ms） | 0 → 0x5（+331 ms） |

解释（避免过度声称）：

- **renderer** 是代码级 pre-start CIG 的直接证据：non-component 下
  `chrome_content_browser_client.cc` 的 `PreSpawnChild` 在
  `#if !defined(COMPONENT_BUILD)` 内对 renderer 设置
  `MITIGATION_FORCE_MS_SIGNED_BINS`，该属性随 CreateProcess 生效，所以第一次成功采样
  已是 `0x5`；component 构建整段被编译掉，renderer 只能靠子进程 `LowerToken` 的
  delayed mitigation，因而能观察到 0→1 的窗口（约 292 ms）。
- GPU 在两种构建里都是 delayed（`PreSpawnChild` 对 kGpu 不设 CIG），WinRelease 的窗口
  约 152–203 ms；这与官方 Chrome 的 165–191 ms 一致。
- network sandbox 的 CIG 在策略上属于 delayed mitigation。WinRelease 第一次采样就看到
  `0x5`，是因为 non-component 只加载 chrome.dll/elf（42 个模块），`LowerToken` 在第一次
  WMI 轮询前已完成；component 构建要先加载约 495 个 DLL，才会出现 0→1 的 331 ms 窗口。
  **不能**据此声称 network 也是 pre-start CIG；这里没有 `NetworkServiceCodeIntegrity`。
- 轮询分辨率约 10–40 ms（WMI 往返）；更短的 0 窗口不可观测，未做推断。

### 5.4 启动

在同一台机器、同一会话、warm file cache 下，3 次中位数：

| 场景 | launch → DevTools listening | launch → 页面 ready |
| --- | --- | --- |
| WinRelease 默认 | 384 ms | 780 ms |
| WinRelease + NetworkServiceSandbox | 402 ms | 785 ms |
| WinDev 默认（component） | 629 ms | 1254 ms |
| 官方 Chrome 默认 | 338 ms | 579 ms |
| 官方 Chrome + NetworkServiceSandbox | 323 ms | 646 ms |

non-component 相对 component 明显更快（约 1.6×），与安装目录模块从 493/495 降到 3/5 的
方向一致；仍比官方 Chrome 慢约 15–35%，因为本构建是 `is_official_build=false`
（无 PGO/orderfile）。这些数字不是正式发布的性能结论。

### 5.5 测试卫生

- 18 个 run 的 argv 逐项检查：无 `--no-sandbox`/`--in-process-gpu`/`--single-process`/
  `--disable-direct-composition`/`--use-mock-keychain`；`--headless=new` 只用于本矩阵
  （免窗口测量），不作为产品启动配置或 TSF 证据；
- 每个 run 独立 profile，结束后删除；`stop-verified.ps1` 只停止 exe 路径 + 父链到本次
  root PID + 创建时间均匹配的显式 PID，没有 `taskkill /IM` 或按名字杀进程；
- 所有 run 的 `cleanup.skipped` 都是 `not-running`（浏览器已正常退出），没有残留进程。

---

## 6. 真实 Sogou TSF 输入

方法：`tests/windows-release/harness/tsf-smoke.mjs`（2026-11-01 21:13 运行）。
可见窗口是 runbook 的要求（真实 IME 无法 headless 自动化）；输入使用
`WScript.Shell.SendKeys` 走 OS 输入队列，**不**使用 CDP `Input.insertText`/paste/直接写
value；每次发送前校验 OS 前台窗口就是本次浏览器窗口，否则 fail closed 不发送。

**verdict PASS，15/15 checks PASS**，退出码 **0**：

| 证据 | 值 |
| --- | --- |
| `<input>` | `你好`；`compositionstart=1`，数据 `n → ni → nih → niha → nihao → 你好` |
| `<textarea>` | `你好`；composition 累计 2 |
| contenteditable | `我`；composition 累计 3，数据 `w → wo → 我` |
| 退格 | 删除真实字符，长度下降 |
| Sogou TSF 模块（browser 进程） | `SogouTSF.ime`、`SogouPY.ime`、`isgpet_bundle64.dll`、`Resource.dll`、`PicFace64.dll`、`systembeautify_bundle64.dll`、`ichat_bundle64.dll` |
| GPU | 独立 `gpu` 进程（无 `--in-process-gpu`） |
| network utility | **Low IL / AppContainer=1 / CIG 0x5 / ACG 0x1**（42 模块，5 个来自安装目录） |
| browser | CIG 0（Sogou 需要 JIT；未开 ACG），126 模块 |
| 前台恢复 | 测试后恢复原前台窗口 |
| 清理 | profile 删除；`stop-verified` 无残留 |
| 截图 | `D:\c\logs\winrelease\tsf\run-2026-11-01T13-13-28-624Z\tsf-window.png` |

启动参数（测试用；产品启动入口尚未实现）：

```text
chrome.exe --user-data-dir=<fresh temp> --no-first-run --no-default-browser-check
  --disable-sync --disable-component-update --disable-background-networking
  --remote-debugging-port=0 --window-size=1100,850 --noerrdialogs
  --enable-logging --log-file=<run>\chrome.log
  --disable-features=Translate,MediaRouter,OptimizationHints
  --enable-features=NetworkServiceSandbox
  --new-window file:///<repo>/tests/windows-release/fixtures/tsf-smoke.html
```

结论：non-component WinRelease 在 **OOP GPU + NetworkServiceSandbox 开启**的条件下，
真实 Sogou TSF 组合输入、三个输入面提交、退格均正常；浏览器进程没有 CIG/ACG，没有
TSF skip/interception。

---

## 7. 复现

```bash
# 配置（会拒绝把 out/WinDev 当成 release 输出）
bash scripts/configure-windows-release.sh --checkout D:/c/src

# 构建（内部未签名；不签名、不打包、不启动）
bash scripts/build-windows-release.sh --checkout D:/c/src --jobs 6

# 安全矩阵（WinRelease / WinDev / Chrome × 默认 / NetworkServiceSandbox）
node tests/windows-release/harness/release-security-compare.mjs --repeat=3

# 真实 TSF smoke（会显示一个窗口，结束后恢复前台）
node tests/windows-release/harness/tsf-smoke.mjs

# 打包/签名/更新 preflight（只读；internal 类应成功且 releasable=false）
bash scripts/preflight-windows-release.sh --checkout D:/c/src --mode internal
```

静态门禁：`tests/check-browser-launch-hygiene.sh`（18 个入口，**OK**）与
`tests/run-script-tests.sh`（**234 passed / 0 failed**，Windows + python3 shim；
日志 `D:\c\logs\winrelease\run-script-tests3.log`）。

---

## 8. Release blocker（本轮未解决，全部为明确失败项）

1. **未签名**：`chrome.exe`、`chrome.dll`、`chrome_elf.dll` 均 `NotSigned`；GPU 进程还会
   从安装目录加载 **`dxcompiler.dll` NotSigned**。没有 Windows Authenticode 发布者身份
   （`config/product-identity.env` 只有 macOS Team ID/codesign SHA-1）。不能产生或宣称
   签名发布产物。non-component 的 pre-start CIG 用 `AllowExtraDll` 显式放行
   `chrome.dll`/`chrome_elf.dll`，这是**未签名内部构建的兼容措施**，正式产物必须签名，
   不能把 allowlist 当作长期方案。
2. **非 official 构建**：`is_official_build=false`、`chrome_pgo_phase=0`，无 PGO/ThinLTO；
   `dcheck_always_on=true`（与 WinDev 相同），因此 DCHECK 路径仍在本产物中生效。
   启动/性能（比官方 Chrome 慢约 15–35%）不代表正式产物。
3. **NetworkServiceSandbox 不是产品默认**：只在显式 `--enable-features` 下生效；launcher
   尚未实现，直接启动 `chrome.exe` 不带该 flag。公开产物必须在启动入口保证该 switch，
   或提交经安全评审的默认值 patch。
4. **更新器/安装器不存在**：无 `update-metadata.json`、无签名更新包、无回滚/anti-downgrade；
   preflight 的 `public` 模式因此必然 exit 1。
5. **隐藏窗口长驻崩溃未修复**：`docs/windows-rc-2026-11-01.md` §2 的
   `layer_tree_view.cc:554` DCHECK（约 60s）仍存在；non-component 构建没有改变该代码路径，
   且 `dcheck_always_on=true`，所以该失败在本产物上仍然可达（本轮未重跑长驻用例）。
6. **P1 原生 cookie 导入未落地**：`036` 仍是 proposed，未编译。
7. **逐模块签名/SmartScreen/EDR 行为未验证**：运行时可证明 non-component 只加载
   3–7 个安装目录模块（Chrome 为 3–6），其中非 Microsoft 模块只有 chrome.dll/elf/exe
   与 dxcompiler.dll；但没有正式签名链、逐模块验证记录与信誉证据。
8. **安装目录额外模块**：runtime 加载 `dbghelp.dll`、`msdia140.dll`（Microsoft 签名）
   来自 out 目录；正式打包必须决定是否随包分发，并确认 DLL 搜索顺序不会优先加载安装目录
   副本（本轮只记录事实，未做打包）。
9. **源码可复现性**：共享 checkout 的 patch 状态是 001–033（本分支）+ 034/035（其它 RC
   工作流的 working-tree 修改），仅用本分支 commit 不能复现该二进制；正式构建必须记录
   patch SHA-256 与完整源码 diff（见 §1）。
