// Test-only service worker for SW-1. Every worker instance gets a random
// instanceId so the harness can prove that a stop really replaced the worker.
// It only answers page messages; it has no timers and no keepalive of its own.
const instanceId =
  (self.crypto && self.crypto.randomUUID)
    ? self.crypto.randomUUID()
    : String(Date.now()) + "-" + Math.random();
const startedAt = Date.now();
let handled = 0;

// Optional native messaging port. SW-1 uses it only for the reduced
// native-host interleave: connect, disconnect (Chrome terminates the host on
// EOF), reconnect. The port is not created unless the page asks for it.
let nativePort = null;
let nativeEvents = [];
let nativeLastMessage = null;

function nativeStatus() {
  return {
    connected: !!nativePort,
    events: nativeEvents.slice(-12),
    lastMessage: nativeLastMessage,
  };
}

function nativeNote(entry) {
  nativeEvents.push(Object.assign({ at: Date.now() }, entry));
}

function nativeConnect() {
  if (nativePort) return nativeStatus();
  try {
    nativePort = chrome.runtime.connectNative('com.browsercli.sw1probe');
    nativeNote({ action: 'connect' });
    nativePort.onMessage.addListener((message) => {
      nativeLastMessage = message;
      nativeNote({ action: 'message', message: message });
    });
    nativePort.onDisconnect.addListener(() => {
      const error = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
      nativeNote({ action: 'disconnect', error: error });
      nativePort = null;
    });
  } catch (error) {
    nativeNote({ action: 'connect-failed', error: String(error) });
    nativePort = null;
  }
  return nativeStatus();
}

function nativeDisconnect() {
  if (nativePort) {
    nativeNote({ action: 'disconnect-requested' });
    nativePort.disconnect();
    nativePort = null;
  }
  return nativeStatus();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handled += 1;
  const kind = message && message.kind;
  if (kind === 'sw1-native-connect') {
    sendResponse({ ok: true, kind: kind, instanceId, native: nativeConnect() });
    return true;
  }
  if (kind === 'sw1-native-disconnect') {
    sendResponse({ ok: true, kind: kind, instanceId, native: nativeDisconnect() });
    return true;
  }
  sendResponse({
    ok: true,
    kind: kind,
    n: message && message.n,
    at: Date.now(),
    instanceId,
    workerStartedAt: startedAt,
    handled,
    senderId: sender && sender.id ? sender.id : null,
    native: nativeStatus(),
  });
  return true;
});

self.addEventListener('install', () => {
  // No skipWaiting: this is an installed test worker, not an update path.
});

self.addEventListener('activate', () => {
  // Deliberately empty.
});
