# P0：后台操作与截图链路审计（阶段 1）

日期：2026-10-28
范围：BrowserPilot 侧前台依赖清理 + 截图语义修正 + Chromium 遮挡截图的最小修复方案
关联：[windows-background-render-2026-10-28.md](windows-background-render-2026-10-28.md)（测量数据）

---

## 1. 审计结论

`BrowserPilot` 里会改变窗口前台状态的调用只有一处来源：`spaces.ts::ensureTabVisible()`。
它做三件事：`chrome.windows.update({focused:true})`、`chrome.tabs.update({active:true})`、
把 `minimized` 恢复成 `normal`。审计结果：

| 调用点 | 原行为 | 处置 | 理由 |
| --- | --- | --- | --- |
| `cdp.ts::captureScreenshot` | `ensureTabVisible(tabId)` | **删除** | 截图不需要前台；隐藏/最小化窗口本身就能取帧（实测 1–4s） |
| `cdp.ts::scrollCaptureScreenshot` | `ensureTabVisible(tabId)` | **删除** | 同上；并防止它内部再次经 `captureScreenshot` 触发 |
| `spaces.ts::open_tab`（`visible` 为真时） | 恢复/聚焦窗口 | **保留** | 显式 `visible` 是调用方的真实意图 |
| `spaces.ts::switch_tab`（`visible` 为真时） | 恢复/聚焦窗口 | **保留** | 同上 |
| `spaces.ts::ensure_visible` | 默认聚焦 | **保留** | 这个命令的语义就是"让它可见可操作" |
| `open_tab` / `switch_tab` 的 `active: true` | 切换窗口内活动标签 | **保留** | 只影响该窗口内部；space 归属保证不会碰用户的 space |

`cdp.ts` 中已无 `ensureTabVisible` 调用（源码注释里保留了"为什么刻意不调用"的说明）。

---

## 2. 截图语义修正

### 2.1 旧实现的问题

```ts
await ensureTabVisible(tabId).catch(() => {});
const res = await sendCommand(tabId, "Page.captureScreenshot", { format });
```

三处缺陷：

1. **改变前台状态**：会聚焦窗口、切换活动标签，与"AI 不打扰用户桌面"直接冲突。
2. **无超时**：`chrome.debugger.sendCommand` 在窗口被遮挡时不会返回，整条命令链会挂死。
3. **无新鲜度判断**：拿到的可能是旧帧却照常返回成功。

### 2.2 新实现

`captureScreenshot(tabId, format, { timeoutMs = 8000 })` 返回 `{ base64, meta }`：

```ts
meta = { fromSurface: boolean, durationMs: number, possiblyStale: boolean, reason?: string }
```

- 先走 `fromSurface: true`（合成器取帧），带硬超时；
- 超时/空数据 → 改用 `fromSurface: false`（浏览器进程快照），**并把 `possiblyStale` 置 true、记录原因**；
- 两条路径都失败 → 抛出明确错误，绝不静默返回空图。

`scroll_screenshot` 的返回里也新增 `possiblyStale`（任一屏降级即 true）。

> 设计取舍：不因为"可能旧帧"就直接失败——有些调用方只想要"大致长什么样"。但它**永远不会自称实时**。

---

## 3. 前台租约（`foreground-lease.ts`）修正

### 3.1 原实现的问题

`FOREGROUND_COMMANDS` 几乎包含所有命令（`snapshot`、`screenshot`、`click`、`type`、`js`、
`tab_cdp_call`、`open_tab`…），而租约是**进程级单实例**（`state.foregroundLease`）。

后果：**两个 Agent 在各自窗口做纯后台操作时也会互相阻塞**，第二个会拿到
`FOREGROUND_BUSY`。这与"多 Agent 并行"的目标直接冲突，是本次审计发现的第二个实质缺陷。

### 3.2 修正

租约只覆盖**会改变窗口展示状态**的命令：

```ts
const FOREGROUND_COMMANDS = new Set<CommandName>([
  "ensure_visible", "open_space", "close_space", "complete_space", "handoff_space",
  "run_template", "start_mask", "stop_mask",
]);
```

后台能力（DOM 读写、点击/输入、滚动、截图、求值、等待、CDP 透传、下载）**不再取租约**。

同窗口内的输入冲突不靠这个租约解决：space 归属已保证一个 space 只有一个 owner；
人机争同一 Tab 的控制权留给 P3（插件遮罩 + 内核输入仲裁）。

测试同步更新（只增不减）：

- 原有的互斥 / 模板重入 / 自释放断言保留；
- 新增断言：上表 22 个后台命令对两个不同 Agent 都必须返回 `undefined`（即并行）。

---

## 4. Chromium 侧：遮挡截图的最小修复方案

### 4.1 已确认的机制（源码）

```text
RenderWidgetHostViewAura::WasOccluded()
  → visibility_ = OCCLUDED; HideImpl()
  → delegated_frame_host_->WasHidden(HiddenCause::kOccluded)

DelegatedFrameHost::WasHidden()            delegated_frame_host.cc:140
#if BUILDFLAG(IS_WIN)
  if (cause == HiddenCause::kOccluded) return;   // ← 保留表面，为标签页预览
#endif
  frame_evictor_->SetVisible(false);             // ← 隐藏时才会走到
```

`WebContentsViewAura::OnCapturerCountChanged()`（截图时由 `IncrementCapturerCount`
触发）只做了 `CreateVideoCaptureLock()`，**不改变可见性、不强制产帧**。

所以：隐藏窗口 → `SetVisible(false)` → 截图请求新帧 → 成功；
遮挡窗口 → 提前返回 → `frame_evictor_` 仍认为有活表面 → `CopyFromCompositingSurface`
等一个永不到来的帧 → **挂起**。

### 4.2 候选方案

| 方案 | 改动 | 成本 | 风险 | 评价 |
| --- | --- | --- | --- | --- |
| **C（已实施，插件侧）** | `fromSurface=false` 降级 + `possiblyStale` 标记 | 0（已完成） | 中：可能旧帧，但已显式标注 | 立即生效的兜底 |
| **A（推荐的内核候选）** | **截图期间把 `kOccluded` 当作 `kHidden` 处理**：在 `DelegatedFrameHost::CopyFromCompositingSurface` 入口，如果 `host_->is_hidden()`（或其他可查询的"正在被捕获"标志）且 `frame_evictor_` 仍为可见，则先 `frame_evictor_->SetVisible(false)` 再发起复制，使本次捕获走"请求新帧"路径 | 约 10–15 行 + 一个单测 | 中：触碰的是上游为**标签页预览**加的 Windows 特例 | 语义最贴合"按需唤醒一帧"：只在截图期间生效，不改变常态节流 |
| **B** | `WasHidden(kOccluded)` 干脆不再提前返回 | 1 行 | 高：会影响 Windows 标签页悬停预览（上游加这个特例的原因） | 不推荐 |
| **D** | 窗口级关闭遮挡计算（`--disable-features=CalculateNativeWinOcclusion`） | 0 行 | 中：被遮挡窗口持续全速渲染（实测 3 个窗口各 +72 frame/1.2s） | 仅开发/自动化，不进发布 |
| **E** | 截图前把 WebContents 临时标为 VISIBLE，截完恢复 | 中（需要新的可见性旁路） | 高：会连带唤醒 rAF/定时器，等于短暂全速渲染 | 不如 A 精准 |

### 4.3 推荐

**先 C（已做），A 作为内核候选**，并明确：

- A 只影响"正在截图"的那一次捕获，常态下遮挡窗口仍然节流 —— 满足"不要全部窗口持续全速渲染"。
- A 需要单测覆盖两种情形：遮挡 + 截图 → 拿到新帧；遮挡 + 无截图 → 仍是节流状态。
- B 只有在确认不影响标签页预览时才考虑。
- 明确不采用"截图前把窗口前置/抢焦点/show"。

**在实施 A 之前需要先确认**：`DelegatedFrameHost` 能否可靠得知"当前有捕获在等待"
（当前 `IncrementCapturerCount` 只到 `WebContentsViewAura`，没有下发到 DFH）。
两种落地方式：

1. 在 `DelegatedFrameHost` 上加一个 `SetCapturePending(bool)`，由 `PageHandler`
   在捕获前后调用（改动集中、可控）；
2. 或者复用 `host_->is_hidden()` + `frame_evictor_` 状态判断，零新接口但语义隐晦。

倾向 1：显式、可测试。

---

## 5. 待验证（本阶段后续）

| 项 | 方法 |
| --- | --- |
| 截图不再改变前台状态 | 对隐藏窗口调用 `screenshot`，前后对比 `chrome.windows.get().focused` 与活动 tabId |
| 多窗口不互相串行 | 两个 Agent 同时对不同 space 发 `click`/`screenshot`，断言都在 ~1 个命令时间内完成，且都不返回 `FOREGROUND_BUSY` |
| 遮挡截图行为 | 复用 `tests/tools/background-render-cdp-check.py --scenario occluded`，确认 `possiblyStale` 被正确置位 |
| 3 / 8 窗口完整操作 | 复用同一 harness 扩展为"操作 + 截图" |

---

## 6. 新增风险

1. **移除截图的前台依赖后，隐藏窗口截图变慢**（1–4 秒，等待渲染进程产帧）。这是真实代价，调度层需要按"截图是慢操作"来设计超时与并发。
2. **后台命令不再有进程级互斥**：两个 Agent 若真操作同一 Tab（space 归属被破坏时）不会再被租约挡住。这是有意的取舍，由 P3 的输入仲裁接管。
3. **`possiblyStale` 需要调用方真正尊重**：若上层把它当实时画面用，等于恢复旧行为。建议在 Popup/CLI 输出里显式提示。

---

## 7. 内核侧遮挡修复结果（2026-10-28 晚）与产品侧加固

### 7.1 内核修复：BLOCKED

Agent A 用运行时证据定位到：被完全遮挡的 Windows 窗口，UI 合成器**一帧都不产**；
`RequestCopyOfOutput` 只是入队，唯一取出口是 `SurfaceAggregator::PrewalkSurface`
→ `Surface::TakeCopyOutputRequestsFromClient`，必须有一次真实聚合。实测遮挡 16s 内
`TakeCopyOutputRequestsFromClient=0`，隐藏窗口 13 次；`DisplayScheduler::AttemptDrawAndSwap=0`。

候选 A/B/D（evictor 走隐藏分支 / 去掉 `WasHidden(kOccluded)` 早退 / capturer-count 定向）
未实施即否决；C1 `ScheduleFullRedraw` 仅开 stderr 日志时偶然通过，关日志 16s 超时；
C2/C3/C4（`SchedulePaintInRect` / 组合 / `SetVisible` 循环）均 0 次聚合。按 fail closed，
**未生成 patch 027，Chromium 工作树已回到 26 个 patch 的基线**。细节见
`windows-occluded-capture-2026-10-28.md`。

### 7.2 新发现（基线既有，P0）：遮挡窗口反复截图会让渲染进程崩溃

未改动的基线构建上，`--scenario occluded --repeat 3` 即触发：

```
FATAL ... layer_tree_view.cc:554
DCHECK failed: callbacks.size() <= PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60)
```

每次 `fromSurface=true` 都在渲染进程累积一个 presentation-time 回调；遮挡窗口不呈现，
回调永不消费，到 60 即 abort。产品侧对遮挡窗口反复截图同样会命中。

### 7.3 产品侧加固：合成器路径冷却 + 同 tab 串行

`src/background/cdp.ts` + `src/background/screenshot-policy.ts`：

- 某个 tab 的 `fromSurface=true` **超时**后进入冷却：基础 30s，连续超时按 2 倍退避
  （30s → 60s → 120s），连续 4 次超时后**硬闸**停止尝试合成器路径，直到
  `resetSurfaceCooldown(tabId)`（`ensure_visible` 成功后会调用）或一次成功的
  `fromSurface=true`；
- 冷却期内直接走 `fromSurface=false`，不调用合成器路径，`possiblyStale=true` 且
  `reason="fromSurface-cooldown-after-timeout"`；
- 同一 tab 的截图用 per-tab 队列串行：并发调用（多 Agent、scroll + viewport）最多只有一个
  `captureScreenshot` 在飞，第二个会等第一个超时/降级结束后再判断冷却，不会各自发起
  `fromSurface`；不同 tab 使用各自的链，互不阻塞；
- 目的：避免每轮截图挂满 8s 超时，并把未呈现回调的累积速率压到 60 阈值以下；
- **不改变“stale 不能当成功”的语义**：调用方仍会看到 `possiblyStale=true`。

#### 7.3.1 主代理审核结论（五个必查点）

1. **超时后 CDP 底层请求是否仍未完成**：是。`chrome.debugger.sendCommand` 没有取消 API；
   超时只清理扩展侧 pending 表、挂迟到消费器并丢弃迟到帧，浏览器侧的
   `Page.captureScreenshot` 可能一直挂到标签关闭/`detach`。冷却只能阻止**新增**请求；
   这不是扩展侧泄漏，但内核侧请求仍占用。要真正释放只能 detach/关标签（本轮不做）。
2. **同一 tab 是否会累积多个挂起截图**：加固前会；现在 per-tab 队列保证同一时刻最多一个
   `captureScreenshot` 在飞，测试用两个并发调用验证 `fromSurface` 只发 1 次。
3. **冷却能否真正阻止 presentation 回调持续积累**：能阻止**无限**积累。基础 30s + 退避 +
   连续 4 次硬闸；按 A 的实测（6 次 fromSurface 达到 61），4 次约 40 个回调，低于 60。
   这是估算值，不是内核保证；且 `ensure_visible` 会重置硬闸，若调用方反复显式恢复窗口而
   窗口仍被遮挡，理论上可再次累积（属于显式前台操作，不在后台截图循环内）。真正的修复
   仍需 viz 层“按需一帧”或内核侧丢弃未呈现回调。
4. **超时/降级是否影响其它 tab**：不影响。冷却表、pending 表和截图队列都按 tabId 隔离；
   测试让 tab 21 永久挂起，同时 tab 22 正常返回新鲜帧。
5. **冷却不是修复**：遮挡窗口仍返回 `possiblyStale=true` 的旧帧；它只是防崩与快速失败，
   不能作为 P0“最新截图成功”的验收。

测试：`npm run typecheck`、`npm run test:core`、`npm run build`、`npm run registry:check`
均通过；新增 `SurfaceCooldown` 退避/硬闸测试、`skipFromSurface` 降级语义测试、
“超时→冷却期直接快照→清冷却后才重新尝试合成器”测试，以及同 tab 串行/跨 tab 独立测试。

### 7.4 最终结论（2026-10-29）

- **遮挡窗口的“最新帧”截图已实现并通过产品级验收**：patch 027 在
  `FrameSinkManagerImpl::RequestCopyOfOutput` 入队后强制激活 pending root frame，
  使 display 聚合一次真实帧并取出 copy 请求。原始 CDP harness 与真实 BrowserPilot
  `screenshot` 命令均 PASS。
- 产品级证据（`product-occluded-2026-10-29T01-36-29-073Z.json`，52/52 checks）：
  - 遮挡窗口 `fromSurface=true`、`possiblyStale=false`，PNG 帧号与 DOM 一致；
  - 60 次连续截图全部 fresh，marker 5→300 全匹配；无 `layer_tree_view.cc:554`、
    无 FATAL、无 0xC0000005、无 Crashpad dump；
  - iframe / Canvas2D / WebGL 像素全部命中；
  - 3 窗口并发截图全部 fresh，前台 HWND 与各窗口 `GW_HWNDPREV` 前后不变；
  - HKCU 注册已恢复原值，测试进程按 PID+创建时间+marker+exe 验证后清理。
- **`possiblyStale=true` 仍然不能作为验收成功**；patch 027 下遮挡截图应返回
  `possiblyStale=false`，返回 stale 即视为失败。
- 残余风险：本机目标窗口在遮挡前已被其它顶层窗口覆盖，未覆盖
  `visible→hidden` 跃迁；遮挡窗口 resize 后 `innerWidth` 滞后（测试用 24px marker
  自校准）；未做分钟级持续压测；多父 root/tab 拖拽、macOS/Linux、发布签名构建未覆盖。

