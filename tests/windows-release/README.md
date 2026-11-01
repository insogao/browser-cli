# tests/windows-release

Windows **release-shape** validation harness. Everything here is test-only; it
does not change product behavior and does not produce a signed or public
artifact.

Read together with:

- `docs/windows-release-build-2026-10-28.md` (build shape + measured evidence)
- `docs/windows-release-packaging-preflight-2026-10-28.md` (artifact classes)
- `config/windows-release-runtime-flags.env` (runtime feature/flag contract)
- `docs/windows-ime-acceptance-runbook-2026-10-28.md` (real IME acceptance)

## Hygiene (fail closed)

- Every run creates a fresh isolated `--user-data-dir` (`fs.mkdtempSync`) and
  removes only that directory. No real Chrome/Browser CLI profile is read.
- No `--no-sandbox`, `--disable-gpu-sandbox`, `--in-process-gpu`,
  `--single-process`, `--use-mock-keychain`, `--disable-direct-composition`.
  `release-security-compare.mjs` re-checks its own argv against
  `config/windows-release-runtime-flags.env` and fails the run on a finding.
- No global kill. `stop-verified.ps1` stops an explicit PID only when its
  executable path matches, its parent chain reaches the launched root PID, and
  its creation time is not before the launch. Never `taskkill /IM`,
  `Stop-Process -Name`, or a sweep over other browsers.
- `release-security-compare.mjs` uses `--headless=new` **only** to measure the
  no-window security matrix. That is not product launch configuration and is
  never cited as TSF/IME evidence.
- `tsf-smoke.mjs` intentionally shows one window because real Sogou TSF input
  cannot be automated headlessly. It brings only the harness-owned PID's window
  to the foreground, restores the previous foreground window, and never injects
  text through CDP (`Input.insertText` / paste / value writes are bypasses).

## Scripts

| file | purpose |
| --- | --- |
| `harness/release-security-compare.mjs` | Matrix: WinRelease / WinDev / official Chrome × default / `NetworkServiceSandbox`; process mitigations, IL/AppContainer, module counts + per-module Authenticode, CIG-at-birth timing, startup timing, page load. |
| `harness/process-mitigations.ps1` | Read-only snapshot of a root PID + descendants: every `GetProcessMitigationPolicy` index, token IL/AC, module surface, signature status of every module and the exe/dll, PE flags. |
| `harness/watch-child-mitigations.ps1` | Started before the browser; records each child's first observed CIG/ACG raw value and whether CIG appeared later (delayed mitigation). |
| `harness/tsf-smoke.mjs` | Real Sogou TSF composition in input/textarea/contenteditable, backspace, Sogou module presence, process/network-sandbox snapshot, screenshot. |
| `harness/tsf-input.ps1` | Foreground the harness-owned HWND (PID verified), send real `WScript.Shell.SendKeys`, restore the previous foreground window. |
| `harness/stop-verified.ps1` | Explicit-PID cleanup with exe + ancestry + creation-time verification. |
| `harness/artifact-preflight.ps1` | Read-only packaging/signing/update preflight; never signs. |
| `fixtures/tsf-smoke.html` | Composition/instrumentation fixture (no external resources). |

## Reproduce

```bash
# Security matrix (after out/WinRelease exists). Use --repeat=3 for startup noise.
node tests/windows-release/harness/release-security-compare.mjs --repeat=3
node tests/windows-release/harness/release-security-compare.mjs \
  --scenarios=winrelease-default,winrelease-netsandbox

# Real TSF/IME smoke (shows one window; restores the previous foreground)
node tests/windows-release/harness/tsf-smoke.mjs

# Packaging/signing/update preflight (read-only; internal class expected)
bash scripts/preflight-windows-release.sh --checkout D:/c/src \
  --artifact D:/c/src/out/WinRelease --mode internal
```

Evidence defaults: `D:/c/logs/winrelease/matrix/` and
`D:/c/logs/winrelease/tsf/` (override with `WINDOWS_RELEASE_EVIDENCE` /
`WINDOWS_RELEASE_TSF_ROOT`).

## Interpreting CIG-at-birth

`firstCigPresentAtBirth=true` means the first successful
`GetProcessMitigationPolicy(ProcessSignaturePolicy)` sample already carried
CIG, i.e. the mitigation was applied at CreateProcess time
(pre-start/proc-thread attribute). `false` + `cigAppliedLater=true` means the
child applied it itself after loading its modules (delayed mitigation). WMI
polling has a ~10-40 ms round trip; a shorter 0-window cannot be observed and
must not be claimed. Module counts + per-module signature status are the
decisive cross-check for which DLLs actually loaded before/under CIG.
