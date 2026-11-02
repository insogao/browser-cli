# Windows RC audit - Agent C (2026-11-02)

Status: **audit only, no product code changed**. Worktree
`D:\work\wt\bc-rc-audit`, branch `agent/rc-audit`, HEAD `643104e` (the PR #1
head; the PR #1 base branch is `windows-port-and-phase7`). Branch switching,
reset/clean, push, merge and changes to `D:/c/src` or other worktrees were not
performed. The only files added are this report and the audit tools under
`tests/windows-rc-audit/`.

Evidence roots:

- `D:/c/logs/longrun-2026-11-01T22-36-11/` (20-minute run)
- `D:/c/logs/winrelease/matrix/` (pre-A2 WinRelease security matrix, 21:12)
- `D:/c/logs/rc-audit-20261102/winrelease-netsandbox/` (fresh A2 probe)
- `D:/c/logs/rc-sec/` (component WinDev / Chrome / Edge baseline)
- `D:/c/logs/release-candidate-e2e-2026-11-01T14-13-50-544Z.json` (release matrix)

Hygiene: every probe used a fresh temporary `--user-data-dir`; no
`--no-sandbox`, no real Chrome/Browser CLI profile, no keychain access, no
global kill. The A2 probe is headless (`--headless=new`) and is security-matrix
evidence only, not TSF/product-launch evidence.

---

## 0. Verdict matrix

| Area | Verdict | Basis |
| --- | --- | --- |
| NetworkServiceSandbox on **all** entry points | **FAIL** | BrowserPilot client wake path, release-matrix show invocation, and 20-minute long-run gate do not pass the switch; direct exe launch is unimplemented/unsandboxed |
| Normal launcher | **PASS with gaps** | `manual-browser.cmd` passes the switch, but its guard does not assert it and it targets `WinDev`, not `WinRelease` |
| Release-like matrix initial launches | **PASS** | normal + agent args include the feature; 210/210 checks after the fix |
| Release-like matrix show invocation | **FAIL (cold start) / PASS only as forwarder** | `showArgs` has no sandbox switch; safe only while another instance already owns the profile |
| WinRelease TSF smoke | **PASS** | `tsf-smoke.mjs` passes the feature |
| WinRelease security matrix | **PASS (netsandbox) / INFO (default control)** | netsandbox network utility = Low IL + AC1 + CIG/ACG; default is an intentional unsandboxed negative control |
| 20-minute long-run gate | **FAIL (gate parity)** | both sessions launched without the switch; evidence command lines prove the run did not exercise a sandboxed network utility |
| 20-minute handle trend | **PASS** | startup warm-up, then plateau; no sustained leak |
| Longer than 20 minutes / handle types | **NOT-TESTED** | one 20-minute run, handles only; no GDI/USER/pool/heap data |
| CIG/ACG + module/timing comparison | **PARTIAL, not equivalence** | policy bits and CIG-at-birth mostly match Chrome; signature, module provenance, Edge extras and many fields remain untested/unequal |
| PR #9 depends on PR #2 | **PASS** | `485590f8` is an ancestor of `57a5fd89`; #9 manifest is 034/035/036 |
| PR #7 A1 rejected | **FAIL (hygiene)** | decision rejected it, but PR #7 is still OPEN/Draft and carries a second proposed 037 |
| PR #8 superseded | **FAIL (hygiene)** | PR #8 is still OPEN/Draft, has a non-contiguous manifest (1..33 + 38), and its 038 is byte-identical to #9's 036 |
| P1 037 collision / accidental merge | **PASS with WARN** | no applied manifest has 037, no PR head is merged into the base; two proposed 037s exist on separate branches |
| WinRelease `releasable=false` | **PASS (kept false)** | no signing/installer/updater work performed; preflight still classifies it as an internal unsigned test build |

---

## 1. NetworkServiceSandbox entry-point audit

### 1.1 Why the switch is mandatory

`disable_fieldtrial_testing_config = true` is required to avoid
`BrowserDynamicCodeDisabled` (browser ACG) breaking Sogou Xbyak JIT. The same
test field-trial config also carried `NetworkServiceSandboxWindows`, so this
build does **not** enable the network sandbox by default. The runtime contract
in `D:/work/wt/bc-winrelease/config/windows-release-runtime-flags.env` says it
explicitly:

> Launching out/WinRelease/chrome.exe directly (no switch) leaves the network
> utility process unsandboxed, exactly like WinDev/official Chrome without a
> variations seed.

Therefore "default sandbox" is not a valid claim for this product. Every
official entry point must pass `--enable-features=NetworkServiceSandbox`, or a
reviewed Chromium patch must flip the feature default (none exists).

### 1.2 Entry-point state

| Entry point | File / line | Class | Switch | Verdict |
| --- | --- | --- | --- | --- |
| Normal launcher | `D:/work/wt/bp-normal/manual-browser.cmd:12` | product | present | **PASS** (target is `D:\c\src\out\WinDev\chrome.exe`, not WinRelease) |
| Normal-launch static guard | `D:/work/wt/bp-normal/scripts/check-normal-launch.mjs` | guard | no positive assertion | **FAIL** (a future edit can silently drop the switch) |
| BrowserPilot native client wake path | `D:/work/BrowserPilot/native-host/client.mjs:176-184` | product | absent | **FAIL** (`launchSavedProfile` passes only user-data-dir/profile-directory) |
| BrowserPilot native client (normal branch) | `D:/work/wt/bp-normal/native-host/client.mjs:176-184` | product | absent | **FAIL** (same code) |
| Release matrix normal initial launch | `D:/work/wt/bp-rc-e2e/scripts/test-release-candidate-e2e.mjs:714-731` | product | present | **PASS** |
| Release matrix agent initial launch | same `buildArgs` | product | present in combined switch | **PASS** (duplicate `--enable-features`; last occurrence wins, see 1.3) |
| Release matrix user-explicit show | same file `:1568` | product (cold start) | absent | **FAIL** if cold start; safe only as second-instance forwarder |
| WinRelease TSF smoke | `D:/work/wt/bc-winrelease/tests/windows-release/harness/tsf-smoke.mjs:205` | product | present | **PASS** |
| WinRelease security matrix netsandbox | `.../release-security-compare.mjs:635` | gate | present via `ENABLE_FEATURES` | **PASS** |
| WinRelease security matrix default | `.../release-security-compare.mjs:633` | negative control | intentionally absent | **INFO**, never a product launch |
| 20-minute long-run gate | `D:/work/wt/bc-longrun/tests/tools/longrun-stability-gate.py:923-943` | gate | absent | **FAIL** |
| 20-minute evidence command lines | `D:/c/logs/longrun-2026-11-01T22-36-11/resources.jsonl` | gate evidence | absent in both sessions | **FAIL** |
| Browser CLI managed/stock smoke | `D:/work/wt/bc-rc-audit/scripts/verify-windows.sh:211,244` | test-only | absent | **FAIL (test-only)**; also uses `--in-process-gpu` |
| K4 cold-start diagnostic | `D:/work/wt/bp-rc-e2e/scripts/test-input-arbitration-e2e.mjs:3225` | test-only | absent | **FAIL (test-only)**; also uses `--in-process-gpu` |
| Direct `chrome.exe` / installer / shortcut | `D:/work/wt/bc-rc-audit/launcher/README.md` | product | not implemented | **NOT-TESTED**; direct start is unsandboxed |

The normal launcher exists only in the `bp-normal` worktree/branch, not in the
main `D:/work/BrowserPilot` checkout. The native client exists in both, and both
copies lack the switch. The audit tool reproduces this table:

```bash
python tests/windows-rc-audit/launch_entrypoints.py --strict
# exit 1 because product/gate/guard findings are still open
```

Current output summary: 5 PASS, 7 FAIL, 1 SKIP.

### 1.3 The duplicate `--enable-features` question

The agent initial launch has both:

```text
--enable-features=NetworkServiceSandbox
--enable-features=InputOwnershipEnforcement,NetworkServiceSandbox
```

Chromium stores switches in a map and the last value wins
(`base/command_line.cc:419-455`), so the final value contains both features.
The 210/210 release matrix result confirms the input-ownership feature was on.
This is a hygiene issue, not a functional sandbox gap. A single merged switch
is still preferable because the behavior depends on duplicate-switch resolution.

### 1.4 Runtime proof that the switch works on the current A2 build

A fresh one-run probe was taken on the current `out/WinRelease` (A2 relink,
`chrome.dll` sha256 `1b740b2d...`, not the 21:03 evidence `23197b6d...`):

```bash
cd D:/work/wt/bc-winrelease
WINDOWS_RELEASE_EVIDENCE=D:/c/logs/rc-audit-20261102/winrelease-netsandbox \
  node tests/windows-release/harness/release-security-compare.mjs \
  --scenarios=winrelease-netsandbox --repeat=1
```

Result: exit 0, 9 processes, DevTools 507 ms, page ready 954 ms.

| Process | IL | AC | CIG | ACG | Modules (total/install) | CIG at birth |
| --- | --- | --- | --- | --- | --- | --- |
| browser | Medium | 0 | 0x00000000 | 0x00000000 | 142/3 | false |
| GPU | Low | 0 | 0x00000005 | 0x00000000 | 70/7 | false, applied later at 185 ms |
| renderer | Untrusted | 0 | 0x00000005 | 0x00000000 | 23/5 | true |
| network utility | Low | **1** | **0x00000005** | **0x00000001** | 42/5 | **true** |
| storage utility | Untrusted | 0 | 0x00000005 | 0x00000001 | 23/5 | true |

Negative control from the existing matrix (`winrelease-default`, no switch):
network utility is Medium IL, AC=0, CIG=0x0, ACG=0x0, 48/5 modules, CIG at
birth false. That is the concrete proof that missing the switch means an
unsandboxed network utility.

### 1.5 Required entry-point fixes

These are in other worktrees/branches and were **not** modified by this audit:

1. `D:/work/BrowserPilot/native-host/client.mjs` and
   `D:/work/wt/bp-normal/native-host/client.mjs`: append
   `--enable-features=NetworkServiceSandbox` to `launchArgs` in
   `launchSavedProfile()`. Without it, the official client wake path starts the
   network utility unsandboxed.
2. `D:/work/wt/bp-rc-e2e/scripts/test-release-candidate-e2e.mjs:1568`: add the
   switch to `showArgs`, or prove and enforce that the invocation can only ever
   be a second-instance forwarder. A cold start with only
   `--browser-cli-user-explicit-show` is a browser launch and is unsandboxed.
   Merge feature flags into one `--enable-features` value while there.
3. `D:/work/wt/bc-longrun/tests/tools/longrun-stability-gate.py:923-943`: add
   the switch to both normal and agent `build_args`, and accept
   `^--enable-features=.*NetworkServiceSandbox` in `ALLOWED_FLAG_PATTERNS`.
   Re-run the 20-minute gate if the gate is meant to represent release security
   configuration.
4. `D:/work/wt/bp-normal/scripts/check-normal-launch.mjs`: add a positive
   assertion for `NetworkServiceSandbox`; the current guard only forbids
   automation flags. Decide whether the normal launcher should target
   `WinRelease` for RC.
5. Optional: add the switch to the managed smoke in
   `D:/work/wt/bc-rc-audit/scripts/verify-windows.sh` if it is meant to test the
   release runtime contract. It remains a test-only entry point and still uses
   `--in-process-gpu`, a documented security downgrade.
6. Product-level guarantee: a reviewed Chromium patch that enables the feature
   for this product build, or a single launcher that every entry point must go
   through. Until then, do not describe the product as sandboxed by default.

**Section verdict: FAIL.** The switch works when present, but it is not applied
or guaranteed on all official entry points.

---

## 2. 20-minute long-run handle trend

### 2.1 Data shape

`resources.jsonl` has 160 lines. They are two interleaved sessions sampled at
the same elapsed time (80 paired samples, 24 processes total):

| Session | Rows | First | Last | Peak |
| --- | --- | --- | --- | --- |
| normal | 80 | 5682 | 6360 | 6377 @ 630.0 s |
| agent | 80 | 6055 | 6211 | 6244 @ 105.1 s |
| aggregate | 80 pairs | **11737** | **12571** | **12599 @ 225.2 s** |

This matches the run report's "handles: start=11737 end=12571 peak=12599@225s".
The aggregate handle figure is not a single process; it is the sum over both
browser profiles.

### 2.2 Selected timeline (aggregate handles)

| t (s) | 1.4 | 75.0 | 150.1 | 225.2 | 300.2 | 450.0 | 600.1 | 750.0 | 900.2 | 1050.2 | 1185.1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| handles | 11737 | 12410 | 12425 | 12599 | 12557 | 12553 | 12551 | 12568 | 12568 | 12574 | 12571 |

### 2.3 Slope and phase classification

| Window | Delta | Least-squares slope | Pearson r | Interpretation |
| --- | --- | --- | --- | --- |
| 0-75 s | +673 | +551/min | 0.91 | one-time startup warm-up |
| 75 s-end | +161 | +3.88/min | 0.53 | mostly early settling; weak fit |
| 600 s-end | +20 | **+0.42/min** | **0.17** | plateau, no trend |
| 300 s-end | +14 | +1.69/min | 0.56 | dominated by one renderer step |

Largest one-sample rises: +227 @15.2 s, +333 @30.0 s, +112 @60.1 s, +124 @165.1 s.
Largest drop: -32 @120.0 s. These are startup/profile-creation steps, not a
steady leak. After 225 s the aggregate never exceeds 12599; the final value is
28 handles **below** the peak.

Classification: **startup growth, then plateau; no sustained handle leak.**

### 2.4 Per-process handles

| Type | First | Last | Min | Max | Slope from 300 s | r |
| --- | --- | --- | --- | --- | --- | --- |
| browser (2) | 3964 | 4005 | 3918 | 4041 | -0.49/min | -0.52 |
| GPU (2) | 1858 | 1868 | 1857 | 1879 | -0.04/min | -0.05 |
| network (2) | 847 | 775 | 775 | 847 | -1.25/min | -0.81 |
| storage (2) | 374 | 380 | 374 | 380 | 0.00/min | 0.00 |
| crashpad-handler (3) | 610 | 607 | 607 | 613 | 0.00/min | 0.00 |
| renderer (10) | 3742 | 4512 | 3742 | 4519 | +3.43/min | 0.89 |
| extension-renderer (2) | 342 | 424 | 342 | 424 | 0.00/min | 0.00 |

The renderer aggregate looks positive only because of one step:

- PID 5804 (normal): 364 handles at 300 s, 364 at 600 s, +30 step at 630 s,
  then 395-401 with no monotonic trend; 401 at 1185 s.
- PID 35808 (agent): 569 at 300 s, 572 at 1185 s (+3 over 885 s).
- The other 10 renderer/extension-renderer PIDs are flat after 300 s; all 12
  PIDs are stable for the entire run (no renderer churn).

Per-PID values are in the 207-577 range. The only "watch" item is PID 5804's
one-time +30 step; it is not a leak.

Memory context (not the task's primary metric): aggregate RSS 2467.7 ->
2591.2 MB (peak 2593.8 @1095 s); private 1178.2 -> 1194.4 MB (peak 1203.3
@1095 s); late slopes after 600 s are +0.8 MB/min RSS and +1.0 MB/min private.
CPU was 570 s over 1185 s, 1.4% of 32 logical CPUs.

### 2.5 Verdict and limits

**Handle trend verdict: PASS (no sustained leak).** The run grows by 7.1% during
startup, peaks at 225 s, and then stays within a roughly 50-handle band. The
late slope is +0.42 handles/min with r=0.17, below any material leak threshold.

Limits:

- One 20-minute run only; multi-hour behavior is **NOT-TESTED**.
- Handles only; GDI/USER objects, paged pool, heap growth and handle-type
  distribution are **NOT-TESTED**.
- The gate's own overall verdict is **BLOCKED** because Native Messaging Host
  restart was not exercised; that is unrelated to the resource trend and must
  not be read as a resource failure.
- The long-run gate did not pass `NetworkServiceSandbox` (section 1), so this
  is not evidence about a sandboxed network utility under load.

---

## 3. CIG/ACG, module loading and timing comparison

### 3.1 Effective process state

`CIG 0x00000005` below is the measured `ProcessSignaturePolicy`; `ACG
0x00000001` is `ProhibitDynamicCode`. "At birth" is the first successful
watcher sample after `CreateProcess`; "delayed" means the process applied the
mitigation later and the exact module-load boundary is not observed.

| Scenario | Process | IL | AC | CIG | ACG | Modules total/install | CIG at birth | exe signature |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WinRelease-netsandbox (fresh A2) | browser | Medium | 0 | 0 | 0 | 142/3 | false | NotSigned |
| | GPU | Low | 0 | 0x5 | 0 | 70/7 | false, +185 ms | NotSigned |
| | renderer | Untrusted | 0 | 0x5 | 0 | 23/5 | true | NotSigned |
| | network | Low | **1** | **0x5** | **0x1** | 42/5 | **true** | NotSigned |
| | storage | Untrusted | 0 | 0x5 | 0x1 | 23/5 | true | NotSigned |
| WinRelease-default | network | Medium | 0 | 0 | 0 | 48/5 | false | NotSigned |
| WinDev-netsandbox (component) | browser | Medium | 0 | 0 | 0 | 631/493 | false | NotSigned |
| | GPU | Low | 0 | 0x5 | 0 | 559/497 | false, +514 ms | NotSigned |
| | renderer | Untrusted | 0 | 0x5 | 0 | 513/495 | false, +292-434 ms | NotSigned |
| | network | Low | **1** | **0x5** | **0x1** | 532/495 | false, +331 ms | NotSigned |
| | storage | Untrusted | 0 | 0x5 | 0x1 | 513/495 | false, +305 ms | NotSigned |
| Chrome-netsandbox | browser | Medium | 0 | 0 | 0 | 142/5 | false | Valid |
| | GPU | Low | 0 | 0x5 | 0 | 69/6 | false, +165 ms | Valid |
| | renderer | Untrusted | 0 | 0x5 | 0 | 20/3 | true | Valid |
| | network | Low | **1** | **0x5** | **0x1** | 39/3 | **true** | Valid |
| | storage | Untrusted | 0 | 0x5 | 0x1 | 19/3 | true | Valid |
| Edge-default | browser | Medium | 0 | 0 | 0 | 164/8 | not measured | Valid |
| | GPU | Low | 0 | 0x5 | 0 | 70/4 | not measured | Valid |
| | renderer | Untrusted | **1** | 0x5 | 0 | 21/4 | not measured | Valid |
| | network | Medium | 0 | 0 | 0 | 45/3 | not measured | Valid |
| | storage | Untrusted | 0 | 0x5 | 0x1 | 20/3 | not measured | Valid |

Edge `renderer AC=1` and `ASLR-RELOC=1` are Edge-only hardening (from
`D:/c/logs/rc-sec/`); they are not a Browser CLI regression, but they mean Edge
is not equivalent either. Edge with `NetworkServiceSandbox` and Edge timing were
not measured in this audit: **NOT-TESTED**.

### 3.2 Module signature/provenance

Fresh WinRelease A2 probe status counts (Authenticode per unique module path):

| Process | Valid | NotSigned | Non-Microsoft signed | Install-dir modules |
| --- | --- | --- | --- | --- |
| browser | 139 | **3** | 10 (Sogou/A-Volute components) | 3 (chrome.dll, chrome.exe, chrome_elf.dll; all NotSigned) |
| GPU | 66 | 4 | 0 | 7 |
| renderer | 20 | 3 | 0 | 5 |
| network | 39 | 3 | 0 | 5 |
| storage | 20 | 3 | 0 | 5 |

WinRelease has 3 core NotSigned modules and no installation-directory signature
trust. WinDev has the same 3 unsigned core modules plus ~493-497 installation
modules per process; Chrome/Edge load 3-8 install modules and their exe/main
DLLs are Valid. This is a large improvement over WinDev but **not** signature
parity with official browsers.

### 3.3 Startup timing

| Scenario | DevTools (ms) | Page ready (ms) | Runs |
| --- | --- | --- | --- |
| WinRelease-netsandbox (fresh A2) | 507 | 954 | 1 |
| WinRelease-netsandbox (pre-A2 matrix) | 402 | 785 | 3 |
| WinRelease-default (pre-A2) | 384 | 780 | 3 |
| WinDev-netsandbox | 612 | 1264 | 3 |
| WinDev-default | 629 | 1254 | 3 |
| Chrome-netsandbox | 323 | 646 | 3 |
| Chrome-default | 338 | 579 | 3 |
| Edge-default / Edge-netsandbox | not measured | not measured | - |

WinRelease is roughly 1.2-1.6x Chrome's startup time in these samples. Timing
variation between runs is large enough that this must not be treated as a
security property or a performance ranking.

### 3.4 What the comparison supports

Supported by measurement:

- With the switch, WinRelease's network utility has the same tested mitigation
  set as Chrome's network utility: Low IL, AppContainer=1, CIG=0x5, ACG=0x1,
  extension-point/child/font/image-load restrictions, CIG at birth.
- WinRelease restores pre-launch CIG for renderer/network/storage that the
  component WinDev build lacks. This is a real release-shape improvement.
- Browser, GPU, renderer and storage tested policy fields match the same
  Chrome/BCLI baseline; no missing mitigation bit was found in those fields.
- GPU CIG is delayed in both WinRelease and Chrome (~150-200 ms), so "CIG at
  birth" does not hold for the GPU process in either product.

Not supported:

- Security equivalence. `NotSigned` vs `Valid`, 3 unsigned installation modules,
  third-party signed DLLs in the browser process, no per-module load-time
  verification, Edge's renderer AppContainer, and the untested list below all
  remain material.
- "Browser code integrity is on": browser CIG/ACG are 0 in every build. Browser
  ACG is deliberately off because Sogou Xbyak requires JIT; this is a known
  trade-off, not a parity claim.
- "Sandboxed by default": the network sandbox is off unless the launch switch is
  present (section 1).

### 3.5 Unobservable / untested fields

- Authenticode trust chain and publisher reputation; SmartScreen, AppLocker and
  EDR behavior for the unsigned build.
- Per-module load timestamps relative to CIG, especially for the delayed GPU CIG
  and for any module loaded before mitigation.
- Signatures of every module (only exe, main DLL and status counts were
  collected; the installation-directory module list is known but full
  per-module verification/policy evaluation was not performed).
- Edge `NetworkServiceSandbox` behavior and Edge startup timing.
- Server-side variations/seed state for official Chrome/Edge; isolated profiles
  have no seed, so online behavior may differ.
- Official-build pipeline (PGO/ThinLTO/orderfile); WinRelease is
  `is_official_build=false`.
- Signing, installer, updater, shortcuts, SmartScreen install reputation.
- Kernel/driver attack surface, GPU driver/kernel paths, crashpad handler
  (no CIG/ACG observed), Win32k lockdown (0), EAF/IAT/ROP/RedirectionTrust (0),
  SEHOP (`NO-API`), `ProcessUserPointer` (`UNAVAILABLE(50)`), opaque
  `MitigationOptionsMask`/`SystemCallFilter` values.
- Sandbox behavior for proxy, client certificates, downloads, QUIC, WebRTC,
  OOPIF, extensions and long-running network load; the security probe loaded a
  simple local page.
- Exploit/sandbox-escape testing; none was performed.

**Section verdict: PARTIAL parity, NOT security equivalence.**

---

## 4. PR dependency and patch-numbering audit

### 4.1 Exact PR state (2026-11-02, `gh pr list/view`)

| PR | Base (OID) | Head (OID) | Draft/State | Patch state |
| --- | --- | --- | --- | --- |
| #1 | `main@f7556d96` | `windows-port-and-phase7@643104e5` | Draft/OPEN | manifest 001-033 |
| #2 | `windows-port-and-phase7@dbb560f1` | `agent/gpu-dcomp@485590f8` | Draft/OPEN | manifest 034, 035 |
| #3 | `windows-port-and-phase7@8d9cc1c1` | `agent/rc-hidden@daf71e33` | Draft/OPEN | reproduction harness; no new patch |
| #4 | `windows-port-and-phase7@8d9cc1c1` | `agent/rc-p1-native@8f11745a` | Draft/OPEN | proposed 037 cookie import; not in manifest |
| #5 | `windows-port-and-phase7@833b880f` | `agent/rc-winrelease@7ff0acbb` | Draft/OPEN | WinRelease build/config/harness; `releasable=false` |
| #6 | `windows-port-and-phase7@833b880f` | `agent/rc-longrun@8429652e` | Draft/OPEN | 20-minute gate; no new patch |
| #7 | `windows-port-and-phase7@833b880f` | `agent/rc-hidden-a1@5f00729e` | Draft/OPEN | proposed 037 A1 route 1; A1 rejected by decision |
| #8 | `windows-port-and-phase7@833b880f` | `agent/rc-hidden-a2@2c03bad4` | Draft/OPEN | applied 038 (non-contiguous); superseded |
| #9 | `agent/gpu-dcomp@485590f8` | `agent/rc-hidden-a2-gpu@57a5fd89` | Draft/OPEN | manifest 034, 035, 036; selected |

### 4.2 Patch numbering facts

- #1 is contiguous 001-033.
- #2 adds **034-gpu-inprocess-direct-composition** and
  **035-gpu-inprocess-overlay-info-after-dcomp**. The remote PR head
  `485590f8` has both; the local `agent/gpu-dcomp@cbe40b4` is six commits behind
  and only shows 034. Treat the PR head as authoritative.
- #9 adds **036-presentation-callback-lifecycle** on top of #2. Its manifest tail
  is exactly 034, 035, 036; the 034 sha256 equals #2's 034
  (`1185dd0be8fb174b...`).
- #8's **038-presentation-callback-lifecycle** is byte-identical to #9's 036
  (content blob `cf49790ca5d5...`; sha256
  `da5563da0a28dd5a21001832bae1e4c9bd3398db95c284e0cfa78d679ca054fc`).
  #8 is therefore a superseded duplicate.
- #8's manifest is **not valid for `scripts/apply-patches.py`**: it has 34
  entries with orders `1..33` followed by `38` (last=38), while the tool requires
  contiguous `1..N`. This is an additional reason not to merge #8.
- #7 carries `chromium/patches/proposed/037-managed-window-contents-visibility.patch`
  (blob `0cc27428bf03...`). Its proposed README still says
  "034-managed-window-contents-visibility.patch"; the file name and commit say
  037. This is a stale documentation/numbering artifact in the rejected branch.
- #4 carries `chromium/patches/proposed/037-browser-cli-cookie-import.patch`
  (blob `d876a0e337f5...`). The P1 renumbering was 034 -> 036 -> 037:
  - `bf44b64` proposed it as 034;
  - `4ac938e` renumbered to 036 to avoid GPU 034/035;
  - `8f11745a` renumbered to 037 because 036 became the selected hidden Blink
    patch.
  The local branch `agent/rc-p1-native@4ac938e` still contains the stale 036
  proposal; the PR head `8f11745a` (also local `agent/rc-p1-build`) is the
  authoritative 037 state.
- No applied manifest contains order 037. The two 037s are **proposed** files on
  separate branches with different names, so there is no current file-path
  collision. There is a numbering collision risk if #7 and #4 were both merged;
  `apply-patches.py` would reject a duplicate order until one is renumbered.
- No merge commit or accidental merge was found. None of the PR heads is an
  ancestor of `windows-port-and-phase7`; #9 is a descendant of #2
  (`485590f8` is an ancestor of `57a5fd89`).

### 4.3 Recommendations

1. Close #7 as rejected (or explicitly mark it superseded) so its 037 cannot be
   merged later.
2. Close #8 as superseded; do not merge its 038 in addition to #9's 036.
3. Keep #9 as the selected route-2 stack; land #2 first, then rebase #9 onto the
   current #1 head (`643104e`) and re-run the build/matrix.
4. Keep #4's 037 as a proposed patch; do not add it to the manifest until its
   native pipe/approval/CookieManager E2E exists.
5. Update the #7 proposed README numbering if that branch is kept for history,
   and update local `agent/rc-p1-native` or stop using it as the review ref.
6. Consider a CI check that proposed patch numbers are unique across open PR
   branches. `apply-patches.py` already rejects non-contiguous/duplicate applied
   manifests, but proposed files are not covered.

**Section verdict: selected stack PASS; #7/#8 hygiene FAIL until closed.** No
accidental merge and no applied 037 collision were found.

---

## 5. WinRelease releasable state

`D:/work/wt/bc-winrelease/docs/windows-release-evidence-2026-11-01.json`
(preflight) remains:

- `artifactClass: internal-unsigned-test-build`
- `releasable: false`
- blockers: not Authenticode-signed; `is_official_build` false (no PGO/ThinLTO);
  no `update-metadata.json`/updater contract; no Windows publisher identity in
  `config/product-identity.env`; per-module signing and
  SmartScreen/AppLocker/EDR behavior not verified.

Current artifacts: `chrome.exe` sha256 `0441080d...` (unchanged), `chrome_elf.dll`
`3f8831db...`, `chrome.dll` `1b740b2d...` (A2 relink at 22:10, different from
the 21:03 security-matrix evidence `23197b6d...`). The fresh probe above is on
the current A2 binary.

No signing, installer, updater, packaging or publisher-identity work was done.
`releasable` was not changed and must stay **false**.

---

## 6. Reproduce the audit

```bash
# Long-run handle trend (real data + synthetic self-test)
python tests/windows-rc-audit/longrun_handle_trend.py \
  D:/c/logs/longrun-2026-11-01T22-36-11/resources.jsonl
python tests/windows-rc-audit/longrun_handle_trend.py --self-test

# Static launch-entry-point audit (report mode; --strict exits 1 on gaps)
python tests/windows-rc-audit/launch_entrypoints.py
python tests/windows-rc-audit/launch_entrypoints.py --strict
python tests/windows-rc-audit/launch_entrypoints.py --self-test

# Patch/PR stack audit (read-only Git; exits 1 while #8's manifest is invalid)
python tests/windows-rc-audit/patch_stack_audit.py

# Fresh WinRelease network-sandbox probe (isolated temp profile, headless)
cd D:/work/wt/bc-winrelease
WINDOWS_RELEASE_EVIDENCE=D:/c/logs/rc-audit-20261102/winrelease-netsandbox \
  node tests/windows-release/harness/release-security-compare.mjs \
  --scenarios=winrelease-netsandbox --repeat=1
```

The audit scripts do not launch a browser. The launch audit is a report, not a
passing regression gate: it exits 1 in `--strict` mode until the BrowserPilot
client, show-forwarder, long-run gate and normal-launcher guard are fixed.

---

## 7. Incidental observation (not used as evidence)

A single `chrome.exe --version` invocation against the current WinRelease build
exited with `FATAL: components/search_engines/template_url_prepopulate_data_resolver.cc:103:
NOTREACHED hit` and produced a stack dump. It was not part of the isolated
probes, was not used as evidence, and must not be repeated without an isolated
`--user-data-dir`. A version query should not initialize a profile or hit that
path; this needs separate root-cause work before RC. It does not change any
verdict above, but it reinforces that `releasable=false` is correct.

---

## 8. Post-audit remediation (2026-11-02)

This section records changes made after the audit snapshot above. The original
FAIL findings are kept for history; they must not be read as current state.

| Original finding | Current state | Evidence |
| --- | --- | --- |
| NetworkServiceSandbox missing on client/long-run/show | **FIXED in branches** | `native-host/client.mjs` now appends `--enable-features=NetworkServiceSandbox`; long-run gate appends `InputOwnershipEnforcement,NetworkServiceSandbox`; release-matrix show args already carry the combined switch; `check-normal-launch.mjs` now positively asserts it. |
| Duplicate `--enable-features` in release matrix | **FIXED** | normal/agent launch args now carry exactly one `--enable-features` value. |
| 20-minute gate did not exercise the sandbox | **FIXED (coverage) / NEW FAIL (stability)** | `D:/c/logs/longrun-2026-11-02c` has the switch on both sessions and network utility `--service-sandbox-type=network`. The gate still FAILs: extension-renderer PID 9308 hit `service_worker_event_queue.cc:185 HasScheduledIdleCallback` at 570s and w5 targetCrashed at 578.7s. A2 presentation itself had 0 DCHECKs. |
| New service-worker crash | **FIXED (SW-1, PR #10)** | Root cause: `ServiceWorkerGlobalScope::AddKeepAlive()` reset the idle timeout without re-arming; with DevTools attached, `OnRequestTermination` sends `SetIdleDelay(30s)` into the invalid state. Fix: hold a `StayAwakeToken` in AddKeepAlive, release in ClearKeepAlive. WinRelease 20-minute run: 0 FATAL/0 target crash, SW restart PASS; DevTools-attached 600s: 0 FATAL; repeated restart stress clean. |
| Host restart BLOCKED | **PASS via dedicated gate** | BrowserPilot PR #7: 196/196 matrix; 20-minute 8-window run 31/31 with a real host restart at t=601s; stale connections/authorizations invalidated. |
| PR #7/#8 hygiene | **FIXED** | browser-cli PR #7 (A1 rejected) and #8 (old A2 superseded) are closed, not merged; PR #9 carries the audited 036. |
| P1 exact artifact blocked by UI-thread DCHECKs | **FIXED** | Rebased proposed patch 037 onto 001-036; async descriptor delete; `WithBaseSyncPrimitives` pipe join with WeakPtr delegate; synchronous client PID; drain/peek before pipe close; USER_EXPLICIT consent presentation. Exact WinDev artifact: 9/9 scenarios PASS, including `ticket-expired` delivery. |
| WinRelease `releasable=false` | **UNCHANGED** | Still internal unsigned test build; no signing/installer/updater work. |

The P1 patch remains **proposed** (not in `patch-manifest.json`) and all PRs
remain Draft. The product is still not release-ready.
