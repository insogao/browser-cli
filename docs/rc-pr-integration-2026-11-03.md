# RC PR integration and dependency audit (2026-11-03)

All PRs remain **Draft**. No merge, reset or force-push was performed.

## browser-cli chain

| PR | base | head | patch / scope | dependency |
| --- | --- | --- | --- | --- |
| #1 | `main` | `windows-port-and-phase7` | 001-033 base/Windows | root |
| #2 | #1 | `agent/gpu-dcomp` | 034-035 GPU/DComp | #1 |
| #9 | #2 | `agent/rc-hidden-a2-gpu` | 036 A2 presentation callbacks | #2 |
| #10 | #9 | `agent/sw1-a2` | 037 SW-1 service-worker keepalive | #9 |
| #11 | #10 | `agent/rc-integration` | 038 P1 cookie import + unified RC gate | #10 |
| #6 | #1 | `agent/rc-longrun` | 20-minute gate (no patch) | #1 |
| #4 | #1 | `agent/rc-p1-native` | proposed P1 patch 038 | **not a merge dependency** |
| #3/#5/#7/#8 | #1 | various | reproduction/WinRelease/A1/old A2 | history; #7/#8 closed |

The applied 001-038 manifest is #11's; `scripts/apply-patches.py --check`
validates order/hash 34→38. #11 already contains the P1 038 patch and the P1
test results; **#4 must not be merged separately** or 038 would be duplicated.
#4 remains as the P1 review/provenance branch only.

## BrowserPilot chain

| PR | base | head | scope |
| --- | --- | --- | --- |
| #1 | `main` | `feat/host-capability-probe` | host capability probe |
| #2 | #1 | `agent/p0-product-screenshot` | hidden screenshot fallback |
| #6 | #1 | `agent/rc-release-e2e` | release-like matrix |
| #7 | #1 | `agent/rc-host-restart` | real host restart + 1200s gate |
| #3 | `main` | `agent/normal-mode-ux` | normal launcher / IME |
| #4 | `main` | `agent/p1-browser-data-import` | P1 prototype |
| #5 | #4 | `agent/p1-product` | P1 product flow |
| #8 | #5 | `agent/rc-p1-native` | P1 native E2E harness |

Merge/advance order (when the owner decides to merge): #1 → #2 → #9 → #10 → #11;
BrowserPilot #1 → #4 → #5 → #8, and #6/#7 after #1. #4 (browser-cli) is not in
the chain; its patch content is in #11.

## Coverage check

- A2 226/226 matrix, SW-1 037, P1 038, Host restart 196/196, NetworkServiceSandbox,
  P1 9/9, long-run 1200s and SW attached/normal coverage are exercised by #11's
  unified gate.
- BrowserPilot #7 supplies the real host restart in the 1200s stage; #8 supplies
  the P1 E2E harness; #6 is the release-like matrix source.
- No passed feature/test was found missing from the integrated artifact/gate.
