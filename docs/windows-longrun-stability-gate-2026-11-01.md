# Windows 长驻稳定性门禁（20 分钟多窗口，2026-11-01）

状态：**harness 已就绪；本轮 WinRelease 不可用，已在 WinDev 上执行基线门禁**。
本文件不宣称 Windows RC 发布就绪。

## 1. 目标与边界

短矩阵（BrowserPilot `scripts/test-release-candidate-e2e.mjs`，14/14 case）回答
“release-like 启动参数下产品链路是否工作”。本门禁回答另一个问题：

> 8 个并发 Agent Space/窗口、真实 hidden/minimized/occluded/前台呈现转换、
> 扩展 SW 重启、AI/ASSIST/HUMAN 所有权转换下，浏览器能否连续 20 分钟不崩溃、
> 截图仍然新鲜、资源有界？

门禁**不**重新解释 release 矩阵，也不以周期重载、计划关窗、遮挡/后台禁用参数、
周期截图来掩盖崩溃。已知 managed hidden 的
`layer_tree_view.cc:554` presentation-callback DCHECK 正是在本门禁中被当作失败，
而不是被 screenshot/重启绕过。

## 2. 构建选择

唯一选择顺序（`--chrome` 未显式给出时）：

1. `D:\c\src\out\WinRelease\chrome.exe`（release-like，`is_component_build=false`）；
2. `D:\c\src\out\WinDev\chrome.exe`（component development build，仅基线）。

本轮执行时 `D:\c\src\out\WinRelease\chrome.exe` **不存在**：`out/WinRelease` 已完成
gn gen，但 ninja 在 `[15719/58343]` 因
`obj/third_party/dawn/third_party/directx-shader-compiler/lib_transforms/Scalarizer.obj`
FAILED 而停止，没有生成 `chrome.exe`。因此本轮结果是 **WinDev baseline**，不是
release-build 门禁；报告中的 `build_kind` 会明确记录。WinRelease 构建修复/完成后，
直接重跑同一命令即可（`--chrome` 指向 `D:/c/src/out/WinRelease/chrome.exe`），
无需改 harness。

## 3. Harness

入口：`tests/tools/longrun-stability-gate.py`（Windows-only；Job Object 精确清理）。

两个隔离 profile 的浏览器进程并发运行，共 8 个窗口：

| 窗口 | profile | 内容 | 初始 native 状态 | 计划 |
|---|---|---|---|---|
| w1 | normal | dynamic DOM/rAF | visible（测试置顶） | 每 60s 截图新鲜度 |
| w2 | normal | 2D canvas | visible（测试置顶） | 每 120s 截图新鲜度 |
| w3 | normal | 跨站 OOPIF/iframe | native visible；可能被无关用户窗口遮挡 | 首尾截图 |
| w4 | normal | WebGL | native visible → 被 w2 完全遮挡 @30s | 遮挡后 900s + 收尾截图 |
| w5 | agent/managed | MV3 extension page + SW | managed hidden | SW 重启；HUMAN 呈现；收尾截图 |
| w6 | agent/managed | dynamic DOM/rAF | managed hidden | 不周期截图（防掩盖） |
| w7 | agent/managed | 2D canvas | 短暂 show 后 minimized | 不周期截图 |
| w8 | agent/managed | WebGL | managed hidden | 不周期截图 |

说明：w1/w2 用 `SetWindowPos(HWND_TOPMOST)` 作为测试呈现动作，保证在桌面存在
无关全屏窗口时仍有一个真正可见的 normal text 和 normal canvas 窗口；这不是
`--disable-backgrounding-occluded-windows` 一类的产品/后台禁用参数，也不移动窗口到
屏幕外。w3/w4 不做置顶，harness 记录它们实际的 `fully_occluded`，不把“native
visible”误报成“用户可见”。

固定时间线（相对 soak 开始，setup 不计入 1200s）：

- `t=5s`：w7 show→minimize；
- `t=8s`：通过 CDP `Target.closeTarget` 停止 probe 扩展的 service worker，并用
  page→worker 真实消息证明新 worker 实例恢复；
- `t=15/25/35s`：对 w5 执行 `chrome.browserCliInputOwnership` 的
  HUMAN → ASSIST → AI 转换（版本单调 +1，set 后 get 复核）；
- `t=30s`：w2 移到 w4 矩形并置顶，w4 变成完全遮挡；
- `t=40s`：第二进程 `--browser-cli-user-explicit-show`（纯产品路径，
  ProcessSingleton 转发）呈现一个现有 managed 窗口；记录产品实际选择的窗口、
  `BROWSER_CLI_USER_EXPLICIT_SHOW ... reason=existing-window-presented` 日志和
  Win32 `visible=true`；
- `t=240s`：原生 `SW_HIDE` 把被呈现窗口送回 managed/hidden；
- `t=600/660/720s`：仅当 w5 仍存活时再跑一轮所有权转换；
- `t=900s`：w4 遮挡状态下的截图新鲜度；
- `t=1200s`：收尾 probe + 每个存活窗口的最终截图。

### 3.1 防作弊（启动前 fail closed）

- 禁止：`--no-sandbox`、`--headless`、`--in-process-gpu`、`--single-process`、
  `--disable-gpu`、`--disable-direct-composition`、
  `--disable-backgrounding-occluded-windows`、`--disable-renderer-backgrounding`、
  `--disable-background-timer-throttling`、`CalculateNativeWinOcclusion`、
  `--site-per-process`、`--enable-field-trial-config`；
- 每个 session 独立 `tempfile.mkdtemp()` profile；启动后从真实命令行复核
  `--user-data-dir` 与禁止参数；
- 源码级：无 `Page.reload`/`location.reload`；唯一的 `Target.closeTarget` 是
  service-worker 重启；
- 清理只用 `tests/tools/win_job.py` 的 Job Object（精确进程树），无镜像名 kill、
  无全局 kill；
- managed 窗口不做周期截图；只有 w1/w2 做周期截图，w5 仅在被 HUMAN 呈现后截一次，
  其余窗口只有收尾截图；
- w1/w2 的置顶是测试呈现动作（保证真正可见的 normal text/canvas），不是产品参数，
  不是遮挡/后台禁用，也不把窗口移到屏幕外。

### 3.2 取证

每次 15s 采样：

- 每窗口 CDP probe（paint/rAF/tick/visibility/focus）+ `Target.targetCrashed` /
  `Inspector.targetCrashed` / detached 事件；
- 进程树（psutil）：browser / gpu / network / storage / utility / renderer /
  extension-renderer / crashpad 的 PID、RSS、private bytes、handles、CPU 秒数；
- renderer/GPU/network PID 消失与重启（排除 SW 重启的预期退出）；
- Chromium log（`--enable-logging --log-file`）中的 DCHECK/FATAL/
  `PresentationTimeCallbackBuffer`/GPU/rendering crash 行；
- Win32 native 状态：`IsWindowVisible`、`IsIconic`、rect、foreground，
  以及 z-order 覆盖判定（完全遮挡的覆盖窗口）；
- 截图新鲜度：`__hsBump` 改变 DOM marker，`Page.captureScreenshot` 后要求
  marker 像素与 DOM paint counter 在 ±30 帧内一致且图像非空白；不是“有像素”即通过。
  marker 只编码 paintCount 的低 16 位，20 分钟 60fps canvas 会跨 65536，比较按
  modulo 65536 的最短有符号帧差，避免回绕误报。

证据目录（不提交仓库）：`D:\c\logs\longrun-<timestamp>\`
- `result.json`、`report.md`、`summary.txt`
- `samples.jsonl`（完整进程树）、`probes.jsonl`、`events.jsonl`、
  `resources.jsonl`
- `chrome-normal.log`、`chrome-agent.log`
- `screenshots/*.png`

## 4. 判定

- **FAIL**：任一窗口 crash/hung、presentation-buffer DCHECK、renderer/GPU/network
  真实消失、截图新鲜度失败、SW 重启失败、启动/setup 失败，或资源极端放大。
- **BLOCKED**：没有 WinRelease 构建、host restart 未覆盖、所有权 API 不可用等
  未完成项；在已有失败时 FAIL 优先。
- **PASS**：8 个窗口 20 分钟全部存活且新鲜、无 crash 证据、SW 重启/所有权转换/
  HUMAN 呈现/遮挡/最小化全部验证，资源在阈值内，且构建为 WinRelease。
- WinDev 结果即使稳定也只能是 BLOCKED（构建不满足 release-like），不能宣称
  release readiness。

## 5. 执行结果（WinDev baseline）

执行命令（原始证据 `D:\c\logs\longrun-20min-final`）：

```bash
python tests/tools/longrun-stability-gate.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --evidence-dir D:/c/logs/longrun-20min-final \
  --duration 1200 --sample-interval 15
```

- **Verdict: FAIL**（`result.json` / `report.md`，RC=1）。
- 构建：WinDev，`chrome.exe` mtime 2026-10-27T00:11:56+08:00，Chromium 156.0.8067.0。
- soak：1202.7 s（requested 1200 s；setup 17.5 s 不计入 soak）。
- 8 个窗口全部创建；normal 4 个 + agent/managed 4 个。

### 5.1 每窗口

| 窗口 | session | 内容 | native 初始 → 最终 | 实际完全遮挡 | probe | paint stalls | 死亡@ | 截图新鲜度 |
|---|---|---|---|---|---|---|---|---|
| w1 | normal | dynamic text | visible → visible | no | 81 | 0 | - | 22/22 |
| w2 | normal | 2D canvas | visible → visible | no | 81 | 0 | - | 12/12 |
| w3 | normal | OOPIF/iframe | visible → visible | yes（被无关全屏窗口） | 81 | 1 | - | 2/2 |
| w4 | normal | WebGL | visible → visible | yes（w2 + 无关窗口） | 81 | 20 | - | 3/3 |
| w5 | agent | extension page | hidden → hidden | yes | 21 | 0 | **323.6s** | 1/1（HUMAN 后）；final dead |
| w6 | agent | dynamic text | hidden → hidden | yes | 4 | 0 | **84.8s** | 0/0 |
| w7 | agent | 2D canvas | minimized → minimized | no | 4 | 0 | **84.8s** | 0/0 |
| w8 | agent | WebGL | hidden → hidden | yes | 4 | 0 | **84.8s** | 0/0 |

结论：**4 个 normal 窗口连续 20 分钟存活且截图新鲜**（w2 在 ~67k frames 的
16-bit marker 回绕已用 modulo 校验，final delta=+4）。**4 个 managed 窗口全部
死亡**。

### 5.2 崩溃数据

- `presentation-buffer-dcheck`: **4**（`layer_tree_view.cc:554`，
  `callbacks.size() <= PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60)`）：
  - 60.1 s：3 个 renderer（w6/w7/w8）；
  - 315.1 s：1 个 extension-renderer（w5，240 s 被 `SW_HIDE` 送回 managed 后）。
- `Target`/`Inspector.targetCrashed`: **4**：w6/w7/w8 @ **84.8 s**；w5 @ **323.6 s**。
- renderer PID 消失：**3**（lifetime 43.7 s）；extension-renderer PID 消失：**1**
  （lifetime 298.7 s）。
- GPU crash: **0**；network utility crash: **0**；browser exit: **0**。
- 时间关系与根因一致：w6/w7/w8 从未被原生呈现，~60 s DCHECK；w5 在 41.4 s 被
  `--browser-cli-user-explicit-show` 真实呈现（`visible=true`，producer
  `reason=existing-window-presented`），241.0 s 被隐藏，~74 s 后再次 DCHECK，
  ~83 s 后 target crash。

### 5.3 其它场景

- **SW 重启 PASS**：CDP `Target.closeTarget` 关闭 probe worker 后，page→worker
  真实消息返回新实例：`workerStartedAt` 从 1793522100340 变为 1793522124251
  （`workerAgeMs` 1 ms），`n` 15→16。
- **HUMAN/ASSIST/AI 所有权 PASS**：w5 上 `chrome.browserCliInputOwnership`
  set/get 复核 human v1、assist v2、ai v3 全部 `accepted=true` 且 `verified_after=true`。
  600/660/720 s 的后续转换在 w5 死亡后按设计跳过，不再产生超时噪声。
- **HUMAN 呈现 PASS**：产品实际呈现 w5；`BROWSER_CLI_USER_EXPLICIT_SHOW
  result=handled reason=existing-window-presented`，Win32 `visible=true`。
- **Normal/Agent 呈现转换 PASS**：41.4 s 呈现 → 241.0 s 原生隐藏；
  `normal/agent modes concurrent` 因 agent 窗口全部崩溃而 FAIL（模式并发在崩溃后
  不复存在）。
- **截图新鲜度 PASS**：w1 22 次、w2 12 次、w4 3 次全部通过；w5 在 HUMAN 呈现后
  1 次通过。
- **资源趋势 PASS**（无极端放大；数值为两个 session 合计）：

| 指标 | start | end | peak |
|---|---|---|---|
| private | 1.822 GB | 1.506 GB | 1.827 GB @45s |
| RSS | 5.164 GB | 4.261 GB | 5.219 GB @45s |
| handles | 14,973 | 10,786 | 15,815 @45s |
| process count | 24 | 20 | 24 @1s |
| GPU process private | 371.8 MB | 293.6 MB | 371.8 MB @1s |
| network utility private | 97.8 MB | 93.1 MB | 97.8 MB @1s |
| CPU seconds（累计） | 27 | 583 | 583 @1185s |

CPU 平均约 **1.4% of 32 logical CPUs**。内存/句柄在 agent 窗口崩溃后下降，normal
窗口 20 分钟内没有单调暴涨。

### 5.4 场景矩阵

PASS：dynamic normal page、canvas、oopif/iframe、occluded window、HUMAN 呈现、
HUMAN/ASSIST/AI 所有权、repeated screenshot freshness、SW restart、resource trends。
FAIL：webgl、extension page、long hidden window、long minimized window、
normal/agent modes concurrent、renderer/gpu/network crash counts。
BLOCKED：host restart。

**结论：FAIL。managed/background presentation 的 long-run 崩溃未修复；不能宣称
Windows RC 发布就绪。** normal 4 窗口 20 分钟稳定只证明非 managed 路径在本轮
没有回归。

## 6. 未证明 / 阻塞项

- WinRelease `chrome.exe` 不存在；本轮不是 release-build 门禁（non-component 构建在
  `[15719/58343]` 因 `Scalarizer.obj` FAILED 停止）。
- BrowserPilot Native Messaging Host 的 host 进程重启未覆盖：harness 只重启并
  验证扩展 service worker；host restart 标记 BLOCKED。
- 进程级 Normal↔Agent 重启未在本门禁内重复：本门禁并发运行 normal/agent 两种
  profile，并验证 managed→user-explicit→managed 的呈现转换；两种模式的短启动矩阵
  由 release-like E2E 覆盖。
- HUMAN/ASSIST/AI 只验证所有权 `setOwnership/getOwnership` 转换与 HUMAN 呈现；
  grant/input 预算、真实人类鼠标/键盘、真实 Sogou TSF/IME 不在本门禁范围内。
- 真实用户前台不干扰、cookie 导入、GPU VRAM（只记录 GPU process private/RSS，
  没有逐进程 Dedicated Usage 计数器）不在本门禁范围内。
- 短 release 矩阵的“无全局 DevTools 端口”结论不被本门禁重新声明：本门禁为了
  8 窗口 probe 使用 127.0.0.1 随机端口的 test-only 控制通道，并在
  `flag_classification`/`config` 中记录。

## 7. 复现

本机静态测试说明：`tests/check-browser-launch-hygiene.sh` **PASS**（17 entry points）；
`tests/run-script-tests.sh` 的 **168 PASS / 60 FAIL** 中，60 个失败全部来自依赖
`python3` 的旧组（18–29、99）——本机 `python3` 是 Windows Store 占位符，直接运行会
提示 “Python was not found”；新增组 **31（Long-run stability gate）6/6 PASS**。
Windows 侧官方静态门 `python tests/tools/test_windows_scripts.py` **24/24 OK**，
`scripts/verify-windows.sh --dry-run` rc=0，`tests/check-extension-allowlist.py
--self-test` PASS。

```bash
# 静态门禁（不启动浏览器）
tests/check-browser-launch-hygiene.sh
tests/run-script-tests.sh

# 自检 / dry-run
python tests/tools/longrun-stability-gate.py --self-test
python tests/tools/longrun-stability-gate.py --dry-run

# 20 分钟门禁；--chrome 显式指向 WinRelease（存在时）或 WinDev 基线
python tests/tools/longrun-stability-gate.py \
  --chrome D:/c/src/out/WinRelease/chrome.exe \
  --evidence-dir D:/c/logs/longrun-2026-11-01 \
  --duration 1200 --sample-interval 15

# 校验已有证据的一致性
python tests/tools/longrun-stability-gate.py --check D:/c/logs/longrun-2026-11-01
```
