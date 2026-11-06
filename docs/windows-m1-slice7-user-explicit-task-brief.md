# Patch 022 任务书：USER_EXPLICIT 的 Windows 原生展示（复用已有窗口）

状态：待执行。基线：patch 001-021 已应用；增量基线 `D:\c\logs\idx021`。
风险等级：高。**可见窗口行为必须标 REVIEW，不得用 headless/单测冒充验收。**

## 目标

把 `chrome/browser/browser_cli/user_explicit_presentation_win.cc` 的
`HandleUserExplicitShowRequest` 从“完全 fail closed”推进到：

- managed 模式下，CLI 显式 `show` 能找到一个已有的顶层 BrowserWindow，
  以 `kUserExplicit` 来源展示并激活它，只让该窗口获得 HUMAN 所有权；
- 成功展示后取消该窗口的 assist 计时器（`OnLeaseReleasedByUserTakeover`）；
- **没有可复用窗口时仍然 fail closed**（窗口创建路径留到 patch 023）。

## 不可违反

1. 不改共享策略、不改 macOS 实现、不改 001-021 的 patch 文件。
2. 不新写状态机；所有决策走共享 policy + patch 016 的 Windows gateway。
3. 不 commit/push/reset/checkout；不启动可见浏览器；不把窗口移到屏幕外。
4. 不伪造 PASS。可见窗口的展示/激活/HUMAN 所有权必须标 REVIEW，除非有真实桌面证据。
5. `kUnknown` 不能当作用户来源；展示必须显式 scope `kUserExplicit`。

## 实现

### 1. `chrome/browser/browser_cli/user_explicit_presentation_win.cc`

新增匿名命名空间 helper：

```cpp
struct ReusableWindow {
  views::Widget* widget = nullptr;
  uint64_t window_id = 0;
  bool user_owned = false;
};

// Iterates browser windows in activation order (most recently active first).
// Prefers a window the user already owns. Only top-level browser windows are
// eligible.
ReusableWindow FindReusableWindow();
```

实现要点：
- `GlobalBrowserCollection::GetInstance()->ForEach(callback, Order::kActivation)`；
- 对每个 `BrowserWindowInterface* browser`：
  `browser->GetWindow()->GetNativeWindow()` →
  `views::Widget::GetWidgetForNativeWindow(...)`；
- 只接受 `widget->is_top_level()`；
- `window_id = views::BrowserCliWindowIdForHwnd(
      widget->GetNativeWindow()->GetHost()->GetAcceleratedWidget())`；
- 第一个可用窗口作为候选；遇到 `policy->IsUserExplicitOwned(window_id)` 为真的窗口
  立即选中并停止遍历；
- 找不到返回空结构。

在 `HandleUserExplicitShowRequest` 中：
- 先算 `managed_mode`；
- 只有在 `managed_mode` 且 `request_class == kShow` 时才查找可复用窗口
  （其它分类不需要触碰窗口）；
- 调用
  `PlanUserExplicitShow(request_class, managed_mode, reusable.widget != nullptr)`；
- `kNoAction` 保持现有日志与返回值；
- `kPresentExistingWindow`：
  ```cpp
  {
    views::ScopedForegroundOrigin origin_scope(
        views::ForegroundOrigin::kUserExplicit);
    reusable.widget->Show();
    reusable.widget->Activate();
  }
  if (reusable.widget->IsVisible()) {
    GetForegroundAssistController()->OnLeaseReleasedByUserTakeover(
        reusable.window_id);
    LogProducerOutcome("handled", "existing-window-presented", managed_mode,
                       reusable.window_id);
  } else {
    LogProducerOutcome("failed", "presentation-denied", managed_mode,
                       reusable.window_id);
  }
  return true;
  ```
- `kCreateWindowThenPresent`：保持 fail closed，日志
  `result=failed reason=window-creation-not-implemented`，返回 true。
- `LogProducerOutcome` 增加 `uint64_t window_id` 参数（或重载），日志追加
  `window_id=`。

include 需要：
- `chrome/browser/browser_cli/foreground_assist_presentation.h`（`GetForegroundAssistController`）
- `chrome/browser/ui/browser_window/public/browser_window_interface.h`
- `chrome/browser/ui/browser_window/public/global_browser_collection.h`
- `ui/aura/window.h`、`ui/aura/window_tree_host.h`
- `ui/views/widget/browser_cli_presentation_win.h`
- `ui/views/widget/widget.h`

### 2. `chrome/browser/browser_cli/BUILD.gn`

`source_set("user_explicit_presentation_win")` 的 deps 增加：
- `":foreground_assist_presentation_win"`
- `"//chrome/browser/ui/browser_window"`
- `"//ui/aura"`

确认没有循环依赖（`foreground_assist_presentation_win` 不依赖
`user_explicit_presentation_win`）。

### 3. 明确不做

- 窗口创建（`CreateBrowserWindow` + 异步 continuation）：patch 023。
- 会话恢复、taskbar、开始菜单。
- 可见窗口的自动化验收；本机只能做 headless 编译/日志验证。

## 验证

1. `gn gen out/WinDev` exit 0。
2. `ninja -C out/WinDev -j 6 chrome` exit 0。
3. policy 32/32、lease 8/8、gateway 8/8、controller 14/14，全部 exit 0。
4. 静态确认 `ScopedForegroundOrigin(kUserExplicit)` 只包住 Show/Activate，
   且 `kCreateWindowThenPresent` 仍 fail closed。
5. headless 运行（`--headless=new --no-sandbox`、独立 profile）：
   `--browser-cli-background-presentation --browser-cli-user-explicit-show`，
   不带 URL。检查是否出现
   `BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=handled
   reason=existing-window-presented` 或明确的 deny/failed 日志。
   - 如果进程不自动退出，用超时终止并确认无残留；
   - **不得**声称该日志证明可见桌面展示成功；它只证明 producer 路径执行。
6. 生成 `D:\c\logs\m1-patch022.diff`：
   `GIT_NO_LAZY_FETCH=1 GIT_INDEX_FILE=D:/c/logs/idx021 git diff`；确认 idx021 未变。
   不要写入仓库 patch 目录。

## 验收状态

- 编译、单测、headless producer 日志：可作为实现证据。
- 真实可见窗口的展示、激活、HUMAN 所有权、assist 计时器取消：**REVIEW**，
  需要签名后的可见桌面人工验收（或后续自动化桌面测试）。不得标 DONE。
