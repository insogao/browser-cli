# docs

| 文档 | 内容 |
| --- | --- |
| [`browser-cli-development-plan.md`](browser-cli-development-plan.md) | 宏观设计：状态机、ForegroundOrigin、行为矩阵、内核不变量、ForegroundLease、Phase 0–9、Gate A–C。**版本化副本**，2026-09-21 从 `~/work/browser/browser-cli-development-plan.md` 原样复制（sha256 `6f9e56b2ff238a1df06596ae1964ef2b835581c1cd8f379bf116132083ddd697`）。 |
| [`phase1-environment-2026-09-21.md`](phase1-environment-2026-09-21.md) | Phase 1 环境与验收快照：revision、工具链、构建/增量数据、磁盘、Gate 1–4 结果、未完成项。 |
| [`phase2-background-presentation-2026-09-22.md`](phase2-background-presentation-2026-09-22.md) | Phase 2 第一切片：BACKGROUND_PRESENTATION 策略、三层拦截、结构化日志、USER_EXPLICIT 保护、patch 001、构建/单测/增量/无窗口 runtime smoke 结果、Gate A 未证明项与风险；追加 5 记录 patch 003 CLI 显式 `show` 生产者的非 runtime 集成证据与已知限制（REVIEW）；追加 6 记录 Foreground Assist Slice 1（策略层 lease + patch 004，lease 8/8、policy 27/27、顺序重放逐字节一致）的 REVIEW 状态与无 producer/host/timer/runtime 限制。 |
| [`signing-and-product-identity-2026-09-22.md`](signing-and-product-identity-2026-09-22.md) | 稳定签名与产品身份：唯一配置源、证书精确匹配、patch 002 branding、inside-out 签名脚本、签名验收脚本、构建流程强制签名、未做 notarization/Hardened Runtime、尚需复核项。 |
| [`pitfalls-xcode27.md`](pitfalls-xcode27.md) | Xcode 27 的两个阻塞（`/usr/bin/python3` shim + 相对 `SDKROOT`；Metal Toolchain 缺失）：错误原文、原因、修复与验证命令。 |
| [`windows-environment-2026-10-22.md`](windows-environment-2026-10-22.md) | Windows 工具链安装与验证、构建并行度/OOM、Sogou IME 导致的 exit 3 根因（含栈）与 patch 023 的 TSF 跳过决策。 |
| [`windows-m1-gateway-2026-10-23.md`](windows-m1-gateway-2026-10-23.md) | Windows M1–M4 主线记录：各切片、可见桌面验收、M2 后台能力、M4 品牌/图标/安装器、上游 `views_unittests` 回归。 |
| [`phase7-browser-cli-capability-2026-10-28.md`](phase7-browser-cli-capability-2026-10-28.md) | Phase 7 能力查询接口 `chrome.browserCli.getCapabilities()`：patch 026、allowlist 哈希机制、两个实测陷阱（SW 无裸 `chrome`、API 注入 0.5s 竞态）、三项端到端验收与已知限制。 |

相关但不入库的现场记录：`~/work/browser/BROWSER-CLI-ENV-STATUS.md`（环境状态）、`~/work/browser/BROWSER-CLI-LOCAL-REPO-STATUS.md`（本仓库的建立与验收记录）。
