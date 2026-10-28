# 多 Agent 并行开发任务表（2026-10-28，阶段 1 起）

主代理：技术管理、派工、审核、集成验收。子 Agent：开发、调查、测试。
所有子 Agent 遵守 `AGENTS.md`：不碰用户真实 profile / 钥匙串；测试一律独立
`--user-data-dir`；不使用 `--no-sandbox`；不修改其它 worktree；不合并主分支。

## 任务分配

| Agent | 分支 / 工作区 | 目标 | 主要修改范围 | 验收 |
| --- | --- | --- | --- | --- |
| A 内核 | Chromium `D:\c\src`（唯一改内核者） | 遮挡窗口可取得**最新帧**的截图，保持节流与预览 | `content/browser/renderer_host/delegated_frame_host.*`、必要时 `web_contents_impl.cc`、`page_handler.cc` | 遮挡场景 `fromSurface=true` 连续 3 次成功且帧序号等于 DOM 计数；窗口 z-order/前台不变；隐藏/最小化不回归 |
| B Native Messaging | `D:\work\wt\bp-native`（`agent/p1-native-messaging`） | HKCU 注册 + 真实 Host 链路 + debugger 六操作 + SW 重启恢复 + generic 回归 | `native-host/*`、`scripts/*`、`tests/*` | 真实 chrome.exe + 真实 Host（非 mock）跑通；注册可回滚；其他 host 名不受影响 |
| C 并发 | `D:\work\wt\bp-concurrency`（`agent/p0-concurrency`） | 租约按目标收敛、截图超时清理、跨层一致性、真实并发测试、CI registry 修复 | `foreground-lease.ts`、`commands.ts`、`spaces.ts`、`templates.ts`、`screenshot-policy.ts`、`cdp.ts`、`scripts/build-registry.mjs` | 两窗口真实并行（时间重叠）且同目标互斥；8 窗口压力；`npm run registry:check` 绿 |
| D 虚拟鼠标 | `D:\work\wt\bp-vmouse`（`agent/p2-virtual-mouse`） | 插件内 AI 虚拟鼠标 + 输入权隔离设计 | `src/content/cursor.ts` 等新文件；复用 `mask.ts`、`actions.ts`、`debugger-bridge.ts` | 光标轨迹来自真实 CDP 坐标；系统鼠标不动；截图默认不含光标；内核方案先审后改 |

文件所有权：C 独占 `commands.ts` / `index.ts` / 租约相关；D 本阶段不修改这两个文件，
只提交接线点清单；A 独占 Chromium 源码。

## 共享基线

`D:\work\wt\browser-cli-baseline` 是当前 `out/WinDev` 的运行快照（含 component DLL 与
AppContainer ACL），供 B/C/D 在 A 重新链接 `out/WinDev` 期间使用，避免 DLL 占用导致
链接失败。基线启动参数：`--headless=new --disable-gpu --in-process-gpu`。

## 安全边界（全员）

- 只读 `HKCU\Software\Google\Chrome\NativeMessagingHosts` 中 `com.browserpilot.browseragent`，
  不触碰 `com.egolite.browseragent` 等其他 host；改动前备份默认值，必须可回滚。
- 不写 HKLM、不安装证书、不改系统输入法、不结束用户正在使用的 Chrome。
- 不把 `possiblyStale=true` 当作最新截图验收通过。
- 不新增 Chromium patch，除非 A 的方案经主代理审核；任何 patch 必须编号入
  `chromium/patches/` 并更新 manifest。
