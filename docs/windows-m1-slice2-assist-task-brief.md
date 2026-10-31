# Patch 017 任务书：Windows Foreground Assist 展示与 lease 接线

状态：待执行。基线：patch 001-016 已应用；增量基线 index `D:\c\logs\idx016`（post-016）。
设计依据：`docs/windows-native-gateway-audit-2026-10-23.md`、`docs/windows-m1-gateway-2026-10-23.md`、
macOS 参考实现 `chrome/browser/browser_cli/foreground_assist_presentation.mm`。

## 目标

把 agent foreground-assist 的 **producer + lease + timeout hide** 接到 Windows，
复用同一套 `BrowserCliPresentationPolicy` 与 `ForegroundAssistController`，
不复制任何决策逻辑。

## 不可违反

1. 不改共享策略、不改 controller、不改 macOS 实现；只加 Windows 管道。
2. 不改 001-016 的 patch 文件；新改动作为 patch 017。
3. 失败即关闭：目标窗口找不到、不是顶层 BrowserWindow、HUMAN-owned、lease 被拒/忙/
   无效，一律 deny + 结构化日志，绝不展示。
4. 不启动可见浏览器；不把窗口移到屏幕外；测试不得短暂上屏。
5. 不 commit/push/reset/checkout。

## 实现

### 1. `ui/views/widget/browser_cli_presentation_win.h/.cc` 增加原生动作

```cpp
// Presents `widget` for an already-granted assist lease. Scopes kAgentAssist and
// issues Show()+Activate(); the host gates re-check the shared policy. Returns
// whether the window ended up visible.
VIEWS_EXPORT bool BrowserCliPresentForAgentAssistAction(Widget* widget);

// Hides `widget` after its assist lease ended. Re-verifies that the window is
// not HUMAN-owned and that no newer live lease covers it. Returns whether Hide
// was issued.
VIEWS_EXPORT bool BrowserCliHideForForegroundAssistRelease(
    Widget* widget, uint64_t generation);

// True for a top-level widget that can be an assist target. The chrome layer
// additionally requires it to belong to a BrowserWindow.
VIEWS_EXPORT bool BrowserCliIsEligibleForegroundAssistTarget(Widget* widget);
```

实现要点：
- 用 `ScopedForegroundOrigin(ForegroundOrigin::kAgentAssist)` 包住 Show/Activate/Hide，
  让 host 门禁看到正确的来源。
- `Hide` 前：`policy->IsUserExplicitOwned(id)` 为真则拒绝；若
  `policy->IsForegroundAssistLeased(id)` 且 `GetForegroundAssistLeaseGeneration(id) != generation`
  则拒绝（新 lease 不得被旧 release 隐藏）。
- 不在此层做展示决策。

### 2. 新文件 `chrome/browser/browser_cli/foreground_assist_presentation_win.cc`

实现 `HandleForegroundAssistRequest` 与 `GetForegroundAssistController`
（声明在 `foreground_assist_presentation.h`，当前只有 macOS .mm 定义）。

窗口解析：
- 用 `GlobalBrowserCollection::GetInstance()->ForEach(...)` 遍历浏览器窗口，
  对每个 `browser->GetWindow()->GetNativeWindow()` 取
  `views::Widget::GetWidgetForNativeWindow(...)`，比较
  `views::BrowserCliWindowIdForHwnd(hwnd)` 与请求的 `target_window`。
- 只接受顶层 BrowserWindow；`views::BrowserCliIsEligibleForegroundAssistTarget` 为假则拒绝。

请求路径（镜像 macOS 语义，但用 Windows gateway）：
1. 分类失败按 `foreground_assist.cc` 的稳定 reason 记录并 consumed。
2. 非 managed → `noop`，consumed。
3. 目标窗口找不到 → `denied target-window-not-found`。
4. 非顶层 BrowserWindow → `denied target-not-top-level-browser-window`。
5. `policy->IsUserExplicitOwned(id)` → `denied target-human-owned`。
6. `policy->RequestForegroundAssist(ForegroundOrigin::kAgentAssist, id, 0, reason)`：
   - `kGranted` → `BrowserCliPresentForAgentAssistAction(widget)`；失败则用
     `ReleaseForegroundAssist(id, kAgentRelease, 刚授予的 generation)` 释放；成功则
     `GetForegroundAssistController()->OnLeaseGranted(id, generation)`。
   - `kDenied`/`kBusy`/`kInvalidTarget` → 对应 deny 日志。
7. release 路径：先取 generation；为 0 → `no-matching-lease`；否则
   `ReleaseForegroundAssist(id, kAgentRelease, generation)`、
   `controller->OnLeaseReleasedByAgent(id, generation)`，再
   `BrowserCliHideForForegroundAssistRelease(widget, generation)`（该函数内部会拒绝 HUMAN）。

Controller delegate（`ForegroundAssistController::Delegate`）：
- `WindowExists(id)`：能找到对应 BrowserWindow。
- `WindowIsUserOwned(id)`：`policy->IsUserExplicitOwned(id)`。
- `CurrentLeaseGeneration(id)`：`policy->GetForegroundAssistLeaseGeneration(id)`。
- `ReleaseLeaseForTimeout(id, gen)`：`policy->ReleaseForegroundAssist(id, kTimeout, gen)`。
- `ReturnWindowToBackground(id, gen)`：找到 widget 后调用
  `BrowserCliHideForForegroundAssistRelease(widget, gen)`。
- 单例用 `base::NoDestructor`，TTL 用 `views::kDefaultForegroundAssistLeaseTtl`。

### 3. `chrome/browser/chrome_browser_main.cc`

把 patch 016 在 Windows 分支里的
`LOG(... windows-gateway-not-implemented)` 替换为
`browser_cli::HandleForegroundAssistRequest(command_line)`（两处：ProcessSingleton 与冷启动）。
include 里补 `foreground_assist_presentation.h`（Windows 也需要）。

### 4. GN

`chrome/browser/browser_cli/BUILD.gn`：新增/扩展 `if (is_win)` 的 source_set，
包含 `foreground_assist_presentation.h` + `foreground_assist_presentation_win.cc`，
deps 至少：`:foreground_assist`、`:foreground_assist_controller`、`//base`、
`//chrome/browser/ui/browser_window`、`//ui/views`。
`chrome/browser/BUILD.gn` 的 Windows deps 加入该 target。

### 5. 单测

- 既有 `foreground_assist_controller_unittests`（10/10）与
  `foreground_assist_unittests`（17/17）在 macOS 已过；Windows 上
  `foreground_assist_unittests` 目前因测试源码的宽字符串比较不能编译（pre-existing），
  不要修改它来凑绿；`foreground_assist_controller_unittests` 可尝试编译运行。
- Windows gateway 单测（patch 016 的 6/6）必须保持通过。
- 新增测试（如果可行）：`BrowserCliHideForForegroundAssistRelease` 在
  HUMAN-owned / 新 generation 下拒绝 hide；否则在报告中说明未覆盖。
- 不新增依赖可见窗口的测试。

## 验收（串行，失败即停）

1. `gn gen out/WinDev` exit 0。
2. policy 32/32、lease 8/8、gateway 6/6 不回归。
3. `ninja -C out/WinDev -j 6 chrome` exit 0。
4. 符号：`ui_views.dll` 含新增 gateway 函数；`chrome.dll` 含
   `BROWSER_CLI_FOREGROUND_ASSIST` 或 `BROWSER_CLI_PRESENTATION stage=producer` 相关字符串。
5. headless（`--headless=new --no-sandbox`，仅本机诊断，独立 profile）运行：
   `--browser-cli-background-presentation --browser-cli-foreground-assist
   --browser-cli-foreground-assist-reason=other --browser-cli-foreground-assist-window=1`
   → 必须出现 `BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=denied
   reason=target-window-not-found`（或等价 deny），exit 0；不得展示窗口。
6. 生成 `D:\c\logs\m1-patch017.diff`：
   `GIT_NO_LAZY_FETCH=1 GIT_INDEX_FILE=D:/c/logs/idx016 git diff`，
   新文件先用 `git add -N` 标记，之后恢复 idx016 备份。不要写入仓库 patch 目录。

## 明确不做

- 扩展 API `kExtension` origin scope（patch 018）。
- 用户菜单/标题栏/History 入口（patch 018）。
- 会话恢复 scope、`Init` 隐式 show 归因（patch 018）。
- USER_EXPLICIT 的 Windows 原生展示（仍需后续 patch；当前 `user_explicit_presentation_win.cc`
  继续 fail closed）。
- taskbar/开始菜单可信来源。
