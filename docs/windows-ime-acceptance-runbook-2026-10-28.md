# Windows IME 验收 Runbook

真实输入法无法自动化：任何 `Input.insertText`、`paste` 或 DOM 注入都绕过了 IME，**不能**作为证据。
本 runbook 是这套验收的唯一可复现形式：用真实按键输入，再从 DOM 读回已提交的文本。

配套文件：`tests/fixtures/ime-acceptance.html`。

## 前提

- 本机装有搜狗输入法（本方案针对的正是它的退出行为），且系统当前输入法是中文。
- 使用独立临时 `--user-data-dir`。
- **不要**加 `--no-sandbox`。
- Windows 开发运行需带 `--in-process-gpu`（临时兼容，见
  [tsf-sogou-investigation-2026-10-28.md](../../docs/tsf-sogou-investigation-2026-10-28.md) §12）。

## 步骤

### 1. 前台窗口（stock 模式）

```sh
chrome.exe --in-process-gpu --remote-debugging-port=9333 \
  --user-data-dir=<temp> --no-first-run --no-default-browser-check \
  --new-window "file:///<repo>/tests/fixtures/ime-acceptance.html"
```

在页面里依次操作，每一项都用**单字符真实按键**输入拼音，再按空格提交：

| # | 目标 | 输入 | 期望 |
| --- | --- | --- | --- |
| 1 | `<textarea>` | `nihao` + 空格 | `textarea.value` 含中文（本次实测 `你好你好`） |
| 2 | `<input type=text>` | `nihao` + 空格 | `input.value` = `你好` |
| 3 | `contenteditable` | `wo` + 空格 | `ce.textContent` = `我` |
| 4 | 地址栏 | Ctrl+L，`nihao` + 空格 | 无障碍树里 `地址和搜索栏` 的 value = `你好`（**不要按回车**，避免触发搜索） |
| 5 | 退格 | 两次 BackSpace | 上面第 1 项少两个字符 |

判定"确实发生了组合输入"的硬证据（在页面里挂 `composition*` 监听）：

```text
compositionstart
compositionupdate  data="…"
input              inputType=insertCompositionText
compositionend     data="你好你好"
```

只看到 `keydown` 或 value 里出现字面 `nihao`，**不算通过**——那说明 IME 没有参与。

### 2. managed + HUMAN

1. 起后台实例（无窗口）：`--browser-cli-background-presentation --in-process-gpu --remote-debugging-port=9335`。
2. 确认零可见窗口（`MainWindowHandle == 0`）。
3. 触发用户显式上屏：同 profile 再跑 `--browser-cli-user-explicit-show`。
4. 日志应有 `human-ownership-granted` 与 `reason=existing-window-presented`。
5. 在该窗口重复第 1 项的 `nihao` + 空格。

### 3. managed + ASSIST

1. 起后台实例，用 CDP `Target.createTarget({"newWindow": true})` 创建一个窗口；日志里记下它的 `window_id`（HWND 数值）。
2. 该窗口应为 `state=background`（拒绝上屏）。
3. 申请 lease：

```sh
chrome.exe --browser-cli-background-presentation --in-process-gpu \
  --browser-cli-foreground-assist --browser-cli-foreground-assist-reason=login \
  --browser-cli-foreground-assist-window=<window_id> \
  --user-data-dir=<temp> --no-first-run
```

4. 日志应有 `lease-granted-and-presented` 与 `state=assist leased=yes`。
5. 在该窗口输入 `nihao` + 空格。
6. 反向检查：对 **HUMAN** 窗口申请 assist 必须被拒（`reason=target-human-owned`）。

## 两个必须知道的测量陷阱

1. **被遮挡的窗口不产帧。** Chrome 对完全被遮挡的窗口停止产出合成帧，于是
   `Page.captureScreenshot` 会一直挂住，屏幕抓图和 CUA 截图会拿到黑帧或其它窗口的内容。
   看起来像"渲染坏了"，其实只是被遮挡。判定渲染前必须先 `Page.bringToFront`
   并确认 `document.hasFocus() === true`。
2. **焦点可能在浏览器 UI 而不是网页。** managed 窗口 show 之后，注入的按键可能落进地址栏
   并触发搜索（本机实测发生过一次）。输入前务必确认 `document.activeElement` 是目标元素、
   `document.hasFocus()` 为真；否则先 `Page.bringToFront` 再 `element.focus()`。

## 记录要求

每次运行保存：`composition*` 事件序列、三个字段的 value、assist/HUMAN 的日志行、
进程存活数、以及 CDP 截图。**不要**用"启动没退出"或"headless 通过"替代本 runbook。
