// Test-only MV3 extension page. It mimics a long-lived popup that refreshes
// visible status text every second and talks to its service worker. The marker
// block encodes paintCount in R/G so a screenshot can be matched against the
// DOM counter instead of being trusted as a "not white" bitmap.
(() => {
  const q = new URLSearchParams(location.search);
  const id = q.get('id') || 'ext';
  const tickMs = Math.max(50, Number(q.get('tick') || 1000));
  const line = document.getElementById('line');
  const marker = document.getElementById('marker');

  let paintCount = 0;
  let tickCount = 0;
  let lastPaintAt = 0;
  let lastReply = null;
  let replyErrors = 0;

  function markerColour(n) {
    return [n & 0xff, (n >> 8) & 0xff, 255];
  }

  function paint(reason) {
    paintCount += 1;
    lastPaintAt = Date.now();
    const [r, g, b] = markerColour(paintCount);
    marker.style.background = 'rgb(' + r + ',' + g + ',' + b + ')';
    line.textContent = 'id=' + id + ' paint=' + paintCount + ' tick=' + tickCount +
      ' vis=' + document.visibilityState + ' focus=' + document.hasFocus() +
      ' reason=' + (reason || '') + ' reply=' + JSON.stringify(lastReply) +
      ' errors=' + replyErrors;
  }

  setInterval(() => {
    tickCount += 1;
    try {
      chrome.runtime.sendMessage({ kind: 'hs-tick', n: tickCount }, (reply) => {
        if (chrome.runtime.lastError) {
          replyErrors += 1;
          lastReply = { error: chrome.runtime.lastError.message };
        } else {
          lastReply = reply || null;
        }
        paint('timer');
      });
    } catch (e) {
      replyErrors += 1;
      lastReply = { error: String(e) };
      paint('timer');
    }
  }, tickMs);

  document.addEventListener('visibilitychange', () => paint('visibilitychange'));

  window.__hsProbe = () => {
    const mr = marker.getBoundingClientRect();
    return {
      id, mode: 'extension',
      paintCount, tickCount, replyErrors, lastReply,
      lastPaintAt, visibilityState: document.visibilityState,
      hasFocus: document.hasFocus(),
      markerRect: { x: mr.x, y: mr.y, w: mr.width, h: mr.height },
      dpr: window.devicePixelRatio || 1,
      title: document.title,
    };
  };
  window.__hsBump = (n = 1) => {
    paintCount += Math.max(0, Number(n) || 1) - 1;
    paint('bump');
    return paintCount;
  };

  document.title = 'hs-' + id + '-extension';
  paint('initial');
})();
