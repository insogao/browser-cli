# Patch 020 任务书：会话恢复 `kSessionRestore` scope + `Init` 隐式 show 观察

状态：待执行。基线：patch 001-019 已应用；增量基线 `D:\c\logs\idx019`。
目标：给 Windows（以及共用代码路径）的会话恢复 show 请求一个明确、fail-closed 的
`kSessionRestore` 来源；并把 `DesktopWindowTreeHostWin::Init` 的隐式内容窗口 show
变成可观察事件。**不改变任何展示行为**。

## 不可违反

1. 不改共享策略、不改 macOS 实现、不改 001-019 的 patch 文件。
2. 不 commit/push/reset/checkout；不启动可见浏览器；不把窗口移到屏幕外。
3. 不伪造 PASS；不为了凑绿改测试或禁用检查。
4. `kSessionRestore` 在共享策略里属于 automation：未归属窗口的 `kShowAndActivate`
   会被拒绝。这是预期行为，不要为了让窗口显示而改成 `kInternal`/`kUserExplicit`。
5. `Init` 的隐式 show 只加观察日志，**不要**阻止它、不要改 aura 可见性。

## 实现

### 1. `chrome/browser/sessions/session_restore.cc`

在以下 4 个 `browser->GetWindow()->Show()` / `new_browser->GetWindow()->Show()`
调用处，紧邻调用建立 scope（只覆盖 Show，不覆盖整个函数）：

- `:500` 附近（`use_new_window` 分支）
- `:562` 附近（startup tabs 分支）
- `:1363` 附近（`browser_ == browser` 之后）
- `:1418` 附近（`kLastAndUrlsStartupPref` 新建窗口）

形式：

```cpp
{
  views::ScopedForegroundOrigin origin_scope(
      views::ForegroundOrigin::kSessionRestore);
  browser->GetWindow()->Show();
}
```

如果同一作用域内还有其它需要归因的 show/activate，可以合并到一个 scope，但不要
把无关的普通启动/用户操作包进去。include
`ui/views/widget/browser_cli_foreground_origin.h`。

### 2. GN

`chrome/browser/sessions/BUILD.gn` 的 `source_set("impl")` 目前没有直接
`//ui/views` 依赖。在 `if (enable_session_service) { ... }` 的 deps 里加
`"//ui/views"`。先确认 `enable_session_service` 只在桌面平台为 true；如果它在
Android 也为 true，则改成 `if (enable_session_service && toolkit_views)`（需要 import
`//build/config/ui.gni` 或使用已有变量）。

### 3. `ui/views/widget/desktop_aura/desktop_window_tree_host_win.cc`

在 `Init()` 末尾的 `window()->Show();` 之后加观察日志（仅在 managed 模式）：

```cpp
if (BrowserCliPresentationPolicy::Get()->IsManagedModeEnabled()) {
  LOG(INFO) << "BROWSER_CLI_PRESENTATION stage=host"
            << " transition=content-window-shown"
            << " window_id=" << BrowserCliWindowIdForHwnd(GetHWND())
            << " reason=init-implicit-show native=win-hwnd";
}
```

需要 include `ui/views/widget/browser_cli_presentation_policy.h` 和
`ui/views/widget/browser_cli_presentation_win.h`（若尚未包含）。确认 `GetHWND()` 在
此处已有效（`message_handler_->Init` 已创建 HWND）。

### 4. 不要动的地方

- `ScopedFullscreenVisibility` 的 `ShowWindow(SW_SHOW)`（后续单独处理）。
- `DesktopWindowTreeHostWin::Init` 的 `window()->Show()` 本身。
- 键盘快捷键（Ctrl+Shift+T 等）归因（patch 021）。
- USER_EXPLICIT 的 Windows 原生展示。

## 验证

1. `gn gen out/WinDev` exit 0。
2. `ninja -C out/WinDev -j 6 chrome` exit 0。
3. 既有测试不回归：policy 32/32、lease 8/8、gateway 8/8、controller 14/14。
4. 静态确认 4 处 scope 与 Init 观察日志存在，报告行号。
5. headless smoke（`--headless=new --no-sandbox`、独立 profile、`--dump-dom about:blank`）
   exit 0；如果 managed 模式下能看到 `transition=content-window-shown` 日志，附上原文；
   如果没有，报告实际日志，不要伪造。
6. 尽力做一次会话恢复验证（可选，失败则如实报告）：
   - 第一次 headless 启动创建一个简单页面/窗口，正常退出；
   - 第二次带 `--restore-last-session --browser-cli-background-presentation` 启动，
     检查是否出现 `origin=session-restore` 的 policy 日志；
   - 若 headless 不产生可恢复会话，停止尝试并报告。
7. 生成 `D:\c\logs\m1-patch020.diff`：
   `GIT_NO_LAZY_FETCH=1 GIT_INDEX_FILE=D:/c/logs/idx019 git diff`；确认 idx019 未变。
   不要写入仓库 patch 目录。

## 明确不做

- 键盘快捷键归因（patch 021）。
- USER_EXPLICIT 的 Windows 原生展示。
- taskbar/开始菜单可信来源。
- `ScopedFullscreenVisibility` 旁路。
