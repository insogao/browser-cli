# Patch 019 任务书：Windows 用户菜单/标题栏 `kUserExplicit` scope

状态：待执行。基线：patch 001-018 已应用；增量基线 `D:\c\logs\idx018`。
目标：让 Windows 上由**用户**点击的菜单/标题栏动作被正确归因为 `kUserExplicit`，
避免 patch 016 的 host 门禁把用户自己的操作当成未归属自动化而拒绝；
同时**不得**把任何可被外部进程伪造的窗口消息当作 `kUserExplicit`。

## 不可违反

1. 不改共享策略、不改 macOS 实现、不改 001-018 的 patch 文件。
2. 不 commit/push/reset/checkout；不启动可见浏览器；不把窗口移到屏幕外。
3. 不伪造 PASS；不为了凑绿改测试或禁用检查。
4. **禁止**把 `WM_ACTIVATE`、`WM_NCACTIVATE`、`WM_SYSCOMMAND`、`GetForegroundWindow`
   当作 `kUserExplicit` 来源。原生系统菜单（`UsesNativeSystemMenu()` 为 true）由 OS
   直接执行 `SC_MINIMIZE`/`SC_MAXIMIZE`/`SC_RESTORE`，任何进程都能
   `SendMessage(WM_SYSCOMMAND)`，因此**不要**在 `PreHandleMSG`/`OnSysCommand` 加 scope。
5. 只做来源标注，不改变菜单/按钮的其它行为。

## 实现

### 1. `chrome/browser/ui/views/toolbar/app_menu.cc`

在 `AppMenu::ExecuteCommand(int command_id, int mouse_event_flags)`（`:1334`）
函数体最前面加：

```cpp
views::ScopedForegroundOrigin origin_scope(
    views::ForegroundOrigin::kUserExplicit);
```

include `ui/views/widget/browser_cli_foreground_origin.h`（放在现有 `ui/views` include 附近）。
这一处覆盖主应用菜单及其子菜单（包括 History 子菜单、New Window、New Incognito、
Preferences/About 等由 `AppMenu` 委托执行的命令）。不要改 `AppMenuModel`。

### 2. `chrome/browser/ui/views/frame/system_menu_model_delegate.cc`

在 `SystemMenuModelDelegate::ExecuteCommand(int command_id, int event_flags)`
（`:252`）函数体最前面加同样的 `ScopedForegroundOrigin(kUserExplicit)`。
这是 Views 系统菜单（`features::IsMenuSimplificationEnabled()` 为 true 时）的命令入口。
include 同上。

### 3. `chrome/browser/ui/views/frame/browser_caption_button_container_win.cc`

minimize/maximize/restore 三个按钮当前直接绑定 `BrowserWidget::Minimize/Maximize/Restore`
（`:47-71`）。把这三个回调改为先建立 `ScopedForegroundOrigin(kUserExplicit)` 再调用：

```cpp
base::BindRepeating(
    [](BrowserWidget* widget) {
      views::ScopedForegroundOrigin origin_scope(
          views::ForegroundOrigin::kUserExplicit);
      widget->Minimize();
    },
    base::Unretained(frame_view_->browser_widget()))
```

maximize/restore 同理。close 按钮不加 scope（关闭不需要展示/授权，销毁路径已有释放）。
include 同上。

### 4. 不要动的地方

- `BrowserDesktopWindowTreeHostWin::PreHandleMSG` / `ShowCustomSystemMenu` /
  `ShowViewsSystemMenuAtDefaultLocation` 的原生消息路径。
- `chrome/browser/ui/views/frame/browser_non_client_frame_view_win.cc` 的 OS 标题栏
  （拖动/双击最大化由 OS 直接执行，不经过 views 门禁）。
- `BrowserCommandController` / accelerators（键盘快捷键归因留到后续切片）。

## 验证

1. `gn gen out/WinDev` exit 0。
2. `ninja -C out/WinDev -j 6 chrome` exit 0。
3. 既有测试不回归：policy 32/32、lease 8/8、gateway 8/8、controller 14/14。
4. 静态确认三处 scope 存在且不在 `PreHandleMSG`/`OnSysCommand` 内：
   `grep -n "ScopedForegroundOrigin" <files>`，并报告实际行号。
5. 符号/字符串：`chrome.dll` 应仍导入 `ScopedForegroundOrigin` 构造/析构；
   报告实际 `dumpbin /imports` 结果。
6. 无窗口 headless 运行一次基础 smoke（`--headless=new --no-sandbox`、独立 profile、
   `--dump-dom about:blank`）确认不崩溃；菜单本身无法在 headless 中点击，不要伪造
   菜单运行证据。真实菜单/标题栏交互必须留给可见桌面验收。
7. 生成 `D:\c\logs\m1-patch019.diff`：
   `GIT_NO_LAZY_FETCH=1 GIT_INDEX_FILE=D:/c/logs/idx018 git diff`；确认 idx018 未变。
   不要写入仓库 patch 目录。

## 明确不做

- 会话恢复 scope、`Init` 隐式 show 归因（patch 020）。
- USER_EXPLICIT 的 Windows 原生展示。
- 键盘快捷键（Ctrl+Shift+T 等）归因。
- 原生系统菜单的 WM_SYSCOMMAND 归因（不可信，永久不做）。
