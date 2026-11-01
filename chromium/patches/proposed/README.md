# chromium/patches/proposed

此目录存放**尚未入库**的 Chromium 补丁提案。它们：

- **不**在 `chromium/patch-manifest.json` 中；
- **没有**应用到共享源码 `D:/c/src`；
- **没有**构建或运行验证（本 worktree 无独立 Chromium checkout）。

把提案转为已入库 patch 的流程：在独立 checkout 上应用 → 增量构建 → 跑
`tests/check-browser-launch-hygiene.sh`、`tests/run-script-tests.sh` 与本目录
对应文档中的验收命令 → 通过后再分配正式编号、计算 sha256、加入 manifest 与
`chromium/patches/README.md`。

## 034-managed-window-contents-visibility.patch（proposed，route 1）

- 基线：`7d0f28c30d1c0fadbce17ecec0ee2565aef9a940`（156.0.8067.0）+ 已应用 patch 001–033。
- 规模：2 个文件，+70/−1（`chrome/browser/ui/views/frame/browser_view.{h,cc}`）。
- 机制：managed 窗口的原生 presentation 被策略拒绝后，把该窗口的
  `WebContents` 主动置为 HIDDEN（`WebContents::WasHidden()`），使 renderer
  不再产生“永远不会被 present”的 main-frame；窗口真正呈现时
  （USER_EXPLICIT show / assist lease / 原生恢复）在
  `OnWidgetVisibilityChanged(visible=true)` 恢复 active contents
  （`WebContents::WasShown()`）。
- 不改 presentation policy，不删 DCHECK，不扩 buffer，不周期重启/截图。
- 只读校验（2026-11-01，对 `D:/c/src` 当前工作区）：

  ```sh
  cd D:/c/src
  git apply --check <this-repo>/chromium/patches/proposed/034-managed-window-contents-visibility.patch
  # exit 0
  ```

- 详细根因、测试计划、基线证据与风险：
  [`docs/windows-hidden-route1-model-visibility-2026-11-01.md`](../../../docs/windows-hidden-route1-model-visibility-2026-11-01.md)。
- 验收 harness：`tests/tools/hidden-stability-cdp-check.py`
  （`--assert-model hidden|auto`、`--human-takeover-at/--human-hide-at/--human-retakeover-at`、
  `--navigate-every`、`--dom-every`、`--assert-transitions`）。
