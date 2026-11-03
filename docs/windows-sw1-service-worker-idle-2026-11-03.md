# SW-1: WinRelease service-worker `SetIdleDelay()` DCHECK

Status: **root-caused and fixed in proposed patch 037; WinRelease 20-minute regression pending**.

## 1. Symptom

The 2026-11-02 WinRelease long-run failed at 570.1 s:

```
FATAL:third_party/blink/renderer/modules/service_worker/service_worker_event_queue.cc:185
DCHECK failed: HasScheduledIdleCallback()
```

Stack (WinRelease): `ServiceWorkerEventQueue::SetIdleDelay` ←
`ServiceWorkerGlobalScope::SetIdleDelay` ← `ServiceWorkerStubDispatch::Accept`
(Mojo from the browser process). The extension-renderer PID 9308 died; the w5
extension page target reported `Inspector.targetCrashed` 8.6 s later.

## 2. First abnormal state

After `ServiceWorkerGlobalScope::AddKeepAlive(seq)` on the worker thread:

| field | value |
| --- | --- |
| `all_events_` / `queued_online_events_` | empty |
| `num_of_stay_awake_tokens_` | 0 |
| `did_idle_timeout_` | false |
| `last_no_inflight_event_time_` | null (`TimeTicks()`) |
| `idle_callback_handle_` | inactive |
| `is_ready_for_processing_events_` | true |

`HasInflightEvent() == false`, `did_idle_timeout() == false`,
`HasScheduledIdleCallback() == false`. The later `SetIdleDelay` assumes this
combination cannot exist and trips the DCHECK.

## 3. Root cause

`ServiceWorkerGlobalScope::AddKeepAlive()` called
`event_queue_->ResetIdleTimeout()` directly. `ResetIdleTimeout()` cancels the
idle callback, clears `did_idle_timeout_` and `last_no_inflight_event_time_`,
but does **not** re-arm. It only increments no stay-awake token, so
`HasInflightEvent()` stays false.

The reachable interleaving:

1. `Target.setAutoAttach` (unfiltered) attaches a DevTools session to the
   extension service worker → `devtools_attached=true`.
2. The worker's idle timer fires and sends `RequestTermination`.
3. An extension event starts an external request →
   `ServiceWorkerVersion::StartExternalRequest` → `AddKeepAlive(N+1)`.
4. The renderer processes `AddKeepAlive` → first abnormal state above.
5. The browser handles the stale `RequestTermination`; because DevTools is
   attached it sends `SetIdleDelay(30 s)` (`service_worker_version.cc:663-681`).
6. Renderer `SetIdleDelay` → `DCHECK(HasScheduledIdleCallback())`.

In a non-DCHECK build the same state produces an immediate spurious idle
timeout/termination because `last_no_inflight_event_time_ + delay` is in the
past. The DCHECK is correct; the state transition was wrong.

This is upstream Blink code (`crbug/1399324` TODOs already mark the same
RAII/keepalive gap). It is not introduced by A2/GPU/P1; those files are
untouched. The `Target.closeTarget` stop path is not causal: a run with
**no stop at all** and SW DevTools attached hit the same DCHECK at 374.4 s.

## 4. Fix

Patch `037-sw1-service-worker-keepalive-idle.patch`:

- `AddKeepAlive()` creates and holds a
  `ServiceWorkerEventQueue::StayAwakeToken` (once) instead of calling a bare
  `ResetIdleTimeout()`. The token cancels the idle callback **and** keeps
  `HasInflightEvent()` true, so `SetIdleDelay` returns safely.
- `ClearKeepAlive()` releases the token; the token destructor calls
  `OnNoInflightEvent()` and re-arms the idle timer. A fallback preserves the
  old `ResetIdleTimeout()+CheckEventQueue()` behavior if no token exists.
- No DCHECK is removed or relaxed; no periodic reload or keepalive workaround;
  normal termination/resource release is unchanged (token is released by the
  same browser keepalive completion signal).

## 5. Isolation evidence

Agent B matrix (WinRelease, fresh profiles, probe extension):

| Scenario | SW DevTools attached | Stop | Result |
| --- | --- | --- | --- |
| baseline_attached | yes | none | **FATAL 374.4 s** (same DCHECK) |
| close_attached | yes | `Target.closeTarget` @8.6 s | **FATAL 374.6 s**; worker replaced cleanly |
| close_filtered | no | `Target.closeTarget` | **clean 1201.7 s**, 0 FATAL, 20 instances |
| baseline_filtered | no | none | **clean 1201.6 s**, 19 instances |
| idle_wake_filtered | no | natural 30 s idle + event wake | clean; natural termination at 50.9 s, new instance at 50.93 s |
| stopworker_page | no | `ServiceWorker.stopWorker(versionId=0)` | clean stop/replace |
| stopall_page | no | `ServiceWorker.stopAllWorkers` | clean stop/replace |
| baseline_attached busy | yes | none; 1 Hz traffic | clean 722 s (no idle cycle) |
| native host restart + attached | yes | close + host disconnect/restart | clean; host PIDs 46132→36400, new host 4296 |

Conclusion: `Target.closeTarget` is not causal; DevTools-attached idle cycles
are. The same stop path is clean when the SW has no DevTools session.

Agent C comparison: stock Chromium snapshots 156.0.8067.0 (r1701528/r1701550)
compile the DCHECK out and cannot be used as a DCHECK-positive control; official
Chrome 156.0.8078.48 refuses `--disable-extensions-except` for the test
extension. Browser CLI's custom patches do not touch this code; the state
machine matches upstream Chromium. The failure is an upstream debug-build race,
exposed by the harness's unfiltered auto-attach.

## 6. Regression

Artifact: `D:/c/src/out/WinRelease/chrome.exe`, final A2 + SW-1 build
(`chrome.dll` sha256 `15197e207d4de6f62a51ae8e437b40076000d5c6d74ab6c1e808b474bc3b270e`),
no P1 code.

- **WinRelease 20-minute release-like gate** (`D:/c/logs/sw1-winrelease-20m`):
  1185.0 s, 8 windows, filtered autoAttach (`sw_devtools_attach=false`).
  `fatal-log=0`, `target-crashed=0`, all crash counters 0; SW restart PASS
  (new target ID); hidden/presentation/ownership/freshness/resources PASS.
  Verdict `BLOCKED` only because this gate has no Native Messaging Host
  process; Host restart is covered separately by BrowserPilot PR #7.
- **WinRelease DevTools-attached stress** (`D:/c/logs/sw1-winrelease-attached-fixed`):
  `baseline_attached` 600 s with SW DevTools attached (the path that crashed at
  374.4 s before the fix), exit 0, 0 FATAL, worker still attached.
- **Natural idle/wake** (`D:/c/logs/sw1-idle-wake-fixed`): filtered
  `idle_wake_filtered` 120 s, exit 0; worker terminated naturally after the idle
  timeout and a real page message started a new instance. The token does not
  leak or block termination.
- **Repeated restart pressure** (`D:/c/logs/sw1-repeat-restart-fixed`):
  `close_filtered` 240 s with `Target.closeTarget` every 20 s (13 restart
  events, 7 distinct worker instances), exit 0, 0 FATAL, final target empty.
- WinDev with the fix + unfiltered autoAttach: 690 s, 0 FATAL, 0 target
  crashes, SW restart PASS.
- The long-run harness now excludes `service_worker` from
  `Target.setAutoAttach` by default (release-like: no DevTools session on the
  worker). `BROWSER_CLI_LONGRUN_SW_DEVTOOLS=1` enables the upstream-watch
  reproduction with SW DevTools attached.
- A2 unit tests `LayerTreeView*` 28/28 still pass; release matrix and P1/Host
  regression are recorded in the RC docs after this run.

## 7. Single token vs. observed keepalive sequence: final audit

`observed_keepalive_sequence_number_` and `keep_alive_token_` serve different
purposes:

- `observed_keepalive_sequence_number_` is the last browser-issued sequence
  number. The renderer returns it in `RequestTermination`; the browser rejects
  stale replies (`has_stale_keepalive_sequence_number`).
- `keep_alive_token_` is the renderer-side lifetime guard. The browser may call
  `AddKeepAlive` for overlapping external requests, but it calls
  `ClearKeepAlive` only when `!HasWorkInBrowser()` (both normal finish and
  timeout paths check this). One token therefore represents the whole
  keepalive interval; it is not a per-sequence reference count.

Audited cases:

| case | behavior |
| --- | --- |
| overlapping AddKeepAlive(N), AddKeepAlive(N+1) | token created once; observed sequence advances to N+1; HasInflightEvent stays true; `SetIdleDelay` returns |
| old request completes while newer keepalive is outstanding | browser sees `HasWorkInBrowser()==true`, does not send Clear; token remains |
| duplicate ClearKeepAlive | first Clear releases token and the destructor re-arms; second Clear takes the fallback ResetIdleTimeout+CheckEventQueue path, cancels/schedules once, no double callback |
| worker destruction | `Dispose()` resets `event_queue_`; the token's WeakPtr becomes null, so its destructor is a no-op; no callback into a dead queue |
| DevTools-attached idle/RequestTermination | 600 s WinRelease attached run: 0 FATAL; the token keeps the queue active across the `SetIdleDelay(30 s)` reply |
| natural idle and wake | filtered `idle_wake_filtered`: worker stops after the idle timeout; a real page message creates a new instance |
| repeated restart pressure | 240 s, close every 20 s: 13 restart events / 7 instances, 0 FATAL |

No DCHECK was removed or relaxed. `ClearKeepAlive` still has the old
`ResetIdleTimeout()+CheckEventQueue()` path for a Clear without AddKeepAlive.
