# Windows 后台渲染与截图能力验收（P0）

日期：2026-10-28
范围：多窗口在后台时能否取得**正确且最新**的画面；不修改内核，先测量。
配套：`tests/tools/background-render-cdp-check.py`、`tests/fixtures/background-render-probe.html`

---

## 1. 结论：**PARTIAL**

按任务要求的判定口径：

- **产品主路径（窗口由 managed 策略保持原生隐藏 / 最小化）→ PASS。**
  3 窗口 × 3 轮 = 9/9；8 窗口 × 2 轮 = 16/16。截图不超时、非黑、**页面主动重绘后拿到的是新帧**。
- **"窗口被用户桌面上的其它窗口完全遮挡" → BLOCKED。**
  `Page.captureScreenshot`（默认 `fromSurface=true`）会**挂起直到超时**；改用 `fromSurface=false` 不再挂起，但**返回旧帧**。
- 该阻塞**有明确且低风险的规避手段**（关闭原生遮挡计算，或产品层不依赖"已上屏但被遮挡"的窗口），因此整体判 PARTIAL 而非 BLOCKED：
  现有 CDP/Compositor 机制足以支撑"多个 AI 在各自窗口里后台工作并读取正确画面"，**前提是不把"用户已经看到、随后又被别的窗口盖住"的窗口当作截图目标**。

---

## 2. 三种状态必须分开：实测矩阵

设施：真实可见窗口（非 headless）。每窗口 3 轮。`raf+` / `timer+` 为 1.2 秒内增量。
截图判定不只"有没有 PNG"：页面把帧号编码成 24×24 色块的 RGB，截图后解码并与 DOM 帧号对照，**旧帧会被识别出来**。

| 原生状态 | `visibilityState` | rAF | timer | 截图（`fromSurface=true`） | 截图（`fromSurface=false`） | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 正常显示（前台） | visible | 运行 | 正常 | OK | — | PASS（基线） |
| **原生隐藏**（`SW_HIDE`） | hidden | **停止** | ~1 次/秒 | **OK，~3.1s，重绘后是新帧** | — | **PASS** |
| **最小化**（`SW_MINIMIZE`） | hidden | **停止** | ~1 次/秒 | **OK，~3.1s，重绘后是新帧** | — | **PASS** |
| **完全遮挡** | hidden | **停止** | ~1 次/秒 | **挂起 → 超时（16s 未返回）** | 返回（386ms）但**旧帧** | **BLOCKED** |
| 遮挡 + `--disable-features=CalculateNativeWinOcclusion` | **visible** | **运行（+72/1.2s）** | 正常 | OK，**~47ms**，新帧 | — | PASS |

关键记录：

- 隐藏与最小化下 **rAF 完全停止**，定时器被节流到约 1 次/秒（Chromium 预期行为）。**持续 Canvas 动画在后台不会跑**；但**按需重绘（JS 主动 paint）后截图能拿到新内容**，这正是 AI 需要的模式。
- 截图耗时：隐藏/最小化约 **1–4 秒**（要等渲染进程产出新帧）；遮挡+关闭遮挡计算约 **47ms**。
- 无一次返回纯黑或透明空图（`dark_ratio=0.0`）。

### 并发与压力

| 测试 | 规模 | 结果 |
| --- | --- | --- |
| 隐藏窗口并发 | 3 窗口 × 3 轮 = 9 采样 | **9/9 新帧**，0 超时，中位 3114ms |
| 隐藏窗口压力 | **8 窗口 × 2 轮 = 16 采样** | **16/16 新帧**，0 超时，0 黑，中位 651ms |
| 遮挡窗口并发（关闭遮挡计算） | 3 窗口 × 3 轮 = 9 采样 | **9/9 新帧**，中位 **47ms** |

> `maxConcurrentWindows=8` 现在有了实测支撑：8 个隐藏窗口并行操作与截图全部成功。但注意这是"隐藏窗口"的 8 个，不是"8 个都在跑动画"。

---

## 3. 复现条件（截图挂起 / 黑图 / 旧帧）

| 现象 | 复现条件 | 说明 |
| --- | --- | --- |
| **挂起（超时）** | 窗口原生可见但被其它窗口**完全遮挡**，调用 `Page.captureScreenshot`（`fromSurface=true`） | 与"窗口被隐藏"完全不同 |
| **旧帧** | 同上，改用 `fromSurface=false` | 走 `GetSnapshotFromBrowser` 的浏览器进程快照，返回缓存位图 |
| **黑图/空图** | 窗口**未前置**时用屏幕抓图或 CUA 截图；CDP `captureScreenshot` 在遮挡时会挂起而不是返回黑图 | 黑帧是**捕获方式**的问题，不是渲染问题；先 `Page.bringToFront` 并确认 `document.hasFocus()` 再判定渲染 |
| **rAF 停止** | 窗口隐藏 / 最小化 / 遮挡 | Chromium 预期节流，不是缺陷 |

---

## 4. 源码调用链与根因

### 4.1 遮挡如何变成"页面不可见"

```text
ui/aura/window_occlusion_tracker*         计算原生遮挡
  → RenderWidgetHostViewAura::WasOccluded()
      visibility_ = Visibility::OCCLUDED
      HideImpl()
        host()->WasHidden()
        delegated_frame_host_->WasHidden(HiddenCause::kOccluded)
  → WebContentsImpl::UpdateWebContentsVisibility(Visibility::OCCLUDED)
      document.visibilityState = "hidden"，rAF 停止
```

### 4.2 为什么"遮挡"挂起、而"隐藏"却成功

根因在 `content/browser/renderer_host/delegated_frame_host.cc:140`：

```cpp
void DelegatedFrameHost::WasHidden(HiddenCause cause) {
  tab_switch_time_recorder_.TabWasHidden();
#if BUILDFLAG(IS_WIN)
  // Ignore if the native window was occluded.
  // Windows needs the frame host to display tab previews.
  if (cause == HiddenCause::kOccluded)
    return;                        // ← Windows 专属：直接返回
#endif
  frame_evictor_->SetVisible(false);
}
```

- **遮挡**（`kOccluded`）：提前 `return`，frame evictor 仍认为存在可用的活动表面。
  随后 `PageHandler` 的截图路径虽然已经 `IncrementCapturerCount(stay_hidden=true, stay_awake=true)`，但 `CopyFromCompositingSurface` 仍等待那个"活表面"提交新帧——而遮挡的 compositor 不产帧 → **一直等到超时**。
- **隐藏/最小化**（`kOther`）：执行 `frame_evictor_->SetVisible(false)`，`DelegatedFrameHost` 明确知道自己没有活动表面，截图改走"请求一帧新的"路径；capturer count 保证渲染进程愿意产帧 → **成功，代价是 1–4 秒**。

这处 `#if BUILDFLAG(IS_WIN)` 是上游为 **标签页预览** 加的（悬停预览需要在窗口被遮挡时也能画出内容），不是为截图设计的。

### 4.3 `fromSurface` 的差别

`content/browser/devtools/protocol/page_handler.cc:1461`：`fromSurface=false` 走
`RenderWidgetHostImpl::GetSnapshotFromBrowser()`（浏览器进程侧快照，异步、不要求新帧），
因此不会挂起，但它拿到的是**缓存位图**，对刚发生的页面更新是旧的。

---

## 5. 修复方案与风险排序

| 优先级 | 方案 | 成本 | 兼容性 | 风险 | 说明 |
| --- | --- | --- | --- | --- | --- |
| **A（推荐先做）** | **产品层调度规则**：截图目标只用 hidden / minimized / 被遮挡时先降级 | 0 代码 | 最好 | 无 | Browser CLI 的正常后台窗口本来就是 hidden（E2E 已 16/16）。**只有"已向用户展示、随后被别的窗口盖住"的窗口不可截图**。调度器对这类窗口应：不把它当视觉目标，或先取 `fromSurface=false` 并标注"可能是旧帧" |
| **B** | 启动参数 `--disable-features=CalculateNativeWinOcclusion`（或 `--disable-backgrounding-occluded-windows`） | 1 行 | 好 | 中：被遮挡窗口会**继续全速渲染**（实测 3 个遮挡窗口 rAF 均 +72/1.2s），有 CPU/GPU 成本；且这两个开关上游定位是测试用 | 可逆、无内核改动；适合开发/自动化运行，是否进发布配置需另行评估 |
| **C** | CDP 层：对遮挡窗口用 `fromSurface=false` + 明确标注可能为旧帧 | 0 代码（调用方改） | 好 | 中：**旧帧冒充实时画面**，正是禁止项，必须由调用方显式接受 | 只作为"至少给一张图"的降级 |
| **D** | 内核窄补丁：让 `WasHidden(kOccluded)` 在 Browser CLI 的 managed 窗口上不提前返回，或让截图路径对 `kOccluded` 强制请求新帧 | 小（约 5–15 行 + 单测） | 中 | **中高**：动的是上游为标签页预览加的 Windows 特例，可能影响悬停预览 | 只有在 A/B 都不能满足时才做；需要单独的可见 UI 回归 |

**不采用**：为了截图把窗口前置、临时抢焦点或自动 show。这违背产品设计，也已被本项目的展示策略禁止。

---

## 6. 建议的产品语义（可直接写进调度层）

1. **AI 的常规后台窗口 = 原生隐藏窗口** → 截图可用、可并发（8 窗口已验证）。
2. **动画不会在后台自己跑**：隐藏/最小化时 rAF 停、定时器约 1Hz。需要"最新画面"时，由调用方先触发一次重绘（例如 `Runtime.evaluate` 触发 paint 或滚动），再截图；实测这样能拿到新帧。
3. **HUMAN / ASSIST 窗口被用户桌面其它窗口盖住时，不要把它当作视觉目标**；要么先 `Page.bringToFront`（属于用户可见操作，需策略允许），要么降级为"非实时"。
4. 截图必须带超时；超时应返回明确错误而不是静默等待。

---

## 7. 交付物与复用方式

```sh
# 隐藏窗口 3 并发 × 3 轮
python tests/tools/background-render-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario hidden --windows 3 --repeat 3 \
  --result-json D:/c/logs/bg-hidden.json

# 被遮挡窗口（复现挂起）
python tests/tools/background-render-cdp-check.py ... --scenario occluded --windows 1 --repeat 1

# 遮挡 + 关闭遮挡计算（验证规避手段）
python tests/tools/background-render-cdp-check.py ... --scenario occluded --windows 3 --repeat 3 \
  "--extra-switch=--disable-features=CalculateNativeWinOcclusion"

# 8 窗口压力
python tests/tools/background-render-cdp-check.py ... --scenario hidden --windows 8 --repeat 2
```

判定不是"有 PNG"：脚本解码 PNG、检查非黑、并用页面编码的帧号色块与 DOM 帧号比对；
`--from-surface false` 可切换捕获路径。退出码 0 只在 **PASS** 时返回，PARTIAL/BLOCKED 均非 0，便于接 CI。

### CI 接入建议

- **可进 CI（无需可见桌面）**：`hidden` 场景（managed 或未 show 的窗口）+ 3 窗口 + 2 轮，断言 PASS。这是产品主路径。
- **不宜进 CI**：`occluded`（需要真实桌面遮挡）与任何可见窗口操作；保留为人工/自托管验收，并记录 `result-json`。
- 建议把 `hidden` 3 窗口这一条加进 `verify-windows.sh` 的第 6 步（作为可选步骤，缺 `--extension-dir` 时跳过）。
