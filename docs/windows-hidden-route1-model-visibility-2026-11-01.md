# Windows managed 窗口长驻：route 1（模型可见性同步）根因、提案与验收计划

日期：2026-11-01
分支：`agent/rc-hidden-a1`（browser-cli PR #1，base `833b880`）
范围：只做 route 1——**managed 原生 presentation 被拒绝时，让 WebContents /
RenderWidgetHostView 真正进入 hidden**；用户再次显式呈现时恢复。不做 route 2
（viz failed-feedback 硬化），不改 presentation policy，不删 DCHECK，不扩 buffer。

状态：**PATCH PROPOSED（未应用、未构建、未跑修复后验收）**。
已有构建的基线崩溃证据在本轮重新复现（见 §4）；修复后断言与 8 窗口 ≥20 分钟
测试计划见 §5–§6。

关联文档：
route 0 根因与 harness：
`D:/work/wt/bc-rc-hidden/docs/windows-hidden-presentation-stability-2026-11-01.md`
（另一 worktree，只读引用）、
[windows-background-render-2026-10-28.md](windows-background-render-2026-10-28.md)、
[windows-occluded-capture-2026-10-28.md](windows-occluded-capture-2026-10-28.md)。

---

## 1. 结论摘要

- **补丁**：`chromium/patches/proposed/037-managed-window-contents-visibility.patch`
  （2 文件，+124/−0；`chrome/browser/ui/views/frame/browser_view.{h,cc}`）。
- **机制**：在 `BrowserView::Show()`（以及 `ShowInactive()`、`OnActiveTabChanged()`）
  之后，如果“该窗口是 managed 后台窗口 + widget 仍不可见 + 同一条 policy 判定
  presentation 被拒绝”，就对该窗口所有 tab 调用 `WebContents::WasHidden()`；
  并置 `browser_cli_contents_hidden_ = true`。当窗口**真的**呈现
  （`OnWidgetVisibilityChanged(visible=true)`，覆盖 USER_EXPLICIT show、
  assist lease、原生恢复）时，对 active contents 调用 `WasShown()`。
- **为什么能根治**：renderer 侧 `WidgetBase::SetHidden(true)` 会让 compositor
  不可见，main-frame 不再提交；`presentation_callbacks_` 不再增长，60 s 后的
  `layer_tree_view.cc:554` DCHECK 从源头消失。页面语义与真正的后台 tab 完全一致：
  `visibilityState=hidden`、rAF 停、timer 约 1 Hz；按需重绘 + 截图仍可用。
- **不改**：窗口仍是原生隐藏；USER_EXPLICIT / HUMAN 所有权、assist lease、
  自动化拒绝规则全部不变；host gate（`DesktopWindowTreeHostWin::Show` 提前返回、
  `OnWindowPosChanging` 剥 `SWP_SHOWWINDOW`）保持原样。
- **基线**：本轮用现有 `D:/c/src/out/WinDev/chrome.exe`（patch 001–033，未含本提案）
  在 1 窗口 text 场景 **65.206 s** 触发精确 DCHECK（61 vs. 60）；既有 8 窗口
  release-like 运行 6/6 动态内容在 81–86 s 崩溃、static/no-paint 存活（§4）。
- **未完成**：本 worktree 没有独立 Chromium checkout，**没有构建/运行修复后版本**；
  `git apply --check` 对 `D:/c/src` 当前工作区通过（只读校验，未修改源码）。

---

## 2. 根因补全：native 拒绝为什么没有变成 model hidden

route 0 已证明“原生隐藏 + 模型可见”是危险组合。本提案把“模型为什么一直可见”
补到具体代码点。

### 2.1 初始化：WebContents 默认就是 VISIBLE

`content/browser/web_contents/web_contents_impl.cc:4427`：

```cpp
visibility_ = params.initially_hidden || initially_hidden_but_painting_
                  ? Visibility::HIDDEN
                  : Visibility::VISIBLE;
```

浏览器 tab 的 `CreateParams::initially_hidden` 默认为 `false`
（`content/public/browser/web_contents.h:254`），因此 managed 窗口里的页面
从创建起就是 `VISIBLE`。

### 2.2 首次导航提交：把 RenderWidgetHostView 显式 show

`web_contents_impl.cc:11511`：

```cpp
void WebContentsImpl::PrimaryMainFrameCommitted(RenderFrameHostImpl* new_frame) {
  ...
  SetPrimaryMainFrameViewVisibility(GetVisibility());   // VISIBLE
}
```

`SetPrimaryMainFrameViewVisibility(VISIBLE)` → `RenderWidgetHostViewAura::ShowWithVisibility()`
（`render_widget_host_view_aura.cc:1075`）→ `ShowImpl()` →
`NotifyHostAndDelegateOnWasShown()`（`:712`）→ `host()->WasShown()`。renderer 从此
认为页面可见。

### 2.3 为什么“先收到 HIDDEN”没有生效

窗口从未 show 时，`WebContentsViewAura` 的 occlusion 会算出 HIDDEN，并调用
`WebContentsImpl::UpdateWebContentsVisibility(HIDDEN)`；但
`web_contents_impl.cc:12651` 的 `did_first_set_visible_` 门禁会**忽略第一次
VISIBLE 之前的 HIDDEN/OCCLUDED**：

```cpp
if (!did_first_set_visible_) {
  if (visibility == Visibility::VISIBLE) {
    WasShown();
    did_first_set_visible_ = true;
  }
  return;                    // HIDDEN 被丢弃
}
```

于是 HIDDEN 被丢弃，紧接着导航提交又走 2.2 把 view 显示出来。**原生 gate 只拦住了
HWND，没有拦住这条 model 可见性链路。**

### 2.4 后果链（route 0 已量化）

```
native show 被策略拒绝（DesktopWindowTreeHostWin::Show 提前 return /
OnWindowPosChanging 剥 SWP_SHOWWINDOW）
  → WebContents/renderer 仍 VISIBLE（2.1–2.3）
  → 页面持续产生 main-frame（text/raf/canvas/webgl/extension/OOPIF 均可）
  → display 永不 present；viz 对 undrawn frames 把 BeginFrame 节流到 ~1 Hz
    （compositor_frame_sink_support.h:86 kUndrawnFrameLimit=3）
  → 每帧注册的 presentation callback 永不消费
  → 60 条约 60 s 后 layer_tree_view.cc:554
    DCHECK_LE(callbacks.size(), kMaxBufferSize /* 60 */) → 61 vs. 60
  → renderer 被杀，browser process 存活
```

### 2.5 现有 Windows 呈现路径（补丁的落点）

| 代码 | 作用 |
| --- | --- |
| `ui/views/widget/desktop_aura/desktop_window_tree_host_win.cc:413` | `Show()` 先问 `BrowserCliAllowsPresentation()`；拒绝时**直接 return**，不 show content window、不 ShowWindow |
| `ui/views/win/hwnd_message_handler.cc:3262` | 外部 `SetWindowPos(SWP_SHOWWINDOW)` / 未门禁 `ShowWindow` 的最后兜底：`BrowserCliShouldSuppressWindowPosChange()` 为真时剥 flag |
| `ui/views/widget/browser_cli_presentation_win.cc:82` | `BrowserCliShouldSuppressWindowPosChange()`：managed + 非 HUMAN/lease → 拒绝 |
| `ui/views/widget/browser_cli_presentation_win.cc:101` | `BrowserCliPresentForUserExplicitAction()`：USER_EXPLICIT 作用域 + Show/Activate（含一次 STARTUPINFO 重试） |
| `chrome/browser/browser_cli/user_explicit_presentation_win.cc:143` | CLI `--browser-cli-user-explicit-show` / 用户动作的生产者入口；patch 033 走 `BrowserWindow::Show()` |
| `chrome/browser/ui/views/frame/browser_view.cc:1601` | `BrowserView::Show()` → `browser_widget_->Show()`；所有窗口 show（扩展 `windows.create`/`tabs.create`、DevTools `Target.createTarget(newWindow)`、session restore、用户 CLI）最终到这里 |

`DesktopWindowTreeHostWin::Show` 的拒绝是“原生层”的；`browser_view.cc` 是
chrome 产品层能看到“Show 请求已经发生、但 widget 仍不可见”的最小位置。

---

## 3. 提案 patch 034 的实现

文件：`chromium/patches/proposed/037-managed-window-contents-visibility.patch`
（**未应用**到 `D:/c/src`；`git apply --check` 通过）。

### 3.1 代码点

新增（`browser_view.cc` 匿名 namespace，`#if BUILDFLAG(IS_WIN)`）：

```cpp
bool BrowserCliShouldKeepContentsHidden(BrowserWindowInterface* browser,
                                        views::Widget* widget) {
  if (!views::BrowserCliPresentationPolicy::Get()->IsManagedModeEnabled())
    return false;
  if (!browser || !widget || widget->IsVisible())
    return false;
  // 与 host gate 同一条决策；USER_EXPLICIT / assist lease 在这里是 allow。
  return !views::BrowserCliAllowsPresentation(
      widget, views::BrowserCliPresentationAction::kShowAndActivate);
}

void BrowserCliHideContentsForUnpresentedWindow(BrowserWindowInterface* browser) {
  // 对该窗口每个 tab：GetVisibility()!=HIDDEN 时 WasHidden()
  // 结束后 LOG(INFO) transition=managed-contents-hidden
}
```

调用点：

1. `BrowserView::Show()`：`browser_widget_->Show()` 之后、`OnWindowDidShow()` 之前；
   条件成立则 `browser_cli_contents_hidden_ = true;` + 隐藏所有 tab。
2. `BrowserView::ShowInactive()`：同上（`kShowInactive` 同样会被拒绝）。
3. `BrowserView::OnActiveTabChanged()`：hidden managed 窗口里激活/新增的 tab
   会再次被 `PrimaryMainFrameCommitted()` 显示；在这里再隐藏一次。
4. `BrowserView::OnWidgetVisibilityChanged(widget, visible)`：
   - `visible=false` 且 managed → 记下“需要恢复”；
   - `visible=true` 且曾隐藏 → 对 active contents `WasShown()`，
     并 `LOG(INFO) transition=managed-contents-restored`。
5. `browser_view.h`：新增 `bool browser_cli_contents_hidden_ = false;`（仅 Windows）。

### 3.2 行为矩阵

| 场景 | 原生 widget | 修复前 model | 修复后 model | 说明 |
| --- | --- | --- | --- | --- |
| managed 后台窗口，agent/extension/session restore Show | hidden | **visible** | **hidden** | 本提案目标；不再产生 frame |
| managed 后台窗口，USER_EXPLICIT show | visible | visible | visible | policy allow，不进隐藏分支；若之前隐藏则在可见事件恢复 |
| managed 后台窗口，assist lease show | visible | visible | visible | `BrowserCliPresentForAgentAssistAction` → widget visible → 可见事件恢复 |
| HUMAN 窗口再次被后台 Show 请求 | hidden | visible | hidden | 窗口本来不可见；隐藏模型正确，用户下次恢复时再 WasShown |
| HUMAN 窗口可见时的后台 Show/Activate | visible | visible | visible | `widget->IsVisible()` 为真，不触碰 contents |
| unmanaged（无 managed switch） | 任意 | 原样 | **原样** | 第一道 `IsManagedModeEnabled()` 直接返回 |
| 真正 native hidden 的普通对照 | hidden | hidden | hidden | 不变 |

### 3.3 为什么这个改法不违反约束

- **不是“只隐藏 HWND、模型仍可见”**：恰恰相反，它把 model 变成 hidden，与原生一致。
- **不是放宽 policy**：没有新增 allow；`BrowserCliAllowsPresentation` 只做查询，
  USER_EXPLICIT 的 ownership 授予仍由 host gate 完成（同一次 Show 中先发生）。
- **不删 DCHECK、不扩 buffer**：`layer_tree_view.cc` / `presentation_time_callback_buffer.h`
  未改动。
- **不依赖周期截图/重启**：隐藏来自首次 Show 拒绝，持续有效；测试计划禁用
  `--screenshot-every` 与页面 reload。
- **恢复路径是显式呈现本身**：`OnWidgetVisibilityChanged(true)` 只在 HWND 真正
  可见时触发，而不是“又调了一次 Show()”。

### 3.4 为什么不把逻辑放进 ui/views / content

- ui/views 不能依赖 `content::WebContents`；把 HWND strip 直接接到 WebContents
  会破坏层依赖。
- 在 `WebContentsImpl::UpdateWebContentsVisibility` 里把“首次 HIDDEN”改成生效，
  是 route 2 式的通用上游改动，影响所有平台/所有隐藏窗口，超出本切片。
- `BrowserView` 已有 `browser_->GetTabStripModel()`、`browser_widget_`、
  `GetActiveWebContents()`，是 chrome 产品层最小落点。

---

## 4. 基线证据（现有构建，未含 patch 034）

### 4.1 本轮新跑（1 窗口，diagnostic）

命令：

```sh
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default --content text --windows 1 \
  --duration 85 --mode diagnostic --expect crash --freshness none \
  --evidence-dir D:/c/logs/a1-baseline-text-1
```

结果（`D:/c/logs/a1-baseline-text-1/result.json`、`chrome.log`）：

- `verdict=REPRODUCED`，`expectation_met=true`，`setup_ok=true`；
- FATAL（日志时间）**65.206 s**；`Inspector.targetCrashed` **75.737 s**；
- 精确行：

  ```text
  [33372:43396:1101/155541.279:FATAL:third_party\blink\renderer\platform\widget\compositing\layer_tree_view.cc:554]
  DCHECK failed: callbacks.size() <= cc::PresentationTimeCallbackBuffer::kMaxBufferSize (61 vs. 60).
  ```

- 原生：`IsWindowVisible=false`、`IsIconic=false`；模型：`visibilityState=visible`；
- 诊断日志中的 host 侧决策（与 route 0 一致）：

  ```text
  BROWSER_CLI_PRESENTATION stage=host transition=content-window-shown reason=init-implicit-show
  BROWSER_CLI_PRESENTATION stage=policy seq=1 decision=deny action=show-and-activate origin=unknown
      state=background owned=no ... reason=background-suppresses-native-presentation
  BROWSER_CLI_PRESENTATION stage=policy seq=2 decision=deny action=show-and-activate origin=unknown ...
  BROWSER_CLI_PRESENTATION stage=host decision=deny action=0 ... reason=policy-denied
  BROWSER_CLI_PRESENTATION stage=host transition=window-pos-changed flags=7999 visible=no
  BROWSER_CLI_PRESENTATION stage=host transition=window-pos-changed flags=63 visible=no
  ```

  注意：没有 `showwindow-stripped`，说明这次 Show 在
  `DesktopWindowTreeHostWin::Show` 就被拒绝（若已进入 `ShowWindow`，host 兜底会先打
  `showwindow-stripped` 再改 flags）。两条 deny 分别来自一次 Widget Show 和随后的
  Activate（推断：`action=0` 是 `kShowAndActivate`，两者共用该 action）。因此
  `BrowserView::Show()` 确实执行到了 `browser_widget_->Show()` 之后的落点，本提案成立。

### 4.2 既有 8 窗口基线（route 0，同一构建）

`D:/c/logs/hs-repro-8w-release`：`managed-default`，8 窗口，
`text,raf,canvas,webgl,extension,iframe,static,no-paint`，90.166 s，release-like：

| 窗口 | 内容 | 原生 | 模型 | 结果 |
| --- | --- | --- | --- | --- |
| w0 | text | hidden | visible | target crashed @85.94 s |
| w1 | raf | hidden | visible | crashed @81.12 s |
| w2 | canvas | hidden | visible | crashed @81.12 s |
| w3 | webgl | hidden | visible | crashed @81.12 s |
| w4 | extension | hidden | visible | crashed @81.12 s |
| w5 | iframe (OOPIF) | hidden | visible | crashed @81.12 s |
| w6 | static | hidden | visible | **存活** |
| w7 | no-paint | hidden | visible | **存活** |

浏览器进程存活；6 个动态窗口全部命中，静态/无绘制对照不受影响。

### 4.3 其它可用基线（route 0 产物，只读引用）

| 证据 | 结果 |
| --- | --- |
| `D:/c/logs/hs-repro-real-popup` | 真实 BrowserPilot popup，FATAL **59.842 s** |
| `D:/c/logs/hs-trace-repro` | trace：内容 surface 66 次 commit/activate，`FramePresented` 仅 2 次；FATAL 65.626 s |
| `D:/c/logs/hs-ctl-managed-hidden` / `managed-minimized` | 从未 show 的 managed 窗口再 `SW_HIDE`/`SW_MINIMIZE` **仍模型可见、仍崩**（7 条 FATAL） |
| `D:/c/logs/hs-ctl-normal-hidden` / `hs-long-stable` | 真模型隐藏（先 show 再 hide）90 s / 180 s 稳定；180 s 后截图仍 fresh |
| `D:/c/logs/hs-repro-screenshot-driven` | 每 10 s 截图 70 s 不崩——**workaround，掩盖**；不作为验收 |

### 4.4 本轮新增的断言/harness 自检（现有构建，预期失败）

- `--assert-model hidden`（20 s，managed-default，text）：
  `model_assertion ok=False, actual=visible`，`exit=1`。证明 post-fix 断言
  能区分修复前后（见 `D:/c/logs/a1-model-assert-prefix`）。
- HUMAN 接管机制自检（45 s，接管 @15 s）：
  `human_events=[{"phase":"takeover","ok":true,"visible_windows":["w0"]}]`，
  `model_assertion auto ok=true`，`exit=0`
  （`D:/c/logs/a1-human-takeover-smoke`）。
- 隐藏/再显示自检（60 s，接管 @15 / SW_HIDE @30 / 再接管 @45）：三个事件
  `ok=true`，`exit=0`（`D:/c/logs/a1-human-cycle-smoke`）。

---

## 5. 修复后预期断言

### 5.1 硬断言（任何 post-fix 运行都必须满足）

1. **无崩溃**：21 分钟内没有 `layer_tree_view.cc:554` /
   `PresentationTimeCallbackBuffer::kMaxBufferSize`；没有
   `Target.targetCrashed` / `Inspector.targetCrashed`；probe 不超时。
2. **模型与原生一致**：`--assert-model hidden`（或 `auto`）通过；
   未呈现的 managed 窗口 `document.visibilityState=hidden`。
3. **rAF 停止**：`--assert-model hidden` 的 `raf_stalled=true`（首末样本
   `rafCount` 相同）。
4. **timer 约 1 Hz**：隐藏窗口的 `tickCount` 在 21 分钟内仍有增长
   （节流不是冻结；若为 0，说明被 freeze/discard，见风险 §7.5）。
5. **截图 fresh**：仅 before/after 各一次；PNG marker 与 DOM `paintCount`
   匹配（±4 帧），非白/非单色/`distinct_colours>=8`、`dark_ratio<0.98`。
6. **诊断日志**（diagnostic 运行）：至少一条
   `transition=managed-contents-hidden`；有 USER_EXPLICIT 接管时至少一条
   `transition=managed-contents-restored`。
7. **浏览器进程存活**：`root_pid` 不变；renderer 数量不因 crash 减少；
   21 分钟内存无单调增长趋势（与 `hs-long-stable` 同口径）。
8. **导航/JS 动作后仍 hidden**：`harness-navigate.ok=true`、
   `harness-dom-poke.ok=true`，且动作后的 probe 仍为 hidden。
9. **HUMAN 接管**：原生可见 + 模型 visible + rAF 增长 + 截图 fresh；
   `hide` 后原生/模型均 hidden、rAF 停止；`retakeover` 再次可见。

### 5.2 关键日志对照

修复前（§4.1）：只有 `content-window-shown` + policy deny，没有 contents 同步。
修复后预期：

```text
BROWSER_CLI_PRESENTATION stage=host transition=managed-contents-hidden
    reason=native-presentation-denied native=win-hwnd
...
BROWSER_CLI_PRESENTATION stage=host transition=managed-contents-restored
    reason=native-presentation-applied native=win-hwnd
```

同时 `--assert-transitions` 会把它们计入 verdict。

### 5.3 与 route 0 workaround 的区分

修复后的 21 分钟运行**不允许**出现以下“通过”：
- 任何 `--screenshot-every` 周期截图（本计划固定为 0）；
- 页面 reload / 定时器重建 / 周期重启 renderer；
- 把窗口临时 show/minimize 以“刷新 presentation 链”；
- 只看进程存活而不看 `visibilityState`/rAF/截图 fresh。

---

## 6. 测试计划与 harness 场景（8 窗口 ≥20 分钟，无周期截图、无 reload）

harness：`tests/tools/hidden-stability-cdp-check.py`（本 worktree 已加入 route 1 选项；
fixtures：`tests/fixtures/hidden-stability/`；扩展页：
`tests/tools/extensions/browser-cli-hidden-stability/`）。

启动约束（已内置）：独立 `tempfile.mkdtemp` profile；无 `--no-sandbox`；无真实用户
profile/钥匙串；进程只由 `win_job` Job Object 回收；不按镜像名 kill；不做 show 补救。

### S1 — 8 窗口 21 分钟纯 soak（主验收）

```sh
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default \
  --window-content text,raf,canvas,webgl,extension,iframe,static,no-paint \
  --windows 8 --duration 1260 --sample-interval 15 \
  --mode release-like --expect stable --freshness both \
  --assert-model hidden \
  --evidence-dir D:/c/logs/a1-route1-s1-8w-21m
```

覆盖：text（popup 形态）、rAF、Canvas、WebGL、扩展页、OOPIF、static/no-paint 对照；
前后各一次截图（不是周期）。通过条件：§5.1 全部 + `VERDICT STABLE`。

### S2 — 8 窗口 21 分钟 + 导航 / JS-DOM 活动

```sh
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default \
  --window-content text,raf,canvas,webgl,extension,iframe,static,no-paint \
  --windows 8 --duration 1260 --sample-interval 15 \
  --navigate-every 300 --dom-every 60 \
  --mode release-like --expect stable --freshness both \
  --assert-model auto \
  --evidence-dir D:/c/logs/a1-route1-s2-8w-nav-dom
```

- `--navigate-every 300`：每 5 分钟对每个窗口做**新的顶层导航**
  （`Page.navigate` 到同一 fixture 的新 query），不是 reload；
- `--dom-every 60`：每 60 s 执行一次 `__hsBump(1)`（可见 DOM 变更，不做导航）；
- 断言：每次 `harness-navigate` / `harness-dom-poke` 都 `ok=true`，动作后仍 hidden；
  21 分钟内无 crash。

### S3 — Canvas / WebGL / OOPIF 重点矩阵（21 分钟）

```sh
python tests/tools/hidden-stability-cdp-check.py ... \
  --scenario managed-default \
  --window-content canvas,webgl,iframe,canvas,webgl,iframe,text,static \
  --windows 8 --duration 1260 --sample-interval 15 --site-per-process \
  --mode release-like --expect stable --freshness both --assert-model hidden \
  --evidence-dir D:/c/logs/a1-route1-s3-gfx-oopif
```

OOPIF：确认 `result.oopif_targets` 非空，父子 renderer 都不崩；父 WebContents 的
`WasHidden()` 通过 frame-tree visibility 传播到子帧。

### S4 — HUMAN 接管 / 隐藏 / 再显示（22 分钟，2 窗口，diagnostic）

```sh
python tests/tools/hidden-stability-cdp-check.py \
  --chrome D:/c/src/out/WinDev/chrome.exe \
  --scenario managed-default --content text,raf --windows 2 \
  --duration 1320 --sample-interval 15 \
  --human-takeover-at 600 --human-hide-at 750 --human-retakeover-at 900 \
  --mode diagnostic --expect stable --freshness after \
  --assert-model auto --assert-transitions \
  --evidence-dir D:/c/logs/a1-route1-s4-human-cycle
```

- 600 s：第二个 `chrome.exe --user-data-dir=<同一隔离 profile>
  --browser-cli-user-explicit-show` 经 ProcessSingleton 转发，运行中的浏览器以
  USER_EXPLICIT 呈现一个已有窗口（HUMAN 所有权）；
- 750 s：对该窗口 `SW_HIDE`（用户再次隐藏）；
- 900 s：再次 USER_EXPLICIT 呈现（re-show after hidden）；
- 断言：`human_events` 三个 `ok=true`；接管后模型 visible + rAF 增长 + 截图 fresh；
  隐藏后模型 hidden + rAF 停止；日志出现 `managed-contents-restored`（≥1）。

### S5 — 真模型隐藏回归对照（21 分钟，2 窗口）

```sh
python tests/tools/hidden-stability-cdp-check.py ... \
  --scenario normal-hidden --content text,raf --windows 2 \
  --duration 1260 --sample-interval 15 --mode release-like \
  --expect stable --freshness both --assert-model hidden \
  --evidence-dir D:/c/logs/a1-route1-s5-normal-hidden
```

确认 patch 不影响 stock（非 managed）隐藏窗口；21 分钟后截图仍 fresh。

### S6 — 可见/遮挡对照（10 分钟）

`normal-visible` / `normal-occluded` 各 10 分钟，`--assert-model auto`：
遮挡场景允许 `native visible + model hidden`，不得崩溃。

### S7 — C++ 回归测试（随 patch 入库时补）

建议加到 `chrome/browser/ui/views/frame/browser_view_browsertest.cc`：

1. managed 模式创建 Browser，`window()->Show()`；
   断言 `!browser_view->GetWidget()->IsVisible()` 且
   `web_contents->GetVisibility()==HIDDEN`；
2. 通过 `browser_cli::PresentBrowserForUserExplicitAction(browser)` 模拟用户显式
   show；断言原生可见、`GetVisibility()==VISIBLE`、页面 rAF 恢复；
3. `browser_view->GetWidget()->Hide()` 后再 show，断言可再次恢复（re-show）。

单元级（可选）：把 `BrowserCliShouldKeepContentsHidden` 抽为可注入 predicate 后再测
4 种组合（managed/unmanaged × visible/hidden × owned/leased）。

### 静态门禁（已跑）

```sh
tests/check-browser-launch-hygiene.sh
# OK (17 entry points; hidden-stability-cdp-check.py 在列)
tests/run-script-tests.sh
# 本机 Windows：162 passed / 60 failed，失败全部是 python3 alias 缺失（rc=49）
# 与依赖 python3 的 fixture 自检；与本次改动无关（本 harness 未被该脚本登记）。
```

---

## 7. 风险与缓解

### 7.1 递归 Hide/Show

- patch **不调用** `Widget::Show()/Hide()`，只调用 `WebContents::WasHidden()/WasShown()`，
  不会再次进入 presentation policy，也不会有 Show↔deny 递归。
- `WasHidden()` 只在 `GetVisibility()!=HIDDEN` 时调用，避免重复通知。
- `WasShown()` 只在“曾由本机制隐藏（或 managed 窗口从 hidden 变可见）+ 当前不是
  VISIBLE”时调用；`RenderWidgetHostViewAura::OnShowWithPageVisibility()` 对
  “host 已可见”的重复调用是 no-op，`ShowImpl` 的 CHECK
  （`host()->IsHidden() || visibility_==VISIBLE`）成立。
- `OnWidgetVisibilityChanged` 不调用 Widget show/hide，不会触发 WM_WINDOWPOS 递归。

### 7.2 用户显式 show 的恢复

- 恢复挂在 `OnWidgetVisibilityChanged(visible=true)`，即 **HWND 真的可见**时，
  而不是“又调了一次 Show()”。因此覆盖：
  `BrowserView::Show()`、`widget->Show()`（assist gateway）、任务栏/系统菜单原生
  恢复、patch 030/033 的 STARTUPINFO 重试。
- USER_EXPLICIT Show 在 policy 里是 allow；`BrowserCliShouldKeepContentsHidden`
  同一决策返回 false，不会“先 show 再隐藏内容”。
- USER_EXPLICIT 失败的极端情况（两次 Show 都被系统吞掉）：窗口不可见 → 保持
  hidden 是正确语义；用户下次操作再恢复。

### 7.3 后台截图

- `Page.captureScreenshot(fromSurface=true)` 走
  `WebContents::IncrementCapturerCount(stay_hidden=true, stay_awake=true)`，
  **不会**把页面变 visible；随后 `RequestForceRedraw` 产帧，patch 027 保证被
  遮挡/未呈现的根 surface 也能完成 copy。
- route 0 已证明真模型隐藏（`normal-hidden`）180 s 后截图仍 fresh；本计划把
  同一断言延长到 21 分钟（S1/S5）。
- 不在 soak 中周期截图；只有 before/after 两次，避免“截图驱动”掩盖问题。

### 7.4 managed presentation 语义

- 窗口仍然从不原生呈现；HUMAN 所有权、assist lease、自动化拒绝矩阵不变。
- 变化仅是**后台页面的 renderer 语义**：`visibilityState=hidden`、rAF 停、
  timer ~1 Hz。这与“后台”应有一致，也与 stock Chrome 隐藏窗口一致。
- 若产品确实需要后台 60 Hz 动画，应走 route 2（viz failed-feedback + renderer
  消费 failed feedback），而不是保留“模型可见 + 永不 present”的不一致状态。
- `BrowserView::OnWindowDidShow()` 仍会在被拒绝的 Show 上执行（`window_has_shown_`
  与启动指标），这是 patch 未改变的既有行为；不用于可见性判断。

### 7.5 隐藏页面的生命周期（freeze/discard）

- `WasHidden()` 后页面与普通隐藏 tab 同优先级；Chrome 可能在内存压力或省电策略下
  freeze/discard。21 分钟测试用 `tickCount`/`probe` 增长检测冻结；若出现，
  需在产品层单独处理（例如对长驻 managed 窗口使用
  `WebContents::SetPageFrozen` 的相反策略/捕获计数），不要用周期截图掩盖。
- 当前基线（`hs-long-stable` 180 s）未见冻结；21 分钟是本计划新增的观察点。

### 7.6 未覆盖范围（已知）

1. **浏览器 UI WebUI（toolbar）**：本 patch 只隐藏 tab 的 WebContents，不隐藏
   `WebUIToolbarWebView` 的 WebContents。基线 trace 中 root UI surface 80 s 内
   只有 2 次 commit（约 4 s 与 68 s），且 `FramePresented` 的 2 次来自 browser UI，
   风险低；但如果 21 分钟运行发现 UI renderer 的 callback 增长，应把同一机制
   扩展到 toolbar WebUI。
2. **从未调用 `BrowserView::Show()` 的窗口**：若某路径只创建窗口、从不 Show，
   然后被外部 `ShowWindow(SW_SHOW)` 命中，host 会剥 flag（原生仍隐藏），但本
   patch 不会收到 Show，模型可能仍可见。当前所有产品路径（扩展 API、DevTools
   newWindow、session restore、CLI show）都会经过 `BrowserView::Show()`；
   如出现新入口需补 hook。
3. **多 tab 切换**：`OnActiveTabChanged` 已覆盖；split view 同一路径。
4. **OOPIF 子进程**：父 WebContents `WasHidden` 通过 frame-tree visibility 传播；
   `normal-hidden` 90 s 对照已包含 iframe 且稳定，S3 再验证 21 分钟。
5. **本 patch 未构建**：编译/链接与真实 21 分钟验收仍是剩余工作（§8）。

---

## 8. 剩余工作（按顺序）

1. 在**独立** Chromium checkout（不得改 `D:/c/src`）应用 034 +
   `gn gen out/WinDev && ninja -C out/WinDev chrome`；记录编译错误/警告。
2. 补 §6 S7 的 browser test；跑
   `browser_cli_presentation_policy_unittests` /
   `browser_cli_presentation_win_unittests` 回归（policy 未改，应全过）。
3. 跑 S1（8 窗口 21 分钟）与 S2（导航/JS-DOM）；记录 `result.json`、
   `summary.txt`、`chrome.log` 中的 `managed-contents-*`。
4. 跑 S4（HUMAN 接管/隐藏/再显示）与 S5（normal-hidden 21 分钟回归）。
5. 若 S1–S5 全部通过：把 034 转正（编号、sha256、manifest、patches/README），
   并在 `docs/windows-hidden-presentation-stability-2026-11-01.md` 更新状态为
   SOLVED（route 1）。
6. 若 21 分钟发现 toolbar WebUI 或生命周期问题：按 §7.5/§7.6 单独切片，
   不回退到周期截图/重启。

---

## 9. 文件清单（本 worktree）

| 文件 | 说明 |
| --- | --- |
| `chromium/patches/proposed/037-managed-window-contents-visibility.patch` | 提案补丁（+124/−0，2 文件；未应用） |
| `chromium/patches/proposed/README.md` | 提案状态与应用/转正流程 |
| `docs/windows-hidden-route1-model-visibility-2026-11-01.md` | 本文档 |
| `tests/tools/hidden-stability-cdp-check.py` | route 0 harness + route 1 选项（`--assert-model`、`--assert-transitions`、`--navigate-every`、`--dom-every`、`--human-takeover-at/--human-hide-at/--human-retakeover-at`） |
| `tests/fixtures/hidden-stability/{dynamic.html,child.html}` | 内容/OOPIF fixture |
| `tests/tools/extensions/browser-cli-hidden-stability/*` | MV3 扩展页 fixture |
| `tests/README.md` | 新增 harness 说明与命令 |
