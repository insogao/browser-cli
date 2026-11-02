# Browser CLI native cookie-import v2（2026-11-01）

分支：`agent/rc-p1-native`
Worktree：`D:\work\wt\bc-rc-p1`
编译验证分支：`agent/rc-p1-build`（`D:\work\wt\bc-p1-build`，2026-11-01）
关联：

- BrowserPilot 侧契约与来源扩展：`D:\work\wt\bp-rc-p1\scripts\import\contract.mjs`、
  `D:\work\wt\bp-rc-p1\extensions\import-source\`
- 本仓库 proposed patch：`chromium/patches/proposed/037-browser-cli-cookie-import.patch`
- 原 P1 背景：`docs/p1-browser-data-import-2026-10-30.md`

> **2026-11-02 update.** E2E execution and the two UI-thread DCHECK blockers are
> recorded in `docs/p1-native-cookie-import-e2e-2026-11-02.md`. Patch 037 was
> compiled into `out/WinDev` on 2026-11-01; it is still **not** in
> `chromium/patch-manifest.json`. The “未进入 chromium 构建 / 未链接” wording in
> the 2026-11-01 status table is the historical snapshot for the manifest/CI
> state at that date; the new document is the current E2E result.

---

## 1. 结论（先说边界）

| 部分 | 状态 | 说明 |
| --- | --- | --- |
| v2 安全契约（票据、TTL、一次性、绑定、失败/速率限制、三态） | **已定义并冻结** | 与 BrowserPilot 侧 `browser-cli.cookie-import/v2` 对齐 |
| 来源扩展 → 原生中转（Native Messaging） | **BrowserPilot 侧已实现并测试** | 无 HTTP；import 模式不启动 loopback 监听；见 BrowserPilot PR/worktree |
| Browser CLI 原生 CookieStore 写入端点 | **已逐 TU 编译验证，未进入 chromium 构建** | proposed patch 已按当前 checkout 修正；5 个 TU 用 `out/WinDev` 的真实 clang-cl 参数编译通过，9 个 policy 单测通过；仍未加入 `chromium/patch-manifest.json`，未 `gn gen`/`ninja chrome`，未在真实 Browser CLI 中运行 |
| 目标浏览器内独立授权确认 | **proposed patch 中实现（编译通过，未运行）** | `cookie_import_dialog.cc`；真实产品必须在目标浏览器内由用户点击允许 |
| 重启后 Cookie 仍在 | **未验证（未链接进 chrome、未 E2E）** | 设计依赖目标 `CookieManager`/`CookieStore`，不是本仓库测试报告 |
| 真实 Chrome/Edge 扩展 + 真实目标端到端 | **未验证** | 本轮禁止使用真实用户 Profile / 真实 Cookie / 真实 token |

**因此不得宣称“产品闭环完成”。** 本轮交付的是：v2 契约、BrowserPilot 侧可测试实现、
目标侧修正后的 proposed patch、逐 TU 编译证据与 policy 单测；patch 尚未链接进
`chrome.exe`，原生写入路径未做端到端运行。

---

## 2. 威胁模型与信任边界

1. **来源 Profile 只读**：来源浏览器不被关闭/重启/写入；扩展只用 `chrome.cookies` 读取
   用户勾选域名。
2. **目标 Profile 单写者**：Cookie 只由目标浏览器进程自己的 `CookieManager` 写入；
   外部进程不写 `Network/Cookies` SQLite。
3. **不解密、不提取密钥**：不使用 `OSCrypt`、DPAPI、App-Bound elevation service、
   `Local State` 或来源 `Cookies` 密文。
4. **同一 OS 用户是信任边界**：同一登录用户下的恶意进程可以读取该用户的浏览器数据；
   本设计不声称防御同等权限的恶意软件。它防御的是：网络上的其他主机、其他 OS 用户、
   非授权扩展、重放、票据猜测、越权域名、静默写入。
5. **目标浏览器内的用户确认是授权闭环的最后一环**：来源扩展的勾选、CLI 的票据输入、
   原生中转的转发都不能替代目标浏览器里的“允许”。

---

## 3. 传输：Native Messaging + 命名管道（不是 localhost HTTP）

产品路径：

```
来源 Chrome/Edge
  extensions/import-source (cookies + storage + nativeMessaging)
    │  chrome.runtime.connectNative("com.browserpilot.browseragent")
    ▼
BrowserPilot native messaging host（import 模式）
  - 只读 stdin/stdout 的 4-byte LE 长度 + JSON 帧
  - 不监听 TCP、不写 auth.json/session 文件、不转发通用命令
  - 只在内存中保留本次 job，校验票据后主动连接目标
    │  \\.\pipe\browser-cli-import-<128-bit random>
    ▼
Browser CLI（proposed 034 patch）
  - Windows named pipe，DACL 仅 owner + SYSTEM，PIPE_REJECT_REMOTE_CLIENTS
  - 单实例、单客户端、单活动 job
  - 目标浏览器内弹窗要求用户确认来源/域名范围
  - 通过 CookieManager（net::CookieStore::SetCanonicalCookieAsync 的 mojo 前端）写入
```

- **没有产品 HTTP 端点**。`scripts/import/target/local-target.mjs` 的 HTTP 目标只是
  BrowserPilot 仓库的测试替身，报告 `native:false`、`transport:"http-test"`、
  `authorization.simulated:true`。
- 原生中转和目标都不接受“客户端批准”；pipe 协议没有 `approve` 帧。
- 原生中转不把 ticket、Cookie 值、域名写入日志；目标日志只含计数、jobId、固定错误码。

---

## 4. 一次性票据与绑定

| 属性 | 值 |
| --- | --- |
| 熵 | 160 bit（20 random bytes） |
| 表示 | Crockford base32，32 字符；显示为 `BCLI-XXXX-XXXX-...`（8 组） |
| 规范化 | 大写、去 `BCLI-`/分隔符、`O→0`、`I/L→1` |
| TTL | 5 分钟 |
| 使用次数 | 首次 `hello` 即消费；第二次 `hello` 返回 `ticket-consumed` |
| 失败限制 | 60 秒内 5 次失败后锁定 60 秒（`rate-limited`） |
| 会话限制 | 每小时最多 10 次 hello 尝试 |
| 存储 | 目标进程内存；会话描述符只写 `ticketHash = SHA-256("browser-cli-ticket-v2:" + ticket)`，不写明文票据 |
| 绑定 | 目标进程 PID/启动时间、目标 Profile hash、targetInstanceId、jobNonce、jobDigest、授权范围 `domainsDigest` |

票据不是“6 位配对码 + Origin”。Origin/扩展 ID 仍由 Chrome 的 native messaging
`allowed_origins` 校验，但它只是附加控制，不是授权本身。

**目标进程身份绑定**：目标在 pipe 连接上调用
`GetNamedPipeClientProcessId()`，与中转 hello 中的 `hostPid` 比较；不匹配返回
`host-identity-mismatch`。**目标 Profile 绑定**：会话描述符包含
`profileHash = SHA-256(per-process salt | profile path)`；目标只在自身 Profile 内运行。
**任务绑定**：`ticket` 首次提交绑定 `jobNonce`、`jobDigest` 与 `domainsDigest`；第二个
任务使用同一票据会被拒绝。**域名范围绑定**：来源 consent 的 `domainsDigest`、目标弹窗
批准的范围、目标写入时使用的 selected domains 三者必须一致。

---

## 5. 目标浏览器内独立授权

目标在收到完整 job 后：

1. 状态置为 `awaiting_authorization`，向来源扩展返回 `accepted` + 只含计数的 status；
2. 在**目标浏览器进程内**弹出 `CookieImportApprovalDialog`，显示：
   - 来源浏览器类型、Profile 标签（可选）；
   - Cookie 总数、Session Cookie 数（明确提示重启不保证保留）；
   - 用户勾选的域名列表（这是用户已选择的元数据，不是 Cookie 值）；
   - “只有你本人启动的导入才允许”的警告；
3. 用户点击 **Allow** 才写入；点击 Deny/关闭窗口返回 `denied`，**不写任何 Cookie**。
4. `authorization.targetUserConfirmed=true` 只由这个弹窗产生；来源扩展/CLI/中转都无法
   设置它。`authorization.simulated` 在产品目标中必须为 `false`；测试替身为 `true`。

---

## 6. 三态与“不自我声称登录”

| 状态 | 权威来源 | 不得从以下推断 |
| --- | --- | --- |
| `dataCopied` | 书签/历史 bundle/apply | Cookie 数量 |
| `cookieImported` | 目标 CookieManager 回调 applied/failed | 页面登录 |
| `loginVerified` | 用户本人 `confirm-login --confirm-user-action` | Cookie 计数、目标授权、目标响应 |

目标返回的 public job view 中 `loginVerified.status` 永远是 `not_verified`。目标授权
（`authorization`）是导入范围授权，不是登录验证；CLI、报告和扩展都分开显示。

Session Cookie 在 `sessionCookies.mayNotSurviveRestart` 中明确标注；capabilities 中
`sessionCookiePersistence = "not_guaranteed"`。持久 Cookie 的重启保留由目标
CookieManager/持久化后端决定，本 proposed patch 未构建，因此未验证。

---

## 7. Cookie 属性规则（目标端强制）

目标使用 `net::CanonicalCookie::CreateSanitizedCookie` + `CookieManager`，并在之前做
显式策略检查：

1. **HostOnly/Domain**
   - `hostOnly=true`：`domain` 不得以 `.` 开头；构建 URL 时使用该 host，向
     `CreateSanitizedCookie` 传空 domain，得到 host-only cookie。
   - `hostOnly=false`：`domain` 必须以 `.` 开头；传带点 domain，得到 Domain cookie。
   - 两者不一致 → `invalid-domain`。
2. **Public Suffix List**：Domain cookie 的注册域为空（`com`、`co.uk`、`github.io` 等）
   → `public-suffix-rejected`。Host-only cookie 允许在公共后缀 host 上（浏览器自身规则）。
3. **Cookie 前缀**（按最长前缀优先）：
   - `__Host-Http-`：Secure + HttpOnly + Path=/ + host-only；
   - `__Host-`：Secure + Path=/ + host-only；
   - `__Secure-`：Secure；
   - `__Http-`：Secure + HttpOnly；
   - 不满足 → `cookie-prefix-rejected`。
4. **SameSite/Secure**：`no_restriction`（None）必须 Secure → 否则 `samesite-rejected`；
   `lax`/`strict`/`unspecified` 原样映射。
5. **Partitioned/partition key**：`topLevelSite` 必须是 `http(s)://host` 的 schemeful
   site（无端口/路径/查询/凭据）；`hasCrossSiteAncestor` 为 bool；Partitioned cookie
   必须 Secure；`CookiePartitionKey::FromStorage` 失败 → `partitioned-cookie-rejected`。
6. **过期**：持久 Cookie 必须有有限、未来的 `expirationDate`；Session Cookie 不得带
   `expirationDate`；否则 `expiry-rejected`。
7. **名称/值/路径**：控制字符、NUL、超长（name 4096 / value 8192 / path 1024 /
   domain 255 bytes）→ `invalid-cookie`。
8. **域名选择**：Cookie domain 必须等于或属于用户勾选域名；否则整个 job 拒绝
   （`invalid-domain`）。

失败按 Cookie 计数，返回固定 code 集合；不返回原始异常文本、Cookie 名、值或域名。

边界（2026-11-01 对齐实现）：中转与目标在**解析 job** 时对结构性非法记录（字段缺失/
类型错误/前缀/过期/HostOnly/SameSite/Partitioned 不合法）整体拒绝，返回 session error
（`protocol-mismatch`/`job-digest-mismatch` 等），不创建 job；只有解析通过后的
**PSL 检查**与**写入时 CookieManager 拒绝**按 Cookie 计数（`public-suffix-rejected`、
`cookie-prefix-rejected`、`samesite-rejected`、`partitioned-cookie-rejected`、
`expiry-rejected` 等）。来源扩展与中转本身也执行同一套结构校验，因此产品路径不会把
“部分记录非法、部分可写”的 job 交给目标。

---

## 8. Wire 协议（v2）

### 8.1 Native Messaging（扩展 ↔ 中转）

- 4-byte little-endian 长度 + UTF-8 JSON（Chrome 协议）。
- `hello`：`{type:"hello", protocol:2, mode:"cookie-import", extensionVersion}`
  → `{type:"hello-ok", protocol:2, ...}`。
- `begin`：`{type:"begin", ticket, jobNonce, jobDigest, totalBytes, chunkCount}`。
- `chunk`：`{type:"chunk", jobNonce, index, data:<base64>}`；单 chunk 解码 ≤ 256 KiB，
  总 job ≤ 16 MiB，≤ 64 chunks。
- `commit`：`{type:"commit", jobNonce}`。
- 中转 → 扩展：`ready`、`accepted`、`status`（public view）、`cancel-ok`、`error`。
- `jobDigest = SHA-256(UTF-8 payload bytes)`；中转和目标都在解析前验证。

### 8.2 Named pipe（中转 ↔ 目标）

- 同样的 4-byte LE + JSON 帧，单帧 ≤ 512 KiB。
- 中转在目标 session 目录（默认
  `%LOCALAPPDATA%\BrowserCLI\import\sessions`）按 `ticketHash` 找到 descriptor，
  校验 `expiresAt`、`targetPid` 存活，再连接 `pipeName`。
- 握手：`hello`（ticket/hostPid/jobNonce/jobDigest/…）→ `ready`（sessionId/targetPid/
  native/capabilities）；然后 `job-begin`/`job-chunk`/`job-commit`；
  目标返回 `accepted`、`status`、`error`、`cancel-ok`。
- 目标只在 `job-commit` 验证 `jobDigest` 后创建 job；`accepted` 时状态为
  `awaiting_authorization`。

### 8.3 Session descriptor

```json
{
  "schema": "browser-cli.cookie-import.session/v2",
  "protocolVersion": 2,
  "targetProduct": "Browser CLI",
  "targetVersion": "156.0.8067.0",
  "targetPid": 1234,
  "targetStartTimeMs": 1790000000000,
  "targetInstanceId": "32 hex",
  "profileHash": "64 hex",
  "ticketHash": "64 hex",
  "transport": "named-pipe",
  "pipeName": "\\\\.\\pipe\\browser-cli-import-<32 hex>",
  "createdAt": "ISO8601",
  "expiresAt": "ISO8601"
}
```

不含明文票据、Cookie 值或域名；5 分钟 TTL；目标退出/终端状态/消费后删除。

---

## 9. Native Chromium 映射（proposed patch）

文件（全部在 `chromium/patches/proposed/037-browser-cli-cookie-import.patch`）：

- `chrome/browser/browser_cli/cookie_import/cookie_import_policy.{h,cc}`：
  票据、domain digest、job digest、字段校验、PSL/前缀/Partitioned 规则、
  `CreateSanitizedCookie` 映射。
- `.../cookie_import_service.{h,cc}`：单例、票据/TTL/失败限制、job 状态机、
  目标弹窗回调、逐 Cookie 写入、flush、三态 view。
- `.../cookie_import_pipe_server_win.{h,cc}`：Win32 单实例命名管道、DACL、
  `PIPE_REJECT_REMOTE_CLIENTS`、`GetNamedPipeClientProcessId`、帧解析。
- `.../cookie_import_dialog.{h,cc}`：票据窗口 + 目标内授权弹窗。
- `.../cookie_import_policy_unittest.cc`：票据/绑定/PSL/前缀/HostOnly/Partitioned/
  CanonicalCookie 单测。
- `chrome_browser_main.cc`：仅在
  `--browser-cli-cookie-import` 且 initial profile 时 `Start(profile)`；
  `PostMainMessageLoopRun()` 调 `Stop()`。
- `chrome/browser/BUILD.gn`：Windows 下依赖新 target。
- `chrome/browser/browser_cli/cookie_import/BUILD.gn`：拆出纯策略 target
  `cookie_import_policy` 供单测链接；`cookie_import` 只含 service/dialog/pipe。

写入路径是
`CookieManager::SetCanonicalCookie`，它是浏览器进程到 network service 的 mojo 前端；
network service 内部调用 `CookieStore::SetCanonicalCookieAsync`。**没有直接写
`Network/Cookies` SQLite，没有读取来源密文。** 每次 job 结束后调用
`FlushCookieStore`。

**状态：proposed，未加入 `chromium/patch-manifest.json`，尚未 `gn gen`/`ninja chrome`，
尚未在真实浏览器中运行。** 2026-11-01 已按当前 post-033 checkout 修正并验证：
`git apply --check` 只读通过；5 个 TU 用 `out/WinDev` 的 clang-cl 参数逐个编译通过；
`cookie_import_policy_unittest.cc` 9 个用例用真实 Chromium `net`/`base` 链接并全部通过
（详见 §12）。这仍不等于 GN target 可构建、chrome 可链接或 runtime 可验收。

---

## 10. 测试

本仓库新增：

- `tests/tools/cookie-import/test_reference_target.mjs`：自包含的 v2 命名管道参考目标
  与协议/安全属性测试（测试替身，memory backend，`native:false`，
  `authorization.simulated:true`；票据一次性、TTL、失败限制、HostOnly/Domain、
  SameSite/Secure、Partitioned、前缀、PSL、Session Cookie 提示、三态、日志/输出无值）。
- `tests/tools/test_cookie_import_native.py`：proposed patch 与文档静态门禁
  （必需 API/规则/无 SQLite/无 OSCrypt/未加入 manifest/文档声明未集成/禁止已移除的旧
  Chromium API）。
- `tests/run-script-tests.sh`：在 node/python 可用时运行上述测试。

这些测试**不启动浏览器**、不读取真实 Profile、不使用真实 Cookie/token；它们证明协议
和静态约束，不证明原生 CookieStore 写入在真实浏览器中成功。

编译验证（不是浏览器测试，2026-11-01，详见 §12）：

- `cookie_import_policy.cc`、`cookie_import_service.cc`、`cookie_import_dialog.cc`、
  `cookie_import_pipe_server_win.cc`、`cookie_import_policy_unittest.cc` 以及带 hunk 的
  `chrome_browser_main.cc` 用 `out/WinDev` 中真实的 clang-cl 头文件、defines、警告
  设置（`/W4 /WX`、`-Wshadow`、raw-ptr/unsafe-buffers 插件）逐个编译通过。
- `CookieImportPolicyTest.*` 9/9 通过（链接真实 `base.dll`、`net.dll`、`crcrypto.dll`、
  `url_lib.dll`、gtest），覆盖票据格式/哈希、domain digest 顺序无关、job 解析、拒绝
  非法属性、PSL/域名选择、HostOnly/Domain/Partitioned 的 `CanonicalCookie` 构造、
  前缀/PSL 失败码、`CookieInclusionStatus` 映射。
- 未执行：`gn gen`、`ninja -C out/WinDev chrome`、`cookie_import_unittests` test target
  的真实 GN 链接、任何浏览器启动或 E2E 写入。

---

## 11. 剩余工作（按顺序）

1. 在 Windows Chromium checkout 上应用 036，执行 `gn gen` 与
   `ninja -C out/WinDev chrome cookie_import_unittests`；修复 GN/链接问题后加入
   manifest 与 CI。**本轮只验证了逐 TU 编译与 policy 单测，没有验证 GN target。**
2. 用隔离 Windows 测试机 + 一次性测试账号/合成 Cookie 验证：
   来源 Chrome/Edge `chrome.cookies` → 原生 host → 目标 pipe → 目标 CookieManager；
   目标弹窗 Allow/Deny；写入后 Cookie 属性（host-only/domain/partition/samesite/prefix）
   与重启持久化。
3. 验证 native host 注册（`allowed_origins` 同时包含主扩展与 import-source 扩展 ID）；
   Web Store/企业策略分发；Chrome 156 对 unpacked 扩展的限制。
4. 目标内 UI 的异步/关闭/取消路径、长时间用户不响应、进程退出、pipe 断开、
   失败计数与速率限制的真实 runtime 测试。
5. 真正的“登录验证”仍只能由用户本人完成；不得由 Agent/目标响应自动写入。

---

## 12. 本地编译验证与精确构建步骤（2026-11-01）

### 12.1 本轮修正的编译阻塞（均已验证）

按 `D:\c\src`（Chromium 156.0.8067.0，post-033 working tree）核对 API 后发现并修正：

1. `base::Value::Dict` / `base::Value::List` 已改名为 `base::DictValue` /
   `base::ListValue`；`cookie_import_policy.h` 还缺少 `base/values.h`。
2. `base/containers/contains.h` 与 `base::Contains` 已不存在；改为
   `base::SplitStringPiece` + `std::find`。
3. `GURL url(std::string(raw));` 是 most-vexing-parse；改为 `GURL url{...}`。
4. `std::optional<Job>` 不能持有前向声明的 `Job`；改为 `std::unique_ptr<Job>` +
   `std::make_unique<Job>`，`job_ = ...` 相应调整。
5. `BrowserContext::GetDefaultStoragePartition` 在当前版本是非静态成员；改为
   `profile_->GetDefaultStoragePartition()`。
6. `views::DialogDelegateView` 默认构造已私有且子类需在 friend 白名单；dialog 改为
   `views::DialogDelegate` + `SetContentsView` 组合，`ShowDialog` 接收
   `std::unique_ptr<views::DialogDelegate>`。
7. 单测里 `base::DictValue` 不可拷贝（`FindDict` 返回指针），改为 `std::move`；
   修正 `IsDomainCookie()` 断言（Domain cookie 应为 true）。
8. `cookie_import_unittests` 不再依赖整个 `:cookie_import`（否则要链接
   service/dialog/pipe/content），拆出纯策略 target `:cookie_import_policy`。

同时收紧的行为（编译验证，未 E2E）：

- hello 必须携带 `hostPid` 且等于 `GetNamedPipeClientProcessId` 的 pipe 客户端 PID，
  否则 `host-identity-mismatch`（无法取得 PID 时 fail closed）。
- hello 中的 `jobNonce/jobDigest/totalBytes/chunkCount` 与 `job-begin` 必须一致，
  否则 `job-binding-mismatch`。
- `cancel` 若带 `jobNonce` 必须匹配。
- job 超时/拒绝后，在途 `SetCanonicalCookie` 回调不再继续写后续 Cookie。
- 管道部分写只丢弃已写前缀，避免整帧重发造成协议错位。
- 先创建命名管道并等待成功，再写 session descriptor，避免 native host 找到
  “尚无管道”的会话。
- 票据/授权窗口 `Show()` 后调用 `Activate()`；用户显式启动导入时窗口可前置。

### 12.2 精确应用与构建步骤（由主 Agent 串行执行）

```sh
# 1. 只读检查（不修改 checkout）
cd /path/to/chromium/src
git apply --check /path/to/browser-cli/chromium/patches/proposed/037-browser-cli-cookie-import.patch

# 2. 应用（post-033 working tree；不要在未确认基线上应用）
git apply /path/to/browser-cli/chromium/patches/proposed/037-browser-cli-cookie-import.patch

# 3. 重新生成 GN（Windows）
gn gen out/WinDev

# 4. 编译 policy 单测与 chrome
autoninja -C out/WinDev cookie_import_unittests
autoninja -C out/WinDev chrome

# 5. 运行单测（不启动浏览器）
out/WinDev/cookie_import_unittests.exe --single-process-tests \
  --gtest_filter=CookieImportPolicyTest.*

# 6. 只有 4/5 均通过后，才可把 036 加入 chromium/patch-manifest.json
```

注意：

- 本仓库只读验证使用 `git apply --check`，没有在 `D:\c\src` 应用或构建；上述步骤必须
  由主 Agent 串行执行，不得并行。
- `--browser-cli-cookie-import` 目前只由 `chrome_browser_main.cc` 消费；产品 CLI /
  BrowserPilot 侧仍需实际传入该开关（当前 `cli/`、`launcher/` 为占位目录）。
- 本轮没有执行 `gn gen`/`ninja`/`chrome` 启动；不得把 §12.1 的逐 TU 编译当作
  GN target、链接或 E2E 已通过。

### 12.3 本轮编译/单测证据（可复现）

- 工具链：`D:\c\src\out\WinDev` 的 clang-cl 24.0.0 + 现有 `defines`/`cflags`/
  `cflags_cc`（含 `/W4 /WX`、`-Wshadow`、raw-ptr 与 unsafe-buffers 插件）。
- 编译对象：`cookie_import_policy.cc`、`cookie_import_service.cc`、
  `cookie_import_dialog.cc`、`cookie_import_pipe_server_win.cc`、
  `cookie_import_policy_unittest.cc`、带 hunk 的 `chrome_browser_main.cc` → 全部 exit 0。
- 链接：用 `foreground_assist_unittests` 的链接输入替换为
  `cookie_import_policy_unittest.obj` + `cookie_import_policy.obj`，lld-link exit 0。
- 运行：`--single-process-tests --gtest_filter=CookieImportPolicyTest.*` → 9 passed。
- 未验证：GN target 解析、`chrome` 链接、CookieManager 真实写入、浏览器进程内
  授权弹窗、重启持久化。
