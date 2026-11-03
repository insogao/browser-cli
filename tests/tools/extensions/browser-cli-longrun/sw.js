// Test-only service worker for the long-run stability gate. It answers the
// probe page's 1 Hz message so the page exercises extension messaging + repaint
// cadence. The harness stops this worker through CDP and verifies the next
// message wakes a new worker instance.
const startedAt = Date.now();

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.kind === 'lr-tick') {
    sendResponse({
      ok: true,
      n: message.n,
      at: Date.now(),
      workerStartedAt: startedAt,
      workerAgeMs: Date.now() - startedAt,
      senderId: sender && sender.id ? sender.id : null,
    });
    return true;
  }
  sendResponse({ ok: false, error: 'unknown-message', at: Date.now() });
  return true;
});
