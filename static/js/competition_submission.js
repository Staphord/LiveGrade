/* Tab-scoped offline queue for invited Competition judge scorecards. */
(function (root) {
  'use strict';
  if (!root.document || !root.LiveGradeSubmission) return;
  const config = root.document.getElementById('competition-transport');
  if (!config) return;
  const form = root.document.getElementById('competition-scorecard-form');
  const stateBox = root.document.getElementById('competition-submission-state');
  const status = root.document.getElementById('competition-submission-status');
  const retryButton = root.document.getElementById('competition-retry-submission');
  let storage;
  try { storage = root.sessionStorage; } catch (_) { storage = null; }

  async function request(url, params) {
    const controller = new AbortController();
    const timeout = root.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await root.fetch(url, Object.assign({
        credentials: 'same-origin', signal: controller.signal,
      }, params));
      const body = await response.text();
      return { ok: response.ok, status: response.status,
        json: async () => JSON.parse(body) };
    } finally { root.clearTimeout(timeout); }
  }

  const transport = root.LiveGradeSubmission.create({
    storage,
    session: config.dataset.competition,
    student: config.dataset.invitation,
    turn: config.dataset.round,
    endpoint: config.dataset.endpoint,
    origin: root.location.origin,
    request,
    online: () => root.navigator.onLine !== false,
    csrf: () => root.document.querySelector('meta[name=csrf-token]').content,
    setCsrf: value => {
      root.document.querySelector('meta[name=csrf-token]').content = value;
    },
    redirect: url => root.location.assign(url),
    reload: () => root.location.reload(),
    synchronizeState: false,
    onState(name, message, pending) {
      if (stateBox) stateBox.hidden = false;
      if (status) status.textContent = message;
      if (retryButton) {
        retryButton.hidden = !pending;
        retryButton.disabled = name === 'sending';
      }
      if (form) {
        form.querySelectorAll('input, button[type=submit]').forEach(field => {
          field.disabled = pending;
        });
      }
    },
  });

  root.LiveGradeCompetitionSubmission = transport;
  if (form) {
    const scoreFields = () => Array.from(form.querySelectorAll('input[name^="entry_"]'));
    scoreFields().forEach(field => {
      if (Object.prototype.hasOwnProperty.call(transport.scores, field.name)) {
        field.value = transport.scores[field.name];
      }
    });
    form.addEventListener('input', () => {
      const scores = {};
      scoreFields().forEach(field => { scores[field.name] = field.value; });
      transport.saveScores(scores);
    });
    form.addEventListener('submit', event => {
      event.preventDefault();
      transport.submit(Array.from(new FormData(form).entries()));
    });
  }
  if (retryButton) retryButton.addEventListener('click', () => transport.retry());
  root.addEventListener('online', () => transport.synchronize());
  root.document.addEventListener('visibilitychange', () => {
    if (root.document.visibilityState === 'visible') transport.synchronize();
  });
  root.setInterval(() => {
    if (root.document.visibilityState === 'visible' && transport.hasPending) {
      transport.synchronize();
    }
  }, 20000);
  if (transport.hasPending) transport.retry();
})(typeof window !== 'undefined' ? window : globalThis);
