# P1 修订：浏览器数据导入的竞品反证与原生方案（2026-10-30）

## 为什么修订

上一轮得出“唯一合规 Cookie 迁移 = 来源和目标都装扩展 + loopback Broker”的排他性结论，
不成立或至少证据不足：

- Chromium 上游 Importer 不支持 Cookie，只说明上游没实现，不代表 Browser CLI 不能增加
  专用迁移模块。
- ego(lite) 官方宣称 Windows 支持 Cookie、登录会话、密码与 Profile 迁移，且近期仍在修复
  Windows 问题；不能仅凭其公开源码的旧快照否定。
- 双扩展 + Broker 可以作为候选，但不能直接作为正式交互。

## 新阶段目标

### 1. 实测 ego(lite) Windows 导入

在独立测试环境和测试账号中验证：

- 是否真的不要求来源 Chrome 安装扩展；
- 是否要求关闭来源浏览器、管理员授权或其他特殊权限；
- Chrome 现代版本登录态能否迁移；
- 关闭并重启 ego(lite) 后会话是否仍有效；
- 哪些项目成功、哪些部分成功。

不得接触真实用户 Chrome Profile；不得用官方宣传代替实测。

### 2. 重新调查原生导入

对比 Chromium、Edge、ego(lite)：

- 哪些数据可以直接迁移；
- 哪些需要来源浏览器协助；
- 哪些受 DPAPI / App-Bound Encryption / 设备绑定会话限制；
- Browser CLI 能否增加专用迁移模块（不注入 Chrome、不提取加密密钥、不绕过应用身份验证）。

### 3. 简化 Cookie 迁移

- 优先“来源 Profile 选择 → 一键导入”；
- 若必须从来源 Chrome 取 Cookie，研究**只安装来源端一个扩展**，目标 Browser CLI 用原生
  Cookie 写入接口；
- 对不方便迁移的登录态，允许 BrowserPilot 在用户授权后直接使用已有 Chrome 会话，避免为了
  运行 Agent 强制复制账号数据；
- 现有双扩展 + Broker 只保留候选，不继续扩大。

### 4. 真实登录验收

15 + 14 项只是组件测试，不算完成。必须用独立测试账号验证：

来源浏览器已登录 → 用户授权迁移 → Browser CLI 已登录 → 重启后仍登录 →
Agent Space 可以使用该会话。

不可迁移服务明确提示，不隐藏部分失败。

### 5. 下一轮比较报告必须回答

1. ego(lite) Windows 导入实际做到了什么？
2. 是否真的需要在来源和目标分别安装扩展？
3. Browser CLI 能否实现无需两个扩展的一键导入？
4. 各方案操作步骤、安全边界、成功率和开发成本？
5. 哪些场景应直接复用已有 Chrome 会话，而不是迁移？

## 边界

- 不接触真实用户浏览器 Profile；只用隔离测试 Profile/测试账号。
- 不注入 Chrome、不提取加密密钥、不改来源浏览器安全配置。
- 不自动关闭/全局 kill 来源浏览器。
- 不把 Cookie/密码/session token 上传或写普通日志。
- 不继续扩大现有 Broker 架构。
