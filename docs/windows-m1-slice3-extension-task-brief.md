# Patch 018 任务书：Windows 扩展 API origin scope

状态：待执行。基线：patch 001-017 已应用；增量基线 `D:\c\logs\idx017`（post-017）。
目标：把 `chrome.windows` / `chrome.tabs.create` 的 `kExtension` 来源标注扩展到 Windows，
让扩展自动化在 Windows 上也走共享策略（HUMAN 窗口的 focus/minimize/re-show 被拒绝）。

## 不可违反

1. 不改共享策略、不改 macOS 实现、不改 001-017 的 patch 文件。
2. 不 commit/push/reset/checkout；不启动可见浏览器；不把窗口移到屏幕外。
3. 不伪造 PASS；不为了凑绿改测试或禁用检查。
4. 只做来源标注（origin scope），不改变扩展 API 的其它行为。

## 实现

### 1. `chrome/browser/extensions/api/tabs/tabs_api.cc`

把以下 `#if BUILDFLAG(IS_MAC)` 改为 `#if BUILDFLAG(IS_MAC) || BUILDFLAG(IS_WIN)`：

- `:82` 的 include 块（`ui/views/widget/browser_cli_foreground_origin.h`）。
- `:1003` 的 `windows.create` scope。
- `:1573` 的 `windows.update` scope。
- `:2378` 的 `tabs.create` 异步 continuation 分支（`CreateBrowserWindow` +
  `OnBrowserWindowCreatedWithOrigin`）。
- `:2398` 的 `OnBrowserWindowCreatedWithOrigin` 定义。

同时更新这三处注释里已过时的 "macOS-only for now: ... other platforms do not compile
ui/views into this target" 说法：`chrome/browser/extensions/BUILD.gn:1224-1226` 已在
`toolkit_views` 下依赖 `//ui/views`，Windows 的 `toolkit_views` 为 true。注释应说明
Windows 也编译该依赖，而不是删掉原因说明。

### 2. `chrome/browser/extensions/api/tabs/tabs_api.h`

把以下 guard 改为 `IS_MAC || IS_WIN`：

- `:72` 的 `namespace views { enum class ForegroundOrigin; }` 前置声明。
- `:327` 的 `OnBrowserWindowCreatedWithOrigin` 声明。

### 3. GN

先读 `chrome/browser/extensions/BUILD.gn` 确认：
- `tabs_api.cc` 在 `source_set("extensions")`；
- `//ui/views` 在 `toolkit_views` 下已加入 deps（`:1224-1226`）。
如果 Windows 实际没有该依赖，按现有模式补上；不要引入循环依赖。

### 4. 验证

1. `gn gen out/WinDev` exit 0。
2. `ninja -C out/WinDev -j 6 chrome` exit 0。
3. 既有测试不回归：policy 32/32、lease 8/8、gateway 8/8、controller 14/14。
4. 符号/字符串检查：`chrome.dll` 中应能搜到 `kExtension` 相关的
   `BROWSER_CLI_PRESENTATION` 日志格式串或 `origin=extension` 的格式化片段
   （若被优化掉，报告实际搜索结果，不要编造）。
5. 尽力做一次真实扩展验证（可选，失败则如实报告）：
   - 在临时目录写一个最小 MV3 扩展，`background.service_worker` 在启动时调用
     `chrome.windows.getAll` + `chrome.windows.update(windowId, {focused:true})`；
   - headless 启动（`--headless=new --no-sandbox --disable-gpu`、独立
     `--user-data-dir`、`--browser-cli-background-presentation`、`--load-extension=<tmp>`）；
   - 期望日志出现 `origin=extension` 且 `decision=deny`（后台未归属窗口不得被扩展聚焦）。
   - 如果 headless 不支持扩展或无法稳定复现，停止尝试并报告，不要改用可见窗口。
6. 生成 `D:\c\logs\m1-patch018.diff`：
   `GIT_NO_LAZY_FETCH=1 GIT_INDEX_FILE=D:/c/logs/idx017 git diff`（无需 add -N，
   本 patch 不改新文件）；之后确认 idx017 未被修改。不要写入仓库 patch 目录。

## 明确不做

- 用户菜单/标题栏/History scope（patch 019）。
- 会话恢复 scope、`Init` 隐式 show 归因（patch 020）。
- USER_EXPLICIT 的 Windows 原生展示。
- `browser_extension_window_controller.cc` 的 fullscreen 门禁（patch 016 已做）。
