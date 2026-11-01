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
| patch 系列 | 本分支 `chromium/patch-manifest.json` 001–033 **+** 共享 checkout 已应用的 034（in-process DComp init）与 035（overlay info after DComp），来自 `bc-gpu-dcomp` / `bc-rc-security` 工作树 |
| GN args | `chromium/args-windows-release.gn`（SHA-256 `bb9e8887…e476194`），无 WinDev 改动 |
| 输出 | `D:\c\src\out\WinRelease`（`out/WinDev` 未触碰） |
| 编译器 | clang `llvmorg-24-init-7747-g62397f8b-29`，VS BuildTools 2026 14.51.36231，Windows SDK 10.0.28000.0 |
| 构建命令 | `scripts/build-windows-release.sh --checkout D:/c/src --jobs 6`（等价 `ninja -C out/WinRelease -j 6 chrome`） |

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

（构建完成后填写：ninja 退出码、总目标数、耗时、产物路径/大小/时间戳、失败项。）

---

## 5. 安全对照：进程缓解 / 模块面 / CIG / 启动

方法：`tests/windows-release/harness/release-security-compare.mjs`。每个场景使用独立
`--user-data-dir`（`fs.mkdtempSync`），headless 仅用于免窗口测量；不使用
`--no-sandbox`/`--in-process-gpu`；只终止本次 root PID 的后代（exe + ancestry +
creation time 校验）。全部数据在 `D:\c\logs\winrelease\matrix\summary.json`。

（构建完成后填写表格与结论。）

---

## 6. 真实 Sogou TSF 输入

方法：`tests/windows-release/harness/tsf-smoke.mjs`。可见窗口是 runbook 的要求（真实 IME
无法 headless 自动化）；输入使用 `WScript.Shell.SendKeys` 走 OS 输入队列，**不**使用
CDP `Input.insertText`/paste/直接写 value；测试结束恢复原前台窗口，并按
`stop-verified.ps1` 规则清理。

（构建完成后填写 composition 事件、三个输入面 CJK、Sogou 模块、network sandbox 同进程
证据、截图路径与退出码。）

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

静态门禁：`tests/check-browser-launch-hygiene.sh`（18 个入口）与
`tests/run-script-tests.sh`（234 passed / 0 failed，Windows + python3 shim）。

---

## 8. Release blocker（本轮未解决，全部为明确失败项）

1. **未签名**：`chrome.exe`/`chrome.dll` `NotSigned`；没有 Windows Authenticode 发布者身份
   （`config/product-identity.env` 只有 macOS Team ID/codesign SHA-1）。不能产生或宣称
   签名发布产物。
2. **非 official 构建**：`is_official_build=false`、`chrome_pgo_phase=0`，无 PGO/ThinLTO；
   启动/性能不代表正式产物。
3. **NetworkServiceSandbox 不是产品默认**：只在显式 `--enable-features` 下生效；launcher
   尚未实现，直接启动 `chrome.exe` 不带该 flag。
4. **更新器/安装器不存在**：无 `update-metadata.json`、无签名更新包、无回滚/anti-downgrade。
5. **隐藏窗口长驻崩溃未修复**：`docs/windows-rc-2026-11-01.md` §2 的
   `layer_tree_view.cc:554` DCHECK（约 60s）仍存在；本轮 release-shape 构建未改变该代码路径。
6. **P1 原生 cookie 导入未落地**：`036` 仍是 proposed，未编译。
7. **逐模块签名/SmartScreen/EDR 行为未验证**；运行时可证明 only-MS + allowlist 模块加载，
   但没有正式签名链与信誉证据。
