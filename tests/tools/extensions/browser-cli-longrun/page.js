// Test-only MV3 extension page for the long-run stability gate. It mimics a
// long-lived extension popup: visible text + canvas repaint on a timer, a
// 1 Hz service-worker round trip, and a marker block that encodes paintCount in
// R/G so a screenshot can be matched against the DOM counter instead of being
// trusted as a "not white" bitmap.
(() => {
  const q = new URLSearchParams(location.search);
  const id = q.get('id') || 'ext';
  const tickMs = Math.max(50, Number(q.get('tick') || 1000));
  const line = document.getElementById('line');
  const marker = document.getElementById('marker');
  const c2d = document.getElementById('c2d');
  const ctx = c2d.getContext('2d');

  let paintCount = 0;
  let rafCount = 0;
  let tickCount = 0;
  let lastPaintAt = 0;
  let lastReply = null;
  let replyErrors = 0;
  let frozen = false;

  function markerColour(n) {
    return [n & 0xff, (n >> 8) & 0xff, 255];
  }

  function draw2d() {
    const [r, g, b] = markerColour(paintCount);
    ctx.fillStyle = '#fff8ee';
    ctx.fillRect(0, 0, 320, 120);
    ctx.fillStyle = 'rgb(' + r + ',' + g + ',' + b + ')';
    ctx.fillRect(0, 0, 320, 24);
    ctx.fillStyle = '#c26b1f';
    ctx.fillRect(0, 40, (paintCount * 7) % 320, 40);
    ctx.fillStyle = '#111';
    ctx.font = '20px monospace';
    ctx.fillText(String(paintCount), 8, 105);
  }

  function paint(reason) {
    if (frozen && reason !== 'bump' && reason !== 'freeze') return;
    paintCount += 1;
    lastPaintAt = Date.now();
    const [r, g, b] = markerColour(paintCount);
    marker.style.background = 'rgb(' + r + ',' + g + ',' + b + ')';
    line.textContent = 'id=' + id + ' paint=' + paintCount + ' raf=' + rafCount +
      ' tick=' + tickCount + ' vis=' + document.visibilityState +
      ' focus=' + document.hasFocus() + ' frozen=' + frozen +
      ' reason=' + (reason || '') + ' reply=' + JSON.stringify(lastReply) +
      ' errors=' + replyErrors;
    draw2d();
  }

  setInterval(() => {
    tickCount += 1;
    try {
      chrome.runtime.sendMessage({ kind: 'lr-tick', n: tickCount }, (reply) => {
        if (chrome.runtime.lastError) {
          replyErrors += 1;
          lastReply = { error: chrome.runtime.lastError.message, n: tickCount };
        } else {
          lastReply = reply || { n: tickCount };
        }
        paint('timer');
      });
    } catch (e) {
      replyErrors += 1;
      lastReply = { error: String(e), n: tickCount };
      paint('timer');
    }
  }, tickMs);

  const loop = () => {
    rafCount += 1;
    paint('raf');
    requestAnimationFrame(loop);
  };
  requestAnimationFrame(loop);

  document.addEventListener('visibilitychange', () => paint('visibilitychange'));

  window.__hsProbe = () => {
    const mr = marker.getBoundingClientRect();
    const cr = c2d.getBoundingClientRect();
    let canvasMarker = null;
    try { canvasMarker = Array.from(ctx.getImageData(2, 3, 1, 1).data); } catch (e) {}
    return {
      id, mode: 'extension',
      paintCount, rafCount, tickCount, replyErrors, lastReply, frozen,
      lastPaintAt, visibilityState: document.visibilityState,
      hasFocus: document.hasFocus(),
      markerRect: { x: mr.x, y: mr.y, w: mr.width, h: mr.height },
      canvasRect: { x: cr.x, y: cr.y, w: cr.width, h: cr.height },
      dpr: window.devicePixelRatio || 1,
      canvasMarker,
      title: document.title,
    };
  };
  window.__hsBump = (n = 1) => {
    paintCount += Math.max(0, Number(n) || 1) - 1;
    paint('bump');
    return paintCount;
  };
  window.__hsFreeze = (on = true) => {
    frozen = Boolean(on);
    paint('freeze');
    return frozen;
  };

  document.title = 'lr-' + id + '-extension';
  paint('initial');
})();
