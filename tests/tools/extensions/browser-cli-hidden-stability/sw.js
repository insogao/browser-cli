// Test-only service worker: answers the probe page's 1 Hz message so the page
// exercises the same extension-messaging + repaint cadence as a real popup.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.kind === 'hs-tick') {
    sendResponse({ ok: true, at: Date.now(), n: message.n });
    return true;
  }
  sendResponse({ ok: false, error: 'unknown-message' });
  return true;
});
