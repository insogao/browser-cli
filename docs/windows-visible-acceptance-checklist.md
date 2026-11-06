# Windows 可见桌面验收清单（patch 001-022）

状态：**待执行**。本清单只能在用户授权的真实可见 Windows 桌面上执行；
headless、单测和符号检查都不能替代。执行前确认没有用户自己的 Chrome 实例
占用同一个 `--user-data-dir`（本清单始终使用独立临时 profile）。

## 0. 前置条件

- 使用 `out/WinDev/chrome.exe`（或后续签名/打包产物），固定 revision
  `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940`。
- 每次启动都带：
  - `--user-data-dir=<独立临时目录>`（不得复用真实 profile）
  - `--browser-cli-background-presentation`（managed 模式）
  - `--enable-logging=stderr --v=0`，stderr 重定向到独立日志文件。
- 记录：启动时间、窗口 HWND/标题、前台窗口、日志中的 `window_id`、`origin`、
  `decision`、`generation`。截图或窗口枚举结果作为附件。
- 失败判据：任何自动化请求让 HUMAN 窗口改变可见性/焦点/z-order；任何非用户请求
  使窗口上屏；重复 `show` 多开窗口；日志缺少对应 `origin`/`decision`。

## 1. 后台冷启动（不应出现窗口）

```bat
chrome.exe --user-data-dir=%TMP%\bcli-a --browser-cli-background-presentation ^
  --enable-logging=stderr --v=0 "data:text/html,<h1>background</h1>"
```

预期：
- 桌面不出现可见窗口；
- 日志出现 `stage=policy ... decision=deny ... origin=unknown ...`
  或 `origin=session-restore`（如带会话恢复），以及
  `stage=host decision=deny ... native=win-hwnd`；
- 页面仍加载（可在后续 attach/日志中确认，不能只看 target 创建）。

## 2. CLI 显式 show（复用已有窗口）

在进程仍运行时，另开一个终端执行同一二进制（同一 `--user-data-dir` 会被
ProcessSingleton 转发）：

```bat
chrome.exe --user-data-dir=%TMP%\bcli-a --browser-cli-background-presentation ^
  --browser-cli-user-explicit-show --enable-logging=stderr --v=0
```

预期：
- 恰好一个窗口出现在前台；
- 日志出现
  `BROWSER_CLI_USER_EXPLICIT_SHOW stage=producer result=handled
  reason=existing-window-presented window_id=<id> managed=yes`；
- 对应的 policy 日志为 `origin=user-explicit decision=allow`（或
  `user-explicit-always-allowed`），且该 `window_id` 之后被标记为 HUMAN-owned；
- 再次执行同一命令：不新开窗口，仍只有原来那个窗口前台；
- 若日志是 `window-creation-not-implemented`，说明当时没有可复用窗口：
  记录场景（冷启动参数、`--no-startup-window` 等），这属于 patch 023 未实现的路径。

## 3. HUMAN 窗口保护

在窗口已 HUMAN-owned 后，分别尝试：

1. 扩展 API：加载一个临时 MV3 扩展，调用
   `chrome.windows.update(<id>, {focused:true, state:"minimized"})`；
2. `chrome.tabs.create` / `chrome.windows.create`；
3. 会话恢复/普通启动（新进程同一 profile）。

预期：
- 扩展调用返回成功或失败均可，但窗口不得被聚焦、最小化、恢复、改变 bounds 或
  z-order；
- 日志出现 `origin=extension ... decision=deny ... human-owned-window-automation-denied`
  或 `assist-window-automation-denied`；
- 用户自己点击标题栏 minimize/maximize/restore、应用菜单 New Window、
  Ctrl+Shift+T 等仍正常工作（这些路径已标 kUserExplicit）。

## 4. Assist lease

1. 后台窗口运行：

```bat
chrome.exe --user-data-dir=%TMP%\bcli-b --browser-cli-background-presentation ^
  --browser-cli-foreground-assist --browser-cli-foreground-assist-reason=other ^
  --browser-cli-foreground-assist-window=<window_id> ^
  --enable-logging=stderr --v=0
```

预期 grant：目标窗口上屏且前台，日志
`BROWSER_CLI_FOREGROUND_ASSIST stage=producer result=handled reason=lease-granted-and-presented`，
policy 为 `origin=agent-assist ... allow`，并记录 generation。

2. 等待 TTL（默认 5 分钟）或发送 release 开关：

```bat
chrome.exe ... --browser-cli-foreground-assist-release ^
  --browser-cli-foreground-assist-window=<window_id>
```

预期：窗口回到后台/隐藏，日志 `reason=lease-released`；若用户先接管（点击窗口），
lease 被释放、HUMAN 生效，后续 timeout 不得隐藏窗口。

3. ABA：同一窗口连续 grant → release → grant，旧 release/timeout 不得影响新 lease。

## 5. 会话恢复

1. 用同一 profile 正常打开一个页面并退出（不带 managed 也可）；
2. 下一次带 `--restore-last-session --browser-cli-background-presentation` 启动。

预期：managed 模式下不出现可见窗口；日志
`origin=session-restore ... decision=deny ... background-suppresses-native-presentation`。
如果窗口可见上屏，立即判定 FAIL 并停止。

## 6. 用户入口

- 应用菜单（三点菜单）：New Window / New Incognito / History / Preferences / About
  均应正常工作，打开的窗口/页面可见；
- 标题栏 minimize/maximize/restore 正常；
- 键盘 Ctrl+N、Ctrl+Shift+N、Ctrl+Shift+T 正常；
- 记录每次操作后是否有 `origin=user-explicit` 的 allow 日志；没有日志但行为正确
  也要记录（可能走 native OS 路径）。

## 7. 已知不在本清单范围

- taskbar/开始菜单点击的可信归因（当前无法在进程内证明，保持 kUnknown/不授权）。
- 原生系统菜单 `WM_SYSCOMMAND` 的归因（永久不做）。
- 无窗口时创建（patch 023，尚未实现）。
- macOS 回归（需在 macOS 机器单独执行）。

## 8. 证据归档

每个场景保存：

```text
logs/<scenario>-stderr.log        # 完整 stderr
logs/<scenario>-windows.txt       # 窗口枚举/前台窗口采样
logs/<scenario>-screenshot.png    # 可选
```

并在 `docs/windows-m1-gateway-2026-10-23.md` 的对应 Slice 下追加“验证明细”与
结论（PASS/FAIL/REVIEW），不得只写“已测试”。
