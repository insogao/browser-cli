// Test-only probe page. It messages its own extension service worker once per
// second by default. The harness can stop the ping loop (to let the worker idle
// out naturally) and can send exactly one message to wake a terminated worker.
const state = (window.__sw1 = {
  startedAt: Date.now(),
  pings: 0,
  replies: 0,
  lastReply: null,
  lastError: null,
  stopped: false,
  pingMs: 1000,
});

let timer = null;

function sendOne() {
  state.pings += 1;
  const pings = state.pings;
  return chrome.runtime
    .sendMessage({ kind: 'sw1-tick', n: pings })
    .then((reply) => {
      state.replies += 1;
      state.lastReply = reply;
      return reply;
    })
    .catch((error) => {
      state.lastError = String(error);
      throw error;
    });
}

function ping() {
  if (state.stopped) return;
  sendOne().catch(() => {});
}

function start(ms) {
  if (timer) clearInterval(timer);
  state.pingMs = Number(ms) || 1000;
  state.stopped = false;
  timer = setInterval(ping, state.pingMs);
  ping();
}

window.__sw1Stop = function () {
  state.stopped = true;
  if (timer) clearInterval(timer);
  timer = null;
  return true;
};

window.__sw1Start = function (ms) {
  start(ms || state.pingMs);
  return true;
};

window.__sw1PingOnce = function () {
  return sendOne();
};

// Native-host interleave controls (SW-1 reduced scenario).
window.__sw1Native = function (action) {
  const kind = 'sw1-native-' + String(action || 'status');
  return chrome.runtime
    .sendMessage({ kind: kind })
    .then((reply) => {
      state.lastReply = reply;
      return reply;
    })
    .catch((error) => {
      state.lastError = String(error);
      throw error;
    });
};

start(state.pingMs);
