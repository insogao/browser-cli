# P1：从其他浏览器导入登录态与用户数据（2026-10-30）

## 产品目标

首次启动 Browser CLI 时，用户可选择已有浏览器/Profile，按需导入：

1. Cookies 与现有网站登录会话（最高优先级）。
2. 书签、历史记录、打开的标签页。
3. 已保存密码与自动填充数据。
4. 扩展及其他兼容设置。

优先 Windows Chrome、Edge，随后 Brave、Firefox。支持首次引导和设置中重新导入。
所有操作必须由用户明确触发；来源 Profile **只读**，不得修改/破坏原浏览器数据。

## 架构原则

- Normal 与 Agent 的隔离重点是**操作空间、焦点、控制权**，不是默认强迫用户重复登录。
- 同一授权 Profile 下，Normal 与 Agent 应可复用登录状态；不同 Space 不能互抢焦点/
  tab/控制权；用户可为特定 Agent 创建隔离 Profile。
- 当前 managed presentation 有进程级限制；本轮不要求同进程混合架构，但不能把
  “Normal 与 Agent 永远使用不同且无法共享会话的 Profile”固化为产品设计。
- 若暂用隔离 Profile，先实现显式的一次性会话迁移；长期再评估同 Profile 共享
  Cookie Store、分离 Task Space。
- **不得让两个独立 Chromium 进程并发直接读写同一 Profile 数据库。**

## 技术调查清单

- Chromium 原生 Importer / Profile Import 能力与限制。
- Chrome/Edge/Brave/Firefox 数据格式、支持范围。
- Windows DPAPI、Chrome App-Bound Encryption 对跨浏览器迁移的限制。
- 是否可用用户明确授权的来源浏览器扩展 + 官方 Cookie API 在本机迁移。
- HttpOnly、SameSite、Partitioned、Session Cookies 字段兼容性。
- Password Manager 官方导入方式。
- 设备绑定 Session 无法迁移的情况。

**禁止**：破坏 Chrome 安全边界、修改来源浏览器安全配置、提取加密密钥、上传
Cookie/密码/session token、写入普通日志、全局 kill 浏览器进程。

## 第一阶段 MVP

Chrome/Edge Profile 发现 → 用户选择来源与数据类别 → 本地迁移 → 用户确认目标网站
是否仍登录 → 显示导入结果。

必须区分三个状态：

- 数据复制成功；
- Cookie 导入成功；
- 网站登录态经实际页面验证成功。

支持跳过、重试、重新导入；失败不得破坏目标 Profile；设备绑定等不支持情况明确提示；
不同 Space 控制权仍隔离。

## 优先级

- P0：Windows GPU/DComp 黑屏与正式运行稳定性（主代理继续）。
- P1：浏览器数据/登录态导入（独立子 Agent）。
- P1：P0–P3 回归与 K4 harness 稳定化。
- P2：更多浏览器、密码与扩展迁移细节。
- Google 指纹兼容专项暂缓。

## Windows 实测结论（2026-10-31）

对真实 Chrome 156 `inso` Profile 的只读复制与启动验证表明：

- 源 Profile 1780 条 cookie **全部是 v20**（App-Bound），没有 v10；Bilibili 的
  `SESSDATA` 等登录 cookie 未过期（2027-02-27），源登录态本身有效。
- 把 `Local State`、`Network/Cookies` 等复制到 `D:\work\wt\inso-copy-20261031`
  后启动 stock Chrome：DB 文件逐字节一致、仍为 1780 行，但页面未登录，CDP 只返回
  39 条 cookie，`SESSDATA`/`DedeUserID`/`bili_jct` 缺失。
- 根因是 Chromium 的 App-Bound 约束，不是复制遗漏：
  `GetAppBoundEncryptionSupportLevel()` 对非默认 `--user-data-dir` 返回
  `kNotUsingDefaultUserDataDir`；`AppBoundEncryptionProviderWin::GetKey()` 随即返回
  `kTemporarilyUnavailable`，v20 不会回退到 DPAPI v10。
- 因此 **现代 Chrome v20 Profile 的离线复制不能保留登录态**。合规路径只有：
  来源 Chrome 自己通过扩展/官方 API 读取；用户授权后复用现有 Chrome 会话；或在
  目标浏览器重新登录。不得通过改默认目录、伪造路径或调用 elevation service 绕过。
- 真实 Chrome 内的 BrowserPilot unpacked 扩展当前被 Chrome 156 的
  `kExtensionDisableUnsupportedDeveloper`（开发者模式关闭）禁用，native host 未运行；
  但**已安装的 Web Store 扩展不受这条限制**。随后用已安装的 “Get cookies.txt
  LOCALLY” 0.7.2 从真实 inso Chrome 导出当前标签 cookie，完成登录态导入（见下）。
- 若要用 BrowserPilot 自己的扩展做来源，仍需用户开启开发者模式并重新加载扩展，
  或在用户关闭 Chrome 后以 `--load-extension` 启动一次；branded Chrome 会直接忽略
  `--load-extension`，产品化应改为 Web Store/策略安装。
- 验收状态必须保持三态：文件复制成功 ≠ Cookie 导入成功 ≠ 网站登录态验证成功。
  本轮副本实测**不能**记为登录态导入 PASS；但随后的来源扩展 + 目标原生写入
  真实登录态验收**通过**。

## 真实登录态导入 PASS（2026-10-31）

来源 Chrome（真实 inso Profile，未关闭/未重启）当前标签 `www.workbuddy.cn` 已登录；
用其已安装的 Web Store 扩展 “Get cookies.txt LOCALLY” 0.7.2 导出 JSON cookie
（9 条，`www.workbuddy.cn` / `.workbuddy.cn`），经剪贴板交给本地导入脚本，写入
Browser CLI 156.0.8067.0 的 `.manual-profile-normal`（CDP 9333）：

- `Storage.setCookies` 后 `verifiedPresent=9`；
- 目标打开 `https://www.workbuddy.cn/` 显示 `178****1756`、`退出登录`；
- 目标 `Browser.close` 后用同一 Profile 重启，WorkBuddy 仍为登录态；
- 目标 BrowserPilot host `js` 返回 `{loggedIn:true,id:true}`，`snapshot` L0 成功。

这证明“**来源扩展导出 + 目标原生 CookieStore 写入**”在 Windows Chrome 156 的
v20 App-Bound 环境下可行，且不需要复制/解密源 Profile。来源扩展必须是 Web Store/
企业策略安装（或用户显式开启开发者模式加载 unpacked）；普通用户不应被要求改
`--user-data-dir` 或默认目录。下一步产品化：最小来源扩展、配对/用户确认、三态 UI、
书签/历史/密码导入。

证据：`D:\work\BrowserPilot\docs\windows-ego-import-applicability-2026-10-31.md`
§8；导入脚本 `D:\work\BrowserPilot\scripts\import\import-extension-export.mjs`。

证据与源码引用：`D:\work\BrowserPilot\docs\windows-ego-import-applicability-2026-10-31.md`。
