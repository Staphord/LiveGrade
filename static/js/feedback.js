(function () {
  const TONES = {success: 'success', info: 'primary', warn: 'warning', danger: 'danger'};

  const MAX_VISIBLE = 4;
  const LIFETIME = {danger: 9000, warn: 7000};
  const DEFAULT_LIFETIME = 5000;

  function dismiss(el, reason) {
    if (!el.parentNode || el.classList.contains('leaving')) return;
    el.classList.add('leaving');
    window.setTimeout(function () { el.remove(); }, 200);
    // A toast can carry work that should only happen if it was NOT undone
    // (delete after the undo window closes). 'undo' skips it; every other way
    // out — timeout, the close button, being pushed off by newer toasts —
    // commits.
    if (el._onExpire && reason !== 'undo') {
      const commit = el._onExpire;
      el._onExpire = null;
      commit(reason || 'timeout');
    }
  }

  function lifeFor(tone) { return LIFETIME[tone] || DEFAULT_LIFETIME; }

  function arm(el, life) {
    let timer = window.setTimeout(function () { dismiss(el, 'timeout'); }, life);
    // Reading a message should not race a countdown.
    el.addEventListener('mouseenter', function () { window.clearTimeout(timer); });
    el.addEventListener('mouseleave', function () {
      timer = window.setTimeout(function () { dismiss(el, 'timeout'); }, 2000);
    });
  }

  function toneOf(el) {
    if (el.classList.contains('alert-danger')) return 'danger';
    if (el.classList.contains('alert-warning')) return 'warn';
    return 'info';
  }

  /*
   * notify(message, tone, options)
   *   tone:    'success' | 'info' | 'warn' | 'danger'
   *   options: { duration: ms,
   *              action:   { label, onClick },   // e.g. Undo
   *              onExpire: fn(reason) }          // runs unless the action was used
   * Returns the toast element.
   */
  window.notify = function (message, tone, options) {
    options = options || {};
    const region = document.getElementById('flash-region');
    if (!region) return;
    const el = document.createElement('div');
    el.className = 'alert alert-' + (TONES[tone] || 'primary') + ' alert-dismissible fade show';
    el.setAttribute('role', tone === 'danger' ? 'alert' : 'status');
    const text = document.createElement('span');
    text.textContent = message;
    el.appendChild(text);

    const life = options.duration || (options.action ? 8000 : lifeFor(tone));
    el._onExpire = typeof options.onExpire === 'function' ? options.onExpire : null;

    if (options.action && typeof options.action.onClick === 'function') {
      const act = document.createElement('button');
      act.type = 'button';
      act.className = 'toast-action';
      act.textContent = options.action.label || 'Undo';
      act.addEventListener('click', function () {
        options.action.onClick();
        dismiss(el, 'undo');
      });
      el.appendChild(act);
    }

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close btn-close-sm';
    close.setAttribute('aria-label', 'Dismiss');
    close.addEventListener('click', function () { dismiss(el, 'close'); });
    el.appendChild(close);

    // The countdown bar shows how long an undo stays possible.
    if (options.action) {
      const bar = document.createElement('span');
      bar.className = 'toast-timer';
      el.style.setProperty('--toast-life', life + 'ms');
      el.appendChild(bar);
    }

    region.prepend(el);
    // Snapshot and pop: dismiss() only marks the toast and removes it 200ms
    // later, so counting live children in a loop condition never terminates.
    const live = Array.prototype.filter.call(region.children, function (c) {
      return !c.classList.contains('leaving');
    });
    while (live.length > MAX_VISIBLE) {
      dismiss(live.pop(), 'bumped');
    }
    arm(el, life);
    return el;
  };

  // "Deleted X. Undo": the destructive call is held back until the toast
  // leaves without Undo being pressed.
  //   notifyUndo('Task deleted', { onUndo: restore, onCommit: reallyDelete })
  window.notifyUndo = function (message, options) {
    options = options || {};
    return window.notify(message, options.tone || 'info', {
      duration: options.duration || 8000,
      action: { label: options.label || 'Undo', onClick: options.onUndo || function () {} },
      onExpire: options.onCommit,
    });
  };

  // Django's messages framework renders server-side, so those alerts exist in
  // the region before this script runs — they need the same dismiss behaviour
  // as the ones notify() creates, or they would sit there until navigation.
  const region = document.getElementById('flash-region');
  if (region) {
    Array.prototype.forEach.call(region.children, function (el) {
      const close = el.querySelector('.btn-close');
      if (close) {
        close.removeAttribute('data-bs-dismiss');
        close.addEventListener('click', function () { dismiss(el, 'close'); });
      }
      arm(el, lifeFor(toneOf(el)));
    });
  }

  // Leaving the page while an undo window is open must not silently drop the
  // deferred work (e.g. the delete the user already confirmed) — commit it.
  window.addEventListener('pagehide', function () {
    const live = document.getElementById('flash-region');
    if (!live) return;
    Array.prototype.forEach.call(live.children, function (el) {
      if (el._onExpire) {
        const commit = el._onExpire;
        el._onExpire = null;
        commit('pagehide');
      }
    });
  });

  const bar = document.getElementById('nav-progress');
  if (bar) {
    document.addEventListener('click', function (e) {
      const link = e.target.closest ? e.target.closest('a[href]') : null;
      if (!link) return;
      const href = link.getAttribute('href');
      if (!href || href.charAt(0) === '#' || href.indexOf('javascript:') === 0) return;
      if (link.target === '_blank' || link.hasAttribute('download')) return;
      if (link.getAttribute('data-bs-toggle')) return;
      bar.classList.add('active');
    });
    // Restores the bar when the browser serves this page from the back/forward
    // cache, where no fresh load resets it.
    window.addEventListener('pageshow', function () { bar.classList.remove('active'); });
  }

  // Bubble phase, so a form whose own handler called preventDefault (the AJAX
  // forms) is already marked and skipped — otherwise their buttons would spin
  // forever with no navigation to end them.
  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;
    const form = e.target;
    if (form.hasAttribute('data-no-busy')) return;
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (!button || button.classList.contains('is-busy')) return;
    button.classList.add('is-busy');
    button.setAttribute('aria-busy', 'true');
  });
})();
