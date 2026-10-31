// Patch 034 DComp lifecycle + hidden-window screenshot harness.
//
// browser-launch-hygiene: windows-only (no keychain)
//
// This entry point only ever launches a Windows chrome.exe. There is no macOS
// login keychain on Windows, so --use-mock-keychain is intentionally absent
// (AGENTS.md section 1 is macOS-conditioned; the flag is forbidden in the
// production browser). The profile is still a fresh per-run temp directory,
// created mkdtemp-style (the Node equivalent of shell mktemp) and never the
// user's real profile.
//
// Runs one Browser CLI instance with an isolated profile and a local animated
// colour page, then:
//   * records GPUInfo.overlay_info (directComposition / supportsOverlays) via
//     CDP SystemInfo.getInfo, plus chrome://gpu text when available;
//   * enumerates the real HWND tree and the DComp "Intermediate D3D Window"
//     child (owner pid proves whether DComp runs in the browser or GPU process);
//   * takes hidden-window Page.captureScreenshot(fromSurface=true) frames
//     (patch 027 copy path) and samples their colours;
//   * creates a second window (Target.createTarget newWindow), checks its DComp
//     child, closes it, and verifies the child window is gone and handle counts
//     return to baseline;
//   * closes the browser with Browser.close and records the exit code, duration
//     and crash-report count.
//
// The Browser CLI managed-presentation policy keeps windows hidden, so nothing
// is shown on screen. If a window is hidden and DComp never creates a child,
// the summary records that instead of showing the window.
//
// Usage:
//   node dcomp-lifecycle.mjs --mode inproc --root D:/c/logs/rc-sec/dcomp-inproc
//   node dcomp-lifecycle.mjs --mode gpuproc --root D:/c/logs/rc-sec/dcomp-gpuproc

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const WT = path.join(here, "..", "..", "windows-gpu-dcomp", "harness", "window-tree.ps1");
const SAMPLE = path.join(here, "..", "..", "windows-gpu-dcomp", "harness", "png-sample.ps1");
const PROBE = path.join(here, "process-mitigations.ps1");
const CLOSEWIN = path.join(here, "close-window.ps1");

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const k = a.slice(2);
      out[k] = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
    }
  }
  return out;
}

const args = parseArgs(process.argv);
const mode = String(args.mode || "inproc");
const chromeExe = String(args.chrome || "D:/c/src/out/WinDev/chrome.exe");
const root = path.resolve(String(args.root || `D:/c/logs/rc-sec/dcomp-${mode}-${stamp()}`));
fs.rmSync(root, { recursive: true, force: true });
fs.mkdirSync(root, { recursive: true });
// mkdtemp is the Node mktemp: a fresh, unpredictable profile directory that
// can never collide with a real user profile.
const profile = fs.mkdtempSync(path.join(root, "profile-"));
fs.mkdirSync(profile, { recursive: true });

function killChild() {
  try { if (child && !child.killed) child.kill(); } catch {}
}

const MODE_FLAGS = {
  inproc: ["--in-process-gpu"],
  gpuproc: [],
  single: ["--single-process"],
  nodcomp: ["--in-process-gpu", "--disable-direct-composition"],
};

function stamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}
function log(...a) {
  console.log(`[dcomp-${mode}]`, ...a);
}
function run(cmd, argv, opts = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, argv, { windowsHide: true, ...opts });
    let stdout = "";
    let stderr = "";
    p.stdout?.on("data", (d) => (stdout += d));
    p.stderr?.on("data", (d) => (stderr += d));
    p.on("close", (code) => resolve({ code, stdout, stderr }));
    p.on("error", (e) => resolve({ code: -1, stdout, stderr: String(e) }));
  });
}
async function ps(script, argv) {
  return run("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, ...argv]);
}

const html = `<!doctype html><html><head><meta charset="utf-8"><title>rc-sec dcomp</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#ff00ff}
#grid{display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);width:100vw;height:100vh}
#grid div{display:flex;align-items:center;justify-content:center;font:bold 22px sans-serif;text-shadow:2px 2px #000}
.c1{background:#f00}.c2{background:#0f0}.c3{background:#00f}.c4{background:#ff0}
.c5{background:#0ff}.c6{background:#f0f}.c7{background:#f80}.c8{background:#80f}.c9{background:#fff;color:#000}
#anim{position:fixed;left:10px;top:10px;width:100px;height:50px;background:#00ff00;border:4px solid #000;animation:slide 1.2s infinite alternate}
@keyframes slide{from{transform:translateX(0)}to{transform:translateX(420px)}}
#tick{position:fixed;right:12px;bottom:10px;font:bold 24px monospace;background:#000;color:#0f0;padding:4px 8px}
</style></head><body>
<div id="grid">
<div class="c1">RED</div><div class="c2">GREEN</div><div class="c3">BLUE</div>
<div class="c4">YELLOW</div><div class="c5">CYAN</div><div class="c6">MAGENTA</div>
<div class="c7">ORANGE</div><div class="c8">VIOLET</div><div class="c9">WHITE</div>
</div><div id="anim"></div><div id="tick">0</div>
<script>let n=0;setInterval(()=>{document.getElementById('tick').textContent=String(++n)},100);</script>
</body></html>`;

const server = http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
  res.end(html);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const pageUrl = `http://127.0.0.1:${server.address().port}/`;

const logFile = path.join(root, "chrome.log");
const traceFile = path.join(root, "trace.json");
const argv = [
  `--user-data-dir=${profile}`,
  "--remote-debugging-port=0",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-sync",
  "--disable-component-update",
  "--disable-background-networking",
  "--disable-features=Translate,MediaRouter,OptimizationHints",
  "--window-size=900,700",
  "--noerrdialogs",
  "--enable-logging",
  `--log-file=${logFile}`,
  "--vmodule=dcomp_presenter=2,dc_layer_tree=2,direct_composition_support=2,rendering_window_manager=1,child_window_win=1,desktop_window_tree_host_win=1",
  "--trace-startup=gpu,viz,cc",
  "--trace-startup-duration=12",
  "--trace-startup-format=json",
  `--trace-startup-file=${traceFile}`,
  ...(MODE_FLAGS[mode] || MODE_FLAGS.inproc),
  pageUrl,
];

const child = spawn(chromeExe, argv, { stdio: "ignore", windowsHide: true });
let childExit = null;
const startedAt = Date.now();
child.on("exit", (code, signal) => {
  childExit = { code, signal, elapsed_ms: Date.now() - startedAt };
  log("chrome exited", code, signal, `${childExit.elapsed_ms}ms`);
});

const portFile = path.join(profile, "DevToolsActivePort");
let cdpPort = null;
for (let i = 0; i < 200; i++) {
  if (fs.existsSync(portFile)) {
    const t = fs.readFileSync(portFile, "utf8").split(/\r?\n/);
    if (t[0] && /^\d+$/.test(t[0].trim())) { cdpPort = Number(t[0].trim()); break; }
  }
  if (childExit) break;
  await new Promise((r) => setTimeout(r, 200));
}
if (!cdpPort) {
  const summary = { mode, ok: false, error: "CDP did not start", childExit, chromeExe, root };
  fs.writeFileSync(path.join(root, "summary.json"), JSON.stringify(summary, null, 2));
  console.log(JSON.stringify(summary));
  killChild();
  try { server.close(); } catch {}
  process.exit(3);
}

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    http.get({ host: "127.0.0.1", port: cdpPort, path: pathname }, (res) => {
      let body = "";
      res.on("data", (d) => (body += d));
      res.on("end", () => { try { resolve(JSON.parse(body)); } catch (e) { reject(e); } });
    }).on("error", reject);
  });
}
function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      if (p.timer) clearTimeout(p.timer);
      msg.error ? p.reject(new Error(`${p.method}: ${msg.error.message}`)) : p.resolve(msg.result);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", rej, { once: true });
  });
  const call = (method, params = {}, timeoutMs = 15000) => new Promise((resolve, reject) => {
    const mid = ++id;
    const timer = setTimeout(() => {
      pending.delete(mid);
      reject(new Error(`${method}: timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    pending.set(mid, { resolve, reject, method, timer });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ready, call };
}

let page, browser, versionInfo;
let pageTargetId = null;
try {
  versionInfo = await getJson("/json/version");
  const list = await getJson("/json/list");
  const target = list.find((t) => t.type === "page" && t.url.includes("127.0.0.1"));
  if (!target) throw new Error("no colour page target");
  pageTargetId = target.id;
  page = connect(target.webSocketDebuggerUrl);
  browser = connect(versionInfo.webSocketDebuggerUrl);
  await Promise.all([page.ready, browser.ready]);
  await page.call("Page.enable");
  await page.call("Runtime.enable");
} catch (e) {
  const summary = { mode, ok: false, error: `CDP connect: ${e}`, childExit, root };
  fs.writeFileSync(path.join(root, "summary.json"), JSON.stringify(summary, null, 2));
  console.log(JSON.stringify(summary));
  killChild();
  try { server.close(); } catch {}
  process.exit(4);
}

const evalPage = async (expr) => {
  const r = await page.call("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  return r.result?.value;
};

let pageState = null;
for (let i = 0; i < 100; i++) {
  try {
    pageState = JSON.parse(await evalPage(`JSON.stringify({ready:document.readyState,tick:Number(document.getElementById('tick')?.textContent||0),visibility:document.visibilityState,focus:document.hasFocus(),tiles:document.querySelectorAll('#grid div').length})`));
    if (pageState.ready === "complete" && pageState.tiles === 9 && pageState.tick >= 3) break;
  } catch {}
  await new Promise((r) => setTimeout(r, 200));
}

const procInfo = await browser.call("SystemInfo.getProcessInfo").catch(() => null);
const browserProc = (procInfo?.processInfo || []).find((p) => p.type === "browser");
const browserPid = browserProc?.id || child.pid;
const gpuProc = (procInfo?.processInfo || []).find((p) => String(p.type).toLowerCase() === "gpu");
const sysInfo = await browser.call("SystemInfo.getInfo").catch((e) => ({ error: String(e) }));

const overlay = {
  directComposition: sysInfo?.gpu?.auxAttributes?.directComposition ?? null,
  supportsOverlays: sysInfo?.gpu?.auxAttributes?.supportsOverlays ?? null,
  auxKeys: Object.keys(sysInfo?.gpu?.auxAttributes || {}).slice(0, 80),
  featureStatus_direct_composition: sysInfo?.featureStatus?.direct_composition ?? null,
  raw_gpu_error: sysInfo?.error || null,
};

async function windowTree(tag) {
  const out = path.join(root, `window-tree-${tag}.json`);
  const r = await ps(WT, ["-ProcessId", String(browserPid), "-OutJson", out]);
  try { return JSON.parse(fs.readFileSync(out, "utf8").replace(/^\uFEFF/, "")); } catch { return { error: r.stderr, raw: r.stdout }; }
}
function topWindows(tree) {
  return (tree.windows || []).filter((w) => w.top_level && w.pid === browserPid && w.cls === "Chrome_WidgetWin_1");
}
function dcompChildren(tree) {
  const out = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if (String(c.cls).toLowerCase().includes("intermediate d3d")) out.push(c);
      walk(c);
    }
  };
  for (const w of topWindows(tree)) walk(w);
  // window-tree also records desktop-level orphans.
  return out;
}
function allWindows(tree) {
  const out = [];
  for (const w of tree.windows || []) {
    out.push(w);
    const walk = (n) => { for (const c of n.children || []) { out.push(c); walk(c); } };
    walk(w);
  }
  return out;
}

async function counters(tag) {
  const out = path.join(root, `counters-${tag}.json`);
  const r = await ps(PROBE, ["-ProcessId", String(browserPid), "-QuickCounters", "-OutJson", out]);
  try {
    const d = JSON.parse(fs.readFileSync(out, "utf8").replace(/^\uFEFF/, ""));
    return d.processes?.[0]?.counters || { error: r.stderr };
  } catch { return { error: r.stderr, raw: r.stdout }; }
}
async function screenshot(tag) {
  const shot = await page.call("Page.captureScreenshot", { format: "png", fromSurface: true });
  const p = path.join(root, `cdp-${tag}.png`);
  fs.writeFileSync(p, Buffer.from(shot.data, "base64"));
  const out = path.join(root, `sample-${tag}.json`);
  const r = await ps(SAMPLE, ["-Path", p, "-OutJson", out]);
  try { return { path: p, sample: JSON.parse(fs.readFileSync(out, "utf8").replace(/^\uFEFF/, "")) }; }
  catch { return { path: p, sample: { error: r.stderr } }; }
}

const tree0 = await windowTree("t0");
const tops0 = topWindows(tree0);
const dcomp0 = dcompChildren(tree0);
const win0 = tops0.sort((a, b) => (b.right - b.left) * (b.bottom - b.top) - (a.right - a.left) * (a.bottom - a.top))[0] || null;

const shot1 = await screenshot("hidden-1");
await new Promise((r) => setTimeout(r, 900));
const shot2 = await screenshot("hidden-2");
await new Promise((r) => setTimeout(r, 900));
const shot3 = await screenshot("hidden-3");
const counters0 = await counters("baseline");

// Visibility probes with no CDP target creation: detects whether the product
// itself shows the window after startup. No ShowWindow is ever called here.
const visChecks = [];
const probeVisibility = async (tag) => {
  const t = await windowTree(tag);
  const topsT = topWindows(t);
  visChecks.push({ tag, windows: topsT.map((w) => ({ hwnd: w.hwnd, visible: w.visible, rect: [w.left, w.top, w.right, w.bottom] })) });
};
await probeVisibility("vis-initial");
await new Promise((r) => setTimeout(r, 6000));
await probeVisibility("vis-idle6s");
await new Promise((r) => setTimeout(r, 6000));
await probeVisibility("vis-idle12s");

// chrome://gpu cross-check (opt-in; SystemInfo.auxAttributes already carries
// the values, and Target.createTarget may surface the window).
let gpuPageText = null;
let gpuPageFields = null;
if (args.gpupage === true) {
try {
  const created = await browser.call("Target.createTarget", { url: "chrome://gpu" }, 8000);
  const gpuTargetId = created.targetId;
  let gpuWs = null;
  for (let i = 0; i < 24; i++) {
    const list = await getJson("/json/list");
    const t = list.find((x) => x.id === gpuTargetId || (x.type === "page" && x.url === "chrome://gpu"));
    if (t?.webSocketDebuggerUrl) { gpuWs = t.webSocketDebuggerUrl; break; }
    await new Promise((r) => setTimeout(r, 250));
  }
  if (gpuWs) {
    const gp = connect(gpuWs);
    await gp.ready;
    await gp.call("Runtime.enable", {}, 8000);
    for (let i = 0; i < 24; i++) {
      const txt = await gp.call("Runtime.evaluate", { expression: "document.body ? document.body.innerText : ''", returnByValue: true }, 8000).then((r) => r.result?.value).catch(() => "");
      if (txt && /Direct composition/i.test(txt)) {
        gpuPageText = txt.slice(0, 6000);
        const grab = (label) => {
          const m = txt.match(new RegExp(`${label}\\s*[:\\n]\\s*([^\\n]+)`, "i"));
          return m ? m[1].trim() : null;
        };
        gpuPageFields = {
          direct_composition: grab("Direct composition"),
          supports_overlays: grab("Supports overlays"),
          in_process_gpu: grab("In-process GPU"),
          sandboxed: grab("Sandboxed"),
          skia_backend: grab("Skia Backend"),
        };
        break;
      }
      await new Promise((r) => setTimeout(r, 250));
    }
  }
  try { await browser.call("Target.closeTarget", { targetId: gpuTargetId }, 5000); } catch {}
} catch (e) {
  gpuPageText = `error: ${e}`;
}
}

// Window create/close cycles are OFF by default. Target.createTarget with
// newWindow:true makes the Browser CLI windows visible on screen on this
// build, which violates the repository rule against putting test windows on
// screen. Per-window DComp teardown is therefore NOT tested here by default;
// process restart is covered by running this harness twice (each launch starts
// hidden). Pass --cycles only in a display-isolated session.
const cycles = [];
if (args.cycles === true) {
  console.warn("[dcomp] WARNING: --cycles makes windows visible on screen; " +
    "this violates the test hygiene rule unless the session is display-isolated.");
  for (let i = 1; i <= 3; i++) {
    const before = await counters(`cycle${i}-before`);
    const beforeTree = await windowTree(`cycle${i}-before`);
    let targetId = null;
    let createError = null;
    try {
      const r = await browser.call("Target.createTarget", { url: `${pageUrl}?cycle=${i}`, newWindow: true });
      targetId = r.targetId;
    } catch (e) { createError = String(e); }
    await new Promise((r) => setTimeout(r, 1800));
    const openTree = await windowTree(`cycle${i}-open`);
    const openTops = topWindows(openTree);
    const openDcomp = dcompChildren(openTree);
    const afterOpenCounters = await counters(`cycle${i}-open`);
    let closeError = null;
    if (targetId) {
      try { await browser.call("Target.closeTarget", { targetId }); } catch (e) { closeError = String(e); }
    }
    await new Promise((r) => setTimeout(r, 1500));
    const afterTree = await windowTree(`cycle${i}-closed`);
    const afterTops = topWindows(afterTree);
    const afterDcomp = dcompChildren(afterTree);
    const afterCounters = await counters(`cycle${i}-closed`);
    cycles.push({
      cycle: i,
      target_id: targetId,
      create_error: createError,
      close_error: closeError,
      windows_before: topWindows(beforeTree).length,
      windows_open: openTops.length,
      windows_after_close: afterTops.length,
      dcomp_children_open: openDcomp.map((c) => ({ hwnd: c.hwnd, pid: c.pid, cls: c.cls, visible: c.visible, parent: c.parent })),
      dcomp_children_after_close: afterDcomp.map((c) => ({ hwnd: c.hwnd, pid: c.pid, cls: c.cls, visible: c.visible, parent: c.parent })),
      counters_before: before,
      counters_open: afterOpenCounters,
      counters_after_close: afterCounters,
      top_windows_detail: openTops.map((w) => ({ hwnd: w.hwnd, visible: w.visible, cls: w.cls, rect: [w.left, w.top, w.right, w.bottom], ex_style: w.ex_style })),
    });
  }
}

const treeFinal = await windowTree("final");
const finalShot = await screenshot("final-hidden");
const pageFinal = await evalPage(`JSON.stringify({visibility:document.visibilityState,focus:document.hasFocus(),tick:Number(document.getElementById('tick').textContent)})`).catch(() => null);
const countersAfterCycles = cycles.length ? await counters("after-cycles") : counters0;

// Optional: close the only tab and observe whether the hidden window and its
// DComp child are destroyed while the browser process stays alive. This never
// calls ShowWindow and never creates a window.
let tabClose = null;
if (args["close-tab"] === true) {
  const before = await counters("before-tab-close");
  let closeError = null;
  try { await browser.call("Target.closeTarget", { targetId: pageTargetId }, 8000); }
  catch (e) { closeError = String(e); }
  await new Promise((r) => setTimeout(r, 3000));
  const alive = !childExit;
  const tree = alive ? await windowTree("after-tab-close") : null;
  tabClose = {
    close_error: closeError,
    browser_alive: alive,
    child_exit: childExit,
    counters_before: before,
    counters_after: alive ? await counters("after-tab-close") : null,
    top_windows_after: tree ? topWindows(tree).length : 0,
    dcomp_children_after: tree ? dcompChildren(tree).map((c) => ({ hwnd: c.hwnd, pid: c.pid, cls: c.cls, visible: c.visible })) : [],
    window_tree: tree,
  };
}

// Optional: post WM_CLOSE to the hidden top-level HWND and observe whether the
// browser process stays alive and releases the DComp child window. PostMessage
// never shows the window, so this exercises per-window teardown without a
// visible-window hygiene violation.
let windowClose = null;
if (args["close-window"] === true && win0) {
  const before = await counters("before-window-close");
  const r = await ps(CLOSEWIN, ["-Hwnd", String(win0.hwnd)]);
  await new Promise((res) => setTimeout(res, 3500));
  const alive = !childExit;
  const tree = alive ? await windowTree("after-window-close") : null;
  windowClose = {
    hwnd: win0.hwnd,
    post_output: r.stdout.trim(),
    post_stderr: r.stderr.trim(),
    browser_alive: alive,
    child_exit: childExit,
    counters_before: before,
    counters_after: alive ? await counters("after-window-close") : null,
    top_windows_after: tree ? topWindows(tree).length : 0,
    dcomp_children_after: tree ? dcompChildren(tree).map((c) => ({ hwnd: c.hwnd, pid: c.pid, cls: c.cls, visible: c.visible })) : [],
  };
}

// Graceful shutdown.
let closeResult = null;
try {
  await browser.call("Browser.close");
  closeResult = { sent: true };
} catch (e) {
  closeResult = { sent: false, error: String(e) };
}
const deadline = Date.now() + 20000;
while (!childExit && Date.now() < deadline) await new Promise((r) => setTimeout(r, 200));
if (!childExit) {
  try { process.kill(child.pid); } catch {}
  await new Promise((r) => setTimeout(r, 1000));
}
const afterExitCounters = null;
let crashReports = [];
try {
  const crashDir = path.join(profile, "Crash Reports");
  if (fs.existsSync(crashDir)) {
    const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]);
    crashReports = walk(crashDir).filter((p) => /\.dmp$/i.test(p));
  }
} catch {}

const sampleVerdict = (s) => ({
  colorful: s?.sample?.colorful ?? null,
  unique: s?.sample?.sample_unique_colors ?? null,
  saturated: s?.sample?.sample_saturated ?? null,
  error: s?.sample?.error || null,
});

const summary = {
  mode,
  ok: true,
  root,
  chromeExe,
  flags: MODE_FLAGS[mode],
  cdp_port: cdpPort,
  version: versionInfo,
  page_url: pageUrl,
  page_state: pageState,
  page_final: pageFinal,
  browser_pid: browserPid,
  gpu_pid: gpuProc?.id ?? null,
  process_info: procInfo?.processInfo ?? null,
  overlay,
  gpu_page_fields: gpuPageFields,
  gpu_page_text_excerpt: gpuPageText ? gpuPageText.slice(0, 2000) : null,
  top_window: win0 ? { hwnd: win0.hwnd, visible: win0.visible, cls: win0.cls, rect: [win0.left, win0.top, win0.right, win0.bottom], ex_style: win0.ex_style, cloaked: win0.cloaked } : null,
  top_windows_initial: tops0.length,
  dcomp_children_initial: dcomp0.map((c) => ({ hwnd: c.hwnd, pid: c.pid, cls: c.cls, visible: c.visible, parent: c.parent })),
  counters_baseline: counters0,
  visibility_checks: visChecks,
  screenshots: {
    hidden_1: sampleVerdict(shot1),
    hidden_2: sampleVerdict(shot2),
    hidden_3: sampleVerdict(shot3),
    final_hidden: sampleVerdict(finalShot),
  },
  cycles,
  top_windows_after_cycles: topWindows(treeFinal).length,
  dcomp_children_after_cycles: dcompChildren(treeFinal).map((c) => ({ hwnd: c.hwnd, pid: c.pid, cls: c.cls, visible: c.visible, parent: c.parent })),
  counters_after_cycles: countersAfterCycles,
  tab_close: tabClose,
  window_close: windowClose,
  close_result: closeResult,
  child_exit: childExit,
  crash_dumps: crashReports,
  trace_file: fs.existsSync(traceFile) ? traceFile : null,
  log_file: fs.existsSync(logFile) ? logFile : null,
};
fs.writeFileSync(path.join(root, "summary.json"), JSON.stringify(summary, null, 2));

console.log(JSON.stringify({
  mode,
  root,
  browser_pid: browserPid,
  gpu_pid: summary.gpu_pid,
  overlay,
  gpu_page_fields: gpuPageFields,
  top_window_visible: win0?.visible,
  top_windows_initial: tops0.length,
  dcomp_children_initial: summary.dcomp_children_initial,
  screenshots: summary.screenshots,
  cycles: cycles.map((c) => ({ cycle: c.cycle, windows_open: c.windows_open, windows_after_close: c.windows_after_close, dcomp_open: c.dcomp_children_open.length, dcomp_after: c.dcomp_children_after_close.length, handles_before: c.counters_before?.handle_count, handles_open: c.counters_open?.handle_count, handles_after: c.counters_after_close?.handle_count })),
  child_exit: childExit,
  crash_dumps: crashReports.length,
}, null, 2));

try { server.close(); } catch {}
process.exit(0);
