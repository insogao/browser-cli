# Windows 构建门禁剩余 15 项分类（2026-10-31）

命令：`bash tests/run-script-tests.sh`（Windows，已用 python3 shim）。
结果：**232 passed / 15 failed**。15 项全部是**在 Windows 上执行 macOS-only 脚本/检查**
导致的，不是本次 patch 030–033 的功能回归。未通过修改断言把门禁刷绿。

| 失败项 | 归属 | 分类 | 处理方向 |
| --- | --- | --- | --- |
| `dry-run missing revision-aware gclient sync` | `scripts/checkout.sh`（macOS） | Windows 上不适用；Windows 走手动 gclient + `configure-windows.sh` | 在 Windows runner 中 SKIP；macOS CI 保留 |
| `dry-run missing with_proxy` | 同上 | 同上 | 同上 |
| `dry-run missing checkout --detach <VERSION rev>` | 同上 | 同上 | 同上 |
| `dry-run does not document HEAD mismatch FAIL` | `scripts/verify-phase1.sh`（macOS） | macOS release/phase1 门禁 | 在 Windows runner 中 SKIP；macOS CI 保留 |
| `DISK_MIN_GIB=24 rejected`（及另外 4 项磁盘阈值） | `scripts/preflight.sh` / `checkout.sh` / `build.sh`（macOS） | 仅适用于 macOS 构建主机的磁盘 hard floor | 在 Windows runner 中 SKIP；macOS release-preflight 保留 |
| `resume on tip-like checkout rejected` | `scripts/checkout.sh`（macOS） | 同上 | 同上 |
| `check-only fails on HEAD != VERSION` | `scripts/verify-phase1.sh`（macOS） | 同上 | 同上 |
| `generate-icons --dry-run plan` | `scripts/generate-icons.sh`（macOS `.icns`/`Assets.car`） | Windows 使用 `scripts/generate-windows-icons.py` | Windows runner 改跑 Windows icon dry-run；macOS 保留 |
| `quoted paths do not round-trip` | macOS `.app/Contents/MacOS` + `--use-mock-keychain` 路径 | 仅 macOS runbook | Windows runner SKIP；macOS 保留 |
| `prepare refuses a missing/ad-hoc app` / `missing app did not fail closed` | macOS Developer ID preflight（assist check） | 正式签名/发布阶段检查 | Windows 开发构建 SKIP；macOS release-preflight 保留 |

## 结论

- 不是代码失败，也不是 Windows 构建工具链失败；是**测试入口没有按宿主平台分流**。
- Windows 已有并通过的门禁：`check-browser-launch-hygiene.sh`（16 个启动入口）、
  `configure-windows/build-windows/verify-windows --dry-run`、`chrome.browserCli`
  allowlist self-test、command-line intent arbitration fixtures、patch manifest
  `STATE applied`。
- 下一步不是删断言，而是：
  1. 新增 Windows 专用 runner（只跑 Windows/通用检查）；
  2. 把上述 15 项移到 macOS `release-preflight`，在 macOS CI 上保持强制执行；
  3. Windows CI 对 macOS-only 项输出明确 `SKIP (requires macOS)`，不得输出 PASS。

## 已落地（2026-10-31）

`tests/run-script-tests.sh` 已加入宿主平台分流：

- `HOST_OS="$(uname -s)"`；仅 Darwin 执行 macOS checkout/verify、磁盘阈值、`.icns`、
  macOS `.app` argv、Developer ID preflight 检查；
- 其他宿主输出 `SKIP  ... (macOS release/preflight only)`，**不输出 PASS**；
- 断言本身未修改，macOS CI 仍会执行原始检查；
- Windows 结果：`223 passed, 0 failed`，另有明确 SKIP；
- 仍需补一个 macOS release-preflight 入口，把这些检查在 macOS CI 上强制跑起来。

本轮没有把 15 项改成 PASS，也没有删除 macOS 检查。
