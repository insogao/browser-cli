#!/usr/bin/env node
// Browser CLI target-side reference implementation and tests for contract v2.
//
// This is NOT the Chromium patch and NOT the product target. It is a small,
// dependency-free named-pipe reference target used by the browser-cli test
// suite to exercise the exact v2 ticket/job/authorization semantics that the
// proposed patch implements. It reports native:false and
// authorization.simulated:true so no caller can mistake it for the product.
//
// It starts no browser, reads no profile, and uses synthetic cookies only.
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const CONTRACT = "browser-cli.cookie-import/v2";
const PROTOCOL = 2;
const SCHEMA = "browser-cli.cookie-import.session/v2";
const TICKET_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const TICKET_TTL_MS = 5 * 60 * 1000;
const MAX_FAILURES = 5;
const FAILURE_WINDOW_MS = 60 * 1000;
const RATE_LIMIT_MS = 60 * 1000;
const MAX_FRAME = 512 * 1024;
const MAX_JOB = 16 * 1024 * 1024;
const TEST_PSL = new Set(["com", "net", "org", "test", "co.uk", "github.io"]);
const CANARY = "BC-REFERENCE-CANARY-71c9";

function sha256Hex(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

function generateTicket() {
  const bytes = crypto.randomBytes(20);
  let bits = 0;
  let value = 0;
  let out = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      out += TICKET_ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  return out;
}

function normalizeTicket(raw) {
  let value = String(raw || "").trim().toUpperCase();
  if (value.startsWith("BCLI-")) value = value.slice(5);
  return value
    .replace(/[\s-]+/g, "")
    .replace(/O/g, "0")
    .replace(/[IL]/g, "1");
}

function isValidTicket(raw) {
  const value = normalizeTicket(raw);
  return value.length === 32 && [...value].every((c) => TICKET_ALPHABET.includes(c));
}

function formatTicket(raw) {
  const value = normalizeTicket(raw);
  if (!isValidTicket(value)) return "";
  return "BCLI-" + value.match(/.{1,4}/g).join("-");
}

function ticketHash(raw) {
  return sha256Hex(`browser-cli-ticket-v2:${normalizeTicket(raw)}`);
}

function normalizeDomain(domain) {
  return String(domain || "").replace(/^\./, "").trim().toLowerCase();
}

function isValidDomain(domain) {
  if (typeof domain !== "string" || !domain || domain.length > 255) return false;
  if (!/^[\x00-\x7f]+$/.test(domain)) return false;
  const bare = domain.startsWith(".") ? domain.slice(1) : domain;
  if (!bare || bare.endsWith(".") || bare.includes("..")) return false;
  return /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$/i.test(bare);
}

function domainMatches(cookieDomain, selected) {
  const cookie = normalizeDomain(cookieDomain);
  return (selected || []).some((raw) => {
    const domain = normalizeDomain(raw);
    return cookie === domain || cookie.endsWith(`.${domain}`);
  });
}

function domainsDigest(domains) {
  const normalized = [...new Set((domains || []).map(normalizeDomain).filter(Boolean))].sort();
  return sha256Hex(`browser-cli-domains-v2:${normalized.join("\n")}`);
}

function prefixFailure(cookie) {
  const name = cookie.name;
  if (name.startsWith("__Host-Http-")) {
    return cookie.secure && cookie.httpOnly && cookie.path === "/" && cookie.hostOnly
      ? null
      : "cookie-prefix-rejected";
  }
  if (name.startsWith("__Host-")) {
    return cookie.secure && cookie.path === "/" && cookie.hostOnly ? null : "cookie-prefix-rejected";
  }
  if (name.startsWith("__Secure-")) return cookie.secure ? null : "cookie-prefix-rejected";
  if (name.startsWith("__Http-")) {
    return cookie.secure && cookie.httpOnly ? null : "cookie-prefix-rejected";
  }
  return null;
}

function normalizeSite(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    if (!url.hostname || url.username || url.password || url.port) return null;
    if (url.pathname !== "/" && url.pathname !== "") return null;
    if (url.search || url.hash) return null;
    return `${url.protocol}//${url.hostname}`;
  } catch {
    return null;
  }
}

function cookiePolicyFailure(cookie, selected, now) {
  if (!cookie || typeof cookie !== "object") return "invalid-cookie";
  if (!isValidDomain(cookie.domain)) return "invalid-domain";
  if (cookie.hostOnly === true && cookie.domain.startsWith(".")) return "invalid-domain";
  if (cookie.hostOnly === false && !cookie.domain.startsWith(".")) return "invalid-domain";
  if (typeof cookie.name !== "string" || !cookie.name || cookie.name.length > 4096) return "invalid-cookie";
  if (typeof cookie.value !== "string" || cookie.value.length > 8192) return "invalid-cookie";
  if (typeof cookie.path !== "string" || !cookie.path.startsWith("/")) return "invalid-cookie";
  if (typeof cookie.secure !== "boolean" || typeof cookie.httpOnly !== "boolean") return "invalid-cookie";
  if (typeof cookie.session !== "boolean") return "invalid-cookie";
  if (!["no_restriction", "lax", "strict", "unspecified"].includes(cookie.sameSite)) {
    return "samesite-rejected";
  }
  if (cookie.session) {
    if (cookie.expirationDate !== undefined) return "expiry-rejected";
  } else if (!Number.isFinite(cookie.expirationDate) || cookie.expirationDate * 1000 <= now) {
    return "expiry-rejected";
  }
  if (cookie.partitionKey) {
    if (!cookie.secure || !normalizeSite(cookie.partitionKey.topLevelSite)) {
      return "partitioned-cookie-rejected";
    }
  }
  if (cookie.sameSite === "no_restriction" && !cookie.secure) return "samesite-rejected";
  const prefix = prefixFailure(cookie);
  if (prefix) return prefix;
  if (!domainMatches(cookie.domain, selected)) return "invalid-domain";
  if (cookie.hostOnly === false && TEST_PSL.has(normalizeDomain(cookie.domain))) {
    return "public-suffix-rejected";
  }
  return null;
}

function computeCookieImported(job) {
  if (job.state === "pending" || job.state === "awaiting_authorization" || job.state === "denied") {
    return "not_started";
  }
  if (job.applied > 0 && job.failed === 0 && (job.state === "done" || job.state === "applying")) {
    return "imported";
  }
  if (job.applied > 0 && job.failed > 0) return "partially_imported";
  if (job.applied > 0 && job.state === "cancelled") return "partially_imported";
  if (job.applied === 0 && job.failed > 0) return "failed";
  if (job.state === "failed") return "failed";
  return "not_started";
}

function publicView(job) {
  const total = job.payload.cookies.length;
  return {
    contract: CONTRACT,
    protocolVersion: PROTOCOL,
    jobId: job.id,
    state: job.state,
    transport: "named-pipe",
    native: false,
    totals: {
      cookies: total,
      domains: new Set(job.payload.cookies.map((c) => normalizeDomain(c.domain))).size,
      selectedDomains: job.payload.domains.length,
      sessionCookies: job.payload.cookies.filter((c) => c.session).length,
      partitionedCookies: job.payload.cookies.filter((c) => c.partitionKey).length,
    },
    progress: {
      applied: job.applied,
      failed: job.failed,
      remaining: Math.max(0, total - job.applied - job.failed),
    },
    authorization: {
      targetUserConfirmed: job.targetUserConfirmed,
      simulated: job.simulated,
      source: job.authorizationSource,
      ...(job.targetUserConfirmed ? { scopeDigest: job.payload.consent.domainsDigest } : {}),
      ...(job.confirmedAt ? { confirmedAt: job.confirmedAt } : {}),
    },
    dataCopied: { cookies: false },
    cookieImported: {
      status: computeCookieImported(job),
      applied: job.applied,
      failed: job.failed,
      failures: { ...job.failures },
    },
    sessionCookies: {
      count: job.payload.cookies.filter((c) => c.session).length,
      mayNotSurviveRestart: true,
    },
    loginVerified: { status: "not_verified" },
    ...(job.errorCode ? { error: { code: job.errorCode, message: job.errorCode } } : {}),
    createdAt: job.createdAt,
    updatedAt: job.updatedAt,
  };
}

function encodeFrame(message) {
  const json = Buffer.from(JSON.stringify(message), "utf8");
  if (json.length > MAX_FRAME) throw new Error("frame-too-large");
  const header = Buffer.alloc(4);
  header.writeUInt32LE(json.length, 0);
  return Buffer.concat([header, json]);
}

function frameReader(onFrame) {
  let buffer = Buffer.alloc(0);
  return (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const length = buffer.readUInt32LE(0);
      if (length > MAX_FRAME) throw new Error("frame-too-large");
      if (buffer.length < 4 + length) return;
      const message = JSON.parse(buffer.subarray(4, 4 + length).toString("utf8"));
      buffer = buffer.subarray(4 + length);
      onFrame(message);
    }
  };
}

function pipePath() {
  if (process.platform === "win32") {
    return `\\\\.\\pipe\\bci-ref-${process.pid}-${crypto.randomBytes(6).toString("hex")}`;
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "bci-ref-"));
  fs.chmodSync(dir, 0o700);
  return path.join(dir, "target.sock");
}

export function startReferenceTarget(options = {}) {
  const ticket = options.ticket || generateTicket();
  const approve = options.approve !== undefined ? options.approve : true;
  const now = options.now || (() => Date.now());
  const sessionDir = options.sessionDir || fs.mkdtempSync(path.join(os.tmpdir(), "bci-ref-sessions-"));
  const logs = [];
  const log = (line) => logs.push(String(line).replace(/[\r\n]+/g, " "));
  const instanceId = crypto.randomBytes(16).toString("hex");
  const pipe = options.pipePath || pipePath();
  const createdAt = now();
  const expiresAt = createdAt + (options.ttlMs || TICKET_TTL_MS);
  const backend = new Map();
  let consumed = false;
  let socket = null;
  let job = null;
  let failures = [];
  let rateLimitedUntil = 0;
  let closed = false;

  const descriptor = {
    schema: SCHEMA,
    protocolVersion: PROTOCOL,
    targetProduct: "Browser CLI reference target",
    targetVersion: "test",
    targetPid: process.pid,
    targetStartTimeMs: createdAt,
    targetInstanceId: instanceId,
    profileHash: "0".repeat(64),
    ticketHash: ticketHash(ticket),
    transport: "named-pipe",
    pipeName: pipe,
    createdAt: new Date(createdAt).toISOString(),
    expiresAt: new Date(expiresAt).toISOString(),
  };
  const descriptorPath = path.join(sessionDir, `session-${instanceId}.json`);

  const server = net.createServer((client) => {
    if (socket && !socket.destroyed) {
      client.write(encodeFrame({ type: "error", code: "target-busy" }));
      client.destroy();
      return;
    }
    socket = client;
    let currentJob = null;
    let chunks = [];
    let nextChunk = 0;
    const send = (message) => {
      if (!client.destroyed) client.write(encodeFrame(message));
    };
    const fail = (code) => send({ type: "error", code });
    const recordFailure = () => {
      const current = now();
      failures.push(current);
      failures = failures.filter((at) => current - at <= FAILURE_WINDOW_MS);
      if (failures.length >= MAX_FAILURES) rateLimitedUntil = current + RATE_LIMIT_MS;
    };
    const reader = frameReader((frame) => {
      void onFrame(frame);
    });
    client.on("data", (chunk) => {
      try {
        reader(chunk);
      } catch {
        client.destroy();
      }
    });
    client.on("close", () => {
      if (socket === client) socket = null;
      if (currentJob && !["done", "failed", "cancelled", "denied"].includes(currentJob.state)) {
        currentJob.state = "cancelled";
        currentJob.updatedAt = new Date(now()).toISOString();
        log(`reference target: job ${currentJob.id} cancelled by disconnect`);
      }
    });

    async function onFrame(frame) {
      if (!frame || typeof frame !== "object") return;
      if (now() < rateLimitedUntil) return fail("rate-limited");
      if (frame.type === "hello") {
        if (frame.protocol !== PROTOCOL) return fail("protocol-mismatch");
        if (consumed) return fail("ticket-consumed");
        if (now() > expiresAt) return fail("ticket-expired");
        if (!isValidTicket(frame.ticket) || ticketHash(frame.ticket) !== ticketHash(ticket)) {
          recordFailure();
          return fail("ticket-invalid");
        }
        consumed = true;
        currentJob = {
          id: crypto.randomBytes(12).toString("hex"),
          jobNonce: frame.jobNonce,
          jobDigest: frame.jobDigest,
          totalBytes: frame.totalBytes,
          chunkCount: frame.chunkCount,
          payload: null,
          state: "pending",
          applied: 0,
          failed: 0,
          failures: {},
          targetUserConfirmed: false,
          simulated: false,
          authorizationSource: "none",
          confirmedAt: null,
          errorCode: null,
          createdAt: new Date(now()).toISOString(),
          updatedAt: new Date(now()).toISOString(),
        };
        job = currentJob;
        return send({
          type: "ready",
          protocol: PROTOCOL,
          sessionId: instanceId,
          targetPid: process.pid,
          targetProduct: "Browser CLI reference target",
          targetVersion: "test",
          native: false,
          capabilities: {
            cookieStoreWrite: true,
            persistAcrossRestart: false,
            partitionedCookies: true,
            sessionCookies: true,
            sessionCookiePersistence: "not_guaranteed",
          },
        });
      }
      if (!currentJob) return fail("protocol-mismatch");
      if (frame.jobNonce !== currentJob.jobNonce) return fail("job-binding-mismatch");
      if (frame.type === "job-begin") {
        if (frame.totalBytes > MAX_JOB || frame.chunkCount > 64) return fail("too-large");
        return;
      }
      if (frame.type === "job-chunk") {
        if (!Number.isInteger(frame.index) || frame.index !== nextChunk) return fail("chunk-missing");
        if (typeof frame.data !== "string" || frame.data.length > MAX_FRAME) return fail("too-large");
        chunks.push(Buffer.from(frame.data, "base64"));
        nextChunk += 1;
        return;
      }
      if (frame.type === "job-commit") {
        if (chunks.length !== currentJob.chunkCount) return fail("chunk-missing");
        const payloadBytes = Buffer.concat(chunks);
        chunks = [];
        if (payloadBytes.length !== currentJob.totalBytes) return fail("chunk-missing");
        if (sha256Hex(payloadBytes.toString("utf8")) !== currentJob.jobDigest) {
          return fail("job-digest-mismatch");
        }
        let payload;
        try {
          payload = JSON.parse(payloadBytes.toString("utf8"));
        } catch {
          return fail("protocol-mismatch");
        }
        if (
          payload.contract !== CONTRACT ||
          payload.protocolVersion !== PROTOCOL ||
          !Array.isArray(payload.domains) ||
          !Array.isArray(payload.cookies) ||
          payload.cookies.length === 0 ||
          domainsDigest(payload.domains) !== payload.consent?.domainsDigest
        ) {
          return fail("protocol-mismatch");
        }
        currentJob.payload = payload;
        currentJob.state = "awaiting_authorization";
        currentJob.updatedAt = new Date(now()).toISOString();
        send({ type: "accepted", jobId: currentJob.id, state: currentJob.state, totals: publicView(currentJob).totals, native: false });
        send({ type: "status", view: publicView(currentJob) });
        void runJob(currentJob);
        return;
      }
      if (frame.type === "cancel") {
        if (currentJob.state === "pending" || currentJob.state === "awaiting_authorization") {
          currentJob.state = "cancelled";
          currentJob.updatedAt = new Date(now()).toISOString();
          send({ type: "cancel-ok", state: currentJob.state });
          send({ type: "status", view: publicView(currentJob) });
        } else {
          currentJob.state = "cancelled";
          currentJob.updatedAt = new Date(now()).toISOString();
          send({ type: "cancel-ok", state: "cancelling" });
        }
        return;
      }
      if (frame.type === "close") {
        client.destroy();
        return;
      }
      return fail("protocol-mismatch");
    }

    async function runJob(current) {
      const approved = typeof approve === "function" ? await approve(current) : approve;
      if (!approved) {
        current.state = "denied";
        current.errorCode = "authorization-denied";
        current.simulated = true;
        current.authorizationSource = "test-auto-deny";
        current.updatedAt = new Date(now()).toISOString();
        send({ type: "status", view: publicView(current) });
        return;
      }
      current.targetUserConfirmed = true;
      current.simulated = true;
      current.authorizationSource = "test-auto-approve";
      current.confirmedAt = new Date(now()).toISOString();
      current.state = "applying";
      current.updatedAt = new Date(now()).toISOString();
      send({ type: "status", view: publicView(current) });
      for (const cookie of current.payload.cookies) {
        const failure = cookiePolicyFailure(cookie, current.payload.domains, now());
        if (failure) {
          current.failures[failure] = (current.failures[failure] || 0) + 1;
          current.failed += 1;
          continue;
        }
        backend.set(`${cookie.domain}\t${cookie.path}\t${cookie.name}`, cookie);
        current.applied += 1;
      }
      if (current.applied === 0 && current.failed > 0) {
        current.state = "failed";
        current.errorCode = Object.keys(current.failures)[0] || "internal";
      } else {
        current.state = "done";
      }
      current.updatedAt = new Date(now()).toISOString();
      log(`reference target: job ${current.id} ${current.state} applied=${current.applied} failed=${current.failed}`);
      send({ type: "status", view: publicView(current) });
    }
  });

  const listening = new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(pipe, () => {
      fs.writeFileSync(descriptorPath, JSON.stringify(descriptor, null, 2), { mode: 0o600 });
      resolve({ pipe, descriptorPath });
    });
  });

  return {
    ticket,
    ticketFormatted: formatTicket(ticket),
    sessionDir,
    descriptorPath,
    pipe,
    logs,
    listening,
    backend,
    get job() {
      return job;
    },
    get connected() {
      return socket !== null;
    },
    get view() {
      return job ? publicView(job) : null;
    },
    async close() {
      if (closed) return;
      closed = true;
      if (socket) socket.destroy();
      await new Promise((resolve) => server.close(() => resolve()));
      try {
        fs.unlinkSync(descriptorPath);
      } catch {}
      if (process.platform !== "win32") {
        try {
          fs.unlinkSync(pipe);
        } catch {}
      }
    },
  };
}

const allSockets = [];

function connectAndRead(pipe) {
  const socket = net.connect(pipe);
  allSockets.push(socket);
  const frames = [];
  let buffer = Buffer.alloc(0);
  socket.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const length = buffer.readUInt32LE(0);
      if (buffer.length < 4 + length) return;
      frames.push(JSON.parse(buffer.subarray(4, 4 + length).toString("utf8")));
      buffer = buffer.subarray(4 + length);
    }
  });
  const ready = new Promise((resolve, reject) => {
    socket.once("connect", resolve);
    socket.once("error", reject);
  });
  return { socket, frames, ready };
}

function waitFor(predicate, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () => {
      const value = predicate();
      if (value) return resolve(value);
      if (Date.now() - started >= timeoutMs) return reject(new Error("timeout"));
      setTimeout(tick, 20);
    };
    tick();
  });
}

async function makeJob({ jobNonce = "a".repeat(32), domains = ["example.test"], cookies = [cookie()] } = {}) {
  const payload = {
    contract: CONTRACT,
    protocolVersion: PROTOCOL,
    jobNonce,
    source: { browser: "chrome", profileLabel: "Synthetic", extensionVersion: "0.3.0" },
    consent: {
      kind: "explicit-user-action",
      at: new Date().toISOString(),
      extensionVersion: "0.3.0",
      scope: "selected-domains",
      domainsDigest: domainsDigest(domains),
    },
    domains,
    cookies,
  };
  const text = JSON.stringify(payload);
  const buffer = Buffer.from(text, "utf8");
  return {
    payload,
    text,
    buffer,
    digest: sha256Hex(text),
  };
}

function cookie(overrides = {}) {
  const value = {
    domain: ".example.test",
    hostOnly: false,
    path: "/",
    name: "sid",
    value: CANARY,
    secure: true,
    httpOnly: true,
    sameSite: "lax",
    session: false,
    expirationDate: 2000000000,
    ...overrides,
  };
  if (value.session && overrides.expirationDate === undefined) delete value.expirationDate;
  return value;
}

async function sendJob(socket, ticket, built) {
  socket.write(encodeFrame({ type: "hello", protocol: PROTOCOL, ticket: formatTicket(ticket), hostPid: process.pid, jobNonce: built.payload.jobNonce, jobDigest: built.digest, totalBytes: built.buffer.length, chunkCount: 1 }));
  socket.write(encodeFrame({ type: "job-begin", jobNonce: built.payload.jobNonce, jobDigest: built.digest, totalBytes: built.buffer.length, chunkCount: 1 }));
  socket.write(encodeFrame({ type: "job-chunk", jobNonce: built.payload.jobNonce, index: 0, data: built.buffer.toString("base64") }));
  socket.write(encodeFrame({ type: "job-commit", jobNonce: built.payload.jobNonce }));
}

const allTargets = [];

function makeTarget(options = {}) {
  const target = startReferenceTarget(options);
  allTargets.push(target);
  return target;
}

async function main() {
  let passed = 0;
  const failures = [];
  const check = async (name, fn) => {
    try {
      await fn();
      passed += 1;
      console.log(`PASS ${name}`);
    } catch (err) {
      failures.push({ name, error: err });
      console.error(`FAIL ${name}: ${err && err.stack ? err.stack.split("\n")[0] : err}`);
    }
  };

  await check("target applies a job with target authorization and three states", async () => {
    const target = startReferenceTarget();
    await target.listening;
    const client = connectAndRead(target.pipe);
    await client.ready;
    const built = await makeJob({
      cookies: [
        cookie({ name: "persistent" }),
        cookie({ name: "session", session: true }),
        cookie({ name: "partitioned", partitionKey: { topLevelSite: "https://top.test", hasCrossSiteAncestor: true } }),
      ],
    });
    await sendJob(client.socket, target.ticket, built);
    const done = await waitFor(() => client.frames.find((f) => f.type === "status" && f.view.state === "done"));
    assert.equal(done.view.cookieImported.status, "imported");
    assert.equal(done.view.loginVerified.status, "not_verified");
    assert.equal(done.view.authorization.targetUserConfirmed, true);
    assert.equal(done.view.authorization.simulated, true);
    assert.equal(done.view.sessionCookies.count, 1);
    const text = JSON.stringify(done.view);
    assert.equal(text.includes(CANARY), false);
    assert.equal(text.includes("example.test"), false);
    assert.equal(target.backend.size, 3);
    assert.equal(target.logs.join("\n").includes(CANARY), false);
    assert.equal(target.logs.join("\n").includes(target.ticket), false);
    client.socket.destroy();
    await target.close();
  });

  await check("target rejects ticket replay", async () => {
    const target = startReferenceTarget();
    await target.listening;
    const first = connectAndRead(target.pipe);
    await first.ready;
    await sendJob(first.socket, target.ticket, await makeJob({ jobNonce: "b".repeat(32) }));
    await waitFor(() => first.frames.find((f) => f.type === "status" && f.view.state === "done"));
    const closed = new Promise((resolve) => first.socket.once("close", resolve));
    first.socket.destroy();
    await closed;
    await waitFor(() => !target.connected);
    const second = connectAndRead(target.pipe);
    await second.ready;
    second.socket.write(encodeFrame({ type: "hello", protocol: PROTOCOL, ticket: formatTicket(target.ticket), jobNonce: "c".repeat(32), jobDigest: "0".repeat(64), totalBytes: 1, chunkCount: 1 }));
    const error = await waitFor(() => second.frames.find((f) => f.type === "error"));
    assert.equal(error.code, "ticket-consumed");
    second.socket.destroy();
    await target.close();
  });

  await check("target locks after repeated invalid tickets", async () => {
    const target = startReferenceTarget();
    await target.listening;
    const client = connectAndRead(target.pipe);
    await client.ready;
    for (let i = 0; i < MAX_FAILURES + 1; i += 1) {
      client.socket.write(encodeFrame({ type: "hello", protocol: PROTOCOL, ticket: "123456", jobNonce: "d".repeat(32), jobDigest: "0".repeat(64), totalBytes: 1, chunkCount: 1 }));
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    const limited = await waitFor(() => client.frames.find((f) => f.type === "error" && f.code === "rate-limited"));
    assert.equal(limited.code, "rate-limited");
    client.socket.destroy();
    await target.close();
  });

  await check("target rejects a tampered job digest before creating a job", async () => {
    const target = startReferenceTarget();
    await target.listening;
    const client = connectAndRead(target.pipe);
    await client.ready;
    const built = await makeJob({ jobNonce: "e".repeat(32) });
    client.socket.write(encodeFrame({ type: "hello", protocol: PROTOCOL, ticket: formatTicket(target.ticket), jobNonce: built.payload.jobNonce, jobDigest: built.digest, totalBytes: built.buffer.length, chunkCount: 1 }));
    client.socket.write(encodeFrame({ type: "job-begin", jobNonce: built.payload.jobNonce, jobDigest: built.digest, totalBytes: built.buffer.length, chunkCount: 1 }));
    const tampered = Buffer.from(built.buffer);
    tampered[0] = tampered[0] === 0x7b ? 0x7c : 0x7b;
    client.socket.write(encodeFrame({ type: "job-chunk", jobNonce: built.payload.jobNonce, index: 0, data: tampered.toString("base64") }));
    client.socket.write(encodeFrame({ type: "job-commit", jobNonce: built.payload.jobNonce }));
    const error = await waitFor(() => client.frames.find((f) => f.type === "error"));
    assert.equal(error.code, "job-digest-mismatch");
    assert.equal(target.backend.size, 0);
    client.socket.destroy();
    await target.close();
  });

  await check("target denies the import and writes nothing", async () => {
    const target = makeTarget({ approve: false });
    await target.listening;
    const client = connectAndRead(target.pipe);
    await client.ready;
    await sendJob(client.socket, target.ticket, await makeJob({ jobNonce: "f".repeat(32), cookies: [cookie()] }));
    const denied = await waitFor(() => client.frames.find((f) => f.type === "status" && f.view.state === "denied"));
    assert.equal(denied.view.authorization.targetUserConfirmed, false);
    assert.equal(denied.view.authorization.simulated, true);
    assert.equal(denied.view.cookieImported.status, "not_started");
    assert.equal(target.backend.size, 0);
    client.socket.destroy();
    await target.close();
  });

  await check("target counts public-suffix and prefix failures without leaking values", async () => {
    const target = startReferenceTarget();
    await target.listening;
    const client = connectAndRead(target.pipe);
    await client.ready;
    const built = await makeJob({
      jobNonce: "9".repeat(32),
      domains: ["example.test", "test"],
      cookies: [
        cookie({ domain: ".test", hostOnly: false }),
        cookie({ name: "__Host-sid", hostOnly: false }),
        cookie({ name: "good" }),
      ],
    });
    await sendJob(client.socket, target.ticket, built);
    const done = await waitFor(() => client.frames.find((f) => f.type === "status" && ["done", "failed"].includes(f.view.state)));
    assert.equal(done.view.cookieImported.failures["public-suffix-rejected"], 1);
    assert.equal(done.view.cookieImported.failures["cookie-prefix-rejected"], 1);
    assert.equal(done.view.cookieImported.applied, 1);
    assert.equal(JSON.stringify(done.view).includes(CANARY), false);
    client.socket.destroy();
    await target.close();
  });

  await check("target rejects an expired ticket", async () => {
    let current = Date.now();
    const target = makeTarget({ ttlMs: 1, now: () => current });
    await target.listening;
    current += 10;
    const client = connectAndRead(target.pipe);
    await client.ready;
    client.socket.write(encodeFrame({ type: "hello", protocol: PROTOCOL, ticket: formatTicket(target.ticket), jobNonce: "1".repeat(32), jobDigest: "0".repeat(64), totalBytes: 1, chunkCount: 1 }));
    const error = await waitFor(() => client.frames.find((f) => f.type === "error"));
    assert.equal(error.code, "ticket-expired");
    client.socket.destroy();
    await target.close();
  });

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    for (const f of failures) console.error(`\n${f.name}\n${f.error && f.error.stack ? f.error.stack : f.error}`);
    process.exitCode = 1;
  }
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  main().catch((err) => {
    console.error(err && err.stack ? err.stack : err);
    process.exitCode = 1;
  });
}
