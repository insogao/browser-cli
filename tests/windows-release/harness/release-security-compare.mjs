#!/usr/bin/env node
// Windows release-shape security matrix.
//
// browser-launch-hygiene: windows-only (no keychain)
// Profile isolation: fs.mkdtempSync (Node's mktemp equivalent), one fresh
// directory per run, removed only after the run's verified processes are gone.
//
// Compares the non-component out/WinRelease build against out/WinDev
// (component developer build) and official Google Chrome:
//   * process mitigations (browser / GPU / renderer / network utility / storage)
//   * integrity level and AppContainer state
//   * module counts and per-module Authenticode status (attack surface)
//   * first-observed CIG/ACG value at child birth (pre-start vs delayed)
//   * startup timing (launch -> DevTools listening, launch -> first child of
//     each type, launch -> page ready)
//
// Test-only hygiene (fail closed, see AGENTS.md):
//   - every run uses a fresh isolated --user-data-dir created by this script;
//   - no --no-sandbox, no --in-process-gpu, no --single-process;
//   - no global kill: cleanup stops only PIDs whose exe path, unique run marker
//     and creation time are verified (tests/windows-release/harness/stop-verified.ps1);
//   - --headless=new is used ONLY for the no-window security matrix. It is not
//     product-launch configuration and it is never TSF/IME evidence; the TSF
//     smoke test uses a visible window instead.
//
// Usage:
//   node tests/windows-release/harness/release-security-compare.mjs
//   node tests/windows-release/harness/release-security-compare.mjs --repeat=3
//   node tests/windows-release/harness/release-security-compare.mjs --scenarios=winrelease-default,chrome-default
//
// Output: <root>/summary.json, <root>/summary.md, one run.json per run.

import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..", "..", "..");
const ROOT = path.resolve(process.env.WINDOWS_RELEASE_EVIDENCE || "D:/c/logs/winrelease/matrix");
const HARNESS = HERE;
const PS = "powershell.exe";
const CHECKOUT = process.env.BROWSER_CLI_CHROMIUM_SRC || "D:/c/src";

const argv = process.argv.slice(2);
const argValue = (name, def) => {
  const hit = argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : def;
};
const REPEAT = Math.max(1, Number(argValue("repeat", "1")) || 1);
const SCENARIO_FILTER = new Set(
  String(argValue("scenarios", ""))
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),
);

const CHROME_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
];
function firstExisting(paths) {
  return paths.find((p) => fs.existsSync(p)) || null;
}

function parseEnvFile(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let v = m[2].trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    out[m[1]] = v;
  }
  return out;
}
const FLAGS = parseEnvFile(path.join(REPO, "config", "windows-release-runtime-flags.env"));
const ENABLE_FEATURES = FLAGS.WINDOWS_RELEASE_ENABLE_FEATURES || "NetworkServiceSandbox";
const FORBIDDEN_FEATURES = (FLAGS.WINDOWS_RELEASE_FORBIDDEN_FEATURES || "BrowserDynamicCodeDisabled")
  .split(/\s+/)
  .filter(Boolean);
const FORBIDDEN_SWITCHES = (FLAGS.WINDOWS_RELEASE_FORBIDDEN_SWITCHES || "")
  .split(/\s+/)
  .filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const nowIso = () => new Date().toISOString();
const utcStamp = (d = new Date()) => d.toISOString();

function runPowerShellFile(file, args, timeoutMs = 240000) {
  const res = spawnSync(
    PS,
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", file, ...args],
    { encoding: "utf8", windowsHide: true, timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024 },
  );
  if (res.error) throw new Error(`${path.basename(file)} failed: ${res.error.message}`);
  if (res.status !== 0) {
    throw new Error(`${path.basename(file)} rc=${res.status}: ${(res.stderr || "").slice(0, 2000)}`);
  }
  return res.stdout;
}

function parseJson(text, what) {
  const trimmed = String(text || "").trim();
  if (!trimmed) throw new Error(`${what}: empty output`);
  try {
    return JSON.parse(trimmed);
  } catch (e) {
    throw new Error(`${what}: invalid JSON (${e.message}): ${trimmed.slice(0, 500)}`);
  }
}

function makePageServer() {
  const server = http.createServer((req, res) => {
    if (req.url === "/data.json") {
      res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
      res.end(JSON.stringify({ ok: true, nonce: Math.random().toString(16).slice(2) }));
      return;
    }
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(
      `<!doctype html><meta charset="utf-8"><title>bcli-release-probe</title>` +
        `<h1 id="h">browser-cli release probe</h1>` +
        `<div id="data">pending</div><script>` +
        `fetch('/data.json').then(r=>r.json()).then(j=>{document.getElementById('data').textContent=JSON.stringify(j);` +
        `document.title='bcli-release-probe-ok';}).catch(e=>{document.getElementById('data').textContent='ERR '+e});` +
        `</script>`,
    );
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      resolve({ server, port, url: `http://127.0.0.1:${port}/index.html` });
    });
  });
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
    send(method, params = {}, timeoutMs = 15000) {
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

async function fetchPageTarget(port, wantUrl, deadlineMs) {
  while (Date.now() < deadlineMs) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/list`, { cache: "no-store" });
      if (res.ok) {
        const list = await res.json();
        const page = list.find(
          (t) => t.type === "page" && String(t.url || "").startsWith(wantUrl.split("?")[0].slice(0, 24)),
        ) || list.find((t) => t.type === "page");
        if (page && page.webSocketDebuggerUrl) return page;
      }
    } catch {}
    await sleep(100);
  }
  return null;
}

function classifyArgs(args) {
  const findings = [];
  for (const a of args) {
    for (const sw of FORBIDDEN_SWITCHES) {
      if (a === sw || a.startsWith(`${sw}=`)) findings.push({ kind: "switch", value: a, why: sw });
    }
  }
  for (const a of args) {
    if (a.startsWith("--enable-features=")) {
      const feats = a.slice("--enable-features=".length).split(",");
      for (const f of feats) {
        if (FORBIDDEN_FEATURES.includes(f.split("<")[0])) {
          findings.push({ kind: "feature", value: f, why: "forbidden feature enabled" });
        }
      }
    }
  }
  return findings;
}

function flattenRun(run) {
  const procs = run.mitigations?.processes || [];
  const byType = {};
  for (const p of procs) {
    if (!byType[p.type]) byType[p.type] = [];
    byType[p.type].push(p);
  }
  const pick = (t) => {
    const candidates = byType[t] || [];
    // A renderer can exit mid-snapshot; prefer a process whose token query
    // succeeded instead of reporting "-" just because the last one was exiting.
    const p = candidates.find((c) => c.integrityLevel !== null) || candidates[0];
    if (!p) return null;
    return {
      pid: p.pid,
      integrityLevel: p.integrityLevel,
      appContainer: p.appContainer,
      cig: p.mitigations?.Signature?.raw ?? null,
      cigStatus: p.mitigations?.Signature?.status ?? null,
      acg: p.mitigations?.DynamicCode?.raw ?? null,
      acgStatus: p.mitigations?.DynamicCode?.status ?? null,
      extPt: p.mitigations?.ExtensionPointDisable?.bits?.DisableExtensionPoints ?? null,
      child: p.mitigations?.ChildProcess?.bits?.NoChildProcessCreation ?? null,
      font: p.mitigations?.FontDisable?.bits?.DisableNonSystemFonts ?? null,
      imgNoRemote: p.mitigations?.ImageLoad?.bits?.NoRemoteImages ?? null,
      win32k: p.mitigations?.SystemCallFilter?.raw ?? null,
      moduleCount: p.moduleCount,
      installDirModuleCount: p.installDirModuleCount,
      installDirModules: p.installDirModules ?? null,
      moduleSignature: p.moduleSignature,
      exeSignature: p.signature?.status ?? null,
    };
  };
  const children = run.watcher?.children || [];
  const lead = Number(run.watcherLeadMs || 0);
  const firstByType = {};
  for (const c of children.sort((a, b) => a.firstSeenDelayFromLaunchMs - b.firstSeenDelayFromLaunchMs)) {
    if (firstByType[c.type] === undefined) {
      firstByType[c.type] = Math.max(0, c.firstSeenDelayFromLaunchMs - lead);
    }
  }
  return {
    scenario: run.scenario,
    repeat: run.repeat,
    ok: run.ok,
    browser: pick("browser"),
    gpu: pick("gpu"),
    renderer: pick("renderer"),
    network: pick("utility:network"),
    storage: pick("utility:storage"),
    processCount: procs.length,
    types: run.mitigations?.types || [],
    startup: run.startup,
    firstChildByTypeMs: firstByType,
    watcherChildren: children.map((c) => ({
      pid: c.pid,
      type: c.type,
      firstCigRaw: c.firstCigRaw,
      firstCigPresentAtBirth: c.firstCigPresentAtBirth,
      cigAppliedLater: c.cigAppliedLater,
      cigAppliedDelayFromCreationMs: c.cigAppliedDelayFromCreationMs,
      firstSeenDelayFromLaunchMs: Math.max(0, c.firstSeenDelayFromLaunchMs - lead),
    })),
    page: run.page,
    exit: run.exit,
    argFindings: run.argFindings,
    errors: run.errors,
  };
}

function markdownSummary(runs) {
  const lines = [];
  lines.push("# Windows release-shape security matrix (generated)");
  lines.push("");
  lines.push(`Generated: ${nowIso()}`);
  lines.push("");
  lines.push("| scenario | rep | proc | browser CIG/IL | gpu CIG/IL | renderer CIG/IL | network CIG/IL/AC | modules b/g/r/n | startup ms (devtools/page) | exit |");
  lines.push("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |");
  const fmt = (p, withAc = false) =>
    p ? `${p.cig ?? "-"}/${p.integrityLevel ?? "-"}${withAc ? `/AC${p.appContainer}` : ""}` : "-";
  const mods = (p) => (p ? `${p.moduleCount ?? "-"}(${p.installDirModuleCount ?? "-"})` : "-");
  for (const r of runs) {
    lines.push(
      `| ${r.scenario} | ${r.repeat} | ${r.processCount} | ${fmt(r.browser)} | ${fmt(r.gpu)} | ${fmt(r.renderer)} | ${fmt(
        r.network,
        true,
      )} | ${mods(r.browser)}/${mods(r.gpu)}/${mods(r.renderer)}/${mods(r.network)} | ${
        r.startup?.devtoolsMs ?? "-"
      }/${r.startup?.pageReadyMs ?? "-"} | ${r.exit?.code ?? "-"} |`,
    );
  }
  lines.push("");
  lines.push("## CIG at birth (pre-start vs delayed)");
  lines.push("");
  lines.push("| scenario | process | first CIG | present at birth | applied later | delay from creation ms |");
  lines.push("| --- | --- | --- | --- | --- | --- |");
  for (const r of runs) {
    for (const c of r.watcherChildren) {
      lines.push(
        `| ${r.scenario}#${r.repeat} | ${c.type}(${c.pid}) | ${c.firstCigRaw ?? "-"} | ${c.firstCigPresentAtBirth} | ${c.cigAppliedLater} | ${
          c.cigAppliedDelayFromCreationMs ?? "-"
        } |`,
      );
    }
  }
  return lines.join("\n");
}

async function runOne(scenario, exe, featureArgs, repeat) {
  const runDir = path.join(ROOT, scenario, `run-${repeat}`);
  fs.mkdirSync(runDir, { recursive: true });
  const profileRoot = path.join(ROOT, "profiles");
  fs.mkdirSync(profileRoot, { recursive: true });
  const profile = fs.mkdtempSync(path.join(profileRoot, `${scenario}-`));
  const run = {
    scenario,
    repeat,
    exe,
    profile,
    startedAt: nowIso(),
    args: [],
    featureArgs,
    argFindings: [],
    ok: false,
    errors: [],
    startup: {},
    page: null,
    mitigations: null,
    watcher: null,
    exit: null,
  };
  let pageServer = null;
  let watcherProc = null;
  let watcherOut = "";
  let child = null;
  let snapshotPids = [];
  const rootPidFile = path.join(runDir, "root.pid");
  const t0 = Date.now();
  const launchIso = utcStamp(new Date());

  try {
    pageServer = await makePageServer();
    const watcherStartedAt = Date.now();
    const watcherPromise = new Promise((resolve) => {
      watcherProc = spawn(
        PS,
        [
          "-NoProfile",
          "-ExecutionPolicy",
          "Bypass",
          "-File",
          path.join(HARNESS, "watch-child-mitigations.ps1"),
          "-RootPidFile",
          rootPidFile,
          "-ExePath",
          exe,
          "-DurationMs",
          "10000",
          "-IntervalMs",
          "10",
          "-OutJsonl",
          path.join(runDir, "children.jsonl"),
        ],
        { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
      );
      watcherProc.stdout.on("data", (d) => (watcherOut += d.toString()));
      watcherProc.on("close", resolve);
      watcherProc.on("error", resolve);
    });
    await sleep(250);

    const args = [
      "--headless=new",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-sync",
      "--disable-component-update",
      "--disable-background-networking",
      "--remote-debugging-port=0",
      "--window-size=900,700",
      "--noerrdialogs",
      "--enable-logging",
      `--log-file=${path.join(runDir, "chrome.log")}`,
      "--disable-features=Translate,MediaRouter,OptimizationHints",
      ...featureArgs,
      pageServer.url,
    ];
    run.args = [exe, ...args];
    // --headless=new is a matrix-only switch (no visible window); it is not
    // product-launch configuration and is never TSF/IME evidence. Keep every
    // other forbidden-switch finding fail-closed.
    run.argFindings = classifyArgs(args).filter((f) => !(f.kind === "switch" && f.why === "--headless"));
    run.testOnlySwitches = ["--headless=new (security matrix only; not product launch)"];

    const spawnedAt = Date.now();
    run.watcherLeadMs = spawnedAt - watcherStartedAt;
    child = spawn(exe, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    fs.writeFileSync(rootPidFile, String(child.pid));
    let stderr = "";
    let devtoolsMs = null;
    let wsUrl = null;
    const exitPromise = new Promise((resolve) => {
      child.on("exit", (code, signal) => resolve({ code, signal, at: Date.now() }));
    });
    child.stdout.on("data", (d) => (stderr += d.toString()));
    child.stderr.on("data", (d) => {
      stderr += d.toString();
      if (!wsUrl) {
        const m = /DevTools listening on (ws:\/\/[^\s]+)/.exec(stderr);
        if (m) {
          wsUrl = m[1];
          devtoolsMs = Date.now() - spawnedAt;
        }
      }
    });

    // Also recover the DevTools endpoint from the log file if stderr missed it.
    const devtoolsDeadline = Date.now() + 20000;
    while (!wsUrl && Date.now() < devtoolsDeadline) {
      await sleep(100);
      try {
        const logTxt = fs.readFileSync(path.join(runDir, "chrome.log"), "utf8");
        const m = /DevTools listening on (ws:\/\/[^\s]+)/.exec(logTxt);
        if (m) {
          wsUrl = m[1];
          devtoolsMs = Date.now() - spawnedAt;
        }
      } catch {}
      if (child.exitCode !== null) break;
    }
    run.startup.devtoolsMs = devtoolsMs;
    if (!wsUrl) throw new Error(`no DevTools endpoint (exitCode=${child.exitCode}); stderr=${stderr.slice(-1500)}`);

    const port = Number(new URL(wsUrl).port);
    const page = await fetchPageTarget(port, pageServer.url, Date.now() + 15000);
    if (page) {
      const c = cdpClient(page.webSocketDebuggerUrl);
      try {
        await c.opened;
        await c.send("Runtime.enable");
        let state = null;
        const pageDeadline = Date.now() + 15000;
        while (Date.now() < pageDeadline) {
          try {
            const ev = await c.send("Runtime.evaluate", {
              expression:
                "({ready:document.readyState,title:document.title,text:(document.body?document.body.innerText:'').slice(0,120),url:location.href})",
              returnByValue: true,
            });
            state = ev.result?.value || null;
            if (state && state.ready === "complete" && /bcli-release-probe-ok/.test(state.title)) break;
          } catch {}
          await sleep(100);
        }
        run.page = {
          ok: !!state && /bcli-release-probe-ok/.test(state.title) && /"ok":true/.test(state.text || ""),
          readyMs: Date.now() - spawnedAt,
          final: state,
          targetUrl: page.url,
        };
      } finally {
        c.close();
      }
    } else {
      run.page = { ok: false, error: "no page target", readyMs: null };
    }
    run.startup.pageReadyMs = run.page?.readyMs ?? null;

    // Let GPU/network settle, then snapshot the live process tree.
    await sleep(1500);
    const snapPath = path.join(runDir, "mitigations.json");
    const snapOut = runPowerShellFile(path.join(HARNESS, "process-mitigations.ps1"), [
      "-RootPid",
      String(child.pid),
      "-OutJson",
      snapPath,
    ]);
    run.mitigations = parseJson(snapOut, "process-mitigations");
    snapshotPids = (run.mitigations.processes || []).map((p) => p.pid);

    // The watcher is independent: wait for its summary.
    await Promise.race([watcherPromise, sleep(12000)]);
    try {
      run.watcher = parseJson(watcherOut, "watch-child-mitigations");
    } catch (e) {
      run.errors.push(`watcher: ${e.message}`);
    }

    // Graceful close through the browser CDP endpoint.
    if (wsUrl) {
      const c = cdpClient(wsUrl);
      try {
        await Promise.race([c.opened, sleep(3000)]);
        await c.send("Browser.close", {}, 5000).catch(() => {});
      } finally {
        c.close();
      }
    }
    const exit = await Promise.race([exitPromise, sleep(12000).then(() => null)]);
    if (exit) {
      run.exit = { code: exit.code, signal: exit.signal, afterMs: exit.at - spawnedAt };
    } else {
      run.exit = { code: null, signal: "timeout", afterMs: Date.now() - spawnedAt };
      run.errors.push("browser did not exit after Browser.close");
    }
    run.stderrTail = stderr.slice(-4000);
    run.ok =
      run.argFindings.length === 0 &&
      run.errors.length === 0 &&
      run.exit.code === 0 &&
      !!run.page?.ok &&
      (run.mitigations.processes || []).some((p) => p.type === "renderer") &&
      (run.mitigations.processes || []).some((p) => p.type === "gpu");

    // Cleanup: verified PIDs only. Include watcher-observed children in case the
    // browser exited but crashpad/utility children survived.
    const allPids = new Set(snapshotPids);
    for (const c of run.watcher?.children || []) allPids.add(c.pid);
    try {
      const stopOut = runPowerShellFile(
        path.join(HARNESS, "stop-verified.ps1"),
        [
          "-ProcessId",
          [...allPids].join(","),
          "-RootPid",
          String(child.pid),
          "-ExpectedExe",
          exe,
          "-NotBeforeUtc",
          launchIso,
        ],
        60000,
      );
      run.cleanup = parseJson(stopOut, "stop-verified");
    } catch (e) {
      run.errors.push(`cleanup: ${e.message}`);
    }
  } catch (e) {
    run.errors.push(e.message);
    if (child && child.exitCode === null) {
      try {
        child.kill();
      } catch {}
    }
    if (snapshotPids.length) {
      try {
        runPowerShellFile(
          path.join(HARNESS, "stop-verified.ps1"),
          [
            "-ProcessId",
            snapshotPids.join(","),
            "-RootPid",
            String(child.pid),
            "-ExpectedExe",
            exe,
            "-NotBeforeUtc",
            launchIso,
          ],
          60000,
        );
      } catch {}
    }
  } finally {
    if (watcherProc && watcherProc.exitCode === null) {
      try {
        watcherProc.kill();
      } catch {}
    }
    if (pageServer) pageServer.server.close();
    // A profile directory is removed only for this run's unique path.
    try {
      fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
      run.profileRemoved = true;
    } catch (e) {
      run.profileRemoved = false;
      run.errors.push(`profile remove: ${e.message}`);
    }
    run.finishedAt = nowIso();
    fs.writeFileSync(path.join(runDir, "run.json"), JSON.stringify(run, null, 2));
    run.durationMs = Date.now() - t0;
  }
  return flattenRun(run);
}

async function main() {
  fs.mkdirSync(ROOT, { recursive: true });
  const winDev = path.join(CHECKOUT, "out", "WinDev", "chrome.exe");
  const winRelease = path.join(CHECKOUT, "out", "WinRelease", "chrome.exe");
  const chrome = firstExisting(CHROME_CANDIDATES);

  const scenarios = [];
  const add = (name, exe, featureArgs) => {
    if (!exe || !fs.existsSync(exe)) {
      scenarios.push({ name, exe, featureArgs, missing: true });
      return;
    }
    scenarios.push({ name, exe, featureArgs });
  };
  add("winrelease-default", winRelease, []);
  add("winrelease-netsandbox", winRelease, [`--enable-features=${ENABLE_FEATURES}`]);
  add("windev-default", winDev, []);
  add("windev-netsandbox", winDev, [`--enable-features=${ENABLE_FEATURES}`]);
  add("chrome-default", chrome, []);
  add("chrome-netsandbox", chrome, [`--enable-features=${ENABLE_FEATURES}`]);

  const selected = scenarios.filter((s) => !SCENARIO_FILTER.size || SCENARIO_FILTER.has(s.name));
  if (!selected.length) throw new Error("no scenario selected");

  const runs = [];
  const hardware = {
    host: os.hostname(),
    os: `${os.platform()} ${os.release()}`,
    cpus: os.cpus().length,
    totalMemGiB: Math.round((os.totalmem() / 1024 ** 3) * 10) / 10,
    node: process.version,
  };
  fs.writeFileSync(path.join(ROOT, "hardware.json"), JSON.stringify(hardware, null, 2));

  for (const s of selected) {
    if (s.missing) {
      console.log(`SKIP  ${s.name}: missing ${s.exe}`);
      runs.push({ scenario: s.name, repeat: 0, ok: false, errors: [`missing ${s.exe}`], processCount: 0, watcherChildren: [] });
      continue;
    }
    for (let i = 1; i <= REPEAT; i++) {
      console.log(`RUN   ${s.name} #${i} -> ${s.exe}`);
      const flat = await runOne(s.name, s.exe, s.featureArgs, i);
      runs.push(flat);
      console.log(
        `      ok=${flat.ok} proc=${flat.processCount} devtools=${flat.startup?.devtoolsMs}ms page=${flat.startup?.pageReadyMs}ms exit=${
          flat.exit?.code
        }${flat.errors.length ? ` errors=${flat.errors.join("; ").slice(0, 200)}` : ""}`,
      );
    }
  }

  const summary = { generatedAt: nowIso(), hardware, flags: FLAGS, repeat: REPEAT, runs };
  fs.writeFileSync(path.join(ROOT, "summary.json"), JSON.stringify(summary, null, 2));
  fs.writeFileSync(path.join(ROOT, "summary.md"), markdownSummary(runs));
  console.log(`\nsummary: ${path.join(ROOT, "summary.json")}`);
  console.log(markdownSummary(runs));
}

main().catch((e) => {
  console.error(`release-security-compare: ${e.stack || e.message}`);
  process.exitCode = 1;
});
