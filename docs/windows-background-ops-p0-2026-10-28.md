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
