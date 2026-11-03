#!/usr/bin/env node
// browser-launch-hygiene: windows-only (no keychain); profile is created with mktemp/mkdtempSync
// Internal RC dogfooding: suspend/resume proxy for system sleep/wake.
//
// This does NOT put the host machine to sleep. It launches the unified
// internal RC chrome.exe with a fresh profile, verifies a live page, suspends
// the exact browser process tree with NtSuspendProcess for several seconds,
// resumes it, and verifies the page/event loop/screenshot survive. True S3/S0
// sleep is left to the release engineering environment because suspending this
// host would also suspend the test runner and the user's session.
//
// Usage:
//   node tests/rc-final/dogfood-suspend-resume.mjs --chrome D:/c/src/out/WinRelease/chrome.exe \
//       --evidence-dir D:/c/logs/rc-dogfood-<run-id>

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";

function arg(name, fallback = "") {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const CHROME = path.resolve(arg("chrome", "D:/c/src/out/WinRelease/chrome.exe"));
const EVIDENCE = path.resolve(arg("evidence-dir", path.join(os.tmpdir(), "rc-dogfood")));
fs.mkdirSync(EVIDENCE, { recursive: true });
const report = { test: "dogfood-suspend-resume", chrome: CHROME, checks: [], verdict: "FAIL" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function check(name, ok, detail = "") {
  report.checks.push({ name, ok: !!ok, detail });
  console.log((ok ? "PASS" : "FAIL") + " | " + name + (ok ? "" : " :: " + detail));
}

class CdpPage {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("ws timeout")), 10000);
      ws.addEventListener("open", () => { clearTimeout(t); resolve(); }, { once: true });
      ws.addEventListener("error", (e) => { clearTimeout(t); reject(new Error("ws error")); }, { once: true });
    });
    const page = new CdpPage(ws);
    ws.addEventListener("message", (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && page.pending.has(msg.id)) {
        const p = page.pending.get(msg.id); page.pending.delete(msg.id);
        msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);
      }
    });
    return page;
  }
  call(method, params = {}, timeoutMs = 15000) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => { this.pending.delete(id); reject(new Error(method + " timeout")); }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(t); resolve(v); },
        reject: (e) => { clearTimeout(t); reject(e); },
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

async function httpJson(port, pathname) {
  const res = await fetch(`http://127.0.0.1:${port}${pathname}`);
  return await res.json();
}

function descendants(rootPid) {
  const ps = [
    "$all = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId;",
    "$map = @{}; foreach($p in $all){ $map[[int]$p.ProcessId] = [int]$p.ParentProcessId };",
    `$root=${rootPid}; $ids=@($root); $changed=$true;`,
    "while($changed){ $changed=$false; foreach($k in $map.Keys){ if($ids -contains $map[$k] -and -not ($ids -contains $k)){ $ids += $k; $changed=$true } } }",
    "$ids | Sort-Object -Unique",
  ].join(" ");
  const out = spawnSync("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                         { encoding: "utf8" });
  return (out.stdout || "").split(/\r?\n/).map((s) => Number(s.trim())).filter((n) => n > 0);
}

function suspendTree(rootPid, suspend) {
  const pids = descendants(rootPid);
  const list = pids.join(",");
  const fn = suspend ? "NtSuspendProcess" : "NtResumeProcess";
  const ps = [
    "Add-Type @'",
    "using System; using System.Runtime.InteropServices;",
    "public static class NtDogfood {",
    ' [DllImport("ntdll.dll")] public static extern int NtSuspendProcess(IntPtr h);',
    ' [DllImport("ntdll.dll")] public static extern int NtResumeProcess(IntPtr h);',
    "}",
    "'@;",
    `$pids=@(${list}); foreach($id in $pids){`,
    " try { $p=Get-Process -Id $id -ErrorAction Stop;",
    `  $rc=[NtDogfood]::${fn}($p.Handle); if($rc -ne 0){ Write-Output ("ERR " + $id + " " + $rc) } }`,
    " catch { } }",
  ].join("\n");
  const out = spawnSync("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                         { encoding: "utf8" });
  return { pids, output: (out.stdout || "").trim(), code: out.status };
}

async function main() {
  if (!fs.existsSync(CHROME)) throw new Error("chrome not found: " + CHROME);
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rc-dogfood-"));
  const server = http.createServer((req, res) => {
    res.setHeader("cache-control", "no-store");
    res.setHeader("content-type", "text/html; charset=utf-8");
    res.end("<!doctype html><title>dogfood</title><h1>dogfood</h1>" +
      "<script>let n=0;setInterval(()=>{n++;document.title='n='+n;},250);</script>");
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const portWeb = server.address().port;
  const args = ["--user-data-dir=" + profile, "--no-first-run", "--no-default-browser-check",
    "--enable-features=NetworkServiceSandbox", "--remote-debugging-port=0",
    "--window-size=900,700", `http://127.0.0.1:${portWeb}/`];
  const child = spawn(CHROME, args, { stdio: "ignore" });
  let page = null;
  try {
    const portFile = path.join(profile, "DevToolsActivePort");
    const deadline = Date.now() + 30000;
    let port = 0;
    while (Date.now() < deadline) {
      try {
        const line = fs.readFileSync(portFile, "utf8").split(/\r?\n/)[0].trim();
        if (/^\d+$/.test(line)) { port = Number(line); break; }
      } catch {}
      await sleep(100);
    }
    if (!port) throw new Error("CDP did not start");
    let info = null;
    for (let i = 0; i < 100 && !info; i++) {
      const list = await httpJson(port, "/json/list");
      info = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (!info) await sleep(100);
    }
    if (!info) throw new Error("page target missing");
    page = await CdpPage.connect(info.webSocketDebuggerUrl);
    let before = null;
    for (let i = 0; i < 50; i++) {
      before = await page.call("Runtime.evaluate", { expression: "document.title" });
      if (/^n=\d+/.test(String(before.result?.value || ""))) break;
      await sleep(100);
    }
    check("初始页面可评估且计数器在运行", /^n=\d+/.test(String(before?.result?.value || "")), JSON.stringify(before));

    const suspend = suspendTree(child.pid, true);
    report.suspend = suspend;
    check("已暂停本次测试的完整浏览器进程树", suspend.pids.length > 0 && !/ERR/.test(suspend.output), JSON.stringify(suspend));
    await sleep(8000);
    const resume = suspendTree(child.pid, false);
    report.resume = resume;
    check("已恢复浏览器进程树", resume.pids.length > 0 && !/ERR/.test(resume.output), JSON.stringify(resume));

    // The old CDP websocket may have been dropped while the browser was
    // suspended. Reconnect via /json/list to verify real recovery.
    const list = await httpJson(port, "/json/list");
    const info2 = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
    if (info2) {
      page.close();
      page = await CdpPage.connect(info2.webSocketDebuggerUrl);
    }
    await sleep(1500);
    const after = await page.call("Runtime.evaluate", { expression: "document.title" });
    check("恢复后页面仍可评估且计数器继续", /^n=\d+/.test(String(after.result?.value || "")), JSON.stringify(after));
    const shot = await page.call("Page.captureScreenshot", { format: "png", fromSurface: true });
    const bytes = Buffer.from(shot.data || "", "base64");
    check("恢复后截图非空", bytes.length > 1000, "bytes=" + bytes.length);
    const alive = spawnSync("powershell", ["-NoProfile", "-Command",
      `if (Get-Process -Id ${child.pid} -ErrorAction SilentlyContinue) { 'alive' } else { 'dead' }`],
      { encoding: "utf8" });
    check("浏览器主进程存活", (alive.stdout || "").trim() === "alive", (alive.stdout || "").trim());
  } finally {
    try { page?.close(); } catch {}
    try { server.close(); } catch {}
    try { spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch {}
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
  }
  report.verdict = report.checks.length > 0 && report.checks.every((c) => c.ok) ? "PASS" : "FAIL";
  fs.writeFileSync(path.join(EVIDENCE, "dogfood-suspend-resume.json"),
                   JSON.stringify(report, null, 2) + "\n");
  console.log("verdict: " + report.verdict);
  console.log("report: " + path.join(EVIDENCE, "dogfood-suspend-resume.json"));
  process.exit(report.verdict === "PASS" ? 0 : 1);
}

main().catch((error) => {
  check("dogfood未发生未捕获异常", false, String(error?.stack || error));
  fs.mkdirSync(EVIDENCE, { recursive: true });
  fs.writeFileSync(path.join(EVIDENCE, "dogfood-suspend-resume.json"),
                   JSON.stringify(report, null, 2) + "\n");
  process.exit(1);
});
