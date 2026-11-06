#!/usr/bin/env node
// Windows release-shape REAL TSF/IME smoke test.
//
// browser-launch-hygiene: windows-only (no keychain)
// Profile isolation: fs.mkdtempSync (Node's mktemp equivalent), one fresh
// directory per run, removed only after the run's verified processes are gone.
//
// This is the only harness in tests/windows-release that is allowed to show a
// window: real Sogou/TSF input cannot be exercised headlessly and
// Input.insertText / clipboard paste / value writes are bypasses, not
// evidence (docs/windows-ime-acceptance-runbook-2026-10-28.md).
//
// What it does:
//   1. launches out/WinRelease/chrome.exe with a fresh isolated profile, a
//      normal visible window, default out-of-process GPU, NetworkServiceSandbox
//      enabled, and NO --no-sandbox / --in-process-gpu / TSF-skip flags;
//   2. brings only that window to the foreground and sends real keystrokes
//      through WScript.Shell.SendKeys (OS input queue; TSF sees them);
//   3. reads composition* events and committed CJK text from the page;
//   4. snapshots the live process tree (browser/GPU/renderer/network) and the
//      Sogou modules loaded in the browser process;
//   5. captures a screenshot, restores the previous foreground window and
//      closes the browser through CDP (Browser.close).
//
// Cleanup uses tests/windows-release/harness/stop-verified.ps1: explicit PIDs
// only, with exe path + unique profile marker + creation time checks. No
// image-name kill, no global kill.
//
// Usage:
//   node tests/windows-release/harness/tsf-smoke.mjs
//   node tests/windows-release/harness/tsf-smoke.mjs --exe=D:/c/src/out/WinRelease/chrome.exe
//
// Output: D:/c/logs/winrelease/tsf/run-<timestamp>/report.json (+ screenshot)

import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..", "..");
const CHECKOUT = process.env.BROWSER_CLI_CHROMIUM_SRC || "D:/c/src";
const PS = "powershell.exe";
const argv = process.argv.slice(2);
const argValue = (name, def) => {
  const hit = argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : def;
};
const EXE = path.resolve(argValue("exe", process.env.WINDOWS_RELEASE_EXE || path.join(CHECKOUT, "out", "WinRelease", "chrome.exe")));
// Win32_Process.ExecutablePath uses backslashes; normalize for comparisons.
const EXE_WIN = path.win32.normalize(EXE);
const FIXTURE = path.join(REPO, "tests", "windows-release", "fixtures", "tsf-smoke.html");
const ROOT = path.resolve(process.env.WINDOWS_RELEASE_TSF_ROOT || "D:/c/logs/winrelease/tsf");
const STAMP = new Date().toISOString().replace(/[:.]/g, "-");
const RUN = path.join(ROOT, `run-${STAMP}`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function runPowerShellFile(file, args, timeoutMs = 240000) {
  const res = spawnSync(PS, ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file, ...args], {
    encoding: "utf8",
    windowsHide: true,
    timeout: timeoutMs,
    maxBuffer: 64 * 1024 * 1024,
  });
  if (res.error) throw new Error(`${path.basename(file)}: ${res.error.message}`);
  if (res.status !== 0) throw new Error(`${path.basename(file)} rc=${res.status}: ${(res.stderr || "").slice(0, 2000)}`);
  return res.stdout;
}
function parseJson(text, what) {
  const t = String(text || "").trim();
  if (!t) throw new Error(`${what}: empty output`);
  try {
    return JSON.parse(t);
  } catch (e) {
    throw new Error(`${what}: invalid JSON (${e.message}): ${t.slice(0, 400)}`);
  }
}
function cdpClient(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`));
      else resolve(msg.result);
    }
  });
  const opened = new Promise((resolve, reject) => {
    ws.addEventListener("open", () => resolve(), { once: true });
    ws.addEventListener("error", () => reject(new Error("websocket error")), { once: true });
  });
  return {
    opened,
    send(method, params = {}, timeoutMs = 20000) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`CDP ${method} timed out`));
        }, timeoutMs);
        pending.set(id, {
          resolve: (v) => {
            clearTimeout(timer);
            resolve(v);
          },
          reject: (e) => {
            clearTimeout(timer);
            reject(e);
          },
        });
        ws.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      try {
        ws.close();
      } catch {}
    },
  };
}
async function fetchPageTarget(port, deadline) {
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/list`, { cache: "no-store" });
      if (res.ok) {
        const list = await res.json();
        const page = list.find((t) => t.type === "page" && /tsf-smoke/.test(String(t.url || ""))) || list.find((t) => t.type === "page");
        if (page?.webSocketDebuggerUrl) return page;
      }
    } catch {}
    await sleep(100);
  }
  return null;
}

async function main() {
  if (!fs.existsSync(EXE)) throw new Error(`missing ${EXE} (build out/WinRelease first)`);
  if (!fs.existsSync(FIXTURE)) throw new Error(`missing fixture ${FIXTURE}`);
  fs.mkdirSync(RUN, { recursive: true });
  const profileRoot = path.join(ROOT, "profiles");
  fs.mkdirSync(profileRoot, { recursive: true });
  const profile = fs.mkdtempSync(path.join(profileRoot, "tsf-"));
  const stateFile = path.join(RUN, "foreground.json");
  const report = {
    startedAt: new Date().toISOString(),
    exe: EXE,
    exeMtime: fs.statSync(EXE).mtime.toISOString(),
    fixture: FIXTURE,
    profile,
    platform: process.platform,
    node: process.version,
    args: [],
    initialForeground: null,
    window: null,
    focus: null,
    steps: {},
    tsf: null,
    sogouModules: [],
    mitigations: null,
    page: null,
    screenshot: null,
    exit: null,
    cleanup: null,
    errors: [],
    verdict: "FAIL",
  };
  const checks = [];
  const check = (name, ok, detail) => {
    checks.push({ name, ok: !!ok, detail });
    return !!ok;
  };

  let child = null;
  let browserPid = 0;
  let pageCdp = null;
  let browserCdp = null;
  let wsUrl = null;
  let stderr = "";
  let snapshotPids = [];
  let launchIso = new Date().toISOString();

  try {
    const args = [
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-sync",
      "--disable-component-update",
      "--disable-background-networking",
      "--remote-debugging-port=0",
      "--window-size=1100,850",
      "--noerrdialogs",
      "--enable-logging",
      `--log-file=${path.join(RUN, "chrome.log")}`,
      "--disable-features=Translate,MediaRouter,OptimizationHints",
      "--enable-features=NetworkServiceSandbox",
      "--new-window",
      `file:///${FIXTURE.replace(/\\/g, "/")}`,
    ];
    report.args = [EXE, ...args];
    for (const forbidden of ["--no-sandbox", "--in-process-gpu", "--single-process", "--headless", "--use-mock-keychain"]) {
      if (args.some((a) => a === forbidden || a.startsWith(`${forbidden}=`))) {
        throw new Error(`forbidden switch in TSF launch: ${forbidden}`);
      }
    }

    launchIso = new Date().toISOString();
    const t0 = Date.now();
    child = spawn(EXE, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: false });
    browserPid = child.pid;
    const exitPromise = new Promise((resolve) => {
      child.on("exit", (code, signal) => resolve({ code, signal, afterMs: Date.now() - t0 }));
    });
    child.stdout.on("data", (d) => (stderr += d.toString()));
    child.stderr.on("data", (d) => {
      stderr += d.toString();
      if (!wsUrl) {
        const m = /DevTools listening on (ws:\/\/[^\s]+)/.exec(stderr);
        if (m) wsUrl = m[1];
      }
    });
    const devtoolsDeadline = Date.now() + 25000;
    while (!wsUrl && Date.now() < devtoolsDeadline) {
      await sleep(100);
      try {
        const m = /DevTools listening on (ws:\/\/[^\s]+)/.exec(fs.readFileSync(path.join(RUN, "chrome.log"), "utf8"));
        if (m) wsUrl = m[1];
      } catch {}
      if (child.exitCode !== null) break;
    }
    if (!wsUrl) throw new Error(`no DevTools endpoint (exit=${child.exitCode}); stderr=${stderr.slice(-2000)}`);
    report.startup = { devtoolsMs: Date.now() - t0 };

    const port = Number(new URL(wsUrl).port);
    const page = await fetchPageTarget(port, Date.now() + 20000);
    if (!page) throw new Error("no page target for the TSF fixture");
    report.page = { targetUrl: page.url };

    pageCdp = cdpClient(page.webSocketDebuggerUrl);
    await pageCdp.opened;
    await pageCdp.send("Runtime.enable");
    // The fixture is local; wait until its instrumentation is installed.
    let ready = false;
    const readyDeadline = Date.now() + 20000;
    while (!ready && Date.now() < readyDeadline) {
      try {
        const v = await pageCdp.send("Runtime.evaluate", {
          expression: "({ready:window.__ready===true,title:document.title,hasFocus:document.hasFocus(),active:(document.activeElement||{}).id||null})",
          returnByValue: true,
        });
        report.page.state = v.result?.value || null;
        ready = report.page.state?.ready === true;
      } catch {}
      if (!ready) await sleep(100);
    }
    check("fixture instrumentation ready", ready, JSON.stringify(report.page.state));

    // Bring ONLY this browser window to the foreground (WS_EX owner verified by PID).
    const fg = parseJson(
      runPowerShellFile(path.join(HERE, "tsf-input.ps1"), ["-BrowserPid", String(browserPid), "-Action", "foreground", "-StateFile", stateFile]),
      "tsf-input foreground",
    );
    report.window = fg;
    check("browser window is the OS foreground window", fg.ok === true, JSON.stringify(fg));

    const assertForeground = () => {
      const state = parseJson(
        runPowerShellFile(path.join(HERE, "tsf-input.ps1"), ["-BrowserPid", String(browserPid), "-Action", "check"]),
        "tsf-input check",
      );
      if (!state.ok) {
        throw new Error(
          `refusing to type: browser window is not the OS foreground (target=${state.target}, foreground=${state.foreground} "${state.foregroundTitle}")`,
        );
      }
      return state;
    };
    const focusField = async (id) => {
      await pageCdp.send("Runtime.evaluate", {
        expression: `(() => { const el = document.getElementById(${JSON.stringify(id)}); el.focus(); return {active:(document.activeElement||{}).id||null, focus:document.hasFocus(), visibility:document.visibilityState}; })()`,
        returnByValue: true,
      });
      await sleep(200);
      const v = await pageCdp.send("Runtime.evaluate", {
        expression: "({active:(document.activeElement||{}).id||null, focus:document.hasFocus(), visibility:document.visibilityState})",
        returnByValue: true,
      });
      return v.result?.value || null;
    };
    const sendKeys = async (text, waitMs = 350) => {
      assertForeground();
      const out = parseJson(
        runPowerShellFile(path.join(HERE, "tsf-input.ps1"), ["-BrowserPid", String(browserPid), "-Action", "sendkeys", "-Text", text]),
        `tsf-input sendkeys ${JSON.stringify(text)}`,
      );
      if (out.ok !== true) throw new Error(`sendkeys refused: ${JSON.stringify(out)}`);
      await sleep(waitMs);
      return out;
    };
    const readTsf = async () => {
      const v = await pageCdp.send("Runtime.evaluate", { expression: "window.__tsf()", returnByValue: true });
      return v.result?.value || null;
    };

    // --- field 1: input -----------------------------------------------------
    // Never type unless the OS foreground is our window; assert before focus
    // and again inside every sendKeys call.
    const foregroundNow = assertForeground();
    report.foregroundCheck = foregroundNow;
    const f1 = await focusField("single");
    report.focus = f1;
    check("input field focused with OS foreground", f1?.active === "single" && f1?.focus === true, JSON.stringify(f1));
    await sendKeys("n");
    const afterN = await readTsf();
    check("real keystroke reaches the page", (afterN?.keydown || 0) >= 1, JSON.stringify({ keydown: afterN?.keydown, keys: afterN?.keys }));
    await sendKeys("ihao");
    await sendKeys(" ", 900);
    let s1 = await readTsf();
    if (!/[\u4e00-\u9fff]/.test(String(s1?.values?.single))) {
      await sendKeys("1", 700);
      s1 = await readTsf();
    }
    report.steps.input = s1;
    check("TSF composition observed (compositionstart > 0)", (s1?.composition || 0) >= 1, JSON.stringify({ composition: s1?.composition, data: s1?.compositionData }));
    check("committed CJK text in <input>", /[\u4e00-\u9fff]/.test(String(s1?.values?.single)), JSON.stringify(s1?.values));

    // --- field 2: textarea --------------------------------------------------
    const f2 = await focusField("multi");
    check("textarea focused with OS foreground", f2?.active === "multi" && f2?.focus === true, JSON.stringify(f2));
    await sendKeys("nihao");
    await sendKeys(" ", 900);
    let s2 = await readTsf();
    if (!/[\u4e00-\u9fff]/.test(String(s2?.values?.multi))) {
      await sendKeys("1", 700);
      s2 = await readTsf();
    }
    report.steps.textarea = s2;
    check("committed CJK text in <textarea>", /[\u4e00-\u9fff]/.test(String(s2?.values?.multi)), JSON.stringify(s2?.values));

    // --- field 3: contenteditable ------------------------------------------
    const f3 = await focusField("ce");
    check("contenteditable focused with OS foreground", f3?.active === "ce" && f3?.focus === true, JSON.stringify(f3));
    await sendKeys("wo");
    await sendKeys(" ", 900);
    let s3 = await readTsf();
    if (!/[\u4e00-\u9fff]/.test(String(s3?.values?.ce))) {
      await sendKeys("1", 700);
      s3 = await readTsf();
    }
    report.steps.contenteditable = s3;
    check("committed CJK text in contenteditable", /[\u4e00-\u9fff]/.test(String(s3?.values?.ce)), JSON.stringify(s3?.values));
    report.tsf = s3;

    // --- backspace on the input field --------------------------------------
    const before = String((await readTsf())?.values?.single || "");
    await focusField("single");
    await sendKeys("{BACKSPACE}", 500);
    const after = String((await readTsf())?.values?.single || "");
    check("backspace deletes a real character", after.length < before.length, JSON.stringify({ before, after }));

    // --- process + module evidence -----------------------------------------
    const snapOut = runPowerShellFile(path.join(HERE, "process-mitigations.ps1"), [
      "-RootPid",
      String(browserPid),
      "-OutJson",
      path.join(RUN, "mitigations.json"),
    ]);
    report.mitigations = parseJson(snapOut, "process-mitigations");
    snapshotPids = (report.mitigations.processes || []).map((p) => p.pid);
    const browserProc = (report.mitigations.processes || []).find((p) => p.type === "browser");
    if (browserProc) browserPid = browserProc.pid;
    const network = (report.mitigations.processes || []).find((p) => p.type === "utility:network");
    check("out-of-process GPU present (no --in-process-gpu)", !!(report.mitigations.processes || []).find((p) => p.type === "gpu"), JSON.stringify(report.mitigations.types));
    check("renderer process present", !!(report.mitigations.processes || []).find((p) => p.type === "renderer"), JSON.stringify(report.mitigations.types));
    check(
      "network utility sandboxed (Low IL + AppContainer) with NetworkServiceSandbox",
      !!network && network.integrityLevel === "Low" && network.appContainer === 1,
      JSON.stringify(network ? { pid: network.pid, il: network.integrityLevel, ac: network.appContainer, cig: network.mitigations?.Signature?.raw } : null),
    );

    // Read-only module query: TSF/IME DLLs live in the browser process.
    const moduleQuery = spawnSync(
      PS,
      [
        "-NoProfile",
        "-Command",
        `$m = (Get-Process -Id ${browserPid}).Modules | Where-Object { $_.ModuleName -match 'sogou|SogouPY' -or $_.FileName -match 'Sogou' } | ForEach-Object { [pscustomobject]@{ name=$_.ModuleName; path=$_.FileName } }; @($m) | ConvertTo-Json -Depth 3`,
      ],
      { encoding: "utf8", windowsHide: true, timeout: 60000 },
    );
    try {
      const parsed = parseJson(moduleQuery.stdout, "sogou modules");
      report.sogouModules = Array.isArray(parsed) ? parsed : parsed ? [parsed] : [];
    } catch (e) {
      report.errors.push(`sogou modules: ${e.message}`);
    }
    check("Sogou TSF modules loaded in the browser process", report.sogouModules.length >= 1, JSON.stringify(report.sogouModules));

    // --- screenshot (visible window, real content) -------------------------
    try {
      await pageCdp.send("Page.enable");
      const shot = await pageCdp.send("Page.captureScreenshot", { format: "png" }, 20000);
      const png = path.join(RUN, "tsf-window.png");
      fs.writeFileSync(png, Buffer.from(shot.data, "base64"));
      report.screenshot = png;
    } catch (e) {
      report.errors.push(`screenshot: ${e.message}`);
    }

    // --- restore foreground ------------------------------------------------
    try {
      const restored = parseJson(
        runPowerShellFile(path.join(HERE, "tsf-input.ps1"), ["-BrowserPid", String(browserPid), "-Action", "restore", "-StateFile", stateFile]),
        "tsf-input restore",
      );
      report.foregroundRestored = restored;
    } catch (e) {
      report.errors.push(`restore foreground: ${e.message}`);
    }
  } catch (e) {
    report.errors.push(e.message);
  } finally {
    // Close through CDP only if the browser is still alive.
    if (child && child.exitCode === null && wsUrl) {
      browserCdp = cdpClient(wsUrl);
      try {
        await Promise.race([browserCdp.opened, sleep(3000)]);
        await browserCdp.send("Browser.close", {}, 5000).catch(() => {});
      } finally {
        browserCdp.close();
      }
    }
    if (child) {
      let exit = null;
      if (child.exitCode !== null) {
        exit = { code: child.exitCode, signal: child.signalCode };
      } else {
        exit = await Promise.race([
          new Promise((resolve) => child.on("exit", (code, signal) => resolve({ code, signal }))),
          sleep(12000).then(() => null),
        ]);
      }
      if (exit) report.exit = exit;
      else {
        report.errors.push("browser did not exit after Browser.close");
        try {
          child.kill();
        } catch {}
      }
    }
    // Verified cleanup of every PID this run observed.
    if (snapshotPids.length) {
      try {
        report.cleanup = parseJson(
          runPowerShellFile(
            path.join(HERE, "stop-verified.ps1"),
            ["-ProcessId", snapshotPids.join(","), "-RootPid", String(browserPid), "-ExpectedExe", EXE_WIN, "-NotBeforeUtc", launchIso],
            60000,
          ),
          "stop-verified",
        );
      } catch (e) {
        report.errors.push(`cleanup: ${e.message}`);
      }
    }
    try {
      fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
      report.profileRemoved = true;
    } catch (e) {
      report.profileRemoved = false;
      report.errors.push(`profile remove: ${e.message}`);
    }
    report.finishedAt = new Date().toISOString();
    report.checks = checks;
    report.verdict = checks.every((c) => c.ok) && report.errors.length === 0 && report.exit?.code === 0 ? "PASS" : "FAIL";
    fs.writeFileSync(path.join(RUN, "report.json"), JSON.stringify(report, null, 2));
    console.log(`report: ${path.join(RUN, "report.json")}`);
    console.log(`verdict: ${report.verdict}`);
    for (const c of checks) console.log(`  ${c.ok ? "PASS" : "FAIL"}  ${c.name}${c.ok ? "" : ` :: ${String(c.detail).slice(0, 300)}`}`);
    if (report.errors.length) {
      console.log("errors:");
      for (const e of report.errors) console.log(`  - ${e}`);
    }
    if (report.verdict !== "PASS") process.exitCode = 1;
  }
}

main().catch((e) => {
  console.error(`tsf-smoke: ${e.stack || e.message}`);
  process.exitCode = 1;
});
