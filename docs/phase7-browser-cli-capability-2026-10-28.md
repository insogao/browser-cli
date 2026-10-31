# Phase 7：Browser CLI 能力查询接口（`chrome.browserCli.getCapabilities()`）

日期：2026-10-28
对应规划：`docs/browser-cli-development-plan.md` Phase 7
对应 patch：`chromium/patches/026-browser-cli-capability-api.patch`
对应插件改动：`D:\work\BrowserPilot`（`src/background/capabilities.ts` 等，见 §5）

---

## 1. 目标与边界

Phase 7 要回答一个问题，且只回答一个问题：

> 同一份 BrowserPilot 插件，此刻跑在**普通 Chrome** 还是**Browser CLI** 上？如果是后者，哪些增强能力真的可用？

明确**不做**的事（本轮范围）：

- 不实现 MCP；
- 不实现任务调度器；
- 不实现多站点并发查询；
- 不引入第二套插件产物或运行时替换插件代码。

因此这是一个"开关"：插件探测到一个内核注入的接口就走 Enhanced 路径，探测不到就走 Generic 路径，**永不通过 User-Agent、产品名或窗口标题推断能力**。

---

## 2. 为什么不能靠 UA / 产品名

| 方案 | 问题 |
| --- | --- |
| 读 `navigator.userAgent` | 可伪造；Chrome 正在冻结 UA；且普通 Chrome 与 Browser CLI 同属 156.x，版本号无法区分 |
| 读 `BRANDING` 派生的产品名 | 网页不可见，且扩展侧拿不到稳定入口；产品名是品牌信息，不是能力契约 |
| 探测 `chrome.windows.create({focused:false})` 的实际效果 | 需要一次真实窗口创建，副作用大，且普通 Chrome 上也会"成功"，无法区分 |
| **自定义 Extension API 是否存在** | 内核层面不存在即 `undefined`，网页与未授权扩展都看不到；零副作用。**本方案采用** |

结论：**API 的存在性本身就是能力信号**。

---

## 3. 内核侧实现（patch 026）

### 3.1 接口形状

`chrome.browserCli.getCapabilities()` → Promise：

```json
{
  "product": "Browser CLI",
  "browserVersion": "156.0.8067.0",
  "protocolVersion": 1,
  "managedMode": true,
  "backgroundWindows": true,
  "backgroundCapture": true,
  "hardFocusIsolation": true,
  "foregroundAssist": true,
  "maxConcurrentWindows": 8
}
```

`protocolVersion` 是契约版本：插件不认识的值必须**降级而不是猜测**。

### 3.2 每个字段都来自活状态，不是编译期常量

这是本实现最关键的一条纪律。`browser_cli_api.cc` 里唯一的状态源是：

```cpp
const bool managed_mode =
    views::BrowserCliPresentationPolicy::Get()->IsManagedModeEnabled();
```

`backgroundWindows` / `backgroundCapture` / `hardFocusIsolation` /
`foregroundAssist` 与 `maxConcurrentWindows` 全部由它派生。

原因：`--browser-cli-background-presentation` 是整条展示策略的总闸。没有这个开关时，
内核里虽然存在这些代码，但**没有任何策略在生效**——此时若仍然返回 `true`，就是在
对插件撒谎，插件会按"有后台隔离保护"行事，而实际上窗口会直接上屏。因此：

| 运行形态 | `managedMode` | 四个能力位 | `maxConcurrentWindows` |
| --- | --- | --- | --- |
| 带 `--browser-cli-background-presentation` | `true` | 全 `true` | 8 |
| 同一二进制，不带该开关 | `false` | 全 `false` | 1 |
| 普通 Chromium / Chrome | —（接口不存在） | — | — |

`maxConcurrentWindows` 在降级时返回 **1 而不是 0**：普通浏览器仍能安全驱动一个窗口，
"没有增强能力"和"一个窗口都不许开"必须可区分。这个数值是**建议上限**，强制执行属于
调度层（规划 Phase 7 要求 8），本轮不实现调度器。

### 3.3 只对受信任扩展开放

`chrome/common/extensions/api/_api_features.json`：

```json
"browserCli": {
  "allowlist": [
    "4E0786F5E275D23D7B66E0B522A104AE22613E3F",
    "0FC2B72BF7525EEFA66518C54DB602118B38EB5E8AE1B2992EEB0E57911C1722"
  ],
  "dependencies": [],
  "contexts": ["privileged_extension"]
}
```

三个要点，都踩过或验证过：

1. **allowlist 用的是扩展 ID 的哈希，不是 ID 字符串。**
   `SimpleFeature::IsIdInList` 比较的是 `HashedExtensionId` 的
   `value_sha1()` / `value_sha256()`，即 `SHA1(id字符串)` / `SHA256(id字符串)` 的
   大写 hex。直接写 `nnollghpaggbcdkkgoieneffnlijinio` 会静默不匹配，表现为
   "接口不存在"，极难排查。两个哈希都列出是因为 SHA-256 迁移仍在 feature flag 后，
   任一命中即可。
2. **不能写成数组形式**：只有一个对象时必须写成单对象，否则
   `feature_compiler.py` 报
   `A complex feature definition is only needed when there are multiple objects...`。
3. **必须声明 `dependencies` 或 `channel`**：否则报
   `Features must specify either a channel or dependencies`。这里用 `"dependencies": []`
   表示不依赖任何 manifest 权限——这也是"同一份 manifest 在普通 Chrome 可安装"的前提：
   **插件 manifest 里完全不出现这个权限**。

`contexts: ["privileged_extension"]` 保证网页侧（`kWebPage`）永远看不到该命名空间。

### 3.4 其余接线

| 文件 | 改动 |
| --- | --- |
| `chrome/common/extensions/api/browser_cli.json` | 新增，`Capabilities` 类型 + `getCapabilities` 函数 |
| `chrome/common/extensions/api/api_sources.gni` | 加入 `schema_sources_`（`enable_extensions` 分支） |
| `chrome/browser/extensions/api/browser_cli/` | 新增 `BUILD.gn` + `browser_cli_api.{h,cc}` |
| `chrome/browser/extensions/api/BUILD.gn` | `api_implementations` 增加依赖 |
| `extensions/browser/extension_function_histogram_value.h` | `BROWSERCLI_GETCAPABILITIES = 1989` |
| `tools/metrics/histograms/metadata/extensions/enums.xml` | 同步直方图枚举 |

补丁规模：9 文件 / 311 行 diff，sha256
`20d83a5d86f5fa8d469bb4817a0ddef9b4d27f5df415cbef98583f16c1d5a9fc`。

---

## 4. 实测中发现的两个真实陷阱

这两条是本轮最有价值的产出，都会让"看起来正确的实现"在生产里静默失效。

### 4.1 MV3 Service Worker 里没有裸 `chrome` 标识符

在扩展 SW 上下文中执行 `typeof chrome` 得到 `"undefined"`，而
`typeof globalThis.chrome` 是 `"object"`，且 `Object.keys(globalThis.chrome)`
确实包含 `browserCli`。

后果：任何 `chrome.browserCli` 的裸访问在 SW 里抛 `ReferenceError`。验收脚本最初
因此报告"接口不存在"，几乎误判为 allowlist 配错。

对策：内核侧无需改动（这是绑定时机的正常行为），插件与验收脚本一律走
`globalThis.chrome`。

### 4.2 API 注入晚于 Service Worker 创建（约 0.5s 竞态）

第一次探测（SW 刚可 attach 时）：

```
attempt 0 -> undefined
attempt 1 -> object      # ~0.5s 后
```

这是**会真实伤害产品**的竞态：插件若只在 `init()` 里探测一次，就会在 Browser CLI 上
拿到 `api-absent`，把宿主误判为普通 Chrome 并**永久降级**。

对策（`src/background/capabilities.ts`）：

- `waitForBrowserCliApi()` 在 5s 上限内以 100ms 间隔轮询（`API_APPEAR_TIMEOUT_MS`）；
- 普通 Chrome 上每次检查都是 `undefined`，走满窗口后正常降级，代价只是首次初始化多等
  一小段时间；
- 单元测试用 `apiAppearTimeoutMs: 0` 保持测试速度，并专门有一个"延迟 150ms 注入"的
  用例证明不会再误判。

---

## 5. 插件侧改动（BrowserPilot）

| 文件 | 改动 |
| --- | --- |
| `src/background/capabilities.ts` | 新增。三种结果：接口缺失 → generic；协议不认识 → generic；调用异常 → generic+原因；正常 → enhanced |
| `src/background/state.ts` | `GlobalState.hostCapabilities` |
| `src/background/index.ts` | `ensureCapabilitiesProbed()`，单次探测、与 `connectHost()` 并列 |
| `src/background/commands.ts` | 新增只读命令 `host_capabilities` |
| `src/shared/types.ts` | `CommandName` 增加 `host_capabilities` |
| `scripts/test-core.mjs` | 新增 8 组离线用例 |

降级判定顺序（fail closed）：

```
接口不存在                    → generic(api-absent)
调用抛异常                    → generic(call-failed: ...)
protocolVersion 非整数         → generic(invalid-protocol-version)
protocolVersion != 本插件支持值 → generic(unsupported-protocol: N)
managedMode != true           → generic(managed-mode-disabled)
以上全部通过                   → enhanced
```

注意第 5 条：**即使内核返回 `managedMode:false` 但其它标志位为 true，也不得相信它们**。
这条与 §3.2 的内核纪律互为校验，两侧都不单方面采信对方的乐观值。

`concurrencyLimit(caps)` 是调度层将来读取并发上限的唯一入口；本轮不实现调度器。

---

## 6. 验收结果

### 6.1 三项契约（`tests/tools/browser-cli-capability-cdp-check.py`）

全部 headless、独立临时 `--user-data-dir`、无可见窗口。

| 场景 | 宿主 | 期望 | 实测 |
| --- | --- | --- | --- |
| Enhanced | Browser CLI（带 managed 开关，**保留沙箱**） | 接口可调用且全部为 true | **PASS** |
| 降级 | 上游 Chromium 151.0.7922.34（Playwright 构建） | 接口不存在 | **PASS**（`chrome.browserCli is undefined`） |
| 网页隔离 | Browser CLI 页面上下文 | 不可见 | **PASS**（`present: false`） |

Enhanced 实测返回值：

```json
{"backgroundCapture": true, "backgroundWindows": true,
 "browserVersion": "156.0.8067.0", "foregroundAssist": true,
 "hardFocusIsolation": true, "managedMode": true,
 "maxConcurrentWindows": 8, "product": "Browser CLI", "protocolVersion": 1}
```

> 降级对照特意选用**真实上游 Chromium**而非"本仓库二进制 + 去掉开关"：
> 后者仍然包含该命名空间，只能证明开关接线，不能证明"普通浏览器会降级"。

### 6.2 插件自身状态（读 SW 的 storage.session）

| 宿主 | `hostCapabilities` |
| --- | --- |
| Browser CLI（managed） | `mode: "enhanced"`, `managedMode: true`, `maxConcurrentWindows: 8` |
| 上游 Chromium | `mode: "generic"`, `degradeReason: "api-absent"`, `maxConcurrentWindows: 1` |

同一份 `dist/` 同时跑在两个宿主上——满足"不构建两套插件"。

### 6.3 离线单测

`npm run test:core` 新增段落 8 组断言全 PASS：

- 接口缺失 → generic 且记录 `api-absent`；
- 调用抛异常 → generic 且记录原因（不向上抛）；
- `protocolVersion: 2` → generic；`protocolVersion: "1"`（字符串）→ generic；
- `managedMode:false` 但其它位全 true → **仍 generic**（不得被伪绿）；
- 正常 → enhanced，字段透传，`concurrencyLimit` 取宿主值；
- `maxConcurrentWindows: 0` → 回落到 1；
- 源码守卫：`capabilities.ts` 不得出现 `userAgent` / `navigator.platform` / `vendor`，
  且不得按 `product` 名字做分支；
- **注入竞态**：API 延迟 150ms 出现时必须判为 enhanced（回归 §4.2）。

### 6.4 能力真的可用：`chrome.debugger` 驱动页面（Phase 7b）

"声明了能力"和"能力真能用"是两件事。`tests/tools/browserpilot-page-drive-cdp-check.py`
在同一个构建里跑完整条自动化链路：

| 步骤 | 结果 |
| --- | --- |
| `chrome.windows.create({focused:false})` | ok（windowId/tabId 均返回） |
| `chrome.debugger.attach(tabId, "1.3")` | ok |
| `Runtime.evaluate` 注入 DOM | ok |
| `Runtime.evaluate` 读回文本 | `"hello"` |
| `Page.captureScreenshot` | 3078 字节，base64 前缀 `iVBORw0K`（PNG magic 正确） |
| `chrome.debugger.detach` | ok |
| 同时刻 `managedMode` | `true` |

两个实现细节值得记下：

1. **`data:` URL 不能用于扩展创建窗口**（Chrome 拒绝），所以窗口先开 `about:blank`，
   内容通过 debugger 注入——这反而更贴近真实用法，也顺带证明了注入链路。
2. 该扩展以 unpacked 方式加载，属于 developer mode，满足 `debugger` API 的
   `developer_mode_only` 门禁；若换成商店安装还需另行评估。

### 6.5 构建与静态门禁

| 检查 | 结果 |
| --- | --- |
| `gn gen out/WinDev` | rc=0，36112 targets |
| `api_registration.lib` / `generated_api_types.lib` | PASS；生成代码含 `BrowserCliGetCapabilitiesFunction` 与双哈希 allowlist |
| `ninja -C out/WinDev chrome.exe` | PASS（增量 359 步，exit 0） |
| `apply-patches.py --check --checkout D:\c\src` | **STATE applied**（26/26） |
| `scripts/verify-windows.sh`（含新增第 6 步） | **16/16 PASS** |
| `tests/tools/test_windows_scripts.py` | 24/24 PASS |
| `tests/check-browser-launch-hygiene.sh` | PASS（15 entry points） |
| BrowserPilot `tsc --noEmit` | PASS |

---

## 7. 已知限制

1. **`maxConcurrentWindows` 是建议值，不是强制。** 8 取自并行窗口验收的上限余量，
   真正的并发控制属于调度层，本轮未实现；插件目前只把它当只读元数据。
2. **`browserVersion` 与 Chromium 版本同源。** Browser CLI 暂不维护独立的语义化产品
   版本，两者都来自 `version_info::GetVersionNumber()`；等 Phase 9 引入独立版本号后
   再拆分字段（`protocolVersion` 已为此预留）。
3. **allowlist 是硬编码的 ID 哈希。** BrowserPilot 的 manifest `key` 若变更，扩展 ID
   随之改变，接口会静默消失，表现与"功能未实现"完全一致。为此提供了单命令漂移检查
   `tests/check-extension-allowlist.py`（见 §8）：它从 manifest 重新推导 ID 与两个哈希并与
   patch / 已应用的 `_api_features.json` 比对，缺任一哈希、或把原始 ID 写进 allowlist 都
   fail closed。`tests/tools/test_windows_scripts.py` 另有静态守卫（哈希必须存在、原始 ID
   不得出现）。**仍未自动化的是"什么时候该跑它"**——换 key 时必须记得执行，或在 CI 里
   接入插件仓库。
4. **普通模式（无 managed 开关）在本机仍会 exit 3。** 这是既有的 Sogou TSF 崩溃问题
   （patch 023 只在 managed 模式跳过 TSF），与本次改动无关；因此"同名二进制不带开关"
   这一降级分支只能靠离线单测覆盖，实机降级证明改用真实上游 Chromium。

---

## 8. 复现命令

```bash
# 静态：补丁链完整性
GIT_NO_LAZY_FETCH=1 python scripts/apply-patches.py --check --checkout D:/c/src

# 静态：脚本与契约测试
python -m unittest tests.tools.test_windows_scripts
bash tests/check-browser-launch-hygiene.sh
bash tests/run-script-tests.sh          # 需 python3 在 PATH

# 构建
export DEPOT_TOOLS_WIN_TOOLCHAIN=0
export vs2026_install="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools"
buildtools/win/gn.exe gen out/WinDev
third_party/ninja/ninja.exe -C out/WinDev -j 6 chrome.exe

# 端到端验收（含 Phase 7 第 6 步）
bash scripts/verify-windows.sh --checkout D:/c/src \
  --extension-dir D:/work/BrowserPilot/dist \
  --stock-chrome "C:/Users/<user>/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe"

# 单独跑能力契约
python3 tests/tools/browser-cli-capability-cdp-check.py \
  --chrome "D:/c/src/out/WinDev/chrome.exe" \
  --extension-dir "D:/work/BrowserPilot/dist" \
  --stock-chrome "C:/.../chrome.exe" \
  --result-json D:/c/logs/phase7-result.json

# 单独跑页面驱动（Phase 7b）
python3 tests/tools/browserpilot-page-drive-cdp-check.py \
  --chrome "D:/c/src/out/WinDev/chrome.exe" \
  --extension-dir "D:/work/BrowserPilot/dist" \
  --result-json D:/c/logs/phase7b-drive.json

# allowlist 漂移检查（插件 manifest key 变更后必须跑）
python3 tests/check-extension-allowlist.py --manifest D:/work/BrowserPilot/manifest.json
```

---

## 9. 后续（不在本轮）

- 调度层读取 `concurrencyLimit()` 并强制执行（规划要求 8）；
- native messaging host 注册（`native-host/register.ps1`，会写 HKCU 注册表，需单独授权后执行）；
- Phase 8：删除旧 Backlight workaround。
