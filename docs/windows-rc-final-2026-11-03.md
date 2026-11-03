# Browser CLI Windows internal RC (001-038)

Status: **internal unsigned test build; NOT a public release**. `releasable=false`.

## Artifact

- Build: non-component `D:/c/src/out/WinRelease`, unified patches 001-038.
- Product version: `156.0.8067.0` (Browser CLI internal RC).
- `chrome.dll` sha256: `a848a1c7a99db7f6b7dc9d8e547c911d71933f50b74cd0cf0dab183a3a308d2c`
  (unified 001-038 build).
- `chrome.exe` sha256: `0441080d13dcab32a87b66b3e3ee90d0122db5a64ccf8e6ed9ef04a097067bb1`.
- Patches: 001-033 (base/Windows), 034-035 (GPU/DComp), 036 (A2 hidden
  presentation), 037 (SW-1 service-worker keepalive), 038 (P1 native cookie
  import, renumbered from proposed 037).
- `chrome.exe` and `chrome.dll` are **unsigned**. Do not distribute outside the
  internal test group.

## Run (internal)

```bat
D:\work\wt\bc-integration\tools\rc\launch-internal-rc.cmd
```

The launcher uses `%LOCALAPPDATA%\BrowserCLI-RC-Internal` (override with
`RC_PROFILE`), never the user's normal Chrome/Browser CLI profile. It passes
`--enable-features=NetworkServiceSandbox`; it does not pass `--no-sandbox`,
`--headless`, or automation flags. `RC_CHROME` can point at another copy of the
same artifact.

## Install / run steps (internal)

1. Keep the complete `D:/c/src/out/WinRelease` directory together; the launcher
   and all DLL/resource files must stay in that directory.
2. Run `D:\work\wtc-integration	ools
c\launch-internal-rc.cmd`.
3. First launch creates `%LOCALAPPDATA%\BrowserCLI-RC-Internal`; use
   `RC_PROFILE` for a separate internal profile.
4. To validate the artifact, run the unified gate in `## Internal acceptance`.

## Dependencies

- Windows 10/11 x64.
- The `out/WinRelease` directory's DLL/resource set must stay together; do not
  copy `chrome.exe` alone.
- GPU: default out-of-process GPU; GPU driver as required by Chromium 156.
- Optional: Sogou TSF IME for Chinese input; P1 import source uses the
  BrowserPilot extension + Native Messaging host.

## Internal acceptance

```powershell
$env:RC_FINAL_CHROME='D:\c\src\out\WinRelease\chrome.exe'
python D:\work\wt\bc-integration\tests\rc-final\run-final-gate.py
```

The unified gate requires all stages PASS and exits 0:
1. 1200s host/8-window stability with a real Native Messaging Host restart.
2. Release-like 14-case matrix (AI input, Sogou IME, hidden screenshots/OOPIF,
   NetworkServiceSandbox).
3. P1 native approval + CookieManager write + restart.
4. SW DevTools-attached stress (SW-1 path).
5. Natural SW idle termination/wake.

## Unified acceptance result (2026-11-03)

`tests/rc-final/run-final-gate.py` on the unified artifact returned **PASS,
exit 0** (`D:/c/logs/rc-final-20261103d/rc-final-report.json`):

| stage | result | evidence |
| --- | --- | --- |
| release-like 14-case matrix | PASS, 226/226 | AI input, Sogou IME, hidden/occluded screenshots, OOPIF, NetworkServiceSandbox |
| P1 native import | PASS, 9/9 | approval, CookieManager write, restart, expiry, host/source interruption |
| SW DevTools-attached | PASS | 600s, 0 FATAL, old SW-1 crash path |
| SW natural idle/wake | PASS | worker terminates and new instance starts |
| 1200s host/8-window gate | PASS, 1224.9s, 31/31 | real Native Messaging Host restart, 8 windows, screenshots, resources |

No stage reported BLOCKED. The artifact is internal only; signing/installer/
updater remain out of scope.

## Known limitations / not public release

- No Authenticode signing, installer, updater, or Windows publisher identity.
- `is_official_build=false`, no PGO/ThinLTO/orderfile; DCHECKs are on.
- P1 patch 038 remains proposed (not merged); internal artifact only.
- SW-1 DevTools-attached state-machine fix is included, but the DevTools watch
  mode remains an upstream regression monitor.
- No smart-screen reputation, per-module signing, EDR/AppLocker validation, or
  server-side variations-seed verification.
- Not a replacement for the formal Phase 9 signing/installer/updater work.

## Internal vs public

| | Internal RC | Public release |
| --- | --- | --- |
| artifact | unsigned `out/WinRelease` | signed, packaged, versioned installer |
| update | none | updater + metadata |
| support | internal test group | end users |
| `releasable` | false | only after Phase 9 gates |
