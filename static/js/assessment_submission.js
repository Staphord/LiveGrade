/* Receipt-driven submission and reconnection. No CSRF tokens enter draft storage. */
(function (root) {
  'use strict';
  function create(options) {
    const storage = options.storage;
    const prefix = 'livegrade-eval-draft-' + options.session + '-' + options.student + '-';
    const currentKey = prefix + (options.turn || 'none');
    let current = read(currentKey) || { scores: {}, key: options.key || '', pending: null };
    let pendingKey = null;
    let busy = false;
    function read(key) {
      try {
        const value = JSON.parse(storage.getItem(key));
        return value && value.scores && typeof value.scores === 'object' && !Array.isArray(value.scores) ? value : null;
      } catch (_) { return null; }
    }
    function write(key, data) { try { storage.setItem(key, JSON.stringify(data)); } catch (_) {} }
    function remove(key) { try { storage.removeItem(key); } catch (_) {} }
    // Only inspect this participant's session namespace. Another tab/person keeps its draft.
    try {
      const keys = Array.from({length: storage.length}, (_, i) => storage.key(i));
      for (const key of keys) {
        if (key && key.indexOf(prefix) === 0) {
          const saved = read(key);
          if (saved && saved.pending && !pendingKey) pendingKey = key;
          else if (saved && !saved.pending && (options.discardUnsent || (key !== currentKey && options.synchronizeState && options.turn))) remove(key);
        }
      }
    } catch (_) {}
    function state(name, message) { options.onState(name, message, !!pendingKey); }
    function destination(url) {
      if (typeof url !== 'string' || !url.startsWith('/') || url.startsWith('//')) throw new Error('Missing confirmation address');
      const target = new URL(url, options.origin);
      if (target.origin !== options.origin) throw new Error('Unexpected confirmation address');
      return target.pathname + target.search;
    }
    async function fetchState() {
      const response = await options.request(options.endpoint, { method: 'GET', headers: { Accept: 'application/json' } });
      const data = await response.json();
      if (response.status === 403 && data.status === 'join_required') { state('join', data.message); return null; }
      if (!response.ok) throw new Error('State unavailable');
      if (data.csrf_token) options.setCsrf(data.csrf_token);
      return data;
    }
    async function retry(refreshToken) {
      if (!pendingKey || busy) return;
      if (!options.online()) { state('retry', 'Offline. Your scores are still here; reconnect and retry.'); return; }
      busy = true;
      state('sending', 'Sending your scores. Waiting for confirmation…');
      try {
        if (refreshToken) {
          const ready = await fetchState();
          if (!ready) { busy = false; return; }
        }
        const record = pendingKey === currentKey ? current : read(pendingKey);
        if (!record || !Array.isArray(record.pending)) throw new Error('Draft unavailable');
        const response = await options.request(options.endpoint, { method: 'POST',
          headers: { Accept: 'application/json', 'X-CSRFToken': options.csrf() },
          body: new URLSearchParams(record.pending) });
        const data = await response.json();
        if (response.ok && data.status === 'accepted') {
          const next = destination(data.next_url);
          remove(pendingKey);
          pendingKey = null;
          state('accepted', 'Your evaluation was received.');
          options.redirect(next);
        } else if (data.status === 'rejected') {
          // A definitive refusal is not an unconfirmed send. Keep the editable scores.
          record.pending = null;
          write(pendingKey, record);
          pendingKey = null;
          state('rejected', data.message || 'Your evaluation was not accepted.');
        } else if (response.status === 403 && data.status === 'join_required') {
          state('join', data.message);
        } else {
          throw new Error('Receipt unavailable');
        }
      } catch (_) {
        state('retry', 'Receipt not confirmed. Your scores are still here. Retry to confirm.');
      } finally { busy = false; }
    }
    async function synchronize() {
      if (busy) return;
      if (pendingKey) return retry(true);
      if (!options.synchronizeState) return;
      try {
        const data = await fetchState();
        if (data && options.changed(data)) options.reload();
      } catch (_) { state('offline', 'Connection interrupted. Your scores are still here.'); }
    }
    return {
      get scores() { return current.scores; },
      get key() { return current.key; },
      get hasPending() { return !!pendingKey; },
      saveScores(scores) { if (!pendingKey) { current.scores = scores; write(currentKey, current); } },
      submit(entries) {
        if (pendingKey) return retry(true);
        current.pending = entries.filter(pair => pair[0] !== 'csrfmiddlewaretoken');
        write(currentKey, current);
        pendingKey = currentKey;
        return retry(false);
      },
      retry() { return retry(true); },
      synchronize,
      requestReload() { return pendingKey ? synchronize() : options.reload(); },
    };
  }
  root.LiveGradeSubmission = { create };
  if (typeof module !== 'undefined' && module.exports) module.exports = { create };
  if (!root.document) return;
  const config = root.document.getElementById('evaluation-transport');
  if (!config) return;
  const form = root.document.getElementById('eval-form');
  const status = root.document.getElementById('submission-status');
  const retryButton = root.document.getElementById('retry-submission');
  const reloadButton = root.document.getElementById('reload-evaluation');
  const box = root.document.getElementById('submission-state');
  const rejoinLink = root.document.getElementById('rejoin-session');
  const keyInput = form && form.querySelector('[name=submission_key]');
  let storage;
  try { storage = root.localStorage; } catch (_) { storage = null; }
  const initial = [config.dataset.status, config.dataset.turn || '', config.dataset.open,
    config.dataset.paused, config.dataset.done].join('|');
  function signature(data) {
    return [data.status, data.turn_id || '', String(data.voting_open),
      String(data.voting_paused), String(data.already_submitted)].join('|');
  }
  async function request(url, params) {
    const controller = new AbortController();
    const timeout = root.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await root.fetch(url, Object.assign({ credentials: 'same-origin', signal: controller.signal }, params));
      // Keep the timeout active through the body, not just until headers arrive.
      const body = await response.text();
      return { ok: response.ok, status: response.status, json: async () => JSON.parse(body) };
    }
    finally { root.clearTimeout(timeout); }
  }
  const transport = create({
    storage, session: config.dataset.session, student: config.dataset.student, turn: config.dataset.turn,
    key: keyInput ? (root.crypto && root.crypto.randomUUID ? root.crypto.randomUUID() : keyInput.value) : '',
    endpoint: config.dataset.endpoint, origin: root.location.origin, request,
    online: () => root.navigator.onLine !== false,
    csrf: () => root.document.querySelector('meta[name=csrf-token]').content,
    setCsrf: value => { root.document.querySelector('meta[name=csrf-token]').content = value; },
    redirect: url => root.location.assign(url), reload: () => root.location.reload(),
    discardUnsent: config.dataset.status === 'closed',
    synchronizeState: config.dataset.page === 'evaluate', changed: data => signature(data) !== initial,
    onState(name, message, pending) {
      if (box) box.hidden = false;
      if (rejoinLink) rejoinLink.hidden = name !== 'join';
      if (status) status.textContent = message;
      if (retryButton) { retryButton.hidden = !pending; retryButton.disabled = name === 'sending'; }
      if (reloadButton) reloadButton.hidden = pending || name === 'sending';
      if (form) {
        form.querySelectorAll('[data-score-slider], [data-score-num], button[type=submit]').forEach(input => {
          input.disabled = pending || form.classList.contains('is-locked');
        });
        const submit = root.document.getElementById('submit-btn');
        if (submit) submit.classList.toggle('btn-loading', name === 'sending');
      }
    },
  });
  root.LiveGradeCurrentSubmission = transport;
  if (keyInput) keyInput.value = transport.key;
  if (retryButton) retryButton.addEventListener('click', () => transport.retry());
  if (reloadButton) reloadButton.addEventListener('click', () => transport.requestReload());
  root.addEventListener('online', () => transport.synchronize());
  root.addEventListener('livegrade:reconnected', () => transport.synchronize());
  root.document.addEventListener('visibilitychange', () => {
    if (root.document.visibilityState === 'visible') transport.synchronize();
  });
  root.setInterval(() => {
    if (root.document.visibilityState === 'visible') transport.synchronize();
  }, 20000);
  if (transport.hasPending) transport.retry();
})(typeof window !== 'undefined' ? window : globalThis);
