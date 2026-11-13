(function () {
  'use strict';

  const root = document.querySelector('[data-live-leaderboard]');
  if (!root || typeof window.fetch !== 'function') return;

  const content = root.querySelector('[data-leaderboard-content]');
  const connection = root.querySelector('[data-leaderboard-connection]');
  const availability = root.querySelector('[data-leaderboard-availability]');
  const refreshUrl = root.dataset.refreshUrl;
  const websocketUrl = root.dataset.websocketUrl;
  if (!content || !connection || !availability || !refreshUrl || !websocketUrl) return;

  const numberFormat = new Intl.NumberFormat(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  let socket = null;
  let reconnectTimer = null;
  let stableConnectionTimer = null;
  let reconnectAttempt = 0;
  let stopped = false;
  let refreshInFlight = false;
  let refreshQueued = false;

  if (typeof window.WebSocket !== 'function') {
    setConnectionState('reconnecting', 'Live updates unavailable');
    return;
  }

  function cell(tag, text, className) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function appendBadge(target, label, className) {
    target.appendChild(cell('span', label, `tc-results-badge ${className}`));
  }

  function displayScore(value) {
    const score = Number(value);
    return Number.isFinite(score) ? numberFormat.format(score) : '-';
  }

  function plural(count, singular, pluralForm) {
    return `${count} ${count === 1 ? singular : pluralForm}`;
  }

  function scoreDetails(entry, weighted, hasJudges) {
    const details = document.createElement('details');
    details.className = 'tc-results-breakdown';
    details.appendChild(cell('summary', 'Score breakdown'));
    const list = document.createElement('ul');

    if (hasJudges) {
      list.appendChild(cell('li',
        `Judge score: ${displayScore(entry.judge_score)} · ${plural(Number(entry.judge_count) || 0, 'judge', 'judges')}`));
      (Array.isArray(entry.score_breakdown) ? entry.score_breakdown : []).forEach((item) => {
        list.appendChild(cell('li',
          `${item.criterion} (${item.weight}%): ${displayScore(item.mean)} · ${plural(Number(item.judge_count) || 0, 'judge', 'judges')}`));
      });
    }
    if (weighted) {
      list.appendChild(cell('li',
        `Audience score: ${displayScore(entry.audience_score)} · ${plural(Number(entry.audience_ballot_count) || 0, 'ballot', 'ballots')}`));
    }

    details.appendChild(list);
    return details;
  }

  function render(data) {
    const fragment = document.createDocumentFragment();
    const weighted = Number(data.audience_weight) > 0;
    const hasJudges = Number(data.judge_weight) > 0;

    (Array.isArray(data.track_results) ? data.track_results : []).forEach((track) => {
      const entries = Array.isArray(track.entries) ? track.entries : [];
      const section = document.createElement('section');
      section.className = 'tc-panel tc-results-track mb-3';

      const headingId = `track-${String(track.track.pk)}`;
      section.setAttribute('aria-labelledby', headingId);
      const header = document.createElement('header');
      header.className = 'tc-results-track-header';
      const titleBlock = document.createElement('div');
      titleBlock.appendChild(cell('p', 'Final standings', 'tc-results-eyebrow'));
      titleBlock.appendChild(cell('h2', track.track.name, ''));
      titleBlock.lastChild.id = headingId;
      header.appendChild(titleBlock);
      header.appendChild(cell('span', `${entries.length} ${entries.length === 1 ? 'entry' : 'entries'}`, 'tc-chip'));
      section.appendChild(header);

      const list = document.createElement('ol');
      list.className = 'tc-results-list';
      entries.forEach((entry) => {
        const rankValue = Number(entry.display_rank);
        const row = document.createElement('li');
        row.className = 'tc-results-rank';
        if (rankValue === 1) row.classList.add('is-winner');
        else if (rankValue === 2) row.classList.add('is-runner-up');
        else if (rankValue === 3) row.classList.add('is-third');

        row.appendChild(cell('span', String(entry.display_rank), 'tc-results-rank-number'));
        row.firstChild.setAttribute('aria-label', `Place ${entry.display_rank}`);

        const entryBlock = document.createElement('div');
        entryBlock.className = 'tc-results-entry';
        const entryHeading = document.createElement('div');
        entryHeading.className = 'tc-results-entry-heading';
        entryHeading.appendChild(cell('span', entry.entry.name, 'tc-results-rank-name'));
        const badges = document.createElement('span');
        badges.className = 'tc-results-badges';
        if (rankValue === 1) appendBadge(badges, 'Winner', 'is-winner-badge');
        if (entry.tie_break) appendBadge(badges, 'Head judge tie-break', 'is-tie-badge');
        entryHeading.appendChild(badges);
        entryBlock.appendChild(entryHeading);

        const meter = document.createElement('progress');
        meter.className = 'tc-results-meter';
        meter.max = 100;
        const score = Number(entry.score);
        meter.value = Number.isFinite(score) ? Math.max(0, Math.min(100, score)) : 0;
        meter.setAttribute('aria-label', `Score for ${entry.entry.name}`);
        entryBlock.appendChild(meter);
        entryBlock.appendChild(scoreDetails(entry, weighted, hasJudges));
        row.appendChild(entryBlock);

        const scoreValue = document.createElement('span');
        scoreValue.className = 'tc-results-score';
        scoreValue.appendChild(cell('span', displayScore(entry.score)));
        scoreValue.appendChild(cell('small', weighted ? 'Final score / 100' : 'Score / 100'));
        row.appendChild(scoreValue);
        list.appendChild(row);
      });
      section.appendChild(list);
      fragment.appendChild(section);
    });
    content.replaceChildren(fragment);
  }

  async function loadProjection() {
    try {
      const response = await window.fetch(refreshUrl, {
        headers: { Accept: 'application/json' },
        credentials: 'omit',
        cache: 'no-store',
      });
      if (response.status === 404) {
        content.replaceChildren();
        availability.textContent = 'The public leaderboard is paused.';
        availability.hidden = false;
        return;
      }
      if (!response.ok) throw new Error('Leaderboard projection unavailable');
      const data = await response.json();
      render(data);
      availability.textContent = '';
      availability.hidden = true;
    } catch (_error) {
      availability.textContent = 'Live results could not be refreshed. Showing the last available results.';
      availability.hidden = false;
    }
  }

  async function refresh() {
    if (refreshInFlight) {
      refreshQueued = true;
      return;
    }
    refreshInFlight = true;
    try {
      do {
        refreshQueued = false;
        await loadProjection();
      } while (refreshQueued);
    } finally {
      refreshInFlight = false;
    }
  }

  function setConnectionState(state, label) {
    connection.textContent = label;
    connection.classList.toggle('is-connected', state === 'connected');
    connection.classList.toggle('is-reconnecting', state === 'reconnecting');
  }

  function scheduleReconnect() {
    if (stopped || reconnectTimer !== null) return;
    const ceiling = Math.min(30000, 1000 * (2 ** Math.min(reconnectAttempt, 5)));
    const delay = Math.floor(ceiling * (0.5 + Math.random() * 0.5));
    reconnectAttempt += 1;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (stopped) return;
    let endpoint;
    try {
      endpoint = new URL(websocketUrl, window.location.href);
      endpoint.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new window.WebSocket(endpoint.href);
    } catch (_error) {
      setConnectionState('reconnecting', 'Live updates unavailable');
      scheduleReconnect();
      return;
    }

    socket.addEventListener('open', () => {
      if (stableConnectionTimer !== null) window.clearTimeout(stableConnectionTimer);
      stableConnectionTimer = window.setTimeout(() => {
        reconnectAttempt = 0;
        stableConnectionTimer = null;
      }, 30000);
      setConnectionState('connected', 'Live updates on');
    });
    socket.addEventListener('message', (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (_error) {
        return;
      }
      if (message.type === 'leaderboard.state' && typeof message.available === 'boolean') {
        refresh();
      }
    });
    socket.addEventListener('close', () => {
      if (stopped) return;
      if (stableConnectionTimer !== null) {
        window.clearTimeout(stableConnectionTimer);
        stableConnectionTimer = null;
      }
      setConnectionState('reconnecting', 'Reconnecting to live updates');
      scheduleReconnect();
    });
    socket.addEventListener('error', () => socket.close());
  }

  window.addEventListener('pagehide', () => {
    stopped = true;
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
    if (stableConnectionTimer !== null) window.clearTimeout(stableConnectionTimer);
    if (socket && socket.readyState < window.WebSocket.CLOSING) socket.close(1000);
  }, { once: true });

  connect();
}());
