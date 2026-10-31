// GPU-B DComp A/B harness.
//
// Runs one variant: launches Chrome/Browser CLI with an isolated profile,
// loads a saturated 3x3 color page, takes a CDP renderer reference frame and a
// trusted Windows Graphics Capture (WGC) frame of the real HWND, then repeats
// the captures after pushing the window to the bottom of the z-order.
//
// Usage:
//   node gpu-dcomp-ab.mjs --variant <name> --chrome <exe> --root <dir>
//
// Variants are defined in VARIANTS below; --chromes bcli=<path>,official=<path>
// is also accepted. See run-ab.ps1 for the batch runner.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const WGC = path.join(here, "..", "tools", "wgc-capture.exe");

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
const variant = String(args.variant || "bcli-dcomp");
const earlyShow = args["early-show"] === true || args.earlyShow === "true";
const root = path.resolve(String(args.root || `D:/c/logs/gpu-b2/${variant}-${stamp()}`));
const profile = path.join(root, "profile");
const chromeExe = String(
  args.chrome ||
    (variant.startsWith("bcli")
      ? "D:/c/src/out/WinDev/chrome.exe"
      : "C:/Program Files/Google/Chrome/Application/chrome.exe"),
);
fs.mkdirSync(profile, { recursive: true });

const FLAGS = {
  "bcli-dcomp": ["--in-process-gpu"],
  "bcli-nodcomp": ["--in-process-gpu", "--disable-direct-composition"],
  "bcli-gpuproc": [],
  "official-inproc-dcomp": ["--in-process-gpu"],
  "official-inproc-nodcomp": ["--in-process-gpu", "--disable-direct-composition"],
  "official-default": [],
  "bcli-inproc-keepredir": ["--in-process-gpu"],
  "official-inproc-keepredir": ["--in-process-gpu"],
  "bcli-inproc-nodcomp-noredir": ["--in-process-gpu", "--disable-direct-composition"],
  "official-inproc-nodcomp-noredir": ["--in-process-gpu", "--disable-direct-composition"],
  "official-inproc-software": ["--in-process-gpu", "--disable-gpu"],
  "bcli-inproc-software": ["--in-process-gpu", "--disable-gpu"],
  "bcli-inproc-dawn-d3d12": ["--in-process-gpu", "--enable-features=SkiaGraphite", "--skia-graphite-dawn-backend=d3d12"],
  "official-inproc-dawn-d3d12": ["--in-process-gpu", "--enable-features=SkiaGraphite", "--skia-graphite-dawn-backend=d3d12"],
  "bcli-inproc-dawn-d3d12-nodcomp": ["--in-process-gpu", "--disable-direct-composition", "--enable-features=SkiaGraphite", "--skia-graphite-dawn-backend=d3d12"],
};

// Extra --disable-features entries per variant (merged with the common list).
const EXTRA_FEATURES = {
  // Keep the top-level window's DWM redirection bitmap even though the
  // in-process GPU cannot initialize DirectComposition. This selects the
  // ANGLE EGL BitBlt fallback instead of the DComp presenter.
  "bcli-inproc-keepredir": ["RemoveRedirectionBitmap"],
  "official-inproc-keepredir": ["RemoveRedirectionBitmap"],
};

function stamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function log(...a) {
  console.log(`[${variant}]`, ...a);
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
  const r = await run("powershell", [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(here, script),
    ...argv,
  ]);
  if (r.code !== 0) log(`powershell ${script} failed`, r.code, r.stderr.trim().slice(0, 800));
  return r;
}

const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>GPU-B DComp color probe</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#ff00ff;color:#fff;font:bold 36px sans-serif}
#grid{display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);width:100vw;height:100vh}
#grid div{display:flex;align-items:center;justify-content:center;font-size:22px;text-shadow:2px 2px #000}
.c1{background:#ff0000}.c2{background:#00ff00}.c3{background:#0000ff}
.c4{background:#ffff00}.c5{background:#00ffff}.c6{background:#ff00ff}
.c7{background:#ff8000}.c8{background:#8000ff}.c9{background:#ffffff;color:#000}
#anim{position:fixed;left:20px;top:20px;width:120px;height:60px;background:#00ff00;border:4px solid #000;animation:slide 2s infinite alternate}
@keyframes slide{from{transform:translateX(0)}to{transform:translateX(500px)}}
#tick{position:fixed;right:16px;bottom:12px;font:bold 28px monospace;background:#000;color:#0f0;padding:6px 10px}
</style></head><body>
<div id="grid">
<div class="c1">RED</div><div class="c2">GREEN</div><div class="c3">BLUE</div>
<div class="c4">YELLOW</div><div class="c5">CYAN</div><div class="c6">MAGENTA</div>
<div class="c7">ORANGE</div><div class="c8">VIOLET</div><div class="c9">WHITE</div>
</div><div id="anim"></div><div id="tick">0</div>
<script>let n=0;setInterval(()=>{n++;document.getElementById('tick').textContent=String(n)},100);</script>
</body></html>`;

const server = http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
  res.end(html);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const serverPort = server.address().port;
const pageUrl = `http://127.0.0.1:${serverPort}/`;

const traceFile = path.join(root, "trace-startup.json");
const chromeLog = path.join(root, "chrome.log");
const chromeErr = path.join(root, "chrome-stderr.log");
const commonArgs = [
  `--user-data-dir=${profile}`,
  "--remote-debugging-port=0",
  "--no-first-run",
  "--no-default-browser-check",
  "--disable-sync",
  "--disable-component-update",
  "--disable-background-networking",
  `--disable-features=${["Translate", "MediaRouter", "OptimizationHints", ...(EXTRA_FEATURES[variant] || [])].join(",")}`,
  "--disable-popup-blocking",
  "--window-size=900,700",
  "--window-position=60,60",
  "--enable-logging",
  `--log-file=${chromeLog}`,
  "--v=0",
  "--vmodule=dc_layer_tree=2,dcomp_presenter=2,direct_composition_support=2,skia_output_device_dcomp=2,swap_chain_presenter=2,rendering_window_manager=2,child_window_win=2,desktop_window_tree_host_win=1",
  "--trace-startup=gpu,viz,cc",
  `--trace-startup-file=${traceFile}`,
  "--trace-startup-duration=15",
  "--trace-startup-format=json",
  ...(FLAGS[variant] || []),
  pageUrl,
];
log("root", root);
log("chrome", chromeExe);
log("args", commonArgs.filter((a) => a !== pageUrl).join(" "));

const errFd = fs.openSync(chromeErr, "a");
const child = spawn(chromeExe, commonArgs, {
  stdio: ["ignore", errFd, errFd],
  windowsHide: true,
});
let childExit = null;
child.on("exit", (code, signal) => {
  childExit = { code, signal };
  log("chrome exited", code, signal);
});

// Optional early presentation: some in-process-GPU fallbacks create their
// window surface while the HWND is hidden. Showing the window before the
// first compositor frame tests whether that ordering is the trigger.
let earlyShowState = null;
if (earlyShow) {
  for (let i = 0; i < 40; i++) {
    const tree = await windowTree("early-probe", child.pid);
    const cand = (tree.windows || []).find(
      (w) => w.top_level && w.pid === child.pid && w.cls === "Chrome_WidgetWin_1",
    );
    if (cand) {
      const r = await ps("show-window.ps1", ["-Hwnd", String(cand.hwnd)]);
      earlyShowState = { hwnd: cand.hwnd, out: r.stdout.trim() };
      break;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
}

const portFile = path.join(profile, "DevToolsActivePort");
let cdpPort = null;
for (let i = 0; i < 160; i++) {
  if (fs.existsSync(portFile)) {
    const txt = fs.readFileSync(portFile, "utf8").split(/\r?\n/);
    if (txt[0] && /^\d+$/.test(txt[0].trim())) {
      cdpPort = Number(txt[0].trim());
      break;
    }
  }
  if (childExit) break;
  await new Promise((r) => setTimeout(r, 250));
}
if (!cdpPort) {
  fs.writeFileSync(
    path.join(root, "summary.json"),
    JSON.stringify({ variant, ok: false, error: "CDP did not start", childExit, chromeExe, root }, null, 2),
  );
  log("CDP did not start; see", chromeErr);
  try { server.close(); } catch {}
  process.exit(3);
}
log("cdp port", cdpPort);

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: "127.0.0.1", port: cdpPort, path: pathname }, (res) => {
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject);
  });
}

function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? p.reject(new Error(`${p.method}: ${msg.error.message}`)) : p.resolve(msg.result);
    } else {
      events.push(msg);
    }
  });
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject, method });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
  return { ws, ready, call, events };
}

let browser = null;
let page = null;
let versionInfo = null;
try {
  versionInfo = await getJson("/json/version");
  const list = await getJson("/json/list");
  const pageTarget = list.find((t) => t.type === "page" && t.url.startsWith("http://127.0.0.1:"));
  if (!pageTarget) throw new Error("no color page target");
  page = connect(pageTarget.webSocketDebuggerUrl);
  browser = connect(versionInfo.webSocketDebuggerUrl);
  await Promise.all([page.ready, browser.ready]);
  await page.call("Page.enable");
  await page.call("Runtime.enable");
} catch (e) {
  log("CDP connect failed", String(e));
  fs.writeFileSync(
    path.join(root, "summary.json"),
    JSON.stringify({ variant, ok: false, error: String(e), childExit, chromeExe, root }, null, 2),
  );
  try { server.close(); } catch {}
  process.exit(4);
}

async function evalPage(expression) {
  const r = await page.call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  return r.result?.value;
}

// Wait for the page to be fully loaded and painted.
let pageState = null;
for (let i = 0; i < 80; i++) {
  try {
    pageState = JSON.parse(
      await evalPage(`JSON.stringify({
        ready: document.readyState,
        tiles: document.querySelectorAll('#grid div').length,
        tick: Number(document.getElementById('tick')?.textContent || 0),
        visibility: document.visibilityState,
        focus: document.hasFocus(),
        size: [innerWidth, innerHeight],
        dpr: devicePixelRatio,
        bodyBg: getComputedStyle(document.body).backgroundColor
      })`),
    );
    if (pageState.ready === "complete" && pageState.tiles === 9 && pageState.tick >= 3) break;
  } catch {}
  await new Promise((r) => setTimeout(r, 250));
}

// Browser process id via SystemInfo (fall back to spawned pid).
let browserPid = child.pid;
let sysInfo = null;
let procInfo = null;
try {
  sysInfo = await browser.call("SystemInfo.getInfo");
  procInfo = await browser.call("SystemInfo.getProcessInfo");
  const b = (procInfo.processInfo || []).find((p) => p.type === "browser");
  if (b?.id) browserPid = b.id;
} catch (e) {
  log("SystemInfo failed", String(e));
}

async function windowTree(tag, pid = browserPid) {
  const out = path.join(root, `window-tree-${tag}.json`);
  const r = await ps("window-tree.ps1", ["-ProcessId", String(pid), "-OutJson", out]);
  try { return JSON.parse(fs.readFileSync(out, "utf8").replace(/^\uFEFF/, "")); } catch { return { error: r.stderr, raw: r.stdout }; }
}

async function wgcCapture(tag) {
  const prefix = path.join(root, `wgc-${tag}`);
  const json = path.join(root, `wgc-${tag}.json`);
  const r = await run(WGC, [
    "--hwnd", String(tagHwnd),
    "--out", prefix,
    "--json", json,
    "--frames", "3",
    "--interval-ms", "450",
    "--timeout-ms", "12000",
  ]);
  let data = null;
  try { data = JSON.parse(fs.readFileSync(json, "utf8")); } catch {}
  return { code: r.code, stderr: r.stderr.trim().slice(0, 1000), data, json };
}

async function pngSample(png, tag) {
  const out = path.join(root, `sample-${tag}.json`);
  const r = await ps("png-sample.ps1", ["-Path", png, "-OutJson", out]);
  try { return JSON.parse(fs.readFileSync(out, "utf8").replace(/^\uFEFF/, "")); } catch { return { error: r.stderr }; }
}

async function cdpScreenshot(tag) {
  const shot = await page.call("Page.captureScreenshot", { format: "png", fromSurface: true });
  const p = path.join(root, `cdp-${tag}.png`);
  fs.writeFileSync(p, Buffer.from(shot.data, "base64"));
  return { path: p, sample: await pngSample(p, `cdp-${tag}`) };
}

// Pick the top-level Chrome window from the window tree.
const treePreShow = await windowTree("pre-show");
function pickTop(tree) {
  const candidates = (tree.windows || []).filter(
    (w) => w.top_level && w.pid === browserPid && w.cls === "Chrome_WidgetWin_1",
  );
  candidates.sort((a, b) => (b.right - b.left) * (b.bottom - b.top) - (a.right - a.left) * (a.bottom - a.top));
  return candidates[0] || null;
}
const top = pickTop(treePreShow);
if (!top) {
  log("no top-level Chrome window", JSON.stringify(treePreShow).slice(0, 1000));
  fs.writeFileSync(
    path.join(root, "summary.json"),
    JSON.stringify({ variant, ok: false, error: "no top-level Chrome window", treePreShow, root }, null, 2),
  );
  try { await browser.call("Browser.close"); } catch {}
  try { server.close(); } catch {}
  process.exit(5);
}
const tagHwnd = top.hwnd;
log("hwnd", tagHwnd, "pid", browserPid, "pre_show_visible", top.visible, "rect", top.left, top.top, top.right, top.bottom);

// Browser CLI currently creates the window hidden (managed presentation
// policy) and only shows it on the explicit-presentation path. The original
// GPU-B repro used capture-gpu-b.ps1 which called ShowWindow()/SetForegroundWindow();
// mirror that here, but first try a no-activate show so we do not steal
// foreground from other tests. Optionally activate with --activate.
const show1 = await ps("show-window.ps1", ["-Hwnd", String(tagHwnd)]);
let showState = show1.stdout.trim();
let treePostShow = await windowTree("post-show");
let topShown = pickTop(treePostShow);
if (!topShown?.visible) {
  log("window still hidden after no-activate show; retrying with activation");
  const show2 = await ps("show-window.ps1", ["-Hwnd", String(tagHwnd), "-Activate"]);
  showState += " " + show2.stdout.trim();
  await new Promise((r) => setTimeout(r, 800));
  treePostShow = await windowTree("post-show-activated");
  topShown = pickTop(treePostShow);
}
try { await page.call("Page.setWebLifecycleState", { state: "active" }); } catch {}
try { await page.call("Emulation.setFocusEmulationEnabled", { enabled: true }); } catch {}
try { await page.call("Page.bringToFront"); } catch {}
await new Promise((r) => setTimeout(r, 1200));
log("post show visible", topShown?.visible, "foreground", topShown?.foreground);

const cdp1 = await cdpScreenshot("visible");
const wgc1 = await wgcCapture("visible");
const treeVisible = await windowTree("visible");

// Push our own window to the bottom of the z-order without activating it, so
// the second capture exercises the DWM-backed (occluded) path. No other
// process is touched.
await ps("send-to-bottom.ps1", ["-Hwnd", String(tagHwnd)]);
await new Promise((r) => setTimeout(r, 1500));
const cdp2 = await cdpScreenshot("bottom");
const wgc2 = await wgcCapture("bottom");
const tree2 = await windowTree("bottom");

// Fully hidden stage: exercises the patch-027 copy path (copy from the
// renderer/root surface, not from the window). This must stay colorful for
// every candidate fix/workaround.
const hideState = await ps("show-window.ps1", ["-Hwnd", String(tagHwnd), "-Hide"]);
await new Promise((r) => setTimeout(r, 1200));
let cdp3 = null;
try {
  cdp3 = await cdpScreenshot("hidden");
} catch (e) {
  cdp3 = { error: String(e).slice(0, 500) };
}
const tree3 = await windowTree("hidden");

const pageFinal = await evalPage(
  `JSON.stringify({visibility:document.visibilityState,focus:document.hasFocus(),tick:Number(document.getElementById('tick').textContent),size:[innerWidth,innerHeight]})`,
);
let pageFinalObj = null;
try { pageFinalObj = JSON.parse(pageFinal); } catch {}

// Trace events (best effort; file may be absent when startup tracing fails).
let traceSummary = { exists: false };
try {
  const raw = fs.readFileSync(traceFile, "utf8");
  const parsed = JSON.parse(raw);
  const evs = Array.isArray(parsed) ? parsed : parsed.traceEvents || [];
  const rx = /DComp|DCLayer|DirectComposition|Commit|Present|Swap|Surface|CompositorFrame|FrameSink|DrawAndSwap|SkiaOutput|ChildWindow|AddChildWindow/i;
  const filtered = evs.filter((e) => rx.test(String(e.name || "")) || rx.test(String(e.cat || "")));
  const ids = new Set();
  for (const e of evs) {
    const args = e.args || {};
    for (const k of ["surface_id", "surface", "frame_sink_id", "surfaceId"]) {
      if (typeof args[k] === "string" || typeof args[k] === "number") ids.add(String(args[k]));
    }
  }
  fs.writeFileSync(path.join(root, "trace-filtered.json"), JSON.stringify(filtered, null, 1));
  traceSummary = {
    exists: true,
    total_events: evs.length,
    filtered_events: filtered.length,
    surface_like_ids: [...ids].slice(0, 200),
    sample_names: [...new Set(filtered.map((e) => e.name))].slice(0, 120),
  };
} catch (e) {
  traceSummary = { exists: false, error: String(e).slice(0, 500) };
}

// Log excerpt (DComp / child window / presentation).
let logSummary = { exists: false };
try {
  const lines = fs.readFileSync(chromeLog, "utf8").split(/\r?\n/);
  const rx = /DComp|DirectComposition|RegisterChild|Child HWND|Intermediate D3D|AddChildWindow|SetParent|BROWSER_CLI_PRESENTATION|DWM|dxgi|SwapChain|Present|surface/i;
  const hits = lines.filter((l) => rx.test(l));
  fs.writeFileSync(path.join(root, "log-excerpt.txt"), hits.join("\n"));
  logSummary = { exists: true, total_lines: lines.length, matched_lines: hits.length, excerpt_path: path.join(root, "log-excerpt.txt") };
} catch (e) {
  logSummary = { exists: false, error: String(e).slice(0, 500) };
}

const summary = {
  variant,
  ok: true,
  root,
  chromeExe,
  flags: FLAGS[variant] || [],
  cdpPort,
  browserPid,
  hwnd: tagHwnd,
  version: versionInfo,
  pageState,
  pageFinal: pageFinalObj,
  top_window: top,
  window_tree_pre_show: treePreShow,
  window_tree_post_show: treePostShow,
  window_tree_visible: treeVisible,
  show_state: showState,
  early_show: earlyShowState,
  window_tree_bottom: tree2,
  cdp_visible: cdp1,
  wgc_visible: wgc1,
  cdp_bottom: cdp2,
  wgc_bottom: wgc2,
  cdp_hidden: cdp3,
  hide_state: hideState.stdout.trim(),
  window_tree_hidden: tree3,
  trace: traceSummary,
  log: logSummary,
  sys_info: sysInfo,
  process_info: procInfo,
  child_exit: childExit,
};
fs.writeFileSync(path.join(root, "summary.json"), JSON.stringify(summary, null, 1));

// Console verdict.
function verdict(cap) {
  const frames = cap?.data?.frames || [];
  if (!frames.length) return { frames: 0, error: cap?.data?.last_frame_error || cap?.stderr };
  return {
    frames: frames.length,
    points: frames.map((f) => f.points),
    unique_colors: frames.map((f) => f.unique_colors),
    all_dark: frames.map((f) => f.all_dark),
    hashes: frames.map((f) => f.hash),
  };
}
console.log(JSON.stringify({
  variant,
  root,
  browserPid,
  hwnd: tagHwnd,
  pageState,
  cdp_visible: { colorful: cdp1.sample?.colorful, unique: cdp1.sample?.sample_unique_colors, saturated: cdp1.sample?.sample_saturated },
  wgc_visible: verdict(wgc1),
  cdp_bottom: { colorful: cdp2.sample?.colorful, unique: cdp2.sample?.sample_unique_colors, saturated: cdp2.sample?.sample_saturated },
  cdp_hidden: cdp3?.error ? { error: cdp3.error } : { colorful: cdp3?.sample?.colorful, unique: cdp3?.sample?.sample_unique_colors, saturated: cdp3?.sample?.sample_saturated },
  wgc_bottom: verdict(wgc2),
  children: (wgc1.data?.children || []).map((c) => ({ hwnd: c.hwnd, pid: c.pid, parent: c.parent, root: c.root, cls: c.cls, visible: c.visible, rect: c.rect })),
  trace: traceSummary,
}, null, 2));

try { await browser.call("Browser.close"); } catch {}
try { server.close(); } catch {}
await new Promise((r) => setTimeout(r, 1500));
try { process.kill(child.pid); } catch {}
await new Promise((r) => setTimeout(r, 500));
process.exit(0);
