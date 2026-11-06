---
type: DeveloperArchitecture
status: design-only（未实现、未构建、未跑测试；D1-D7 决策已确认，见 §9）
generated: { by: agent-c, at: 2026-10-30 }
scope: |
  K15：新 tab / 会话恢复 / 跨窗口移动时，窗口级输入权快照必须**立即**写入新 WebContents，
  不得等到下一次 10s heartbeat。本文只做代码级诊断与设计；不改 D:\c\src、不构建、不启动 Chromium。
  patch 编号：031（030 保留给 Agent A 的 user-explicit show 修复）。
depends_on:
  - docs/input-ownership-api-2026-10-29.md §1.1 / §3 / §4.3 / §13.6（K15 条目）
  - chromium/patches/028-input-ownership-arbitration.patch
  - chromium/patches/029-input-ownership-api.patch
---

# K15：新 Tab 立即继承窗口输入权（诊断 + 实现设计）

> 一句话：**窗口账本升级为带 version 的「窗口快照」；每个 Browser 挂一个
> `TabStripModelObserver`，在 `kInserted` / `kReplaced` 的同一同步调用栈里把快照写成新
> WebContents 的 inherited 状态；新增 `origin` 维度把「窗口派生」与「tab 显式覆盖」分开，
> 两者版本空间互不比较。**
>
> 本轮**没有**修改 `D:\c\src`、**没有**构建 Chromium、**没有**运行任何测试。本文所有
> 「行为」都是设计意图，状态一律标 **待实现 / 待验证**。

---

## 0. 结论摘要

| # | 决定 | 状态 |
|---|---|---|
| 1 | 真源不变：扩展仍是所有权唯一真源；chrome 窗口账本只是「窗口快照缓存」，content 每 WebContents 状态是仲裁真值 | 设计 |
| 2 | 新增正交维度 `origin ∈ {none, explicit, inherited}`；`tab 显式 override` 与 `窗口派生` 是**两个独立版本空间**，永不互比 | 待实现 |
| 3 | 即时继承钩子 = chrome 层每个 Browser 一个 `TabStripModelObserver`，监听 `OnTabStripModelChanged` 的 `kInserted` / `kReplaced` / `kRemoved` | 待实现 |
| 4 | 本 revision（156.0.8067.0）**没有** `TabStripModelObserver::TabInsertedAt`；只有 `OnTabStripModelChanged` | 已核对源码 |
| 5 | 窗口账本从 API `.cc` 的 `static std::map` 提升为 BrowserContext 级 registry，随 Browser / Profile 生命周期销毁 | 待实现 |
| 6 | 继承只在窗口账本「新鲜」（`now - last_ack ≤ 30s`）时发生；账本已过期的新 tab 保持 ASSIST（fail closed），由下一次 heartbeat 绑定 | 待实现 |
| 7 | 窗口 `setOwnership` 不再覆盖显式 tab override；窗口 heartbeat 只刷新 liveness（`Touch`），不改 mode/version | 待实现 |
| 8 | 窗口账本自身执行 version 单调性（修复「所有 tab 都被替换后低 version 被接受」的回退洞） | 待实现 |
| 9 | Feature 关闭零行为：API 不注册 + content fail closed + registry 根本不会被创建（三层） | 待实现 |
| 10 | 不新增第二套状态机、不新增 `base::Feature`、不改 heartbeat/setOwnership 的 wire 协议 | 设计 |
| 11 | D1-D7 七项决策已由主代理确认（2026-10-30），实现必须逐条遵守 | **已确认**（§9） |

---

## 1. 现状（代码事实，逐条给出位置）

### 1.1 content 层：每 WebContents 一个仲裁状态

`content/public/browser/input_ownership.h`

```cpp
struct InputOwnershipSnapshot {
  InputOwnershipMode mode;           // 扩展写入的 configured mode
  InputOwnershipMode effective_mode; // stale 时 = kAssist
  uint64_t version;                  // 每目标单调
  bool stale;                        // now - last_update > 30s
  base::TimeTicks last_update;
};
```

- `InputOwnershipDecider::SetOwnership()`（`input_ownership_decider.cc:481-529`）：
  `version > version_` 接受；`==` 且同 mode 幂等（只刷新 `last_update`）；`==` 不同 mode →
  `kVersionConflict`；`<` → `kStaleVersion`。**mode/version 没有任何来源标记**。
- `Heartbeat()`（同文件 `531-560`）：`version` 必须严格相等，否则 `kStaleVersion` /
  `kVersionUnknown`；只刷新 `last_update`，不推进 version、不清 grant。
- `Refresh()`（`87-102`）：超过 `kOwnershipStaleAfter`（`55` 行，30s）置 `stale_` 并清
  grant/scope/automation window，但**不覆写** `mode_/version_`。
- controller 是 `WebContentsUserData`，只有 feature 打开时才会被创建
  （`content/browser/web_contents/web_contents_impl.cc:4664` `GetInputOwnershipController`）。
- 自由函数在 `content/browser/input_ownership/input_ownership_api.cc`；全树只有
  `chrome/browser/extensions/api/browser_cli_input_ownership/browser_cli_input_ownership_api.cc`
  调用（外加 content 自己的单测）。→ **改签名的影响面是封闭的**。

### 1.2 chrome 层：窗口目标 = 每次调用时「展开当前 tabs」

`browser_cli_input_ownership_api.cc`

| 位置 | 行为 |
|---|---|
| `WindowRecords()`（`170-209`） | `std::map<std::pair<BrowserContext*, int>, WindowRecord{mode,version}>`，`base::NoDestructor`，上限 256，仅在 heartbeat 遇到「窗口无 tab」时删除 |
| `SetOwnership` 的 window 分支（`403-444`） | 遍历 `window->GetTabCount()` 逐个 `content::SetInputOwnership()`；只有**全部** tab 接受才 `StoreWindowRecord()` |
| `Heartbeat` 的 window 分支（`508-575`） | 重新展开当前 tabs：`before.version == 0` → 用窗口快照 `SetInputOwnership` 绑定；`== record.version` → `HeartbeatInputOwnership`；其它 → **跳过**（把它当成 tab override） |
| `GetOwnership` 的 window 分支（`619-641`） | 返回**第一个 tab** 的 snapshot |

### 1.3 缺口（与任务描述逐条对应）

| 编号 | 缺口 | 后果 |
|---|---|---|
| G1 | 新 tab 只能等下一次 heartbeat（≤10s）才被绑定 | 期间 effective ASSIST，AI 命令静默无效（API 文档 §10.3 已列为风险） |
| G2 | 没有 `origin`：`setOwnership({windowId})` 在 version 更高时会**覆盖**显式 tab override；heartbeat 只能靠「`version != record.version`」猜 override，同 version 时无法区分 | tab override 可能被心跳/窗口写破坏 |
| G3 | 窗口账本没有自己的 version 单调性，接受与否取决于当时有哪些 tab | 所有 tab 被替换/关闭后，低 version 的窗口写会被接受 → 版本回退 |
| G4 | 账本键是 `int window_id` + 裸 `BrowserContext*`，且只在 `UNKNOWN_TARGET` 时清理 | 指针/ID 复用风险；profile 销毁后残留 |
| G5 | `SessionServiceBrowserHelper` 在**同一条** `kInserted` 通知里才 `SessionTabHelper::SetWindowID()`（`chrome/browser/ui/sessions/session_service_browser_helper.cc:87-100`） | 插入回调里 `ExtensionTabUtil::GetWindowIdOfTab()` 不可靠（observer 顺序决定是否已赋值） |
| G6 | 跨窗口移动、prerender/discard 替换（`kReplaced`）、会话恢复、关闭最后一个 tab 后的 fallback tab 都没有处理 | 移动后的 tab 保留源窗口 mode；替换后状态丢失 |

---

## 2. 真源与优先级

### 2.1 四层状态与真源

| 层 | 载体 | 内容 | 是否真源 |
|---|---|---|---|
| L0 扩展 | BrowserPilot `SpaceState` + `kernelInputOwnership` 账本 | 派生 mode、每 target version | **唯一真源**（模式从 SpaceState 派生） |
| L1 chrome 窗口账本 | `WindowRecord{mode, version, last_ack, SessionID}`（本次新增 registry 持有） | 窗口快照缓存 | 派生缓存，永不反向改 L0 |
| L2 content per-WebContents | `{mode, version, last_update, stale, origin}` | 内核仲裁状态 | 派生 + 写入历史 |
| L3 grant / command scope | `{WebContents, DevToolsSession}` 桶 | 只影响**投递** | 不参与 mode/version |

### 2.2 派生规则（实现必须逐字遵守）

```
tab_effective(tab) =
    origin == explicit   → (mode, version)              // 来自 setOwnership({tabId})
  | origin == inherited  → (mode, version)              // 来自窗口快照
  | origin == none       → (kAssist, 0)                 // fail closed

窗口写入(record) 是否可以落到 tab：
    origin != explicit                                  // 显式 override 永久优先
```

补充：`effective_mode` 仍由 content 的 30s stale 规则派生（`stale ? kAssist : mode`），
与 `origin` 正交；`origin` 只回答「这个 mode 是谁写的」。

### 2.3 冲突优先级（明确到每个方向）

| 冲突 | 结果 |
|---|---|
| tab 显式 override vs 窗口快照 | **tab 胜**（窗口写跳过，返回 `kOverridePresent` 并被窗口路径忽略） |
| 窗口 setOwnership(version↑) vs 该 tab 先前的 inherited 状态 | 窗口胜（同 origin 内版本单调） |
| heartbeat vs mode/version | heartbeat **永不**改 mode/version；只刷新 liveness |
| heartbeat vs tab 显式 override | 只 `Touch`（刷新 `last_update`），mode/version 原样 |
| mode 变化 vs grant/scope | mode 变化清空该 WebContents 的全部 session grant/scope（content 既有语义） |
| grant vs mode/version | grant 永不改 mode/version（content 既有语义） |

### 2.4 版本空间

- `win:<windowId>` 与 `tab:<tabId>` 是**两个独立版本空间**（API 文档 §3.1 已如此规定）。
- 因此 **explicit 与 inherited 的 version 永不比较**：
  - 首次 explicit 写入（当前 origin 为 `none`/`inherited`）→ 接受任意 `version ≥ 1`，
    显式版本空间从该值重新开始；
  - 之后同 origin 内才执行单调规则。
- 反例（必须避免的实现）：拿窗口 record.version 与 tab override 的 version 比大小来决定
  「谁赢」——这正是 G2 的根因。

### 2.5 为什么不在每个 tab 上再存一份「窗口副本」

那会引入第二套状态机，且需要在 tab 移动/替换时做双向同步。`origin` 只是给现有
`(mode, version)` 打一个来源标签，状态仍然只有一份。

---

## 3. 即时继承钩子

### 3.1 候选钩子与结论

| 候选 | 覆盖 | 结论 |
|---|---|---|
| `TabStripModelObserver::OnTabStripModelChanged(kInserted/kReplaced/kRemoved)` | 新建 tab、`chrome.tabs.create`、`window.open`（同窗口）、duplicate、会话恢复、跨窗口移入、关闭/移出 | **采用** |
| `TabStripModelObserver::TabInsertedAt` | — | **不存在**：本 revision 只有 `OnTabStripModelChanged`（全树只有 `Browser::OnTabInsertedAt` 这个 Browser 私有方法，不是 observer 接口） |
| `WebContentsCreated` | 太早（还没有窗口归属），且对 popup/devtools/非 tab contents 也触发 | 不采用 |
| `content::WebContentsDelegate::AddNewContents` → `chrome::AddWebContents`（`browser_tabstrip.cc:98`） | 只覆盖 `window.open`/新窗口路径，漏掉 restore / duplicate / 移入 | 不采用（但所有路径最终都走 `InsertWebContentsAt`，见下） |
| `tabs::TabInterface::RegisterDidInsert` | 需要逐 tab 提前注册；触发时机与 `chrome.tabs.create` 响应的先后未验证 | 备选（§8.1） |
| `ActiveTabChanged` | 状态跟随 WebContents，不随激活变化 | **不需要** |
| `DidFinishNavigation` | 导航不换 WebContents，mode 不受影响 | **不需要** |

关键事实：所有插入路径最终都进 `TabStripModel::InsertTabAtIndexImpl`
（`chrome/browser/ui/tabs/tab_strip_model.cc:4889`），它先
`contents_data_->AddTabRecursive(...)`，再同步 `OnChange(change, selection)`（同函数尾部）。
所以 observer 收到 `kInserted` 时 tab 已在 strip 内、WebContents 已存在、尚未开始加载。

### 3.2 在哪个进程/对象上触发

- **browser 进程、UI 线程、chrome 层**。content 不感知窗口。
- 每个 `BrowserWindowInterface` 一个 `WindowTabBinding`（`TabStripModelObserver` +
  `BrowserWindowInterface::RegisterBrowserDidClose` 订阅）。
- 由 `WindowOwnershipRegistry` 持有，registry 挂在 `BrowserContext` 的 user data 上
  （`BrowserContext::SetUserData`），随 profile 销毁。
- 绑定在**第一次成功 `setOwnership({windowId})` 时惰性创建**：没有窗口账本就没有继承，
  也不需要 observer（feature 关闭/未使用的 profile 零成本）。

### 3.3 为什么「第一条输入事件前」一定完成

1. `chrome.tabs.create` → `AddTabAt` → `InsertWebContentsAt` → `InsertTabAtIndexImpl` →
   **同步** `OnChange` → 我们的 observer 写状态。
2. 扩展拿到 `chrome.tabs.create` 的响应（Promise resolve）**在插入之后**；`chrome.debugger.attach`
   与第一条 `Input.dispatch*` 只会更晚。
3. 页面自身不可能在新 tab 上产生 debugger 输入；第三方 CDP `Target.createTarget` 同样要等
   插入完成才拿到 targetId。
4. `tabs.onCreated` 也在同一条插入通知里由 `TabsEventRouter` 派发
   （`chrome/browser/extensions/api/tabs/tabs_event_router.cc:466-470`），但它是
   `EventRouter::BroadcastEvent` → 扩展渲染进程 IPC，扩展 JS 在**后续任务**才执行；
   UI 线程一定先跑完当前插入通知（含我们的同步写）。
5. 因此**不需要** `DidFinishNavigation` 或「首帧」钩子；也不需要等导航提交。

### 3.4 禁止在插入回调里用 `ExtensionTabUtil::GetWindowIdOfTab()`

`SessionServiceBrowserHelper::OnTabStripModelChanged` 在**同一条** `kInserted` 通知里才写
`SessionTabHelper::SetWindowID()`（`session_service_browser_helper.cc:95`）。observer 顺序
不保证，所以插入回调里 `GetWindowIdOfTab(contents)` 可能返回 0/旧值。
**必须**从 binding 自己持有的 `BrowserWindowInterface::GetSessionID()` 取窗口身份
（`BrowserExtensionWindowController` 的 `GetWindowId()` 就是 `session_id_.id()`，
`browser_extension_window_controller.cc:122,138`）。

### 3.5 会话恢复

- 启动恢复 / 恢复上次会话 / 重开关闭的 tab 全部走
  `session_restore.cc:1140` → `chrome::AddRestoredTab`（`browser_tabrestore.cc:136`）→
  `InsertWebContentsAt`（`browser_tabrestore.cc:186-192`）；`ReplaceRestoredTab`
  （`browser_tabrestore.cc:295`）同样走 `InsertWebContentsAt`。
- 因此**同一钩子即可覆盖**：只要目标窗口已有窗口账本，恢复出来的 tab 在插入时立即继承。
- 跨浏览器重启：新 Browser 拿到**新的 `SessionID`**（`SessionID::NewUnique()` 保证本机
  从未用过），旧账本不存在 → 不继承。这是正确行为：扩展 SW 重启后会重新解析 space 并
  `setOwnership({windowId})`（API 文档 §3.4），那一次调用会把恢复出的全部 tab 绑定。

### 3.6 切换 tab

不写任何状态。理由：ownership 是 WebContents 属性，不是激活属性；`ActiveTabChanged`
只影响可见性/焦点。唯一与「激活」有关的写入是关闭最后一个 tab 后的 fallback tab
（`TabStripModel::TabStripEmpty` → `delegate_->AddTabAt(GURL(), -1, true)`），它本身会走
`kInserted`，自动继承。

### 3.7 `kReplaced`（prerender 激活 / tab discard）

`TabStripModelChange::Replace{old_contents, new_contents, index}`：

- `old_contents` 的 `origin == explicit` → 把 `(mode, version, explicit)` 复制到 `new_contents`
  （tab override 必须跨替换存活）；
- 否则 → 按 §6.3 的 `ApplyToNewContents` 规则重新派生（等价于「新 tab 继承」）。
- 不复制 `last_update`：替换后重新计时；显式复制同样刷新 `last_update`（一次真实写入）。

---

## 4. 生命周期

| 事件 | 动作 | 触发点 |
|---|---|---|
| tab 插入 | 若 `record` 存在且新鲜：`SetInputOwnership(new, record.mode, record.version, kInherited)`；若 tab 已是 explicit → 跳过 | `OnTabStripModelChanged(kInserted)` |
| tab 替换 | §3.7 | `kReplaced` |
| tab 被移动到其它窗口 | `ClearInheritedInputOwnership(contents)`（explicit 不动） | `kRemoved` 且 `remove_reason == kInsertedIntoOtherTabStrip` / `tab_detach_reason == kInsertIntoOtherWindow`（`tab_strip_model.cc:705-741`） |
| tab 关闭 | 无需动作（controller 随 WebContents 销毁，`~InputOwnershipController` 清 session） | `kRemoved`（`kDelete`） |
| 窗口关闭 | `EraseWindow(SessionID)`：销毁 binding + 删除账本 | `RegisterBrowserDidClose`（`UnloadController::OnWindowCloseComplete`，`unload_controller.cc:942`） |
| TabStripModel 销毁 | `model_ = nullptr`（防悬挂），把账本标记为 dead；**不**在回调里删除自己（避免在 `~TabStripModel` 的 observer 迭代中 `RemoveObserver`） | `OnTabStripModelDestroyed`（`tab_strip_model.h:601`，`tab_strip_model.cc:284-288` 派发） |
| Profile 销毁 | registry（BrowserContext user data）析构 → 所有 binding 析构 | `BrowserContext` 生命周期 |
| ownership 变化（窗口） | 跳过 explicit tab；其余 inherited 覆盖 | `SetOwnership` window 路径 |
| ownership 变化（tab） | `origin` 翻成 explicit，版本空间重置 | `SetOwnership` tab 路径 |
| heartbeat 过期 | `record.last_ack` 老化 → 新 tab 不再继承；已有 tab 由 content 自己的 30s 规则独立降级 | 读时计算，无定时器 |
| OTR / split incognito | registry 不会为 OTR profile 创建（API 信任检查拒绝 OTR 调用方）；binding 创建前再防御性 `IsOffTheRecord()` 跳过 | `IsTrustedBrowserCliCaller` + registry |

**防悬挂**：

- 账本里**不存** `WebContents*`，只存 `SessionID` + 标量；插入时现取 `contents`。
- binding 持 `raw_ptr<BrowserWindowInterface>` 与 `raw_ptr<TabStripModel>`，两者都在
  `RegisterBrowserDidClose` / `OnTabStripModelDestroyed` 中置空。
- `RegisterBrowserDidClose` 返回的 `base::CallbackListSubscription` 作为 binding 成员，
  析构自动退订；`base::CallbackList` 允许在回调中移除自身订阅。
- registry 不观察 `GlobalBrowserCollection`（不需要 `OnBrowserCreated`：新窗口 = 新
  `SessionID` = 无账本，惰性 attach 已覆盖）。

---

## 5. 版本语义与幂等

### 5.1 窗口账本规则（与 content 同构，写在 registry 里）

```
if record.version != 0:
  version <  record.version                       → kStaleVersion   （返回 record）
  version == record.version && mode != record.mode → kVersionConflict（返回 record）
  version == record.version && mode == record.mode → 幂等：刷新 last_ack + 补写未绑定 tab
version > record.version                          → 接受：写 record，遍历 tabs
```

- 这修掉 G3：即使窗口当前 0 个 tab 或全部是 explicit override，低 version 也会被拒绝。
- 返回给扩展的 `mode/version` **一律取 record**，不再取「某个 tab 的快照」；
  `getOwnership({windowId})` 的 `stale` 由 `!record.IsFresh(now)` 计算，`lastUpdateAt`
  取 `record.last_ack` 的墙钟投影（与现有 `ToEpochMs` 一致）。
- `getOwnership({tabId})` 仍返回该 WebContents 自己的快照（inherited 或 explicit 均可），
  K15 不暴露 `origin`（§9-D3）。

### 5.2 继承不推进 version

新 tab 写入的就是 `(record.mode, record.version)`；它不产生新版本、不改变窗口账本。
因此「同一个窗口快照应用到 N 个 tab」是同一个 version 的 N 次幂等写。

### 5.3 heartbeat 幂等

`heartbeat({windowId}, V)`：
1. 无 record → `UNKNOWN_TARGET`（**已决策 D1**，见 §9；不保留逐 tab 回退）；
2. `V != record.version` → `STALE_VERSION` / `VERSION_UNKNOWN`，不刷新任何东西；
3. 相等 → `record.last_ack = now`，然后逐 tab：
   - `origin == explicit` → `TouchInputOwnership(tab)`（只刷新 `last_update`）；
   - `origin == inherited && snapshot.version == record.version` → `HeartbeatInputOwnership(tab, record.version)`；
   - `origin == inherited && version 不一致` → `SetInputOwnership(tab, record.mode, record.version, kInherited)`（自愈）；
   - `origin == none` → `SetInputOwnership(tab, record.mode, record.version, kInherited)`（补绑漏掉的 tab）。
4. 重复调用任意次 = 一次（全部幂等）。

### 5.4 即时继承 vs heartbeat 的重复应用

同一 `(mode, version)` 的 `SetInputOwnership` 是幂等刷新（content `decider.cc:502-514`），
所以「插入时写一次 + 10s 后 heartbeat 再写一次」不会产生版本抖动，也不会清掉不该清的
grant（同 mode 幂等路径不清 grant）。

### 5.5 origin 切换时的版本空间（表）

| 当前 origin | 写入 origin | 版本比较 | 结果 |
|---|---|---|---|
| none | explicit | 无（base=0） | 接受 |
| none | inherited | 无 | 接受 |
| inherited(W) | inherited(W') | 与 W 比 | 单调规则 |
| inherited(W) | explicit(V) | **不比较** | 接受，origin=explicit，版本空间从 V 开始 |
| explicit(V) | explicit(V') | 与 V 比 | 单调规则 |
| explicit(V) | inherited | — | `kOverridePresent`，不改状态 |

### 5.6 三层版本回退防线

1. 扩展账本：绝不发比账本更小的 version（既有）。
2. 窗口账本：record 自身单调（本次新增）。
3. content：inherited 内单调、explicit 内单调、跨 origin 不比较（本次新增）。

---

## 6. 增量实现计划（最小 patch，文件/函数级）

### 6.1 编号与形态

- 新 patch：`chromium/patches/031-input-ownership-new-tab-inheritance.patch`（在 029 之后；
  同步更新 `chromium/patch-manifest.json` 的 order/sha256）。
  **030 已被保留**给 Agent A 的 user-explicit show 修复，K15 不得占用。
- 不新增 `base::Feature`；复用 `features::kInputOwnershipEnforcement` +
  `_api_features.json` 的 `requires_delegated_availability_check`。
- 不改 `browser_cli_input_ownership.json` 的必填字段（见 §9-D3）。

### 6.2 P0 — content：`origin` 与两个新入口（约 120 行）

`content/public/browser/input_ownership.h`

```cpp
enum class InputOwnershipOrigin { kNone, kExplicit, kInherited };

struct InputOwnershipSnapshot {
  /* ...existing... */
  InputOwnershipOrigin origin = InputOwnershipOrigin::kNone;  // 新增
};

enum class InputOwnershipWriteResult {
  /* ...existing... */
  kOverridePresent,   // 新增：inherited 写入被显式 tab override 拒绝
};

// 新签名（不提供默认参数：每个调用点必须显式选择来源）
CONTENT_EXPORT InputOwnershipApplyResult SetInputOwnership(
    WebContents*, InputOwnershipMode, uint64_t version,
    InputOwnershipOrigin origin);

// tab 被移出窗口时调用；origin==explicit 时是 no-op。
// 仅 content/chrome 内部使用，**不是**扩展 API，与 D5（不提供清除 override 的 API）不冲突。
CONTENT_EXPORT InputOwnershipWriteResult ClearInheritedInputOwnership(WebContents*);

// 只刷新 last_update（窗口 heartbeat 覆盖显式 override tab 的 liveness）
CONTENT_EXPORT InputOwnershipWriteResult TouchInputOwnership(WebContents*);

// 供 chrome 层判断「窗口账本是否还新鲜」，避免复制 30s 常量。
// 用函数而不是 extern const：跨 Windows DLL 的 const 动态初始化/导出更容易踩坑。
CONTENT_EXPORT base::TimeDelta InputOwnershipStaleAfter();
```

`content/browser/input_ownership/input_ownership_decider.cc`

- 新增成员 `InputOwnershipOrigin origin_ = kNone;`
- `SetOwnership(mode, version, origin, now)`：按 §5.5 表实现；
  `kOverridePresent` 只在 `origin_ == kExplicit && origin == kInherited` 时产生。
- 新增 `ClearInherited(now)` / `Touch(now)`。
- `snapshot()` 带上 `origin_`；`SetModeForTesting()` 设 `origin_ = kExplicit`。
- `InputOwnershipStaleAfter()` 返回现有的 `kOwnershipStaleAfter`（`55` 行）。

`content/browser/input_ownership/input_ownership_controller.{h,cc}`：转发 3 个新方法。

`content/browser/input_ownership/input_ownership_api.cc`：新增 3 个自由函数
（`GetController` 的空指针/feature-off 语义与现有函数一致）。

`content/browser/BUILD.gn`：源文件列表不变（同文件内改动）。

### 6.3 P1 — chrome：窗口绑定（新文件，约 300 行）

`chrome/browser/extensions/api/browser_cli_input_ownership/browser_cli_input_ownership_window_bindings.h/.cc`
（namespace `extensions::browser_cli_input_ownership`）

```cpp
struct WindowRecord {
  content::InputOwnershipMode mode = content::InputOwnershipMode::kAssist;
  uint64_t version = 0;
  base::TimeTicks last_ack;   // setOwnership / heartbeat 成功时刷新
  SessionID session_id;       // 完整 SessionID，不只是 int
  bool dead = false;          // TabStripModel 已销毁
  bool IsFresh(base::TimeTicks now) const {
    return !dead && version != 0 && !last_ack.is_null() &&
           now - last_ack <= content::InputOwnershipStaleAfter();
  }
};

class WindowTabBinding : public TabStripModelObserver {
  // ctor(BrowserWindowInterface*): model_->AddObserver(this);
  //   close_subscription_ = browser_->RegisterBrowserDidClose(...)
  // dtor: if (model_) model_->RemoveObserver(this);
  // OnTabStripModelChanged(): kInserted / kReplaced / kRemoved（§4 表）
  // OnTabStripModelDestroyed(): model_ = nullptr; record_.dead = true;
  // ApplyToNewContents(WebContents*): 新鲜 + origin != explicit 时写 inherited
  // ApplyReplace(old, new)
};

class WindowOwnershipRegistry : public base::SupportsUserData::Data {
  static WindowOwnershipRegistry* GetForBrowserContext(content::BrowserContext*); // 惰性创建
  // SetOwnership(browser, mode, version, WindowRecord* out)
  // Heartbeat(browser, version, WindowRecord* out)
  // bool GetOwnership(browser, WindowRecord* out) const
  // void EraseWindow(const SessionID&)
  std::map<SessionID, std::unique_ptr<WindowTabBinding>> bindings_;
};
```

要点：
- `SetOwnership` 先做账本 version 校验（§5.1），**再** `GetOrCreateBinding(browser)`
  （observer 在遍历 tabs 之前就位），最后逐 tab 写 inherited。
- `Heartbeat` 要求 binding 存在（`UNKNOWN_TARGET` 否则），按 §5.3 逐 tab 处理。
- `GetOwnership` 返回 record；`stale = !IsFresh(now)`。
- `ApplyToNewContents` 里 `SetInputOwnership` 的 `kOverridePresent` 是**正常路径**，忽略。
- `BrowserContext` 用 `SetUserData`（D4 已决策：不引入 `ProfileKeyedServiceFactory`）。

`chrome/browser/extensions/api/browser_cli_input_ownership/BUILD.gn`：source_set 增加两个源文件。

### 6.4 P2 — 接线 API（改 3 个函数 + 删旧账本）

`browser_cli_input_ownership_api.cc`

| 函数 | 改动 |
|---|---|
| `SetOwnership` window 分支（403-444） | 删除逐 tab 循环与 `StoreWindowRecord`；改调 `registry->SetOwnership(browser, mode, version, &record)`；`accepted/reason/mode/version` 全部来自 record |
| `Heartbeat` window 分支（508-575） | 删除逐 tab 展开；改调 `registry->Heartbeat(browser, version, &record)` |
| `GetOwnership` window 分支（619-641） | 改调 `registry->GetOwnership(browser, &record)`；无记录 → `UNKNOWN_TARGET` |
| `WindowRecords()` / `WindowKey` / `StoreWindowRecord` / `EraseWindowRecord` / `GetWindowRecord`（170-209） | **删除** |
| `ToReason()`（56-78） | 增加 `kOverridePresent → "OVERRIDE_PRESENT"`（防御性，正常不会到达 API 层） |
| tab 分支 `content::SetInputOwnership(...)`（389-391） | 追加 `InputOwnershipOrigin::kExplicit` |
| `ResolveWindow` 之后 | `window->GetBrowserWindowInterface()` 为 null（非 Browser 窗口）→ 不建账本，返回 `UNKNOWN_TARGET`；**不**退回逐 tab 展开（避免两套语义并存） |

### 6.5 P3 — 测试（见 §7）

### 6.6 实现顺序与依赖

```
P0 content（origin/clear/touch）          ← 无 chrome 依赖，可先合
  → P0 content_unittests
  → P1 chrome registry + binding（只依赖 P0）
    → P2 API 接线（删除旧账本）
      → P2 chrome browser test
        → P3 真实 E2E（K5-K12）
```

### 6.7 Feature 关闭路径核对（必须逐条成立）

1. API 层：`_api_features.json` 的 delegated availability 在 feature off 时隐藏整个
   namespace → 扩展零调用。
2. chrome 层：registry 只由 `setOwnership` 创建；feature off 时 content 返回
   `kFeatureDisabled`，API 返回失败且不建 record → **observer 永不安装**。
3. content 层：`GetController` 在 feature off 时返回 nullptr，controller 根本不被创建。
4. 结论：feature off 时没有任何新对象、没有 observer、没有状态写入。

### 6.8 明确不做

- 不改 `heartbeat` / `setOwnership` 的 wire 协议（不新增必填参数）。
- 不新增 `base::Feature`、不改 `_api_features.json` 的可见性。
- 不动 K4 相关的窗口呈现代码（Agent A 正在同一 tree 工作）。
- 不实现「清除 tab override」的新 API（见 §9-D5）。

---

## 7. 测试计划（全部**待实现 / 待验证**，本轮未运行）

> 本轮只做设计：以下没有任何结果被记录为 PASS。实现后必须贴原始输出。

### 7.1 content 单测（`content_unittests`，`content/test/BUILD.gn:2683` 已列文件）

| # | 用例 | 断言 |
|---|---|---|
| C1 | `inherited` 写入后 snapshot.origin == kInherited | mode/version 正确 |
| C2 | `explicit` 写入覆盖 inherited，版本空间重置（inherited=5 → explicit=1 接受） | origin=kExplicit, version=1 |
| C3 | `inherited` 写入被 explicit 拒绝 | 返回 `kOverridePresent`，状态不变 |
| C4 | inherited 内版本单调（5 → 4 拒绝，5 同 mode 幂等，5 异 mode 冲突） | 与现有 explicit 规则一致 |
| C5 | `ClearInheritedInputOwnership` 在 explicit 上是 no-op；在 inherited 上回到 none/assist/0 | snapshot 断言 |
| C6 | `TouchInputOwnership` 刷新 last_update 但不改 mode/version/origin；version==0 时拒绝 | 断言 |
| C7 | feature off：三个新入口全部不创建 controller、不写状态 | `GetController==nullptr` |
| C8 | 现有 explicit 用例全部保持通过（回归） | 原断言不变 |

### 7.2 chrome browser test（新增 `browser_cli_input_ownership_window_bindings_browsertest.cc`）

需要真实 `Browser` + `TabStripModel`（`content::BrowserTestBase` / `InProcessBrowserTest`）。
直接调用 registry（不经过扩展 API），因为扩展身份在测试里不可达。

| # | 用例 | 断言 |
|---|---|---|
| B1 | 窗口 setOwnership(ai,3) 后 `AppendWebContents` 新 tab | 新 tab snapshot = ai/3/origin=inherited，**无需 heartbeat** |
| B2 | 新 tab 插入后立即 `GetInputOwnershipSnapshot`（同一任务内） | 已是 ai/3（时序保证） |
| B3 | `AddRestoredTab`（会话恢复路径）插入 | 继承 ai/3 |
| B4 | 跨窗口移动（`DetachTabAtForInsertion` → `InsertDetachedTabAt`） | 目标窗口快照生效；源窗口不同 mode 时不残留 |
| B5 | 窗口 setOwnership(version↑) 与已存在的显式 tab override | override 保持不变；其它 tab 更新 |
| B6 | 窗口 heartbeat 覆盖 explicit tab | mode/version 不变，`stale=false`（Touch 生效） |
| B7 | 窗口 setOwnership 低 version（所有 tab 被关闭/替换后） | `STALE_VERSION`，账本不变 |
| B8 | 窗口关闭后同 profile 新窗口 | 新窗口无继承（SessionID 不同，账本已删） |
| B9 | `kReplaced`（`ReplaceWebContentsAt` 模拟 discard） | explicit override 被复制；inherited 重新派生 |
| B10 | feature off | registry 不会被创建（`GetIfExists()==nullptr`）；observer 未安装；行为与今天逐字节一致 |
| B11 | 窗口账本过期（`last_ack` 人为置为 40s 前）后新 tab | 不继承，保持 assist/0 |

### 7.3 真实 E2E（`scripts/test-input-arbitration-e2e.mjs`，`kernel` stage，K5+）

复用现有 helper：`ctx.apiCall()`、`ctx.bumpModeOnTab()`、`ctx.kernelSnapshotOnTab()`、
`ctx.createUntrackedWindow()`、`ctx.popupDebuggerAttach()`、`ctx.probe()`（DOM 计数）。

| # | 用例 | 步骤 | 断言 |
|---|---|---|---|
| K5 | 新 tab 立即继承（AI） | `setOwnership({windowId}, ai)` → 扩展 `chrome.tabs.create` 同窗口 → **不 heartbeat** → 立即 AI click | `getOwnership({tabId}).mode==ai && version==窗口版本`；DOM clicks +1 |
| K6 | 新 tab 立即继承（HUMAN） | 同上但 human | 新 tab AI click → `INPUT_HUMAN_OWNED`；内核 `Input.dispatchMouseEvent` 被丢弃（DOM 不变） |
| K7 | 会话恢复 | `chrome.sessions.restore()`（或 Ctrl+Shift+T 等价的 `chrome.tabs.create` + restore API）到已绑定窗口 | 恢复 tab snapshot == 窗口快照；AI click 立即生效 |
| K8 | 切换 tab | 两个 tab 交替激活，各自 AI/HUMAN | 激活不改变任一 tab 的 mode/version |
| K9 | tab override 冲突 | 窗口 ai → `setOwnership({tabId}, human)` → 窗口 `heartbeat` ×3 + 窗口 `setOwnership(human, v+1)` | tab 仍 human（override 不被覆盖）；其它 tab 跟随窗口 |
| K10 | 移动/替换 | 把 tab 拖到另一窗口（或 `chrome.tabs.move`） | 目标窗口 mode 生效；源窗口不残留 |
| K11 | 30s 失联后新 tab | 窗口 ai 后停止心跳 >30s → 新建 tab | 新 tab 保持 assist/0（不继承过期账本）；heartbeat 恢复后补绑 |
| K12 | Feature off | 同一组步骤在无 `--enable-features` 下 | 无 kernel API、行为与今天一致 |

### 7.4 不伪造

- 本轮**未构建**、**未运行**任何上述用例。
- 实现后：unit/browser test 贴 `content_unittests --gtest_filter=...` 原始输出；
  E2E 贴 `D:\c\logs\input-arbitration-e2e-<timestamp>.json` 与逐条 PASS/FAIL。
- 若某条因环境缺失无法跑（例如 `chrome.sessions.restore` 在测试 profile 不可用），
  必须 `markBlocked` 并写依赖，不得记为 PASS。

---

## 8. 风险与替代方案

### 8.1 钩子覆盖不到的 fallback（按优先级）

1. **`BrowserWindowFeatures` 每浏览器 feature**：在 `browser_window_features.cc` 注册一个
   每 Browser 的 `WindowTabBinding` 持有者（不受 `WindowController` 解析限制，覆盖 app
   window）。代价：所有 browser 都会构造（需 feature 关闭时早退），且 `browser_window_features`
   要依赖本 API target。
2. **`BrowserTabStripTracker` + `GlobalBrowserCollection`**：一个全局 observer 观察所有
   `TabStripModel`。注意：`MaybeTrackBrowser` 会为已有 tab 合成一次 `kInserted`
   （`browser_tab_strip_tracker.cc:47-66`），实现必须幂等；并且必须自己维护
   `TabStripModel* → BrowserWindowInterface*` 映射（不能用 `GetWindowIdOfTab`，见 §3.4）。
3. **`OnBrowserCreated` 全量绑定 + 惰性 attach**：适合窗口类型多样、`GetBrowserWindowInterface()`
   返回 null 的场景；成本是每个新窗口都装 observer（仍受 feature 门控）。
4. **heartbeat-only（现状）**：最终兜底。不满足 K15 目标，但保证安全（fail closed）。

### 8.2 风险表

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | observer 顺序 vs `SessionServiceBrowserHelper` | 用错窗口 id → 继承到错误窗口 | 只用 binding 的 `SessionID`；禁止 `GetWindowIdOfTab` |
| R2 | `OnTabStripModelDestroyed` 里删除自己 | 在 `~TabStripModel` 迭代中 `RemoveObserver` → UAF | 只置 `model_=nullptr` + `dead=true`；真正的删除走 `RegisterBrowserDidClose` |
| R3 | 窗口账本过期仍被继承 | 死掉的扩展快照把新 tab 变成 AI | `IsFresh()` 30s 门控（§5.1/§6.3） |
| R4 | explicit override 粘滞，无清除 API | 扩展若曾对某 tab 做过精确覆盖，窗口模式不再影响它 | 文档化；后续可选新增 `mode:"inherit"`（§9-D5） |
| R5 | 跨重启后窗口 id 变化 | 恢复的窗口不会被旧账本命中 | 由扩展 SW 重新 `setOwnership`（既有流程） |
| R6 | `chrome.windows.create` 新窗口无账本 | 新窗口内的 tab 初始 ASSIST | 既有行为；扩展对每个 space 都会 `setOwnership` |
| R7 | 大量窗口时每窗口一个 observer | 内存/CPU | 只有存在账本的窗口才绑定；插入 O(1) |
| R8 | 与 Agent A 在同一 tree 冲突 | 构建/合并冲突 | K15 只碰 input ownership 的 content 公开头/decider/controller/api 四个文件 + 1 个 chrome target；不碰呈现/窗口代码 |
| R9 | `InputOwnershipWriteResult` 新增枚举值 | 所有 switch 必须补 case | `ToReason()` 与 content 内部 switch 同步更新；编译期发现 |
| R10 | 第三方 CDP 在 AI 模式注入 | 非本 API 能解决 | 既有边界（API 文档 §7.2）；K15 不改变 |

### 8.3 与 029 的兼容性

- 纯增量：老客户端只发 window 目标，行为只会变好（新 tab 立即可用），协议不变。
- 唯一可观察的行为变化是 §5.3 第 1 条（heartbeat 无账本 → `UNKNOWN_TARGET`）与
  §6.4 的「窗口写不再因 tab override 而 accepted=false」。两者都更严格/更可预测，
  但必须在 API 文档与客户端测试里同步说明。

---

## 9. 决策记录（D1-D7，已确认，2026-10-30）

> 以下 7 条由主代理确认，**不再开放讨论**；实现必须逐条遵守。
> 本文其余章节中出现的「§9-D*」均指本表。

| # | 决策 | 实现落点 |
|---|---|---|
| **D1** | `heartbeat({windowId})` 在没有窗口账本时返回 `UNKNOWN_TARGET`（fail closed），**不**保留逐 tab 回退 | `WindowOwnershipRegistry::Heartbeat()` 在 `bindings_` 中找不到 `SessionID` 时直接返回；`browser_cli_input_ownership_api.cc` 的 heartbeat window 分支删除逐 tab 展开。客户端按既有约定删除账本并重放 `setOwnership` 自愈（API 文档 §3.5） |
| **D2** | 窗口 `setOwnership` **跳过**显式 tab override；tab explicit **永久优先**于窗口快照 | content `SetOwnership(..., kInherited)` 在 `origin_ == kExplicit` 时返回新增的 `kOverridePresent` 且不改状态；registry 的窗口写把该结果当正常跳过。窗口写永不覆盖 explicit，无论 version 大小 |
| **D3** | 本轮 `getOwnership` **不暴露** `origin`，保持 JSON schema 最小 | `browser_cli_input_ownership.json` 无改动；`origin` 只存在于 content 的 `InputOwnershipSnapshot`（内部），不进入 `api_io::Snapshot` |
| **D4** | registry 生命周期用 `BrowserContext::SetUserData`，**不引入** `ProfileKeyedServiceFactory` | `WindowOwnershipRegistry : base::SupportsUserData::Data`，`GetForBrowserContext()` 惰性 `SetUserData`；不新增 factory 文件、不注册 profile keyed service |
| **D5** | 本轮**不提供**「清除 tab override / 回到窗口快照」的 API | 不新增方法、不改 schema；explicit override 的粘滞性写入已知限制（§8.2 R4），后续有真实用例再评估 `mode:"inherit"` |
| **D6** | 窗口账本 `>30s` 过期时，新 tab **不继承**（fail closed） | `WindowRecord::IsFresh(now)` 用 `content::InputOwnershipStaleAfter()` 判定；`ApplyToNewContents()` 在 `!IsFresh` 时直接返回，tab 保持 `none/assist/0`，由下一次 heartbeat 补绑 |
| **D7** | K15 使用**独立 patch 031**；030 保留给 Agent A 的 user-explicit show 修复 | patch 文件 `chromium/patches/031-input-ownership-new-tab-inheritance.patch`；`patch-manifest.json` order=031；不得修改 030 |

### 9.1 决策带来的确定后果（实现时不要再动摇）

1. **没有「逐 tab heartbeat 回退」分支**：窗口目标的唯一真值是窗口账本；窗口账本不存在 =
   `UNKNOWN_TARGET`，不读也不写任何 tab。
2. **`kOverridePresent` 是新增的稳定结果码**：只可能由 inherited 写入产生；API 层 `ToReason()`
   必须补 case（映射为 `"OVERRIDE_PRESENT"`），但正常窗口路径会吞掉它、不返回给扩展。
3. **explicit override 的粘滞是接受的产品行为**：扩展当前只用 window 目标
   （API 文档 §8.3 决策 1），因此不影响现网；文档与测试需明确这一点。
4. **新 tab 的「空窗期」上限从 ≤10s 降为 0**：账本新鲜时插入即生效；账本过期时保持
   ASSIST（安全方向），恢复依赖下一次 heartbeat，不依赖新的定时器。
5. **feature off 零行为不变**：D1-D7 都不引入新对象或新 feature flag；registry 仍只由
   成功的 `setOwnership({windowId})` 创建。

### 9.2 剩余未决问题

无。实现期若发现源码事实与本文冲突（例如 `GetBrowserWindowInterface()` 在目标窗口返回
null），按 §8.1 的 fallback 顺序处理并在 PR 描述中记录，不另行改变 D1-D7 的语义。

---

## 10. 关键代码位置索引

| 用途 | 文件:位置 |
|---|---|
| 窗口展开 / 旧账本 | `chrome/browser/extensions/api/browser_cli_input_ownership/browser_cli_input_ownership_api.cc:170-209, 403-444, 508-575, 619-641` |
| content 状态机 | `content/browser/input_ownership/input_ownership_decider.cc:481-529`（SetOwnership）、`531-560`（Heartbeat）、`87-102`（Refresh/stale）、`55`（30s 常量） |
| content 公开 API | `content/public/browser/input_ownership.h:76-83`（snapshot）、`166-176`（Set/Heartbeat） |
| controller 创建门控 | `content/browser/web_contents/web_contents_impl.cc:4664` |
| 插入通知 | `chrome/browser/ui/tabs/tab_strip_model.cc:4889`（`InsertTabAtIndexImpl`，先插入后 `OnChange`） |
| 删除/移动通知 | `chrome/browser/ui/tabs/tab_strip_model.cc:705-741`（`ProcessTabsForDetach`，`kInsertedIntoOtherTabStrip`） |
| 模型销毁通知 | `chrome/browser/ui/tabs/tab_strip_model.cc:284-288`、`tab_strip_model.h:601` |
| observer 接口（本 revision） | `chrome/browser/ui/tabs/tab_strip_model_observer.h:521-523`（`OnTabStripModelChanged`）；**无** `TabInsertedAt` |
| Browser 侧插入处理 | `chrome/browser/ui/browser.cc:558-615`（`OnTabStripModelChanged`）、`638`（`OnTabInsertedAt`） |
| `window.open` 路径 | `chrome/browser/ui/browser_tabstrip.cc:98`（`AddWebContents`） |
| 会话恢复路径 | `chrome/browser/sessions/session_restore.cc:1140`；`chrome/browser/ui/browser_tabrestore.cc:136,186-192,295` |
| window id 赋值时机 | `chrome/browser/ui/sessions/session_service_browser_helper.cc:87-100` |
| 窗口身份 | `chrome/browser/extensions/browser_extension_window_controller.cc:122,138`（`session_id_(browser->GetSessionID())`） |
| 窗口关闭回调 | `chrome/browser/ui/browser_window/public/browser_window_interface.h:111`；`chrome/browser/ui/unload_controller.cc:942-949` |
| 浏览器集合（fallback） | `chrome/browser/ui/browser_window/public/browser_collection_observer.h`；`.../internal/global_browser_collection.cc:44-63`；`chrome/browser/ui/browser_tab_strip_tracker.cc:47-66` |
| API 可用性门控 | `chrome/common/extensions/api/_api_features.json:330-344`；`browser_cli_input_ownership_availability.{h,cc}` |
| 现有 content 单测 | `content/browser/input_ownership/input_ownership_controller_unittest.cc`；`content/test/BUILD.gn:2683` |
| 现有 E2E | `scripts/test-input-arbitration-e2e.mjs`（K0-K4 为 `kernel` stage 模板） |
