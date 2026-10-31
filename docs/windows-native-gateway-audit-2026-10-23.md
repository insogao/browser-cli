# M1 Windows Gateway Map

Baseline: `D:\c\src` @ `7d0f28c30d1c0fadbce17ecec0ee2565aef9a940` (Chromium 156.0.8067.0), patches 001-014 applied to the working tree (`git status --porcelain` shows the expected M + ?? set).
Scope: read-only source audit. No build, no browser, no profile/keychain access.
All citations are `path:line` inside `D:\c\src`.

Correction to the task brief, verified by search: **the macOS adapters are NOT named `browser_cli_*presentation*.mm` under the four listed directories.** The only files matching `browser_cli_*` in `ui/views/widget`, `chrome/browser/ui/views`, `chrome/browser/ui/cocoa`, and `components/remote_cocoa` are:

- `ui/views/widget/browser_cli_presentation_policy.{h,cc}` (shared C++, cross-platform)
- `ui/views/widget/browser_cli_foreground_lease.{h,cc}` (shared C++, cross-platform)
- `ui/views/widget/browser_cli_foreground_origin.h` (shared header)
- `ui/views/widget/browser_cli_presentation_policy_unittest.cc`, `browser_cli_foreground_lease_unittest.cc`
- `components/remote_cocoa/app_shim/browser_cli_managed_presentation_unittests.mm` (test only)

The actual macOS production plumbing lives in:
- `chrome/browser/browser_cli/user_explicit_presentation.h/.mm`
- `chrome/browser/browser_cli/foreground_assist_presentation.h/.mm`
- `ui/views/cocoa/native_widget_mac_ns_window_host.h/.mm` (the real per-window gateway)
- `components/remote_cocoa/app_shim/native_widget_mac_nswindow.h/.mm` (AppKit hard stop)
- `components/remote_cocoa/app_shim/native_widget_ns_window_bridge.mm` (bridge backstop)

`ui/views/widget/native_widget_win.cc` and `chrome/browser/ui/views/frame/browser_frame_win.cc` **do NOT exist** at this revision. NOT FOUND in source. The Windows equivalents are `ui/views/widget/desktop_aura/desktop_window_tree_host_win.{h,cc}` + `ui/views/win/hwnd_message_handler.{h,cc}` + `chrome/browser/ui/views/frame/browser_desktop_window_tree_host_win.{h,cc}` + `chrome/browser/ui/views/frame/browser_widget.{h,cc}`. There is also no `chrome/browser/ui/views/frame/browser_frame.{h,cc}` (the class was replaced by `BrowserWidget`, `chrome/browser/ui/views/frame/browser_widget.h:58`).

---

## 1. Windows native show / activate paths

### 1.1 The single funnel (what every programmatic path goes through)

```
BrowserView::Show/Activate/SetBounds/...          chrome/browser/ui/views/frame/browser_view.cc
  -> BrowserWidget (a views::Widget)              chrome/browser/ui/views/frame/browser_widget.h:58
    -> Widget::Show/Activate/...                  ui/views/widget/widget.cc
      -> NativeWidgetPrivate (DesktopNativeWidgetAura)  ui/views/widget/desktop_aura/desktop_native_widget_aura.cc
        -> DesktopWindowTreeHost (DesktopWindowTreeHostWin) ui/views/widget/desktop_aura/desktop_window_tree_host_win.cc
          -> HWNDMessageHandler                   ui/views/win/hwnd_message_handler.cc
            -> ::ShowWindow / ::SetForegroundWindow / ::SetWindowPos / ::SetWindowPlacement / ::FlashWindowEx
```

Verified edges:

| Hop | file:line | function |
| --- | --- | --- |
| BrowserView::Show | `chrome/browser/ui/views/frame/browser_view.cc:1601` | body; `browser_widget_->Show()` at `:1655` |
| BrowserView::ShowInactive | `browser_view.cc:1679` | `browser_widget_->ShowInactive()` at `:1681` |
| BrowserView::Hide | `browser_view.cc:1685` | `browser_widget_->Hide()` at `:1686` |
| BrowserView::SetBounds | `browser_view.cc:1693` | policy gate at `:1694-1705`, then `frame_view->SetFrameBounds` / `browser_widget_->SetBounds` |
| BrowserView::Activate | `browser_view.cc:1729` | `browser_widget_->Activate()` at `:1639`/`:1760` |
| BrowserView::Deactivate | `browser_view.cc:1763` | `browser_widget_->Deactivate()` at `:1764` |
| BrowserView::SetZOrderLevel | `browser_view.cc:1779` | `browser_widget_->SetZOrderLevel(level)` at `:1780` |
| BrowserView::Maximize/Minimize/Restore | `browser_view.cc:2255`, `:2259`, `:2263` | forwards to `browser_widget_` |
| BrowserView::RequestFullscreen | `browser_view.cc:5577` | `browser_widget_->SetFullscreen(...)` at `:5580` and `:5634` |
| Widget::Show | `ui/views/widget/widget.cc:1113` | `native_widget_->Show(...)` at `:1130`, `:1133`, `:1141` |
| Widget::Hide | `widget.cc:1148` | `native_widget_->Hide()` at `:1153` |
| Widget::ShowInactive | `widget.cc:1158` | `native_widget_->Show(kInactive,...)` at `:1173` |
| Widget::Activate | `widget.cc:1179` | `native_widget_->Activate()` at `:1181` |
| Widget::Deactivate | `widget.cc:1185` | `native_widget_->Deactivate()` at `:1187` |
| Widget::SetZOrderLevel | `widget.cc:1199` | `native_widget_->SetZOrderLevel(order)` at `:1201` |
| Widget::Maximize/Minimize/Restore | `widget.cc:1254`, `:1260`, `:1266` | `native_widget_->Maximize()/Minimize()/Restore()` at `:1256`, `:1262`, `:1268` |
| Widget::SetFullscreen | `widget.cc:1286` | `native_widget_->SetFullscreen(...)` at `:1300` |
| Widget::SetBounds | `widget.cc:900` | `native_widget_->SetBounds(bounds)` at `:902` |
| Widget::SetVisible | `widget.cc:1983` | routes to `Show()`/`Hide()` |
| DNWA::Show | `ui/views/widget/desktop_aura/desktop_native_widget_aura.cc:998` | `desktop_window_tree_host_->Show(...)` at `:1004` |
| DNWA::Hide | `desktop_native_widget_aura.cc:1007` | `AsWindowTreeHost()->Hide()` at `:1009` |
| DNWA::Activate | `desktop_native_widget_aura.cc:1052` | `desktop_window_tree_host_->Activate()` |
| DNWA::Deactivate | `desktop_native_widget_aura.cc:1071` | |
| DNWA::SetZOrderLevel | `desktop_native_widget_aura.cc:1089` | |
| DNWA::Maximize/Minimize/Restore | `desktop_native_widget_aura.cc:1113`, `:1119`, `:1136` | |
| DNWA::SetFullscreen | `desktop_native_widget_aura.cc:1148` | |
| DNWA::SetBounds | `desktop_native_widget_aura.cc:942` | `SetBoundsInDIP(bounds)` at `:946` |
| DWTHWin::Show | `ui/views/widget/desktop_aura/desktop_window_tree_host_win.cc:398` | `UpdateDisplayAffinity()`, `content_window()->Show()`, `message_handler_->Show(...)` at `:419`, then `WidgetActivationDelegate::MaybeActivate` |
| DWTHWin::Activate | `desktop_window_tree_host_win.cc:529` | `message_handler_->Activate()` at `:534` |
| DWTHWin::Deactivate | `desktop_window_tree_host_win.cc:537` | `message_handler_->Deactivate()` at `:541` |
| DWTHWin::Maximize/Minimize/Restore | `desktop_window_tree_host_win.cc:556`, `:560`, `:564` | at `:557`, `:561`, `:565` |
| DWTHWin::SetZOrderLevel | `desktop_window_tree_host_win.cc:580` | `SetAlwaysOnTop(order != kNormal)` at `:585` |
| DWTHWin::SetFullscreen | `desktop_window_tree_host_win.cc:702` | `message_handler_->SetFullscreen(...)` at `:705` |
| DWTHWin::ShowImpl / HideImpl | `desktop_window_tree_host_win.cc:779`, `:783` | ShowImpl -> `Show(kNormal)`; HideImpl -> `message_handler_->Hide()` at `:785` |
| DWTHWin::SetBoundsInPixels | `desktop_window_tree_host_win.cc:806` | `message_handler_->SetBounds(...)` at `:822` |
| DWTHWin::SetBoundsInDIP | `desktop_window_tree_host_win.cc:1544` | converts then `SetBoundsInPixels` |
| HWNDMessageHandler::Show | `ui/views/win/hwnd_message_handler.cc:771` | `::SetWindowPlacement` `:783`, `::ShowWindow` `:825` and `:834`, `Activate()` `:844`, `SetInitialFocus()` `:848` |
| HWNDMessageHandler::Hide | `hwnd_message_handler.cc:852` | `::SetWindowPos(..., SWP_HIDEWINDOW | SWP_NOACTIVATE | ...)` `:858` |
| HWNDMessageHandler::Maximize | `hwnd_message_handler.cc:864` | `ExecuteSystemMenuCommand(SC_MAXIMIZE)` `:868` -> `::SendMessage(WM_SYSCOMMAND)` `:1713` |
| HWNDMessageHandler::Minimize | `hwnd_message_handler.cc:871` | `ExecuteSystemMenuCommand(SC_MINIMIZE)` `:872` |
| HWNDMessageHandler::Restore | `hwnd_message_handler.cc:876` | `SC_RESTORE` `:880` |
| HWNDMessageHandler::Activate | `hwnd_message_handler.cc:884` | `::ShowWindow(SW_RESTORE)` `:887`, `::SetWindowPos(HWND_TOP,...)` `:894`, `::SetForegroundWindow` `:895` |
| HWNDMessageHandler::Deactivate | `hwnd_message_handler.cc:898` | `::SetForegroundWindow(next_hwnd)` `:902` |
| HWNDMessageHandler::SetAlwaysOnTop | `hwnd_message_handler.cc:909` | `SetWindowPos(HWND_TOPMOST/HWND_NOTOPMOST,...)` `:910` |
| HWNDMessageHandler::StackAbove / StackAtTop | `hwnd_message_handler.cc:751`, `:763` | `SetWindowPos` at `:759`, `:767` |
| HWNDMessageHandler::SetFullscreen | `hwnd_message_handler.cc:1096` | `FullscreenHandler::SetFullscreen` `:1102` -> `ui/views/win/fullscreen_handler.cc:62-163` (`SetWindowPos` `:107`, `:134`, `:153`, style writes `:87`, `:115`) |
| HWNDMessageHandler::SetBounds / SetBoundsInternal | `hwnd_message_handler.cc:659`, `:3910` | `::SetWindowPos(..., SWP_NOACTIVATE | SWP_NOZORDER)` `:3914` |
| HWNDMessageHandler::FlashFrame | `hwnd_message_handler.cc:980` | `::FlashWindowEx` `:991` |

Important Windows-only implicit-show sites that no policy can currently see:

- `DesktopWindowTreeHostWin::Init` calls `window()->Show()` at `desktop_window_tree_host_win.cc:304`. This runs during `Widget::Init` (`ui/views/widget/widget.cc:600` -> `native_widget_->InitNativeWidget`, `widget.cc:600`), i.e. **before** any `Widget::Show()` and before the Browser CLI policy has any chance to run. Combined with `ui/gfx/win/window_impl.cc:30-31` (`kWindowDefaultStyle = WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN`, no `WS_VISIBLE`) the HWND itself is created hidden, but the aura content window is marked visible; a later `SetWindowPos(SWP_SHOWWINDOW)` (any caller) then makes it appear with no `Widget::Show()` in the stack.
- `HWNDMessageHandler::Activate` un-minimizes with `::ShowWindow(hwnd(), SW_RESTORE)` (`hwnd_message_handler.cc:887`) **before** calling `SetForegroundWindow`. So `Widget::Activate()` on a minimized managed window is also a *show*.
- `HWNDMessageHandler::OnSysCommand` (`hwnd_message_handler.cc:3001`) reaches `::DefWindowProc(WM_SYSCOMMAND)` at `:3080`, and `WM_SYSCOMMAND` can also arrive from the taskbar/system menu, the window's own `SC_*` buttons (`:3411-3428`), or any external process via `::SendMessage`. `ExecuteSystemMenuCommand` (`:1711`) posts the same messages. Therefore `Maximize()/Minimize()/Restore()` are **not** distinguishable from the user's own title-bar actions at this layer.
- `ScopedFullscreenVisibility` unconditionally `::ShowWindow(hwnd_, SW_SHOW)` at `ui/views/win/scoped_fullscreen_visibility.cc:41`, and strips `SWP_SHOWWINDOW` in `OnWindowPosChanging` at `hwnd_message_handler.cc:3257-3261`. This is a fourth independent show path that ignores every policy.
- `fullscreen_handler.cc:115` restores visibility with `GWL_STYLE | WS_VISIBLE` on fullscreen exit, i.e. exit-fullscreen can also reveal a formerly hidden window.

### 1.2 User action vs automated vs session restore

There is **no existing attribution** on any Windows path above. Every entry below lands in the same `Widget`/`DesktopWindowTreeHostWin` code with `views::CurrentForegroundOrigin() == kUnknown` (default at `ui/views/widget/browser_cli_presentation_policy.cc:116-117`).

| Entry | file:line | Classification | Where a policy check belongs |
| --- | --- | --- | --- |
| CLI / automation `show` | `chrome/browser/chrome_browser_main.cc:2167` (Start), `:2179` arbitration is inside `#if BUILDFLAG(IS_MAC)` at `:2169` | automated (or `kUserExplicit` when produced by the show switch) | Windows: after Start, before/around any `HandleUserExplicitShowRequest` equivalent |
| ProcessSingleton forwarded argv | `chrome/browser/chrome_browser_main.cc:543-600`; Windows WM_COPYDATA handler `chrome/browser/process_singleton_win.cc:142-166`, callback at `:163` | automated; trusted only if the switch is a pure show | Windows branch of `ProcessSingletonNotificationCallbackImpl` (`chrome_browser_main.cc:552` shows a Windows-only early return already) |
| Startup default window | `chrome/browser/ui/startup/startup_browser_creator_impl.cc:475` `browser->GetWindow()->Show()` | browser-initiated (not user) | after `LaunchBrowser`; origin `kUnknown`/`kSessionRestore` |
| Session restore | `chrome/browser/sessions/session_restore.cc:500`, `:562`, `:1363`, `:1418` | `kSessionRestore` | must scope each of these four sites |
| Tab restore (user menu) | `chrome/browser/ui/browser_live_tab_context.cc:158` (`base_window_->Show()`), reached from `components/sessions/core/tab_restore_service_helper.cc:952`, `:1071`, `:1164`, `:1216` | user, but only if the menu scoped it; on Windows the menu is not scoped today | scope at the Views menu dispatch |
| Extension `chrome.windows.create/update` | `chrome/browser/extensions/api/tabs/tabs_api.cc:1003`, `:1573`, `:2378` (all `#if BUILDFLAG(IS_MAC)`) | `kExtension` | Windows: enable the same scopes (needs GN dep, see 4.4) |
| Extension fullscreen | `chrome/browser/extensions/browser_extension_window_controller.cc:153` (`#if BUILDFLAG(IS_MAC)`) | `kExtension` | Windows: same |
| Renderer/WebContents activation | `chrome/browser/ui/browser_web_contents_delegate/browser_web_contents_delegate.cc:749-759` (`window_->Activate()`) via `content/browser/web_contents/web_contents_impl.cc:4659` | `kWebPage`/`kUnknown`; deliberately unattributable | do not open; must stay fail-closed |
| Navigator-disposition show | `chrome/browser/ui/navigator/browser_navigator.cc:556` (`window->Show()`), `:565` (`window->Activate()`) | producer-dependent (`params.user_gesture` is renderer-supplied) | a policy check is possible but must not trust `user_gesture` |
| Omnibox Everywhere activation | `chrome/browser/ui/omnibox/omnibox_everywhere_service.cc:284-302`; on Windows the `#else` branch at `:294-302` calls `Restore()/Show()/Activate()` with no policy | automated/WebUI (`user_gesture` false by contract at `omnibox_everywhere_service.h:88-92`) | the `#else` branch |
| Dock/Launchpad equivalent | does not exist on Windows; taskbar activation is handled entirely by the OS | `kUserExplicit` but unobservable — see 3 | cannot be done in-process (see 6) |

Recommended insertion points that do **not** duplicate policy (single shared decision, native plumbing only):

1. `DesktopWindowTreeHostWin::Show` (`desktop_window_tree_host_win.cc:398`) — gate `kShowAndActivate`/`kShowInactive` before `content_window()->Show()` / `message_handler_->Show()`. Return early on deny; never show-then-hide.
2. `DesktopWindowTreeHostWin::Activate` (`:529`) — gate `kShowAndActivate` (this is also an un-minimize, `hwnd_message_handler.cc:887`).
3. `DesktopWindowTreeHostWin::Deactivate` (`:537`) — gate `kDeactivate`.
4. `DesktopWindowTreeHostWin::Maximize/Minimize/Restore` (`:556`, `:560`, `:564`) — gate `kMaximize`/`kMinimize`/`kRestore`.
5. `DesktopWindowTreeHostWin::SetFullscreen` (`:702`) — gate `kFullscreen`; also guard `ScopedFullscreenVisibility` (`scoped_fullscreen_visibility.cc:41`) since that path bypasses everything.
6. `DesktopWindowTreeHostWin::SetZOrderLevel` (`:580`) and `StackAbove`/`StackAtTop` (`:439`, `:446`) — gate `kSetZOrder`.
7. `DesktopWindowTreeHostWin::SetBoundsInPixels` (`:806`) — gate `kSetBounds` **only if** the `kSetBounds` exception is re-proven for `SetWindowPos`; see W-02 risk in section 6.
8. `DesktopWindowTreeHostWin::HandleVisibilityChanged` (`:1289`) — observation-only; this is where a "native bypass detected" telemetry/log hook can fire, because `WM_WINDOWPOSCHANGED`/`SWP_SHOWWINDOW` and `ShowWindow` both land here.
9. `HWNDMessageHandler::OnWindowPosChanging` (`hwnd_message_handler.cc:3109`) — the only place that can *strip* `SWP_SHOWWINDOW` for an external `SetWindowPos`, exactly like `ScopedFullscreenVisibility` already does at `:3257-3261`. This is the equivalent of `-[NativeWidgetMacNSWindow orderWindow:]`'s hard stop (`components/remote_cocoa/app_shim/native_widget_mac_nswindow.mm:854-873`), and it must call the **shared** policy, not a Windows-only decision.

`BrowserWidget` overrides nothing relevant: `chrome/browser/ui/views/frame/browser_widget.h:58-140` shows only theme/accelerator/system-menu overrides. There is no `BrowserFrameWin` class to patch.

---

## 2. Where the shared policy is consulted today (macOS only)

Shared policy API surface (`ui/views/widget/browser_cli_presentation_policy.h`):
`CurrentForegroundOrigin()` `:100`, `ScopedForegroundOrigin` `:106-116`, `AllowProgrammaticWindowChangeFromCurrentOrigin()` `:143-145`, `BrowserCliPresentationPolicy::AllowPresentation` `:212`, `ReleaseUserExplicitOwnership` `:220`, `IsUserExplicitOwned` `:223`, `RequestForegroundAssist` `:241`, `ReleaseForegroundAssist` `:256`, `IsForegroundAssistLeased` `:267`, `GetForegroundAssistLeaseGeneration` `:273`.

### 2.1 `ui/views/cocoa/native_widget_mac_ns_window_host.{h,mm}` — the macOS gateway

| Function | mm line | shared-policy call |
| --- | --- | --- |
| `~NativeWidgetMacNSWindowHost` | `:330` | `ReleaseUserExplicitOwnership(widget_id_, "window-destroyed")` `:337`; `ReleaseForegroundAssist(..., kWindowDestroyed)` `:342` |
| `IsBrowserCliManagedPresentationEnabled()` (static) | `:915` | `BrowserCliPresentationPolicy::Get()->IsManagedModeEnabled()` `:916` |
| `IsUserExplicitOwned()` | `:919` | `IsUserExplicitOwned(widget_id_)` `:920` |
| `SetVisibilityState(new_state)` | `:923` | `ApplyVisibilityState(new_state, CurrentForegroundOrigin())` `:927` |
| `PresentForUserExplicitAction()` | `:930` | `ApplyVisibilityState(kShowAndActivateWindow, kUserExplicit)` `:931-932` |
| `RequestForegroundAssistLease(tab, reason)` | `:949` | `RequestForegroundAssist(kAgentAssist, widget_id_, ...)` `:957-958` |
| `IsForegroundAssistLeased()` | `:961` | `:962` |
| `GetForegroundAssistLeaseGeneration()` | `:966` | `:968-969` |
| `ReleaseForegroundAssistLease(reason, gen)` | `:972` | `:974-975` |
| `PresentForAgentAssistAction()` | `:979` | `ApplyVisibilityState(kShowAndActivateWindow, kAgentAssist)` `:984-985` |
| `HideForForegroundAssistRelease(gen)` | `:995` | re-arm suppression `:1010-1012`, then `ApplyVisibilityState(kHideWindow, kAgentAssist)` `:1014-1015` |
| `AllowProgrammaticChangeFromCurrentOrigin(action)` | `:1021` | `EvaluatePresentationRequest(action, CurrentForegroundOrigin())` `:1023` |
| free `AllowProgrammaticWindowChangeFromCurrentOrigin(native_window, action)` | `:1031` | `:1035` (this is the **only definition**; grep confirms) |
| `EvaluatePresentationRequest` | `:1038` | `AllowPresentation(origin, action, widget_id_)` `:1041` |
| `ApplyVisibilityState` | `:1057` | `EvaluatePresentationRequest` `:1080`, then `GetNSWindowMojo()->SetVisibilityState(new_state)` `:1104` |

Callers of the gateway entry points, all macOS:
- `ui/views/widget/native_widget_mac.mm:793` (Show), `:807` (Hide), `:822` (Activate), `:832` (`kDeactivate`), `:851` (`kSetZOrder`), `:895` (`kMaximize`), `:910` (`kMinimize`), `:938` (`kRestore`), `:955` (`kFullscreen`).
- `chrome/browser/browser_cli/user_explicit_presentation.mm:70` (`PresentForUserExplicitAction`), `:107` (`IsUserExplicitOwned`), `:52/161/181/197` (`IsBrowserCliManagedPresentationEnabled`).
- `chrome/browser/browser_cli/foreground_assist_presentation.mm:46-78` (delegate: `GetFromWidgetId`, `IsUserExplicitOwned`, `GetForegroundAssistLeaseGeneration`, `ReleaseForegroundAssistLease`, `HideForForegroundAssistRelease`), `:119`, `:183-245`.
- `chrome/browser/ui/omnibox/omnibox_everywhere/mac_window_util.mm:49-61` (`IsBrowserCliManagedPresentationEnabled` + `PresentForUserExplicitAction`).
- `chrome/browser/app_controller_mac.mm:1953`, `:2005`, `:2061`, `:2131` (+ menu scopes `:1793`, `:2320`, `:2351`).
- `chrome/browser/ui/views/frame/browser_view.cc:1613`, `:1701`, `:1741` (cross-platform code, but the `AllowProgrammaticWindowChangeFromCurrentOrigin` call is inside `#if BUILDFLAG(IS_MAC)` `:1694`/`:1705`).
- `chrome/browser/extensions/browser_extension_window_controller.cc:161` (inside `#if BUILDFLAG(IS_MAC)` `:153`).
- `chrome/browser/extensions/api/tabs/tabs_api.cc:1004`, `:1574`, `:2408` (all inside `#if BUILDFLAG(IS_MAC)`).

### 2.2 Per-function Windows analogue (native plumbing only; no re-implementation)

| macOS function | Windows analogue that would call the same shared decision |
| --- | --- |
| `SetVisibilityState` -> `ApplyVisibilityState` -> `AllowPresentation` | `DesktopWindowTreeHostWin::Show` (`desktop_window_tree_host_win.cc:398`) and `Activate` (`:529`) call `AllowPresentation(CurrentForegroundOrigin(), kShowAndActivate/kShowInactive, hwnd_id)` before `message_handler_->Show/Activate` |
| `PresentForUserExplicitAction` | `DesktopWindowTreeHostWin::PresentForUserExplicitAction()` (new), same `AllowPresentation(kUserExplicit, kShowAndActivate, id)` then `message_handler_->Show`/`Activate` |
| `PresentForAgentAssistAction` | same new method with `kAgentAssist`; only reachable after `RequestForegroundAssistLease` granted |
| `HideForForegroundAssistRelease` | new method calling `AllowPresentation(kAgentAssist, ...)`/direct `message_handler_->Hide()`; must never hide `IsUserExplicitOwned()` (mirror `native_widget_mac_ns_window_host.mm:999-1001`) |
| `IsUserExplicitOwned` | trivial: `BrowserCliPresentationPolicy::Get()->IsUserExplicitOwned(hwnd_id)` |
| `RequestForegroundAssistLease` / `IsForegroundAssistLeased` / `GetForegroundAssistLeaseGeneration` / `ReleaseForegroundAssistLease` | identical forwarding, keyed by a Windows window id |
| `AllowProgrammaticChangeFromCurrentOrigin` | the free function `AllowProgrammaticWindowChangeFromCurrentOrigin` must get a **Windows definition** (currently only `native_widget_mac_ns_window_host.mm:1031`); implement it in a Windows TU inside the same `ui/views` component, resolving `gfx::NativeWindow` (an `aura::Window*` on Windows, `ui/gfx/native_ui_types.h:124`) via `aura::WindowTreeHost::GetForAcceleratedWidget` / `Widget::GetWidgetForNativeWindow` (`ui/views/widget/widget.cc:404`) and then calling the same policy |
| AppKit `-[NativeWidgetMacNSWindow orderWindow:/orderFront:/...]` hard stop (`native_widget_mac_nswindow.mm:854-927`) | `HWNDMessageHandler::OnWindowPosChanging` (`hwnd_message_handler.cc:3109`) stripping `SWP_SHOWWINDOW` (`:3260` pattern), and `OnWindowPosChanged` (`:3270`) only for observation |
| bridge `SetVisibilityState` backstop (`native_widget_ns_window_bridge.mm:913-927`) | `HWNDMessageHandler::Show`/`SetBoundsInternal` entry guard, or a per-HWND "suppressed" flag consumed in `OnWindowPosChanging` |

Windows has no per-window stable id equivalent to macOS `widget_id_` (`ui/views/cocoa/native_widget_mac_ns_window_host.mm:262`, `:320`, monotonic counter). `views::Widget` has no id (`grep` over `ui/views/widget/widget.h` finds none) and `aura::Window::GetId()` returns an `int` (`ui/aura/window.h:215`, `ui/aura/window.cc:342`). A Windows gateway therefore needs either `base::win::HandleToUint64(hwnd)` or a new monotonically increasing per-host id with the same destruction-release semantics as `native_widget_mac_ns_window_host.mm:337-343`.

---

## 3. Trusted user entry points on Windows

Mapping policy for each; `kUnknown` is the fail-closed default (`browser_cli_presentation_policy.cc:116-117`) and is classified as automation (`:96-108`).

| Entry point | Where the intent becomes observable | Should map to | Notes |
| --- | --- | --- | --- |
| Taskbar button click / hover thumbnail | **NOT OBSERVABLE in-process.** Activation arrives as `WM_ACTIVATE` (`ui/views/win/hwnd_message_handler.cc:1274-1277`) and `WM_NCACTIVATE` (`:490`); the OS owns the click. Chromium uses `ITaskbarList` only for overlay icons (`chrome/browser/taskbar/taskbar_decorator_win.cc:61-106`), jump-list progress (`chrome/browser/download/download_status_updater_win.cc:49-51`), and `MarkFullscreenWindow` (`ui/views/win/fullscreen_handler.cc:35-53`); no thumbnail-toolbar (`THUMBBUTTON`) registration exists. | n/a — must not be treated as a producer | A taskbar click on a *hidden* window is impossible (the shell has no thumbnail for a hidden window); a taskbar click on a *visible* window produces only `WM_ACTIVATE`, i.e. arrives after the window is already visible. |
| Start menu / shortcut activation | `chrome/browser/win/chrome_process_finder.cc:65-71` records `STARTF_TITLEISLINKNAME` into `--source-shortcut` (`chrome/common/chrome_switches.h:998`); second-instance argv is forwarded at `:90-92` and lands in `chrome/browser/process_singleton_win.cc:163` -> `chrome/browser/chrome_browser_main.cc:543` | `kUserExplicit` **only** when the forwarded command line carries the pure `--browser-cli-user-explicit-show` switch (`chrome/browser/browser_cli/user_explicit_show.h:18-19`, classifier `user_explicit_show.cc:13-33`) | `--source-shortcut` itself is not proof of a human gesture (any process can pass it). Do not use it as provenance. |
| Second-process command line (ProcessSingleton) | `chrome/browser/process_singleton_win.cc:142-166` -> `chrome/browser/chrome_browser_main.cc:552-600`; note the Windows-only `kUninstall` early return at `:552-560` and the missing arbitration because `:562-586` is `#if BUILDFLAG(IS_MAC)` | `kUserExplicit` for the pure show switch; `kAgentAssist` for assist switches; `kUnknown`/ordinary otherwise | Windows has **no** call to `ArbitrateBrowserCliCommandLineIntent` (`chrome/browser/browser_cli/foreground_assist.cc:94`) or `CommandLineForColdStartStartup` (`:111`). This is W-04. |
| Cold start argv | `chrome/browser/chrome_browser_main.cc:2151-2166` — the cold-start sanitizer is `#if BUILDFLAG(IS_MAC)` | same as above | Windows uses the raw command line at `:2164-2165`. |
| Browser menu items (Views `AppMenu` / system menu) | `chrome/browser/ui/toolbar/app_menu_model.cc:1283` -> `:1318` `chrome::ExecuteCommand` -> `chrome/browser/ui/browser_commands.cc:864` -> `BrowserCommandController::ExecuteCommandWithDispositionAndContext` (`chrome/browser/ui/browser_command_controller.cc:623`, dispatch at `:645` -> `command_updater_impl.cc:40-50`) -> `HandleCommandWithDisposition` (`browser_command_controller.cc:649`) | `kUserExplicit` (mirrors macOS menu scopes `chrome/browser/app_controller_mac.mm:1793`, `:2320`, `:2351`) | Best single choke point on Windows: `AppMenu::ExecuteCommand` (`chrome/browser/ui/views/toolbar/app_menu.cc:1334`) or `AppMenuModel::ExecuteCommand` (`app_menu_model.cc:1283`). `BrowserCommandController::ExecuteCommandWithDispositionAndContext` is too broad (webui toolbar, actions framework, `chrome/browser/ui/webui/browser_command/browser_command_handler.cc`), so it must not be blanket-scoped. |
| Windows system menu (Alt+Space / right-click title) | `chrome/browser/ui/views/frame/system_menu_model_delegate.cc:80`; native path `chrome/browser/ui/views/frame/browser_desktop_window_tree_host_win.cc:270` (`ShowCustomSystemMenu`) and `PreHandleMSG` (`:407-444`) | `kUserExplicit` | Currently unscoped. `IDC_MINIMIZE_WINDOW`/`IDC_MAXIMIZE_WINDOW`/`IDC_RESTORE_WINDOW` reach `browser_command_controller.cc:868-876`. |
| Caption buttons (custom-drawn frame) | `chrome/browser/ui/views/frame/browser_caption_button_container_win.cc:47-71` binds `BrowserWidget::Minimize/Maximize/Restore` directly | `kUserExplicit` | These call `Widget::Minimize()` etc. with **no** command id, so a `#if`-scoped menu check in the command controller will not cover them. Scope must be at the button callback or the `BrowserWidget` call. |
| Caption buttons (system-drawn frame) | `ui/views/win/hwnd_message_handler.cc:3362-3366` (`SetMsgHandled(FALSE)`), then `WM_SYSCOMMAND` at `:3001`/`:3080` | `kUserExplicit` | Same `WM_SYSCOMMAND` channel as automation via `ExecuteSystemMenuCommand` (`:1711`); cannot be distinguished without a message-source classifier. |
| Extension `chrome.windows` API | `chrome/browser/extensions/api/tabs/tabs_api.cc:1003`, `:1573`, `:2378`; focus path `:1718-1724` | `kExtension` | Already implemented on macOS; on Windows the scopes and includes are compiled out. |
| Extension `chrome.tabs.create` | `tabs_api.cc:2378-2411` (`OnBrowserWindowCreatedWithOrigin`) | `kExtension` | async continuation origin capture only on macOS. |
| Web page `window.open` focus / `ActivateContents` | `chrome/browser/ui/browser_web_contents_delegate/browser_web_contents_delegate.cc:758` | `kWebPage` (unattributable today → `kUnknown`) | deliberately not opened; header comment `browser_cli_presentation_policy.h:52-56` names this. |
| CLI `show` | `chrome/browser/browser_cli/user_explicit_show.h:18`, classifier `user_explicit_show.cc:13-33` | `kUserExplicit` | producer exists and is cross-platform pure logic; only the handler `.mm` is macOS. |

### 3.1 Why WM_ACTIVATE / GetForegroundWindow must NOT be USER_EXPLICIT

1. `WM_ACTIVATE` is generated by the OS for **every** activation, including `::SetForegroundWindow` calls Chromium itself makes in `HWNDMessageHandler::Activate` (`ui/views/win/hwnd_message_handler.cc:895`) and `Deactivate` (`:902`), and by other applications stealing focus. The handler (`:1274-1277` -> `PostProcessActivateMessage` `:1609`) cannot see who initiated it.
2. `GetForegroundWindow()` is used at `hwnd_message_handler.cc:1645` purely to find the "window losing activation" for fullscreen bookkeeping — it is OS state, not provenance.
3. The policy already classifies `kUnknown` as automation (`browser_cli_presentation_policy.cc:96-108`) precisely because unattributed requests must not gain powers. `WM_ACTIVATE` arrives *after* `::ShowWindow`/`::SetForegroundWindow` have already made the window visible and focused, so treating it as USER_EXPLICIT would (a) grant HUMAN ownership retroactively to a window automation just presented, and (b) allow any third-party process that calls `SetForegroundWindow(chrome_hwnd)` to mint HUMAN ownership for a window.
4. Windows focus stealing is not a user gesture: `::AllowSetForegroundWindow` (`chrome/browser/win/chrome_process_finder.cc:81`) explicitly lets one process hand foreground rights to another; any process can do this for itself.
5. The macOS analogue is documented and enforced: on macOS the user gesture enters only via AppKit dispatch (`chrome/browser/app_controller_mac.mm:1782-1794`) and Dock reopen (`:1980-2007`), never via `NSWindowDidBecomeKey`. Windows has no equivalent "the shell called us" signal that is unforgeable.

Conclusion: on Windows, `WM_ACTIVATE`/`WM_NCACTIVATE`/`GetForegroundWindow` may be used only as *observation* (log the native side effect and window id), never as a producer of `kUserExplicit`. A genuine Windows USER_EXPLICIT producer requires an in-process, unforgeable signal: the CLI show switch on a *pure* command line (`user_explicit_show.cc:13-33`), or a scoped Views menu/button callback. Taskbar/Start-menu activation cannot be produced in-process at all; the honest options are (a) treat it as `kUnknown`/deny and document it, or (b) add an OS-level handshake (see section 6).

---

## 4. Build wiring gaps

### 4.1 `ui/views/BUILD.gn`

- Shared policy headers listed in `component("views")` public headers are only `ui/views/BUILD.gn:341-342` (`browser_cli_foreground_lease.h`, `browser_cli_foreground_origin.h`). `browser_cli_presentation_policy.h` is **not** in `public`; it appears only in the source_set at `:1076`. Includes still compile because `check_includes_strict` is off for this target (`:1048` sets it to `CHECK_INCLUDES_STRICT_PUBLIC_INCLUDES_PRIVATE`, which is `false` at `build/strict_deps_failures.gni:21`) and `-I../..` is on every command line (see the `include_dirs` in `out/WinDev/obj/ui/views/browser_cli_presentation_policy.ninja`). A future strict-includes change would break it; the header should be added to `public` next to `:341-342`.
- `component("views")` deps include the policy source_set unconditionally: `:630` `":browser_cli_presentation_policy"`, and the target itself is defined unconditionally at `:1071-1082`. So **the policy is already in the Windows graph**. Verified empirically: `out/WinDev/obj/ui/views/views.ninja` links `obj/ui/views/browser_cli_presentation_policy/browser_cli_presentation_policy.obj` and `.../browser_cli_foreground_lease.obj`.
- **Export macro defect (W-02 / matrix 2.2 item 1), confirmed with the generated ninja files:** the source_set is compiled **without** `VIEWS_IMPLEMENTATION` (`out/WinDev/obj/ui/views/browser_cli_presentation_policy.ninja` line 1 contains `-DCOMPONENT_BUILD` but not `-DVIEWS_IMPLEMENTATION`), while `views` itself has it (`out/WinDev/obj/ui/views/views.ninja` line 1: `-DVIEWS_IMPLEMENTATION`, `-DCOMPONENT_BUILD`). Under component build on Windows, `ui/views/views_export.h:14-18` expands `VIEWS_EXPORT` to `__declspec(dllimport)` in that TU, so defining `BrowserCliPresentationPolicy::*` and `AllowProgrammaticWindowChangeFromCurrentOrigin` there is ill-formed (C2491). This has not been observed because no policy object has been compiled yet (`out/WinDev/obj/ui/views/browser_cli_presentation_policy/` does not exist; only 7626 objects, none of them policy).
- The Windows show/activate implementation files are under `if (is_win)` inside `component("views")`: `ui/views/BUILD.gn:797-845` (`win/hwnd_message_handler.cc` `:818`, `win/fullscreen_handler.cc` `:817`, `win/scoped_fullscreen_visibility.cc` `:824`), and desktop aura under `if (enable_desktop_aura)` at `:927-946` with the Windows host at `:948-960` (`desktop_window_tree_host_win.cc` `:960`). There is **no** `is_win`-specific guard that excludes the browser_cli sources; only the macOS host `.mm` is `is_mac`-guarded (`:731-795`, host source at `:753`).

### 4.2 `chrome/browser/BUILD.gn`

```
1508:  if (is_mac) {
...
1535:      "//chrome/browser/browser_cli:foreground_assist",
1536:      "//chrome/browser/browser_cli:foreground_assist_presentation",
1537:      "//chrome/browser/browser_cli:reopen_window_selection",
1538:      "//chrome/browser/browser_cli:user_explicit_presentation",
```
(`chrome/browser/BUILD.gn:1508`, deps at `:1535-1538`.)

So `source_set("core")` (defined at `:831`) does not compile or link any browser_cli target on Windows. The pure C++ targets `:user_explicit_show`, `:foreground_assist`, `:foreground_assist_controller`, `:reopen_window_selection` are not in the Windows graph. Verified against the generated graph: `rg -c user_explicit_show out/WinDev/build.ninja` = 0, and likewise 0 for `foreground_assist` and `reopen_window`. Only the two `ui/views` policy test targets and the policy source_set appear (`browser_cli_presentation_policy`, `browser_cli_presentation_policy_unittests`, `browser_cli_foreground_lease_unittests`).

`chrome/browser/browser_cli/BUILD.gn` has exactly one platform guard:
```
103:if (is_mac) {
104:  source_set("user_explicit_presentation") {
...
123:  source_set("foreground_assist_presentation") {
```
(`chrome/browser/browser_cli/BUILD.gn:103-136`.) Lines 7-101 (`reopen_window_selection`, `user_explicit_show`, `foreground_assist`, `foreground_assist_controller` and their tests) are unconditional, i.e. they *could* build on Windows today; nothing depends on them there.

`chrome_browser_main.cc` is compiled by `source_set("core")` (`chrome/browser/BUILD.gn:843`), and its browser_cli includes and all producer calls are inside `#if BUILDFLAG(IS_MAC)`: includes `chrome/browser/chrome_browser_main.cc:221-226`; ProcessSingleton arbitration `:562-586`; cold-start sanitizer `:2151-2166`; post-start arbitration `:2169-2193`.

### 4.3 `chrome/browser/ui/BUILD.gn`

There is **no** browser_cli target reference at all (`grep -n browser_cli chrome/browser/ui/BUILD.gn` returns nothing). Browser views sources are under `if (toolkit_views)` (`chrome/browser/ui/BUILD.gn:3113-3115` lists `browser_view.cc` at `:3119`, `browser_widget.cc` at `:3121`) inside `static_library("ui")` (`:1155`). `toolkit_views` is true on Windows (`build/config/ui.gni:32`: `toolkit_views = is_mac || is_win || is_linux || is_chromeos || is_fuchsia`), so `browser_view.cc`'s cross-platform origin-capture code (`browser_view.cc:1613`, `:1741`) does compile on Windows today — only the `SetBounds` gate (`:1694-1705`) is `#if BUILDFLAG(IS_MAC)`.

`chrome/browser/ui` already has `//ui/views` as a public dep on Windows (`chrome/browser/ui/BUILD.gn:4292-4298`, and `:4279` for non-Android). So a Windows call to `views::AllowProgrammaticWindowChangeFromCurrentOrigin` from `browser_view.cc` needs no new GN dependency — only a Windows definition of the free function.

### 4.4 `chrome/browser/extensions/BUILD.gn`

`api/tabs/tabs_api.cc` (`:175`) and `browser_extension_window_controller.cc` (`:198`) are in `source_set("extensions")` (`:42`), compiled inside `if (enable_extensions)` (`:764`) and gated for the `//ui/views` dep by:
```
1224:    if (toolkit_views) {
1225:      deps += [ "//ui/views" ]
1226:    }
```
(`chrome/browser/extensions/BUILD.gn:1224-1226`.) `toolkit_views` is true on Windows, so **the dependency already exists**; the reason the origin scopes are macOS-only is only the `#if BUILDFLAG(IS_MAC)` in the .cc (`tabs_api.cc:82-84` include, `:1003-1006`, `:1573-1576`, `:2378-2411`; `browser_extension_window_controller.cc:34-36`, `:153-166`). The comment at `tabs_api.cc:1000-1002` ("other platforms do not compile ui/views into this target") is **stale** at this revision given `:1224-1226`.

### 4.5 `components/remote_cocoa/app_shim/BUILD.gn`

```
141:test("browser_cli_managed_presentation_unittests") {
142:  sources = [ "browser_cli_managed_presentation_unittests.mm" ]
...
153:  frameworks = [ "Cocoa.framework" ]
154:}
```
(`components/remote_cocoa/app_shim/BUILD.gn:141-154`.) This is **not** wrapped in `if (is_mac)`, while the two sibling components are (`:17 assert(is_mac)` and `:108 assert(is_mac)`), and `import("//testing/test.gni")` was added at `:6`.

Empirically this target is *not* in the current Windows graph: `grep -c browser_cli_managed_presentation_unittests out/WinDev/build.ninja` = 0 and `grep -c remote_cocoa out/WinDev/build.ninja` = 0. Reason: `//components/remote_cocoa/app_shim` is only referenced from `is_mac` blocks elsewhere (`ui/views/BUILD.gn:783`, `:1250`, `:1599`, `:1776`; `content/browser/BUILD.gn:2763`; `chrome/test/BUILD.gn:3192`, `:8983`; `ui/shell_dialogs/BUILD.gn:137`, `:189`; `components/user_education/views/BUILD.gn:62`; `chrome/browser/ui/webui_browser/BUILD.gn:163`; `chrome/browser/image_editor/BUILD.gn:43`), and `components/BUILD.gn:941` only references it inside `if (is_mac)` (`:938`). So `gn gen` succeeds on Windows only because nothing reaches this directory. `gn_all` does not reference it.

Residual risk: the directory is still *parseable*, and any future unconditional dep (or `gn gen` of `//components/remote_cocoa/app_shim` directly) would evaluate `frameworks` on a non-Apple toolchain. `frameworks` is consumed only in the Apple toolchain (`build/toolchain/apple/toolchain.gni:649`, `:694`, `:722`); on the Windows toolchain it is an unused-but-legal target variable, so this is likely a warnings/no-op rather than the hard GN error the matrix (2.2 item 2) predicted. **This contradicts the matrix assumption and should be re-verified by running `gn gen --check` on the directory**, which this read-only task cannot do.

### 4.6 Other build/export facts

- `ui/views` is a `component()` with `defines = [ "VIEWS_IMPLEMENTATION" ]` (`ui/views/BUILD.gn:627`); the policy `source_set` has no such define (`:1071-1082`). See 4.1.
- `is_component_build` defaults to true for debug non-mobile builds (`build/config/BUILDCONFIG.gn:171-172`), and the existing `out/WinDev/args.gn` sets `is_component_build = true` explicitly.
- Policy unit tests are unconditional: `test("browser_cli_presentation_policy_unittests")` `ui/views/BUILD.gn:1299-1308` and `test("browser_cli_foreground_lease_unittests")` `:1310-1319`, and both **are** in the Windows graph (`grep -c` = 6 each), meaning they can be built as soon as the dllimport defect in 4.1 is fixed. The tests are not gated by `is_mac`.
- The other browser_cli tests (`user_explicit_show_unittests`, `foreground_assist_unittests`, `foreground_assist_controller_unittests`, `reopen_window_selection_unittests`) are defined unconditionally in `chrome/browser/browser_cli/BUILD.gn:15`, `:62`, `:76`, `:92` but are absent from the Windows graph because nothing depends on the directory.
- `mac_app_icon_dir` GN arg and `_mac_app_icon_dir` are inside `} else if (is_mac) {` (`chrome/BUILD.gn:488`, arg declared at `:80`, uses at `:495-498`, `:678`, `:683`), so they do not affect Windows.
- `chrome/browser/BUILD.gn:4205-4222` adds `//ui/views` to `core_unit_tests` only inside `if (is_mac)`; a Windows browser_cli browser test would need its own dep.

---

## 5. Proposed patch 015 scope (minimal first Windows increment)

Goal: make the shared policy **reachable and honest** on Windows without inventing a Windows-only decision and without pretending the port is done. Fail closed everywhere; no new user-visible behavior is claimed.

### 5.1 Build (prerequisite, no behavior)

1. `ui/views/BUILD.gn`
   - Add `widget/browser_cli_presentation_policy.h` to `component("views")` `public` next to `:341-342` (so Windows consumers do not depend on loose include paths).
   - Fix the export semantics of `source_set("browser_cli_presentation_policy")` (`:1071-1082`). Preferred minimal fix: move the four files into `component("views")` proper and drop the source_set, so `VIEWS_IMPLEMENTATION` applies. Alternative: give the source_set its own export header + `defines`, which is more churn. Either way the unit tests at `:1299`/`:1310` must keep linking.
2. `components/remote_cocoa/app_shim/BUILD.gn:141-154` — wrap `test("browser_cli_managed_presentation_unittests")` in `if (is_mac)`. This is hygiene even though the target is currently unreached on Windows.
3. `chrome/browser/BUILD.gn:1508-1538` — add, outside `if (is_mac)`, `//chrome/browser/browser_cli:user_explicit_show` and `//chrome/browser/browser_cli:reopen_window_selection` to `source_set("core")`; leave `:foreground_assist` too if the controller is not needed yet. Keep `:foreground_assist_presentation` and `:user_explicit_presentation` macOS-only until their Windows `.cc` exists.

### 5.2 Windows gateway skeleton (the actual increment)

New file `ui/views/widget/browser_cli_presentation_win.{h,cc}` inside the `views` component (Windows-only source added under `if (is_win)` at `ui/views/BUILD.gn:797-845`):

- `uint64_t BrowserCliWindowIdForNativeWindow(gfx::NativeWindow)` — `aura::Window*` (`ui/gfx/native_ui_types.h:124`) -> `window->GetHost()->GetAcceleratedWidget()` (`ui/aura/window.h:257`, `ui/aura/window_tree_host.h:215`), then widen to 64 bits (`base::win::HandleToUint32` exists at `base/win/windows_handle_util.h:33-39`; HWNDs are documented as 32-bit-significant, so `static_cast<uint64_t>(HandleToUint32(hwnd))` is sufficient). No new counter, but HWND values are recycled by the OS after `DestroyWindow`, so the gateway must release ownership/lease in `DesktopWindowTreeHostWin::HandleDestroyed` (`desktop_window_tree_host_win.cc:1207`) exactly as the macOS host does at `ui/views/cocoa/native_widget_mac_ns_window_host.mm:337-343`.
- `bool BrowserCliShouldSuppressNativePresentation(HWND)` — queries the shared policy for `kUnknown` + `kShowAndActivate` on that id. Used by `onWindowPosChanging`.
- `bool AllowProgrammaticWindowChangeFromCurrentOrigin(gfx::NativeWindow, BrowserCliPresentationAction)` — **the Windows definition** of the free function declared at `browser_cli_presentation_policy.h:143`. Resolve `Widget::GetWidgetForNativeWindow` (`ui/views/widget/widget.cc:404`) and call `AllowPresentation(CurrentForegroundOrigin(), action, id)`. No policy logic of its own.
- `bool BrowserCliPresentForUserExplicitAction(gfx::NativeWindow)` / `...ForAgentAssistAction(...)` — call `AllowPresentation(kUserExplicit|kAgentAssist, kShowAndActivate, id)` and only on `true` perform the native action (for 015: return the decision without performing anything — see "fail closed").

Minimal production edits:

4. `ui/views/widget/desktop_aura/desktop_window_tree_host_win.cc`
   - `Show` (`:398`): before `content_window()->Show()` / `message_handler_->Show()`, call the shared `AllowPresentation(CurrentForegroundOrigin(), action, id)` where action is `kShowAndActivate` for `kNormal`/`kMaximized`/`kDefault` and `kShowInactive` for `kInactive`. On deny, return without touching the HWND and log (`BROWSER_CLI_PRESENTATION stage=host decision=deny ... native=win-hwnd`).
   - `Activate` (`:529`): same gate with `kShowAndActivate` (because `HWNDMessageHandler::Activate` un-minimizes at `hwnd_message_handler.cc:887`).
   - `Deactivate` (`:537`) -> `kDeactivate`; `Maximize`/`Minimize`/`Restore` (`:556`, `:560`, `:564`) -> matching actions; `SetZOrderLevel` (`:580`) -> `kSetZOrder`; `SetFullscreen` (`:702`) -> `kFullscreen`.
   - `SetBoundsInPixels` (`:806`): **do not** allow `kSetBounds` yet. Ask the policy with `kSetBounds`; if denied, skip. Because the macOS proof (`browser_cli_presentation_policy.cc:233-243`) does not transfer, patch 015 should also make the Windows path call `AllowPresentation` with `kSetZOrder`-equivalent strictness — simplest honest version: on Windows treat an unowned window's `kSetBounds` as `kSetZOrder` (deny) unless `--browser-cli-...` is off. This is a deliberate, documented Windows capability difference, not duplicated logic (it is a platform capability flag passed into the same policy call, or a policy-side `PlatformCanSetBoundsWithoutPresenting()` query added once to the shared policy).
   - `ShowImpl` (`:779`) / `HideImpl` (`:783`): route through the gated `Show`; `Hide` always allowed (`browser_cli_presentation_policy.cc:169-171` returns true when unmanaged, and hiding is permitted by every branch).
5. `ui/views/win/hwnd_message_handler.cc`
   - `OnWindowPosChanging` (`:3109`): after the existing `ScopedFullscreenVisibility` strip at `:3257-3261`, add an equivalent strip for the managed/suppressed case by calling the shared query. This is the only place that can stop an **external** `SetWindowPos(SWP_SHOWWINDOW)`, including the one from `ScopedFullscreenVisibility::~ScopedFullscreenVisibility` (`ui/views/win/scoped_fullscreen_visibility.cc:41`).
   - `OnWindowPosChanged` (`:3270`): observation log only (`stage=native transition=visible`, window id, flags). Do not decide here.
   - `Show` (`:771`): assert/log if reached with a suppressed window (defense in depth), but keep the decision in `DesktopWindowTreeHostWin`.
6. `chrome/browser/browser_cli/` — add a Windows `user_explicit_presentation_win.cc` that implements only `HandleUserExplicitShowRequest` by delegating to the *same* pure classifier (`user_explicit_show.cc:13-33`) and the *same* planner (`:35-49`), and returns `true` (consumed) with a `failed`/`not-implemented-native` log when a presentation would be required. Everything else in that header stays unimplemented on Windows. `chrome/browser/browser_cli/BUILD.gn` gets a matching `if (is_win)` source_set.
7. `chrome/browser/chrome_browser_main.cc` — change `#if BUILDFLAG(IS_MAC)` at `:562-586` and `:2151-2193` to include `IS_WIN`, calling the same `ArbitrateBrowserCliCommandLineIntent` (`foreground_assist.cc:94`) and the Windows `HandleUserExplicitShowRequest`, and using `CommandLineForColdStartStartup` (`:111`) for the Windows cold start. This is the highest-value silence fix (W-04): without it, Windows never even classifies the command line.

### 5.3 Fail-closed defaults patch 015 must state explicitly

- If no Windows gateway knows how to present, `PresentForUserExplicitAction`-equivalent returns `false` (not `void`, not `true`) and logs `result=failed reason=windows-gateway-not-implemented`. Never fall through to `ShowWindow`.
- If `BrowserCliShouldSuppressNativePresentation` cannot resolve an id (null `aura::Window*`, null host, non-top-level widget), it returns `true` (suppress) for managed mode and `false` only when `BrowserCliPresentationPolicy::Get()->IsManagedModeEnabled()` is false. Rationale: mirror `browser_cli_presentation_policy.cc:169-171`, where unmanaged mode is the only "allow everything" state.
- `kSetBounds` on Windows is denied for unowned windows until there is a proof (see above). `kRestore`/`kMaximize`/`kFullscreen`/`kSetZOrder` remain denied for unowned windows exactly as the shared policy already does (`browser_cli_presentation_policy.cc:244-250`).
- No new `ScopedForegroundOrigin` producers on Windows in 015 except the command-line producers, because every in-process Windows user entry point first needs the origin mapping from section 3 (deferred).
- No `WM_ACTIVATE`/`GetForegroundWindow` producer. Add a comment + a unit test asserting the policy sees `kUnknown` there.

### 5.4 Unit tests that would prove 015

Pure and runnable without a desktop where possible:

1. `ui/views/widget/browser_cli_presentation_policy_unittest.cc` (existing, `ui/views/BUILD.gn:1299`): add the Windows capability matrix as policy tests, not platform tests. Introduce one `BrowserCliPlatformCapabilities` value (or one query function) in the shared header and assert: on a "Windows-like" capability value, `kSetBounds` is denied on an unowned window; on the "macOS-like" value it is allowed. This keeps one decision matrix.
2. New `ui/views/widget/browser_cli_presentation_win_unittest.cc` (added only under `if (is_win)` in `ui/views/BUILD.gn`, in the same place as `:1650-1653`): with `BrowserCliPresentationPolicy::SetManagedModeEnabledForTesting(true)` (`browser_cli_presentation_policy.h:277`), create a `Widget` via `DesktopWidgetTest` (pattern at `ui/views/widget/desktop_aura/desktop_window_tree_host_win_unittest.cc:32-55`) and assert:
   - `BrowserCliShouldSuppressNativePresentation(hwnd)` is true for an unowned managed window;
   - after `AllowPresentation(kUserExplicit, kShowAndActivate, id)` the same query is false and `IsWindowVisible(hwnd)` transitions only then;
   - `kAgentAssist` without a lease still returns false from `AllowPresentation`;
   - an unresolvable window id suppresses (fail closed).
3. `ui/views/widget/browser_cli_presentation_win_unittest.cc` external-`SetWindowPos` test: call `::SetWindowPos(hwnd, nullptr, 0,0,0,0, SWP_SHOWWINDOW | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)` directly, pump messages, and assert `IsWindowVisible` is still false. This is the direct analogue of `components/remote_cocoa/app_shim/browser_cli_managed_presentation_unittests.mm:40-60`.
4. `chrome/browser/browser_cli/user_explicit_show_unittest.cc` (exists, `chrome/browser/browser_cli/BUILD.gn:62`) — already platform-free; just ensure it is in the Windows graph via 5.1 item 3. Add one case asserting the Windows producer returns `false` from `PlanUserExplicitShow` when managed mode is off (`user_explicit_show.cc:42-46`), which is what makes the "no new behavior in 015" claim testable.
5. A `chrome_browser_main` level test is **not** proposed for 015 (would need a full browser test); instead assert by symbol/string: `ArbitrateBrowserCliCommandLineIntent` and `BROWSER_CLI_USER_EXPLICIT_SHOW` must appear in the Windows binary (matrix 2.3 requirement, `windows-wiring-matrix-2026-10-22.md:91`).

### 5.5 Deliberately deferred

- `foreground_assist_presentation` Windows implementation (lease request/release/timeout delegate) — needs the Windows gateway id and hide path first. macOS reference: `foreground_assist_presentation.mm:42-102`.
- `user_explicit_presentation` Windows implementation of `PresentBrowserForUserExplicitAction` / `PresentNativeWindowForUserExplicitAction` / `PresentExistingWindowForUserExplicitAction` (`user_explicit_presentation.h:24-39`) — 015 only consumes the request.
- All Windows user-entry producers: Views `AppMenu` (`app_menu_model.cc:1283`), system menu (`system_menu_model_delegate.cc:80`), caption buttons (`browser_caption_button_container_win.cc:47-71`), History menu, Omnibox Everywhere `omnibox_everywhere_service.cc:294-302`. Each needs the section-3 mapping and a human-gesture argument.
- Extension origin scopes on Windows (`tabs_api.cc:1003`, `:1573`, `:2378`; `browser_extension_window_controller.cc:153`). The GN dep already exists (4.4); this is a follow-up patch that must be validated against `kHuman` windows, not part of 015 because it *changes* behavior (it starts denying/failing extension calls on Windows).
- Taskbar / Start-menu trusted provenance. Cannot be done in-process; see section 6.
- Session restore `kSessionRestore` scoping (`session_restore.cc:500`, `:562`, `:1363`, `:1418`) and tab restore (`browser_live_tab_context.cc:158`).
- `ScopedFullscreenVisibility` full redesign (`scoped_fullscreen_visibility.cc:36-47`) beyond the `SWP_SHOWWINDOW` strip.
- Windows args/preflight/signing (W-06), Profile/OSCrypt (W-09), background rendering/gate C (W-08).
- `reopen_window_selection.{h,cc}` on Windows: it is pure logic, but its input model is AppKit focus (`reopen_window_selection.h:50-85`); a Windows taskbar "reopen" does not exist as an in-process event, so do not wire it in 015.

---

## 6. Risks / unknowns

1. **`kSetBounds` Windows semantics are unproven.** The shared policy allows `kSetBounds` on unowned windows based on a macOS chain only (`browser_cli_presentation_policy.cc:233-243`). On Windows `SetBounds` -> `HWNDMessageHandler::SetBoundsInternal` -> `::SetWindowPos(..., SWP_NOACTIVATE | SWP_NOZORDER)` (`hwnd_message_handler.cc:3914`). That specific call has no `SWP_SHOWWINDOW`, so it probably does not show a hidden window — but **not proven** for a managed hidden window, and `OnWindowPosChanging` may rewrite flags (`:3109-3268`). Evidence needed: a Windows unit test using the `DesktopWidgetTest` harness asserting `IsWindowVisible` stays false across `Widget::SetBounds`, plus a manual watcher run.
2. **HWND reuse** makes the HWND value a non-monotonic policy key (whether taken from `base::win::HandleToUint32` at `base/win/windows_handle_util.h:33-39` or from an explicit cast). The macOS host uses a monotonic counter and releases ownership/lease on destruction (`native_widget_mac_ns_window_host.mm:337-343`). Windows must do the equivalent in `DesktopWindowTreeHostWin::HandleDestroyed` (`desktop_window_tree_host_win.cc:1207`) or `~DesktopWindowTreeHostWin`; otherwise a recycled HWND inherits HUMAN ownership. Evidence needed: a test that destroys a HUMAN window, creates a new widget, and asserts `IsUserExplicitOwned` is false.
3. **Whether `gn gen` actually rejects `frameworks = [ "Cocoa.framework" ]` on a Windows host** could not be determined from source alone; `frameworks` is only consumed by the Apple toolchain (`build/toolchain/apple/toolchain.gni:649`, `:694`, `:722`). The current Windows graph does not contain the target at all (verified empirically), which is why `gn gen` succeeds. Evidence: `gn gen --check` on a Windows output dir with an artificial dep on `//components/remote_cocoa/app_shim:browser_cli_managed_presentation_unittests`, or `gn desc` of that target with `--os=win`.
4. **Whether the Windows build currently links `ui_views.dll` at all** is unknown: `out/WinDev` has 7626 `.obj` files and several DLLs, but `ui_views.dll` is absent and `grep -c remote_cocoa out/WinDev/build.ninja` is 0. The matrix records a blocked toolchain (`windows-wiring-matrix-2026-10-22.md:125-137`). Any claim that patch 015 "builds on Windows" must wait for the SDK 10.0.28000.0 / ATL toolchain.
5. **`VIEWS_EXPORT` + source_set is confirmed by generated ninja but not by a compiler error**, because the policy objects were never compiled. The exact failure mode (`C2491` vs a silent local definition) must be confirmed once the toolchain works. Evidence: compile `out/WinDev/obj/ui/views/browser_cli_presentation_policy/browser_cli_presentation_policy.obj`.
6. **The real provenance of a taskbar click is not observable in-process.** The shell calls `ShowWindow`/`SetForegroundWindow` on the HWND; Chromium only sees `WM_ACTIVATE`/`WM_WINDOWPOSCHANGED`. If the product requires taskbar click to be USER_EXPLICIT, the options are: (a) accept `kUnknown` and document that the user's taskbar click on a hidden managed window is impossible (the shell cannot show a hidden window's thumbnail), (b) install a `WH_SHELL`/`SetWinEventHook(EVENT_SYSTEM_FOREGROUND)` hook and treat only a foreground change to *this* process's HWND that follows a taskbar `WM_ACTIVATE` as a user hint — still forgeable by any process with `SetForegroundWindow` rights, or (c) require the launcher/CLI to send the explicit show switch. This is a product decision (matrix W-07, `windows-wiring-matrix-2026-10-22.md:112`), not something source can answer.
7. **Whether `WM_SHOWWINDOW` arrives before or after `WM_WINDOWPOSCHANGING` for all external show paths** was not determined. `ui/views/win/hwnd_message_handler.cc` has no `WM_SHOWWINDOW` handler (`grep` finds zero occurrences in `ui/views`), and the message map (`ui/views/win/hwnd_message_handler.h:480-529`) does not list it. Relying on `WM_WINDOWPOSCHANGING` alone may miss the `ShowWindow(SW_SHOWNORMAL)` path (DefWindowProc generates `WM_SHOWWINDOW` + `WM_WINDOWPOSCHANGING` together, but ordering per path is not proven). Evidence: `spy`/`OutputDebugString` trace of the message order for `ShowWindow`, `SetWindowPos(SWP_SHOWWINDOW)`, and taskbar activation.
8. **`ScopedFullscreenVisibility` can re-show a window without any `Widget` call** (`ui/views/win/scoped_fullscreen_visibility.cc:41`). If 015 only gates `DesktopWindowTreeHostWin`, fullscreen transitions remain a bypass. The `SWP_SHOWWINDOW` strip in `OnWindowPosChanging` should catch the `ShowWindow` path too only if the window is in the `full_screen_windows_` map; a managed window that is not fullscreen is not covered. Evidence: a test enabling fullscreen on a suppressed window and sampling `IsWindowVisible`.
9. **`BrowserView::Show` calls `browser_widget_->Activate()` when already visible** (`browser_view.cc:1639`), i.e. a repeated automation `Show()` becomes an `Activate()` with no re-check at the `BrowserView` level. On Windows the gate must be in the host, not in `BrowserView`, or a second `Show()` will focus a `kUnknown` window if `Activate` is not gated.
10. **`BrowserView::SetBounds` is gated only on macOS** (`browser_view.cc:1694`), and `BrowserView::RequestFullscreen` (`browser_view.cc:5577`) has no gate at all; on Windows fullscreen is reached via `FullscreenController` -> `BrowserView::RequestFullscreen` -> `browser_widget_->SetFullscreen`. So a Windows `kFullscreen` gate must live in `DesktopWindowTreeHostWin::SetFullscreen` (`desktop_window_tree_host_win.cc:702`), not in `browser_view.cc`, unless `browser_view.cc` is also changed.
11. **Duplicated `//ui/views` dependency layers**: both `chrome/browser/ui/BUILD.gn:4279` and the `extensions` target at `:1224-1226` make ui/views available, so adding Windows origin scopes is a source-level change only. But `chrome/browser/ui/BUILD.gn` has no browser_cli reference whatsoever, so any shared helper (e.g. the Windows id resolver) must be exported from `ui/views`, not from `chrome/browser/ui`, or the macOS rule "one shared implementation" is violated.
12. **Not verifiable here:** whether the macOS-only `allow_circular_includes_from` arrangement (`chrome/browser/ui/BUILD.gn:4000-4006` for `browser_desktop_window_tree_host_win_impl`) permits including a new `ui/views` header from `browser_desktop_window_tree_host_win.cc`. `:3994-3996` already deps on `//ui/views`, so it should; needs a compile.
