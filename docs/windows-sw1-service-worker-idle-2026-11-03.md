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

- WinDev with the fix + **unfiltered** autoAttach (DevTools attached):
  690 s, 0 FATAL, 0 target crashes, SW restart PASS, all presentation/ownership
  scenarios except the known WinDev-only producer-log check.
- WinRelease with the fix: 20-minute release-like run and DevTools-attached
  stress recorded below (to be filled by the acceptance run).
- The long-run harness now excludes `service_worker` from
  `Target.setAutoAttach` by default (release-like: no DevTools session on the
  worker). `BROWSER_CLI_LONGRUN_SW_DEVTOOLS=1` enables the upstream-watch
  reproduction with SW DevTools attached.
- A2, Host restart and P1 regression results are unchanged.
