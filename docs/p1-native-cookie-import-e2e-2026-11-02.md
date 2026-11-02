# P1 native CookieStore import E2E（2026-11-02）

范围：patch 037（`chromium/patches/proposed/037-browser-cli-cookie-import.patch`）
的真实最小闭环：

```
来源 Browser CLI（extensions/import-source，显式 consent）
  -> chrome.runtime.connectNative
  -> BrowserPilot native messaging host（import-only relay）
  -> 目标 Browser CLI 命名管道
  -> 目标浏览器内用户批准
  -> 目标 CookieManager 写入
  -> 本地合成测试站点登录
  -> 目标重启后持久化
```

测试代码：`D:\work\wt\bp-rc-p1\scripts\e2e\`。测试站点、
来源/目标 Profile、Cookie 全部为合成数据；没有读取真实 WorkBuddy
Cookie、真实 Profile 或真实 token。

---

## 1. 结论

| 项目 | 结论 | 证据 |
| --- | --- | --- |
| **已将 patch 037 编译进 `out/WinDev/chrome.dll` 的原始产物** | **BLOCKED** | 第一次 `hello` 即触发 DCHECK，目标进程崩溃；来源扩展收到 `target-disconnected` |
| 本地 E2E 构建热修（只在 `out/WinDev` 重新编译/链接，**不属于 patch 037**） | 8 个场景 PASS，1 个场景 FAIL（见 §4） | 见 §3、§4 |
| 原生 CookieManager 写入 | **PASS** | 目标 `cookieImported = imported`，`applied=2, failed=0`；测试站点返回 `LOGIN_OK`；`VALUE_MATCH` |
| 重启持久化 | **PASS** | 同一 Profile 重启后 `LOGIN_OK`；会话 Cookie 变为 `SESSION_MISSING` |
| 目标浏览器独立批准 | **PASS** | 目标批准窗口 `visible=True`（组合运行 HWND `15734624`），目标主窗口 `visible=False`；物理 `SendInput` 点击 Allow（topmost）；`authorization.simulated=false`，`source=target-browser-dialog` |
| 过期票据 | 执行层 **PASS**（5 分钟 TTL 后会话被销毁，票据不可再用），协议层 **FAIL**（`ticket-expired` 帧未送达） | `elapsedMs=298898`，`expiryOutcome=closed`，描述符删除，relay 返回 `target-not-running`；见 §4.9 |

**不得宣称产品闭环完成。** 原始 patch 037 产物在当前
`dcheck_always_on` 的 Windows 构建中无法完成第一次 `hello`；下面的
E2E 结果来自同一 `chrome.dll` 的本地构建热修，热修没有提交到 patch 037，
也没有修改 `D:\c\src` 的源码。

---

## 2. 构建与阻塞

### 2.1 构建

- Chromium `156.0.8067.0`，`D:\c\src\out\WinDev`，component build。
- patch 037 已应用到 `D:\c\src` working tree；`out/WinDev/chrome.dll`
  构建时间 `2026-11-01 22:27:02`，原始 SHA-256
  `07c7310e148bc3bafe0c782a98f15f0f8ac117ea47d9b8f96494030fe5552ae9`。
- 策略单测：`out/WinDev/cookie_import_unittests.exe
  --single-process-tests --gtest_filter=CookieImportPolicyTest.*` →
  **9/9 PASS**。
- BrowserPilot 侧：`test-native-import.mjs` **11/11**，
  `test-import-product.mjs` **42/42**，
  `test-browser-data-import.mjs` **15/15**，
  `sync-extension-contract.mjs --check` 通过。
- 静态门禁：`tests/check-browser-launch-hygiene.sh` PASS；
  `tests/run-script-tests.sh` 首次运行因当前 shell 的 `python3` 指向
  Windows Store 别名而有 60 个环境失败；用真实 Python 3.11 shim 重跑
  **223 passed / 0 failed**，其中 cookie-import v2 reference/static 测试
  均 PASS。

### 2.2 阻塞 #1：hello 阶段 UI 线程阻塞 I/O

第一次 `hello` 到达目标后，`HandleHello` → `RemoveDescriptor()` →
`base::DeleteFile()` 在 UI 线程触发：

```text
FATAL:base\threading\thread_restrictions.cc:62] DCHECK failed:
!tls_blocking_disallowed. Function marked as blocking was called from a
scope that disallows blocking!
  base!base::AssertBlockingAllowed
  base!base::ScopedBlockingCall::ScopedBlockingCall
  base!base::DeleteFileW
  chrome!browser_cli::CookieImportService::RemoveDescriptor
  chrome!browser_cli::CookieImportService::HandleHello
  chrome!browser_cli::CookieImportService::OnImportPipeMessage
```

观察：`2026-11-02 08:56:55`（目标 PID 9292）。原始产物上来源扩展结果为
`发送失败：与 Browser CLI 的连接已中断`，目标进程退出，0 个 Cookie 写入。
重新在 `2026-11-02 10:16:27` 用恢复后的原始 `chrome.dll` 复现，堆栈地址
（`0x7ffaad13d457` / `0x7ffaad13e720` / `0x7ffaad13dd38`）与上方一致；
该次日志因链接产生的 PDB 与原始 DLL 不匹配，符号名显示错误，但地址和
`DeleteFileW`/`AssertBlockingAllowed` 帧一致。

### 2.3 阻塞 #2：Shutdown 阶段 UI 线程 join

修复 #1 后，作业完成/管道断开时 `Shutdown()` → `CookieImportPipeServer::Stop()`
→ `SimpleThread::Join()` 又触发：

```text
FATAL:base\threading\thread_restrictions.cc:166] DCHECK failed:
!tls_base_sync_primitives_disallowed. Waiting on a //base sync primitive is
not allowed on this thread ...
  base!base::internal::AssertBaseSyncPrimitivesAllowed
  base!base::internal::ScopedBlockingCallWithBaseSyncPrimitives::...
  base!base::PlatformThreadBase::Join
  base!base::SimpleThread::Join
  chrome!browser_cli::CookieImportPipeServer::Stop
  chrome!browser_cli::CookieImportService::Shutdown
  chrome!browser_cli::CookieImportService::OnImportPipeDisconnected
```

观察：`2026-11-02 09:28:15`，目标 PID 43436。该崩溃发生在作业已完成后
（`source.close()` 关闭 relay 时），不是 Cookie 写入失败；它仍会让目标
浏览器退出。

### 2.4 本地 E2E 构建热修（不属于 037）

为继续验证真实 CookieManager 写入和重启持久化，在
`D:\work\.tmp-browsercli\hotfix\cookie_import_service.cc` 做最小本地修改，
用 `out/WinDev` 的真实 clang-cl 参数编译，并用 lld-link 重新链接
`chrome.dll`：

```cpp
#include "base/threading/thread_restrictions.h"

void CookieImportService::RemoveDescriptor() {
  if (!descriptor_path_.empty()) {
    base::ScopedAllowBlockingForTesting allow_blocking;
    base::DeleteFile(base::FilePath::FromUTF8Unsafe(descriptor_path_));
    descriptor_path_.clear();
  }
}

void CookieImportService::Shutdown() {
  ...
  if (pipe_server_) {
    {
      base::ScopedAllowBaseSyncPrimitivesForTesting allow_sync;
      pipe_server_->Stop();
    }
    pipe_server_.reset();
  }
  ...
}
```

- 热修后的 `chrome.dll` SHA-256：
  `97875d26bd5a7926096c1fc00aa46759554222ad7c2a059ceca738f540d989ed`。
- 该热修仅用于 E2E，**没有**写入 `chromium/patches/proposed/037-...patch`，
  也没有修改 `D:\c\src` 源文件。
- 测试结束后已把 `out/WinDev/chrome.dll` 与
  `cookie_import_service.obj` 恢复为原始文件（DLL 原始 SHA-256 见 §2.1）。
- `out/WinDev/chrome.dll.pdb` 已被本地重新链接覆盖，原始 PDB 未备份；
  下一次 `ninja -C out/WinDev chrome.dll` 会重新生成一致的 PDB。
- 热修过程中使用过 `ninja -t restat`；当前 build 的 deps log 会把约 316 个
  edge 视为 dirty，下一次 `ninja` 会重建它们。源码树未改动。

---

## 3. 测试方法

- **来源**：同一 `out/WinDev/chrome.exe`，`--load-extension=D:\work\wt\bp-rc-p1\extensions\import-source`，
  独立临时 `--user-data-dir`。扩展 ID `ljoekiddijnohefaghljlcodgljienoj`。
  通过 CDP 把 `chrome-extension://<id>/popup.html` 作为标签页打开，真实点击
  acknowledgement、Scan、域名复选框，输入票据，勾选发送授权并点击 Send。
  service worker 自身仍要求 `acknowledged === true` / `consent === true`。
- **中转**：从 `bp-rc-p1/native-host/host.js` 生成临时 SEA host exe；
  只在 `HKCU\Software\Chromium\NativeMessagingHosts\com.browserpilot.browseragent`
  注册一个测试 manifest（`allowed_origins` 仅含来源扩展 ID），运行后恢复原值。
  Google Chrome / Edge 的注册项未被修改。
- **目标**：同一 `out/WinDev/chrome.exe`，独立临时 `--user-data-dir`，
  `--browser-cli-cookie-import --browser-cli-cookie-import-session-dir=<本次临时目录>`。
  组合运行实测：目标主窗口 `visible=False`，但票据窗口
  `visible=True`（HWND `14881508`）和批准窗口 `visible=True`
  （HWND `15734624`）。点击时前台窗口是来源浏览器
  `Browser CLI Import - Chromium`（PID `9284`），批准窗口被置为 topmost 后
  接受真实 Win32 `SendInput` 点击；点击落在 Allow 按钮中心 `(1696,792)`，
  作业继续到 done。

  这说明批准窗口确实显示且点击生效；但最终组合运行中前台仍是来源浏览器
  （PID `9284`），`Show()+Activate()` 没有稳定把批准窗口带到前台。产品若
  使用 managed-hidden 目标，必须由用户显式触发并把批准窗口呈现给用户；
  仅创建窗口或仅由 harness 置 topmost 后点击，不能证明真实用户看到了它。
- **站点**：`127.0.0.1:<临时端口>`，`/set` 写入随机 canary 的持久 Cookie
  和 session Cookie；`/login-check` 只返回 `LOGIN_OK`/`LOGIN_MISSING`；
  `/session-check` 返回 `SESSION_PRESENT`/`SESSION_MISSING`；
  `/value-check` 返回 `VALUE_MATCH`/`VALUE_MISMATCH`，从不回显值。
- **票据**：目标自己的 `CookieImportSessionDialog` 显示；harness 用 UI
  Automation 读取该只读文本框，票据只存在于内存。会话描述符只包含
  `ticketHash`。
- **批准**：approve 场景把批准窗口置为 topmost 后，用 Win32 `SendInput`
  在 Allow 按钮中心做真实鼠标点击；deny/cancel/late-allow 用
  `UIAutomation.Invoke`。目标代码中的 `SetApprovalCallbackForTesting`
  从未被安装，因此 `authorization.simulated=false`。

---

## 4. 场景结果

下表使用各自独立运行的精确 PID/管道名；最终 `--scenarios all` 组合运行
在 §4.10 单独记录。所有运行使用本地构建热修（§2.4）。

| 场景 | 结果 | 关键证据 |
| --- | --- | --- |
| approve + restart | **PASS** | §4.1 |
| deny | **PASS** | §4.2 |
| cancel + late Allow | **PASS** | §4.3 |
| host interruption | **PASS** | §4.4 |
| source-process replacement | **PASS** | §4.5 |
| duplicate submit / replay | **PASS** | §4.6 |
| cross-Profile | **PASS** | §4.7 |
| pipe DACL + client identity | **PASS** | §4.8 |
| expired ticket | **FAIL（执行层 PASS，协议帧 FAIL）** | §4.9 |

### 4.1 approve + restart（PASS）

- 目标 PID `34040`；管道
  `\\.\pipe\browser-cli-import-219b400364dd938e8d4766884a3dbd98`；
  job `8182fe20e7387f6f64df70e2`。
- 来源弹出页：
  `chrome-extension://ljoekiddijnohefaghljlcodgljienoj/popup.html`；
  Scan 结果 `共 2 条 Cookie / 1 个域名`（HttpOnly 1、Session 1、分区 0）。
- 目标批准窗口 HWND `148314532`；点击前前台窗口是
  `34040:Allow cookie import into Browser CLI?`，点击后是
  `34040:Browser CLI cookie import`；方法 `SendInput-topmost`，
  点击坐标 `(1696,792)`。
- 组合运行复核（`--scenarios all`）：目标 PID `8452`，管道
  `\\.\pipe\browser-cli-import-7ace47b8ccfdd889e815ac1e68d70d4a`，
  job `0fdbbcc62c7b1e6c61755707`；批准窗口 HWND `15734624`，
  主窗口 `visible=False`，批准/票据窗口 `visible=True`；点击时前台是
  来源浏览器 PID `9284`（不是目标），topmost 点击仍成功。结果同样是
  `applied=2`、`LOGIN_OK`，重启后 `LOGIN_OK`/`SESSION_MISSING`。
- 目标 view：`state=done`，`cookieImported.status=imported`，
  `applied=2, failed=0`，`loginVerified.status=not_verified`；
  `authorization = {targetUserConfirmed:true, simulated:false,
  source:target-browser-dialog, scopeDigest:2c57048b...}`。
- 写入后目标站点：`/login-check=LOGIN_OK`、`/session-check=SESSION_PRESENT`、
  `/value-check=VALUE_MATCH`。
- 关闭来源和目标，用同一目标 Profile **不带导入开关**重启
  （重启 PID `22800`）：`/login-check=LOGIN_OK`，
  `/session-check=SESSION_MISSING`。持久 Cookie 跨重启保留，session Cookie
  不保证保留的契约成立。

### 4.2 deny（PASS）

- 目标 PID `37996`；管道
  `\\.\pipe\browser-cli-import-26fe567e72f278ce3350906242d0e92f`。
- 批准窗口 HWND `12717276`；`UIAutomation.Invoke` 点击 Deny。
- 来源终端：`你在目标浏览器中拒绝了本次导入。`；
  `cookieImported.status=not_started`，`applied=0, failed=0`。
- 目标站点：`LOGIN_MISSING`、`SESSION_MISSING`。

### 4.3 cancel + late Allow（PASS）

- 目标 PID `32020`；管道
  `\\.\pipe\browser-cli-import-caedb2c0a5d769b6b6f0fd5d994ee712`。
- 来源弹出页 Cancel → 终端 `导入已取消。`，`applied=0`。
- 取消后目标批准窗口仍存在；随后 `UIAutomation.Invoke` 点击 Allow
  （HWND `1382838`），目标仍不写入：`LOGIN_MISSING`、`SESSION_MISSING`。
- 来源 `storage.session` 的 `bp_last_job.state=cancelled`；没有创建新 job。

### 4.4 host interruption（PASS）

- 目标 PID `13656`；来源 PID `6032`；native host PID `43832`
  （父 PID `34220`）。
- 作业进入等待批准后杀掉 host；`hostExited=true`。
- 目标站点：`LOGIN_MISSING`；没有 Cookie 写入。
- 来源弹出页在 host 被杀后仍显示“等待你在 Browser CLI 中确认…”（
  service worker 尚未收到新的 status），因此来源 UI 没有伪造成功状态。

### 4.5 source-process replacement（PASS）

- 目标 PID `41892`；来源 PID `25252`；host PID `3172`。
- 作业进入等待批准后 `taskkill /T` 杀掉来源浏览器进程树；
  `hostExited=true`。
- 目标站点：`LOGIN_MISSING`、`SESSION_MISSING`。
- 用同一来源 Profile 新起一个来源浏览器进程：`chrome.storage.session`
  为空（`keys=[]`，`hasLastJob=false`），票据/任务没有落盘，无法重放。

### 4.6 duplicate submit / replay（PASS）

- 目标 PID `43332`；来源 PID `37848`；第一个 job
  `42b16237e542d98fee8c0aca`，`applied=2`。
- 在来源 UI 里重新 scan、重新输入同一票据并第二次 Send：
  返回 `发送失败：没有找到运行中的 Browser CLI 导入会话`
  （`target-not-running`；第一个 `hello` 已消费票据并删除描述符），
  `storage.session` 的 jobId 仍是第一个，未创建第二个 job。
- 独立目标 PID `41040` 上做原始 pipe 重放：同一连接第二次 `hello` 返回
  `ticket-consumed`。第一次结果 `ready` 后没有提交 job，未写入 Cookie。

### 4.7 cross-Profile（PASS）

- 目标 A PID `5872`，管道
  `\\.\pipe\browser-cli-import-36c5bdd1f2192180c864ab4d325d5b84`；
  目标 B PID `10184`，管道
  `\\.\pipe\browser-cli-import-28037095dd3f82ec53fee896188b8f64`。
- 用 A 的票据连接 B 的管道（hostPid 为真实 pipe client PID）：
  返回 `error ticket-invalid`。B 的站点 `LOGIN_MISSING`/`SESSION_MISSING`。
- 正向对照：A 自己的票据连接 A 的管道 → `ready`。说明拒绝的是票据/
  进程绑定，不是管道本身。
- A/B 的 `targetPid`、`ticketHash`、`pipeName` 均不同；描述符不含明文票据。

### 4.8 pipe DACL + client identity（PASS）

- 错误 PID 目标 `35008`，管道
  `\\.\pipe\browser-cli-import-62c50b6586da6f8870939a9a282295bc`；
  正确 PID 目标 `23300`，管道
  `\\.\pipe\browser-cli-import-8c51e016b4751ae5e3b35ee89dae86d5`。
- 实时 DACL（通过复制目标进程的 pipe server handle，用
  `GetSecurityInfo` 查询，不打开客户端连接）：

  ```text
  SDDL=O:S-1-5-21-4090210138-3018339935-804111768-1001
       D:(A;;FA;;;OW)(A;;FA;;;SY)
  ```

  仅 owner（`OW`）和 SYSTEM（`SY`）有 `FA`；没有 `WD`/`AU`/`BU`/`AN`。
- 源码静态检查：`PIPE_REJECT_REMOTE_CLIENTS`、`FILE_FLAG_FIRST_PIPE_INSTANCE`、
  `nMaxInstances=1`、`GetNamedPipeClientProcessId`。
- hello 的 `hostPid` 与 pipe client PID 不一致 → `host-identity-mismatch`；
  正确 PID → `ready`（两个独立目标，避免单客户端会话被前一次连接关闭）。

### 4.9 expired ticket（执行层 PASS，协议帧 FAIL）

- 目标 PID `21880`（最终组合运行）／`8032`（单独运行）。
- 连接管道但不发送 hello，等待 5 分钟：`elapsedMs=298898`。
- 结果：`expiryOutcome=closed`；目标会话描述符在连接关闭后
  `descriptorAfterExpiry=removed`；`Shutdown()` 在 TTL 到期时销毁会话。
- 老票据的产品 relay：`target-not-running`（真实 session 目录和
  stale-descriptor 目录都测试）；目标站点 `LOGIN_MISSING`。
- **FAIL**：目标没有把 `ticket-expired` 帧送达客户端。`OnSessionTimeout()`
  先 `SendError("ticket-expired")`（入队），再 `Shutdown()`
  → `CookieImportPipeServer::Stop()`；pipe 线程在 drain 写队列前退出，
  客户端只看到连接关闭。来源会显示 `target-disconnected`，而不是
  contract 中的 `ticket-expired`。
- 结论：过期票据**不可用**（安全属性成立），但协议错误帧丢失，
  需要产品修复（先 drain 再 stop，或从 pipe 线程同步发送错误）。

### 4.10 组合运行（`--scenarios all`）

组合运行同时启动 expiry 场景，其余 8 个场景串行执行；所有场景使用同一
临时 session 目录。目标描述符按 `targetPid` 与启动的浏览器绑定，避免
长寿命 expiry 目标先写描述符时被其他目标误取。

最终组合运行结果：**8 PASS + 1 FAIL**。FAIL 仅为 §4.9 的
`ticket-expired` 帧丢失；expiry 的执行层子检查为 PASS。

- approve：目标 PID `8452`，job `0fdbbcc62c7b1e6c61755707`，
  `applied=2`，重启后 `LOGIN_OK`/`SESSION_MISSING`。
- cross-profile：目标 A PID `36252` / B PID `12756`；A 票据对 B 管道
  `ticket-invalid`，A 自己的票据 `ready`。
- audit：错误/正确 PID 目标分别为 `40112`/`41028`；
  `host-identity-mismatch` / `ready`。
- expiry：目标 PID `21880`；`elapsedMs=298898`，
  `descriptorAfterExpiry=removed`，`expiryEnforced=true`，
  `errorFrameDelivered=false`。

完整临时报告已按测试卫生要求删除；本文件保留计数、错误码、PID 和
管道名，不保留 Cookie 值或票据。

---

## 5. 安全审计

- **票据**：160-bit，5 分钟 TTL，首次 `hello` 消费；原始 pipe 重放返回
  `ticket-consumed`；第二次产品提交返回 `target-not-running`。
- **描述符**：只写 `ticketHash`、`targetInstanceId`、`profileHash`、
  `pipeName`、PID/时间；无明文票据、无 Cookie 值。`hello` 后删除。
- **跨 Profile**：A 票据对 B 管道返回 `ticket-invalid`；B 无写入；
  A 自己的票据仍可用。
- **管道**：DACL 仅 owner+SYSTEM；`PIPE_REJECT_REMOTE_CLIENTS`；
  单实例；client PID 比较。注意：同一 OS 用户下的恶意进程不受此 DACL
  保护，这与 v2 威胁模型一致。
- **来源替换/host 中断**：进程被杀后目标没有写入；来源 session storage
  没有票据/任务，不能重放。
- **目标批准**：只有目标进程内的 `CookieImportApprovalDialog` 产生
  `targetUserConfirmed=true`；来源/CLI/relay 没有 approve 帧；
  `authorization.simulated=false`。批准窗口真实显示并接受真实鼠标点击。
- **日志/报告/snapshot 卫生**：所有日志、报告、UIA 快照、描述符扫描
  未包含 canary 或票据（`hygiene=PASS`）。临时 Profile/站点/报告在
  `--keep` 之外会删除；本轮保留的 run root 在提取证据后删除。

### 5.1 额外发现：client PID 竞态

如果原始 pipe 客户端在连接后立即发送 `hello`，目标 UI 线程可能尚未处理
`OnImportPipeConnected`，`client_pid_` 仍为 0，`hello` 返回
`host-identity-mismatch`。实测 500 ms 延迟仍可能命中；2 s 后可稳定通过。
产品 relay 在 `connect` 后立即发 `hello`，其描述符扫描/host 启动通常提供
足够延迟，但这不是保证。建议目标在 pipe 线程同步记录 client PID，或在
`hello` 时重新调用 `GetNamedPipeClientProcessId` 比较。

---

## 6. 剩余工作

1. 修复 patch 037 的两个 UI 线程 DCHECK（`RemoveDescriptor` 的
   `DeleteFile`、`Shutdown` 的 `Stop/Join`）；不要把 `ScopedAllow...ForTesting`
   留在产品代码中，应把阻塞 I/O/join 移到允许阻塞的线程或异步收尾。
2. 修复 expiry 错误帧：在停止 pipe 线程前 drain 写队列，或由 pipe 线程
   同步发送 `ticket-expired`；增加“连接但未 hello，TTL 到期”的测试。
3. 修复 client PID 竞态；增加 target-level service/browser test。
4. 把 037 加入 `chromium/patch-manifest.json` 前，在**原始产物**上重跑
   本 harness，确认 9/9 场景（含 expiry 帧）通过。
5. 目标主窗口在当前环境为隐藏；如果产品要在 managed-hidden 模式下使用，
   必须明确“用户显式触发才上屏批准窗口”的路径，并验证
   `Show()+Activate()` 在后台/锁屏/多桌面下的行为。本轮批准窗口确实显示
   并接受真实点击，但不能据此证明所有 hidden 场景。

---

## 7. 复现

```sh
# 原始产物（无热修）：第一次 hello 即 BLOCKED
node D:/work/wt/bp-rc-p1/scripts/e2e/p1-native-cookie-import-e2e.mjs \
  --scenarios approve --chrome D:/c/src/out/WinDev/chrome.exe \
  --root D:/work/.tmp-browsercli/e2e-pristine --keep

# 本地构建热修后：全部场景
node D:/work/wt/bp-rc-p1/scripts/e2e/p1-native-cookie-import-e2e.mjs \
  --scenarios all --chrome D:/c/src/out/WinDev/chrome.exe \
  --root D:/work/.tmp-browsercli/e2e-all \
  --report D:/work/.tmp-browsercli/e2e-all-report-final.json --keep
```

构建热修的源码 diff 见 §2.4；临时构建目录已删除。patch 037 本身未修改。
