(function () {
  'use strict';

  const root = document.querySelector('[data-live-leaderboard]');
  if (!root || typeof window.fetch !== 'function') return;

  const content = root.querySelector('[data-leaderboard-content]');
  const status = root.querySelector('[data-leaderboard-status]');
  const url = root.dataset.refreshUrl;
  if (!content || !status || !url) return;

  function cell(tag, text, header) {
    const element = document.createElement(tag);
    if (header) element.scope = 'row';
    element.textContent = text;
    return element;
  }

  function render(data) {
    const fragment = document.createDocumentFragment();
    const weighted = Number(data.audience_weight) > 0;
    const hasJudges = Number(data.judge_weight) > 0;
    const method = document.createElement('p');
    method.className = 'text-muted';
    method.textContent = weighted
      ? `Final score combines ${data.judge_weight}% judge scores and ${data.audience_weight}% audience rankings. Components and response counts are shown separately.`
      : 'Results are based on judge scores. Any audience-choice vote is separate.';
    fragment.appendChild(method);
    if (data.has_head_judge_tie_decisions) {
      const note = document.createElement('p');
      note.textContent = 'Equal final scores are ordered by recorded head-judge decisions.';
      fragment.appendChild(note);
    }
    data.track_results.forEach((track) => {
      const section = document.createElement('section');
      section.className = 'tc-panel mb-3';
      const heading = document.createElement('h2');
      heading.className = 'h3';
      heading.id = `track-${track.track.pk}`;
      heading.textContent = track.track.name;
      section.setAttribute('aria-labelledby', heading.id);
      section.appendChild(heading);

      const list = document.createElement('ol');
      list.className = 'list-unstyled mb-0';
      track.entries.forEach((entry) => {
        const row = document.createElement('li');
        row.className = `tc-results-rank${entry.display_rank === 1 ? ' is-first' : ''}`;
        const rank = cell('span', String(entry.display_rank));
        rank.className = 'tc-results-rank-number';
        rank.setAttribute('aria-label', `Place ${entry.display_rank}`);
        const details = document.createElement('span');
        const name = cell('span', entry.entry.name);
        name.className = 'tc-results-rank-name';
        details.appendChild(name);
        const disclosure = document.createElement('details');
        disclosure.className = 'mt-1';
        disclosure.appendChild(cell('summary', 'Score details'));
        const componentList = document.createElement('ul');
        componentList.className = 'small mt-2 mb-0';
        if (hasJudges) {
          componentList.appendChild(cell('li',
            `Judge score: ${Number(entry.judge_score).toFixed(2)} · ${entry.judge_count} judge${entry.judge_count === 1 ? '' : 's'}`));
          (Array.isArray(entry.score_breakdown) ? entry.score_breakdown : []).forEach((item) => {
            componentList.appendChild(cell('li',
              `${item.criterion} (${item.weight}%): ${Number(item.mean).toFixed(2)} · ${item.judge_count} judge${item.judge_count === 1 ? '' : 's'}`));
          });
        }
        if (weighted) {
          componentList.appendChild(cell('li',
            `Audience score: ${Number(entry.audience_score).toFixed(2)} · ${entry.audience_ballot_count} ballot${entry.audience_ballot_count === 1 ? '' : 's'}`));
        }
        disclosure.appendChild(componentList);
        details.appendChild(disclosure);
        const score = cell('span', Number(entry.score).toFixed(2));
        score.className = 'tc-results-score';
        const scoreLabel = cell('small', weighted ? 'Final score' : 'Score');
        scoreLabel.className = 'd-block text-muted';
        score.appendChild(scoreLabel);
        row.append(rank, details, score);
        list.appendChild(row);
      });
      section.appendChild(list);
      fragment.appendChild(section);
    });
    content.replaceChildren(fragment);
  }

  function criterionCell(breakdown) {
    const target = cell('td', '');
    if (!Array.isArray(breakdown) || !breakdown.length) return target;
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'View criteria';
    details.appendChild(summary);
    const list = document.createElement('ul');
    breakdown.forEach((item) => {
      const line = document.createElement('li');
      line.textContent = `${item.criterion} (${item.weight}%): ${Number(item.mean).toFixed(2)} · ${item.judge_count} judges`;
      list.appendChild(line);
    });
    details.appendChild(list);
    target.appendChild(details);
    return target;
  }

  async function refresh() {
    try {
      const response = await window.fetch(url, {
        headers: { Accept: 'application/json' },
        credentials: 'omit',
        cache: 'no-cache',
      });
      if (response.status === 404) {
        content.replaceChildren();
        status.textContent = 'The public leaderboard is unavailable or has been hidden.';
        return;
      }
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      render(data);
      status.textContent = `Leaderboard updated at ${new Date().toLocaleTimeString()}. Refreshing every 30 seconds.`;
    } catch (_error) {
      status.textContent = 'Leaderboard refresh is temporarily unavailable. The displayed published results remain unchanged.';
    }
  }

  window.setInterval(refresh, 30000);
}());
