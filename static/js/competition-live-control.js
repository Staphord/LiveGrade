(() => {
  const root = document.querySelector('[data-competition-live-console]');
  if (!root) return;

  const isActive = root.dataset.liveStatus === 'active';
  const isResults = root.dataset.liveView === 'results';
  const stateUrl = root.dataset.liveStateUrl;
  const actionUrl = root.dataset.liveActionUrl;
  const socketPath = root.dataset.liveWebsocketPath;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const clock = { state: 'idle', remaining: 0, duration: 180, syncedAt: 0 };
  let socket = null;
  let retryCount = 0;
  let reconnectTimer = null;
  let refreshTimer = null;
  let refreshing = false;
  let refreshPending = false;
  let offlineSyncTimer = null;

  function showInlineError(message) {
    const box = document.getElementById('competition-live-inline-error');
    if (!box) return;
    const content = box.querySelector('.inline-message-content') || box;
    let text = content.querySelector('[data-inline-message-text]');
    if (!text) {
      text = document.createElement('span');
      text.dataset.inlineMessageText = '';
      content.appendChild(text);
    }
    text.textContent = message;
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function clearInlineError() {
    const box = document.getElementById('competition-live-inline-error');
    if (box) box.hidden = true;
  }

  function showFormError(form, message) {
    const box = form?.querySelector('[data-live-form-error]');
    if (!box) {
      showInlineError(message);
      return;
    }
    box.textContent = message;
    box.hidden = false;
  }

  function setConnection(status) {
    const badge = document.getElementById('competition-conn-badge');
    if (!badge) return;
    const labels = { live: 'Live', reconnecting: 'Reconnecting', offline: 'Offline' };
    badge.classList.toggle('is-offline', status !== 'live');
    badge.dataset.connectionState = status;
    badge.setAttribute('aria-label', `Competition updates ${labels[status].toLowerCase()}`);
    badge.innerHTML = `<span class="dot" aria-hidden="true"></span>${labels[status]}`;
  }

  function timerStateFromData(data) {
    if (data.pitch_timer_state) return data.pitch_timer_state;
    if (data.timer_paused) return 'paused';
    return Number(data.pitch_seconds_remaining) > 0 ? 'running' : 'expired';
  }

  function syncClock(data) {
    if (typeof data.pitch_seconds_remaining !== 'number') return;
    clock.state = timerStateFromData(data);
    clock.remaining = Math.max(0, Number(data.pitch_seconds_remaining));
    clock.duration = Math.max(1, Number(data.pitch_timer_duration_seconds) || 180);
    clock.syncedAt = performance.now();
    updateTimerDisplay();
  }

  function syncClockFromDom() {
    const ring = document.getElementById('competition-pitch-ring');
    if (!ring) return;
    syncClock({
      pitch_timer_state: ring.dataset.state,
      pitch_seconds_remaining: Number(ring.dataset.seconds),
      pitch_timer_duration_seconds: Number(ring.dataset.duration),
    });
  }

  function currentSeconds() {
    if (clock.state !== 'running') return clock.remaining;
    const elapsed = (performance.now() - clock.syncedAt) / 1000;
    return Math.max(0, Math.ceil(clock.remaining - elapsed));
  }

  function formatTime(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }

  function updateTimerDisplay() {
    const ring = document.getElementById('competition-pitch-ring');
    const label = document.getElementById('competition-timer-label');
    const badge = document.getElementById('competition-timer-state-badge');
    if (!ring || !label) return;
    const remaining = currentSeconds();
    label.textContent = formatTime(remaining);
    label.setAttribute('aria-label', `Pitch time remaining ${formatTime(remaining)}`);
    ring.style.setProperty('--turn-pct', Math.min(100, Math.round(remaining * 100 / clock.duration)));
    const expired = clock.state === 'running' && remaining === 0;
    ring.classList.toggle('is-urgent', clock.state === 'running' && remaining > 0 && remaining <= 30);
    ring.dataset.state = expired ? 'expired' : clock.state;
    if (badge) {
      const paused = clock.state === 'paused';
      badge.textContent = paused ? 'Paused' : (expired || clock.state === 'expired' ? 'Time elapsed' : 'Active pitch');
      badge.className = `badge ${paused ? 'text-bg-warning' : (expired || clock.state === 'expired' ? 'text-bg-danger' : 'text-bg-success')}`;
    }
    const pauseButton = document.querySelector('[data-competition-action="toggle_pitch_pause"]');
    if (pauseButton && expired) {
      pauseButton.disabled = true;
      pauseButton.removeAttribute('data-competition-action');
      pauseButton.innerHTML = '<i class="bi bi-hourglass-bottom me-1" aria-hidden="true"></i>Time elapsed';
    }
  }

  function updateDom(data, { keepJudges = false } = {}) {
    if (!isResults && data.hero_html) {
      const hero = document.getElementById('competition-live-hero');
      if (hero) hero.outerHTML = data.hero_html;
    }
    if (!keepJudges && data.judges_html) {
      const judges = document.getElementById('competition-live-judges');
      if (judges) judges.outerHTML = data.judges_html;
    }
    if (!isResults && data.audience_html) {
      const audience = document.getElementById('competition-live-audience');
      if (audience) audience.outerHTML = data.audience_html;
    }
    if (data.standings_html) {
      const standings = document.getElementById('competition-live-standings');
      if (standings) standings.outerHTML = data.standings_html;
    }
    if (!isResults && data.projector_html) {
      const projector = document.getElementById('competition-live-projector');
      if (projector) projector.outerHTML = data.projector_html;
    }
    if (isResults && data.rounds_html) {
      const rounds = document.getElementById('competition-round-timeline');
      if (rounds) rounds.outerHTML = data.rounds_html;
    }
    if (data.projector_mode) {
      document.querySelectorAll('[data-competition-projector]').forEach((button) => {
        const selected = button.dataset.competitionProjector === data.projector_mode;
        button.classList.toggle('btn-primary', selected);
        button.classList.toggle('btn-outline-secondary', !selected);
        button.setAttribute('aria-pressed', String(selected));
      });
    }
    syncClock(data);
  }

  async function refreshState() {
    if (!stateUrl) return;
    if (refreshing) {
      refreshPending = true;
      return;
    }
    refreshing = true;
    try {
      const response = await fetch(stateUrl, {
        cache: 'no-store',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await response.json();
      if (data.redirect) {
        window.location.assign(data.redirect);
        return;
      }
      if (!response.ok || !data.ok) throw new Error(data.error || 'Live state is unavailable.');
      updateDom(data);
    } catch (_error) {
      setConnection(navigator.onLine ? 'reconnecting' : 'offline');
    } finally {
      refreshing = false;
      if (refreshPending) {
        refreshPending = false;
        refreshState();
      }
    }
  }

  function scheduleRefresh() {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(refreshState, 120);
  }

  function connectSocket() {
    if (!isActive || !window.WebSocket || !navigator.onLine) {
      if (isActive) setConnection('offline');
      return;
    }
    window.clearTimeout(reconnectTimer);
    const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    try {
      socket = new WebSocket(`${scheme}//${window.location.host}${socketPath}`);
      socket.onopen = () => {
        retryCount = 0;
        setConnection('live');
        window.clearInterval(offlineSyncTimer);
        refreshState();
      };
      socket.onmessage = scheduleRefresh;
      socket.onclose = () => {
        if (!isActive) return;
        setConnection(navigator.onLine ? 'reconnecting' : 'offline');
        window.clearInterval(offlineSyncTimer);
        offlineSyncTimer = window.setInterval(() => {
          if (!socket || socket.readyState !== WebSocket.OPEN) refreshState();
        }, 30000);
        const base = Math.min(30000, 1000 * (2 ** Math.min(retryCount, 5)));
        const jitter = Math.random() * Math.min(1000, base / 4);
        retryCount += 1;
        reconnectTimer = window.setTimeout(connectSocket, base + jitter);
      };
      socket.onerror = () => socket.close();
    } catch (_error) {
      setConnection('reconnecting');
      reconnectTimer = window.setTimeout(connectSocket, 2000);
    }
  }

  async function postAction(params, source = null) {
    const previousText = source?.textContent;
    if (source) {
      source.disabled = true;
      source.setAttribute('aria-busy', 'true');
    }
    const body = new FormData();
    Object.entries(params).forEach(([key, value]) => body.append(key, value));
    try {
      const response = await fetch(actionUrl, {
        method: 'POST',
        body,
        headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await response.json();
      if (data.redirect) {
        window.location.assign(data.redirect);
        return false;
      }
      if (!response.ok || data.error) {
        showInlineError(data.error || 'Action could not be completed.');
        if (source) {
          source.disabled = false;
          source.removeAttribute('aria-busy');
        }
        return false;
      }
      clearInlineError();
      updateDom(data, { keepJudges: params.action === 'nudge_judge' });
      return true;
    } catch (_error) {
      showInlineError('Connection interrupted. Your action was not confirmed. Try again.');
      if (source) {
        source.disabled = false;
        source.removeAttribute('aria-busy');
      }
      return false;
    } finally {
      if (source && source.isConnected && source.disabled && params.action !== 'nudge_judge') {
        source.disabled = false;
        source.removeAttribute('aria-busy');
      }
      if (source && previousText && params.action === 'nudge_judge' && !source.disabled) {
        source.textContent = previousText;
      }
    }
  }

  const startButton = document.getElementById('confirm-competition-start-btn');
  if (startButton) {
    startButton.addEventListener('click', async (event) => {
      if (startButton.dataset.confirm && !startButton.dataset.confirmed) return;
      event.preventDefault();
      startButton.disabled = true;
      const body = new FormData();
      body.append('action', 'start_competition');
      try {
        const response = await fetch(actionUrl, {
          method: 'POST', body,
          headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrfToken },
        });
        const data = await response.json();
        if (data.redirect) window.location.assign(data.redirect);
        else {
          showInlineError(data.error || 'Could not start the competition.');
          startButton.disabled = false;
        }
      } catch (_error) {
        showInlineError('Connection interrupted. The competition was not started. Try again.');
        startButton.disabled = false;
      }
    });
  }

  document.addEventListener('click', async (event) => {
    const actionButton = event.target.closest('[data-competition-action]');
    if (actionButton) {
      event.preventDefault();
      const params = { action: actionButton.dataset.competitionAction };
      if (actionButton.dataset.delta) params.delta = actionButton.dataset.delta;
      await postAction(params, actionButton);
      return;
    }
    const nudgeButton = event.target.closest('[data-competition-nudge]');
    if (nudgeButton) {
      event.preventDefault();
      const sent = await postAction({ action: 'nudge_judge', judge_id: nudgeButton.dataset.judgeId }, nudgeButton);
      if (sent && nudgeButton.isConnected) {
        nudgeButton.disabled = true;
        nudgeButton.removeAttribute('aria-busy');
        nudgeButton.textContent = 'Reminder sent';
      }
      return;
    }
    const projectorButton = event.target.closest('[data-competition-projector]');
    if (projectorButton) {
      event.preventDefault();
      await postAction({ action: 'set_projector_mode', mode: projectorButton.dataset.competitionProjector }, projectorButton);
    }
  });

  document.addEventListener('change', async (event) => {
    if (event.target.matches('[data-competition-select-entry]')) {
      await postAction({ action: 'set_active_entry', entry_id: event.target.value }, event.target);
    }
  });

  document.addEventListener('submit', async (event) => {
    const form = event.target.closest('form[data-competition-live-action-form]');
    if (!form || form.action !== new URL(actionUrl, window.location.href).href) return;
    event.preventDefault();
    if (form.dataset.competitionTieForm !== undefined) {
      for (const group of form.querySelectorAll('[data-tie-choice-group]')) {
        const required = Number(group.dataset.requiredCount);
        const selected = group.querySelectorAll('input[type="checkbox"]:checked').length;
        if (selected !== required) {
          showFormError(form, `Select exactly ${required} tied entrant${required === 1 ? '' : 's'} for ${group.querySelector('legend')?.textContent.split(':')[0] || 'this track'}.`);
          group.querySelector('input[type="checkbox"]')?.focus();
          return;
        }
      }
    }
    const submitButton = form.querySelector('[type="submit"]');
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.setAttribute('aria-busy', 'true');
    }
    const body = new FormData(form);
    try {
      const response = await fetch(actionUrl, {
        method: 'POST', body,
        headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' },
      });
      const data = await response.json();
      if (data.redirect) {
        window.location.assign(data.redirect);
        return;
      }
      if (!response.ok || data.error) {
        showFormError(form, data.error || 'Action could not be completed.');
        return;
      }
      clearInlineError();
      const formError = form.querySelector('[data-live-form-error]');
      if (formError) formError.hidden = true;
      updateDom(data);
      const modal = form.closest('.modal');
      if (modal && window.bootstrap?.Modal) window.bootstrap.Modal.getInstance(modal)?.hide();
    } catch (_error) {
      showFormError(form, 'Connection interrupted. Your action was not confirmed. Try again.');
    } finally {
      if (submitButton?.isConnected) {
        submitButton.disabled = false;
        submitButton.removeAttribute('aria-busy');
      }
    }
  });

  document.addEventListener('keydown', (event) => {
    const active = document.activeElement;
    const interactive = active && (active.matches('input, textarea, select, button, a, [contenteditable="true"]')
      || active.closest('[contenteditable="true"]'));
    if (!isActive || event.code !== 'Space' || event.repeat || event.altKey || event.ctrlKey || event.metaKey
        || event.shiftKey || active !== document.body || interactive) return;
    event.preventDefault();
    const pause = document.querySelector('[data-competition-action="toggle_pitch_pause"]');
    if (pause) postAction({ action: 'toggle_pitch_pause' }, pause);
  });

  syncClockFromDom();
  window.setInterval(updateTimerDisplay, 250);
  if (isActive) {
    connectSocket();
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') refreshState();
    });
    window.addEventListener('online', connectSocket);
    window.addEventListener('offline', () => setConnection('offline'));
  }
})();
