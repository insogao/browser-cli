# K4 可见窗口：HWND 可见但 surface 黑（2026-10-30 后续）

## 现象

用户/手动窗口出现“纯黑屏”：

- Win32：`visible=True`、`foreground=True`、窗口 rect 正常；
- 浏览器内部：AX 树完整、`document.visibilityState=visible`、CDP
  `Page.captureScreenshot` 有正常内容；
- 屏幕像素：`RGB≈25–35`（黑），不是页面内容；
- stock Chrome 的普通窗口在同一机器上正常白页，所以不是显示器/DWM 全局失效。

## 两层根因

### 1. 只 `Widget::Show()`，没有走 `BrowserWindow::Show()`

`user_explicit_presentation_win.cc` 原先只对 views `Widget` 调用 `Show()/Activate()`。
这能让 HWND 变可见（patch 030 后），但不会执行 `BrowserView::Show()` 的内容初始化/
同步；结果是“窗口可见、内容没上屏”。

修复（patch 033）：

- 保留 `Widget` 首次 ShowWindow 被 STARTUPINFO 吞掉时的重试；
- 新增 `PresentBrowserWindowForUserExplicitAction(browser, widget)`：
  `BrowserWindow::Show()` → `widget->Show()` → `Activate()`，仍不可见时同序列重试一次；
- 既有窗口和冷启动创建窗口两条 producer 路径统一走该 helper。

### 2. dev build 的 in-process GPU + 默认 GPU 合成

即便走 `BrowserWindow::Show()`，本 dev build 仍有第二层问题：

- 不带 `--in-process-gpu`：Chrome 以 exit code 3（KILLED_BAD_MESSAGE）退出；
- 带 `--in-process-gpu` + 默认 GPU 合成：窗口 surface 黑；
- 带 `--in-process-gpu --disable-direct-composition`：同一窗口屏幕像素
  `RGB=255,255,255`，内容正常；
- 对照：stock Chrome（独立 GPU 进程）正常渲染。

因此当前 dev/可视验收必须使用：

```
--in-process-gpu --disable-direct-composition
```

这是 **dev 兼容组合**，不是产品长期方案。正式发布需要修复 GPU 进程
KILLED_BAD_MESSAGE / DComp 合成路径后去掉该组合。

## 证据

- 构建：`ninja -C out/WinDev -j 6 chrome` exit 0。
- 屏幕像素检测（PowerShell `GetPixel`）：
  - 仅 `BrowserView::Show` + 默认合成：`RGB=35,31,29`（黑）；
  - `+ --disable-direct-composition`：`RGB=255,255,255`（白）。
- 窗口 `GetWindowRect`/`IsWindowVisible=True`，AX/CDP 内容正常。
- patch：`chromium/patches/033-windows-user-explicit-browserview-show.patch`
  （1 文件、40+/3-、87 行，sha256
  `91321ed22d2358400b1080338b8bc84ee3491a3e256c5fb98cde38b93e28b4f8`）。

## 剩余缺口

- 需要单独验证“正常 GPU 合成 + BrowserView::Show”是否足够；当前只能证明组合
  `--disable-direct-composition` 下正常。
- `BrowserView::Show()` 的 `InitialWebUIManager` 异步 deferral 边界未做专门测试。
- 正式发布不得使用 `--disable-direct-composition` 作为长期方案，必须修 GPU 进程。
