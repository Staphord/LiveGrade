(() => {
  const viewport = document.getElementById('projector-viewport');
  if (!viewport) return;
  const stateUrl = viewport.dataset.projectorStateUrl;
  const socketPath = viewport.dataset.projectorWebsocketPath;
  const clock = { state: 'idle', remaining: 0, duration: 180, syncedAt: 0 };
  let socket = null;
  let retryCount = 0;
  let reconnectTimer = null;
  let syncTimer = null;
  let safetySyncTimer = null;
  let refreshing = false;
  let pendingRefresh = false;

  function setConnection(state) {
    const badge = document.getElementById('projector-connection-status');
    if (!badge) return;
    const labels = { live: 'Live', reconnecting: 'Reconnecting', offline: 'Offline' };
    badge.textContent = labels[state];
    badge.dataset.connectionState = state;
  }

  function syncClockFromDom() {
    const screen = document.getElementById('projector-screen-content');
    if (!screen) return;
    clock.state = screen.dataset.pitchTimerState || 'idle';
    clock.remaining = Math.max(0, Number(screen.dataset.pitchSecondsRemaining) || 0);
    clock.duration = Math.max(1, Number(screen.dataset.pitchTimerDuration) || 180);
    clock.syncedAt = performance.now();
    drawTimer();
  }

  function secondsRemaining() {
    if (clock.state !== 'running') return clock.remaining;
    return Math.max(0, Math.ceil(clock.remaining - (performance.now() - clock.syncedAt) / 1000));
  }

  function drawTimer() {
    const timer = document.getElementById('proj-timer-clock');
    if (!timer) return;
    const seconds = secondsRemaining();
    const display = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    timer.textContent = display;
    timer.setAttribute('aria-label', `Pitch time remaining ${display}`);
    const expired = clock.state === 'running' && seconds === 0;
    timer.classList.toggle('is-urgent', clock.state === 'running' && seconds > 0 && seconds <= 30);
    const badge = document.getElementById('proj-timer-badge');
    if (badge) {
      const paused = clock.state === 'paused';
      badge.textContent = paused ? 'Pitch timer paused' : (expired || clock.state === 'expired' ? 'Time elapsed' : 'Live pitch in progress');
      badge.className = `badge ${paused ? 'text-bg-warning' : (expired || clock.state === 'expired' ? 'text-bg-danger' : 'text-bg-success')} fs-6 px-3 py-2`;
    }
  }

  async function refreshScreen() {
    if (refreshing) {
      pendingRefresh = true;
      return;
    }
    refreshing = true;
    try {
      const response = await fetch(stateUrl, {
        cache: 'no-store',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await response.json();
      if (!response.ok || !data.ok || !data.projector_screen_html) {
        throw new Error('Projector state is unavailable.');
      }
      const current = document.getElementById('projector-screen-content');
      if (current) current.outerHTML = data.projector_screen_html;
      syncClockFromDom();
      setConnection('live');
    } catch (_error) {
      setConnection(navigator.onLine ? 'reconnecting' : 'offline');
    } finally {
      refreshing = false;
      if (pendingRefresh) {
        pendingRefresh = false;
        refreshScreen();
      }
    }
  }

  function scheduleRefresh() {
    window.clearTimeout(syncTimer);
    syncTimer = window.setTimeout(refreshScreen, 120);
  }

  function connectSocket() {
    if (!window.WebSocket || !navigator.onLine) {
      setConnection('offline');
      return;
    }
    window.clearTimeout(reconnectTimer);
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      socket = new WebSocket(`${scheme}//${window.location.host}${socketPath}`);
      socket.onopen = () => {
        retryCount = 0;
        window.clearInterval(safetySyncTimer);
        refreshScreen();
      };
      socket.onmessage = scheduleRefresh;
      socket.onclose = () => {
        setConnection(navigator.onLine ? 'reconnecting' : 'offline');
        window.clearInterval(safetySyncTimer);
        safetySyncTimer = window.setInterval(() => {
          if (!socket || socket.readyState !== WebSocket.OPEN) refreshScreen();
        }, 30000);
        const base = Math.min(30000, 1000 * (2 ** Math.min(retryCount, 5)));
        retryCount += 1;
        reconnectTimer = window.setTimeout(connectSocket, base + Math.random() * Math.min(1000, base / 4));
      };
      socket.onerror = () => socket.close();
    } catch (_error) {
      setConnection('reconnecting');
      reconnectTimer = window.setTimeout(connectSocket, 2000);
    }
  }

  syncClockFromDom();
  window.setInterval(drawTimer, 250);
  connectSocket();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshScreen();
  });
  window.addEventListener('online', connectSocket);
  window.addEventListener('offline', () => setConnection('offline'));
})();
