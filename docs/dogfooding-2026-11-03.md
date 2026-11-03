# Browser CLI internal RC dogfooding (2026-11-03)

Artifact: unified 001-038 `D:/c/src/out/WinRelease/chrome.exe`
(`chrome.dll` sha256 `a848a1c7...a308d2c`), internal unsigned build.

Run ID: `20261103-142522-34a76313`
(final gate report: `D:/c/logs/rc-final/20261103-142522-34a76313/rc-final-report.json`)

## Results

| Area | Result | Evidence |
| --- | --- | --- |
| Normal browsing/login | **PASS (synthetic login)** | P1 target approve/restart: CookieManager login `LOGIN_OK`, restart `LOGIN_OK` / session cookie `SESSION_MISSING`; no real external account credentials used |
| Long session | **PASS** | 1200s host/8-window gate, 1224.5s, 31/31 checks; normal+agent sessions |
| Hidden windows | **PASS** | release-matrix hidden/occluded screenshots and managed-hidden probes; SW idle/attached stages |
| Multiple agents concurrently | **PASS** | 8 windows across normal+agent profiles, product commands and screenshots; ownership/presentation matrix |
| Real Chinese input | **PASS** | Sogou TSF compositionstart + CJK in input/textarea/contenteditable; HUMAN/ASSIST arbitration |
| Host reconnect | **PASS** | real Native Messaging Host restart at t=600s; old connections/authorizations invalid; commands recover |
| Foreground/cleanup after long run | **PASS** | post-host normal 108/108 (N1-N6, including N5/N6 foreground); foreground restored to 590308; no residual host/session |
| Suspend/resume proxy | **PASS** | `D:/c/logs/rc-dogfood-20261103d/dogfood-suspend-resume.json`; test tree suspended 8s, resumed; page/counter/screenshot/browser alive |

## Sleep/wake limitation

The host machine was **not** put into S3/S0 sleep: suspending the workstation
would also suspend the test runner and the user's session. The test uses a
suspend/resume proxy on the exact test-created browser process tree. True system
sleep/wake (including lock screen, network change and NAT rebind) remains a
release-engineering follow-up; no product defect is claimed from the proxy.

## Defects opened

None new. The SW-1, foreground/N5-N6, P1 source-login and P1 report-selection
issues found earlier were harness/state-machine work already fixed in this
cycle; no new Chromium feature patch was added.

## Conclusion

Internal dogfooding **PASS with the system-sleep limitation above**. The build is
suitable for internal trial only; `releasable=false`, unsigned, no installer or
updater.
