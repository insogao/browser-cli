# M3：BrowserPilot capability 握手（Windows 侧就绪状态）

状态：**契约已定义，Browser CLI 侧能力已实测；BrowserPilot 侧实现不在本仓库。**
本文件记录已经可用的探测手段、扩展侧建议实现，以及当前阻塞点。

## 需求（设计 §7）

BrowserPilot 使用**同一份插件**同时支持普通 Chrome 与 Browser CLI，启动后主动握手：

```json
{
  "browser": "browser-cli",
  "protocol": 1,
  "capabilities": {
    "backgroundWindows": true,
    "backgroundCapture": true,
    "parallelWindows": true,
    "foregroundAssist": true,
    "hardFocusIsolation": true
  }
}
```

普通 Chrome 没有该 capability，走 Generic 路径。禁止拆成两份插件；禁止运行时 patch
插件 JS；内核必须是最终硬保护层。

## Windows 侧能力实测状态（2026-10-26，managed 模式 + 沙箱开启）

| capability | 状态 | 证据 |
| --- | --- | --- |
| `backgroundWindows` | **可用** | 后台启动零可见窗口；`decision=deny origin=unknown` |
| `backgroundCapture` | **可用** | 截图 800×600 PNG、Canvas 320×200 PNG 均有效；JS 定时器正常 |
| `parallelWindows` | **可用** | 临时 MV3 扩展 `chrome.windows.create({focused:false})` 实测窗口数 1→2→3，全部 `origin=extension` deny 不上屏；单窗口复用与冷启动创建也已验证 |
| `foregroundAssist` | **可用** | assist grant 上屏 + release 回后台；HUMAN 拒绝自动化 |
| `hardFocusIsolation` | **可用** | HUMAN 窗口连续 32 次拒绝扩展 focus/minimize；外部 `ShowWindow`/`SetWindowPos` 被剥离 |

因此 Windows 端握手可以如实上报全部 `true`，但 `parallelWindows` 的可信度低于 macOS
（仅覆盖单窗口与复用，未覆盖多窗口并发）。

## 探测机制（当前可实现的最小方案）

扩展无法读取 `chrome://version`，也没有现成的 managed 模式 API。三种可选路径：

### 方案 A：静态能力文件（推荐先用）
BrowserPilot 在扩展侧读取自身打包的 `browser-cli.json`（安装时由 Browser CLI 安装器
写入），内容即上面的握手 JSON。优点：零 Chromium 改动、不扩大攻击面；缺点：无法区分
同一份插件运行在哪个浏览器。

### 方案 B：`chrome.runtime.getPlatformInfo` + 品牌字符串
需要 Chromium 侧让 `getPlatformInfo` 或 `getBrowserInfo` 返回 `Browser CLI` 品牌。
当前 Windows 构建已通过 `chrome/app/theme/browser_cli/BRANDING` 注入
`PRODUCT_FULLNAME=Browser CLI`，但扩展 API 不暴露它。若采用此方案，需要一个只读
capability API（新增 `browserCli` 私有 namespace）。

### 方案 C：原生消息（native messaging）
由 CLI 提供 native messaging host，扩展通过 `connectNative` 询问能力。最准确，
但需要安装器注册 host，属于 M4 打包范围。

## 建议

1. **先做方案 A**：不改 Chromium，BrowserPilot 侧读能力文件即可拿到全部 capability；
2. **M4 打包时补方案 C**：native messaging host 作为权威来源，静态文件只做快速路径；
3. 方案 B 暂不实施：它需要新增扩展 API，扩大攻击面，且与“内核是硬保护层”的设计
   相比收益有限——能力上报不该是授权依据，真正的保护在 Chromium 策略里。

## 当前阻塞

- BrowserPilot 源码不在本仓库，无法在 Windows 上跑真实握手；
- 需要 BrowserPilot 侧确认它期望的探测方式（A/B/C）。

## 不做什么

- 不在本仓库实现 BrowserPilot 插件代码；
- 不通过 URL/HTTP 暴露能力（避免网页可探测）；
- 不让能力上报成为绕过策略的入口：即使上报 true，HUMAN 保护仍由内核强制。
