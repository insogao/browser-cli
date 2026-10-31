# Windows K4 用户显式 show：根因与修复（2026-10-30）

## 结论

`created-window-presentation-denied` / `presentation-denied` 的根因不是“managed 模式不支持
用户显式展示”，而是 **Windows 吞掉了窗口的第一次 `ShowWindow`**：

- 浏览器由隐藏控制台宿主启动时（Node `windowsHide: true`、Windows 服务、CI runner），
  进程带有 `STARTF_USESHOWWINDOW` 且 `wShowWindow = SW_HIDE`。
- Windows 对每个窗口的**第一次** `ShowWindow` 使用启动信息中的 `wShowWindow` 解析，
  调用方传入的 `SW_SHOWNORMAL` 被忽略；HWND 保持不可见，且调用返回不发错。
- 该 managed 窗口在创建时的隐式 show 已被后台策略拒绝，因此用户显式 show 恰好成为
  第一次原生 `ShowWindow`，被吞掉后 `widget->IsVisible()` 仍为 false，producer 只能
  记录 `*-presentation-denied`。

策略本身没有拒绝：日志中第二次请求已经是
`decision=allow action=show-and-activate origin=user-explicit`，host gate 也放行；失败发生在
更底层的 Win32 可见性语义。

## 修复

新增 `views::BrowserCliPresentForUserExplicitAction(Widget*)`：

1. 只接受顶层窗口；否则返回 false（window id 0 永远不能获得 HUMAN 所有权）。
2. 在 `ScopedForegroundOrigin(kUserExplicit)` 内执行 `Show() + Activate()`。
3. 若窗口仍不可见，**恰好重试一次**同样的 `Show() + Activate()`，并记录
   `BROWSER_CLI_PRESENTATION stage=host transition=presentation-retry
   reason=first-showwindow-not-honored`。
4. 每次尝试都重新经过共享 presentation policy 与 patch 016 的 Windows host 门禁；
   不会因为“重试”绕过 deny，也不会在窗口已经可见时二次触碰。

`HandleUserExplicitShowRequest` 的两条呈现路径（复用已有窗口、冷启动创建窗口）统一调用
该 helper，删除原内联 Show/Activate。

Patch：`chromium/patches/030-windows-explicit-show-first-show-retry.patch`
（3 文件、58+/17-、119 行，sha256
`a5f97b9447334781fe3e088efe6a42cf3ea5c19fb80f954540d39636cf1e0ba0`）。

## 真实验证（Windows，out/WinDev）

构建：`ninja -C out/WinDev -j 6 chrome` exit 0，`ui_views.dll`/`chrome.dll` 重链。

### Path A：已有 managed 隐藏窗口 + 第二次 show

1. 以隔离 profile 启动：
   `chrome.exe --user-data-dir=<tmp> --browser-cli-background-presentation about:blank`
   （`windowsHide: true`，复现隐藏控制台宿主）。
2. 等待窗口创建并被后台策略拒绝，Win32 状态：
   `HWND=0x214B8 visible=False foreground=no`。
3. 同一 profile 启动第二个进程：
   `chrome.exe --user-data-dir=<tmp> --browser-cli-user-explicit-show`
   （由 ProcessSingleton 转发到第一个进程）。
4. 结果：
   - Win32：`HWND=0x214B8 visible=True foreground=yes`；
   - producer：`result=handled reason=existing-window-presented`；
   - policy：`deny(background) → allow(user-explicit)`，host gate 放行；
   - 日志出现 `transition=presentation-retry`（第一次被吞）后
     `window-pos-changed ... visible=yes`。

### Path B：冷启动无复用窗口 → `kCreateWindowThenPresent`

命令：

```
chrome.exe --user-data-dir=<tmp> \
  --browser-cli-background-presentation \
  --browser-cli-user-explicit-show \
  --no-startup-window
```

结果：

- producer：`result=handled reason=window-created-and-presented`；
- Win32：`HWND=0x13149E visible=True foreground=yes`；
- 日志同样出现 `transition=presentation-retry` 与 `visible=yes`；
- 进程按预期常驻。

两条路径均未出现任何 presentation policy 放宽：policy 只对 USER_EXPLICIT origin 放行，
host gate 仍逐次检查；后台/自动化 origin 的 deny 路径未变。

复现脚本（临时，不入库）：`D:\c\logs\k4-pathA.mjs`、`D:\c\logs\k4-pathB.mjs`。
脚本使用独立 profile；清理只针对自己启动的 PID/进程树与本次运行时间之后、带唯一
profile 标记的残留进程，不使用 `taskkill /IM`，不触碰用户 Chrome。

## 未验证 / 剩余缺口

- 可见 managed 窗口上的 K4 真实人类输入、`human_presence` 事件、1s pause 与真实
  搜狗 TSF IME（需在可见窗口上执行，A 完成后交给 D）。
- 正常可见控制台启动（无 `STARTF_USESHOWWINDOW` 吞掉首次 Show）时重试不会触发；
  该分支未被本次测试覆盖，但代码只在 `IsVisible()==false` 时重试，不会二次 Show。
- 多窗口选择策略（已有 USER_EXPLICIT 窗口优先）沿用 patch 022，本轮未改。
- F3 产品截图 P0 仍独立阻塞：managed hidden 窗口的 `screenshot` 超时/新鲜度问题
  不属于 K4，需按 B 的 A/B 结论继续。
