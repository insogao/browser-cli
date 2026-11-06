# Patch 021 任务书：键盘快捷键 `kUserExplicit` 归因

状态：待执行。基线：patch 001-020 已应用；增量基线 `D:\c\logs\idx020`。

## 目标

让 Windows 上由键盘快捷键触发的浏览器命令（Ctrl+N、Ctrl+Shift+T、Ctrl+Shift+N 等）
被正确归因为 `kUserExplicit`，避免 patch 016 的 host 门禁把用户自己的快捷键当成未归属
自动化而拒绝。

## 实现（唯一改动）

`chrome/browser/ui/views/frame/browser_view.cc` 的
`bool BrowserView::AcceleratorPressed(const ui::Accelerator& accelerator)`
（约 `:5254`）函数体最前面加：

```cpp
// Keyboard accelerators are user input. Attribute the command they execute so
// the presentation policy does not treat a user shortcut as unattributed
// automation; the policy still decides every action.
views::ScopedForegroundOrigin origin_scope(
    views::ForegroundOrigin::kUserExplicit);
```

`browser_view.cc` 已经在 `:365` include
`ui/views/widget/browser_cli_foreground_origin.h`，无需新增 include。

**不要**改 `BrowserCommandController::ExecuteCommandWithDispositionAndContext` 或
`chrome::ExecuteCommand`：那些入口也会被扩展/WebUI 调用，blanket scope 会误归因。
`AcceleratorPressed` 是 `views::AcceleratorManager` 分发键盘快捷键的窄入口。

## 已确认不需要改的项（写进报告，不要改代码）

`ui/views/win/scoped_fullscreen_visibility.cc` 的 `::ShowWindow(SW_SHOW)` 不是独立旁路：
它只由 `FullscreenHandler::SetFullscreen` 创建，而 `FullscreenHandler::SetFullscreen`
只由 `HWNDMessageHandler::SetFullscreen` 调用，后者只由
`DesktopWindowTreeHostWin::SetFullscreen` 调用——该函数已在 patch 016 最前面查询共享
策略。因此任何创建 `ScopedFullscreenVisibility` 的路径都已经过策略门禁。
在报告中引用这三层调用链，不要修改该文件。

## 验证

1. `gn gen out/WinDev` exit 0。
2. `ninja -C out/WinDev -j 6 chrome` exit 0。
3. policy 32/32、lease 8/8、gateway 8/8、controller 14/14，全部 exit 0。
4. 静态确认 scope 位于 `AcceleratorPressed` 函数体第一行，且没有出现在
   `BrowserCommandController`/`chrome::ExecuteCommand`。
5. headless `--dump-dom about:blank`（`--headless=new --no-sandbox`、独立 profile）exit 0；
   键盘快捷键无法在 headless 中可靠注入，不要伪造快捷键运行证据。
6. 生成 `D:\c\logs\m1-patch021.diff`：
   `GIT_NO_LAZY_FETCH=1 GIT_INDEX_FILE=D:/c/logs/idx020 git diff`；确认 idx020 未变。
   不要写入仓库 patch 目录。

## 硬约束

- 不改共享策略、macOS 实现、001-020 的 patch 文件、patch-manifest.json。
- 不 commit/push/reset/checkout；不启动可见浏览器；不把窗口移到屏幕外。
- 不伪造 PASS；不为了凑绿改测试。
- 禁止把 `WM_SYSCOMMAND`/`WM_ACTIVATE`/`GetForegroundWindow` 当 `kUserExplicit`。
