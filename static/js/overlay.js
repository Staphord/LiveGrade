/*
 * F-6 Drawer, modal and confirm dialog (built on Bootstrap's Offcanvas/Modal so
 * focus trapping, Esc and backdrop behaviour are the tested ones).
 *
 *   DevPerf.confirm({ title, message, confirmLabel, tone: 'danger'|'primary' }) -> Promise<boolean>
 *       Destructive actions only. Anything reversible should use notifyUndo() instead.
 *   DevPerf.openDrawer({ url | html, title, large })   side-peek: keep the list, see the record
 *   DevPerf.openModal({ url | html, title, selector })  quick-create shell
 *
 * Declarative:
 *   <a href="/tasks/12/" data-peek data-peek-title="Task 12">…</a>      opens in the drawer
 *   <a href="/tasks/new/" data-modal data-modal-title="New task">…</a>  opens in a modal
 *   <button data-confirm="Delete this?" data-confirm-label="Delete">…</button> (or a <form>/<a>)
 *
 * Fetched pages are read as HTML and only the part named by `selector`
 * (default #peek-content, then .page-body) is shown. Scripts inside fetched
 * content do not run, so peek is for reading and for plain forms.
 */
(function (global) {
  'use strict';

  function el(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }
  function escapeHtml(v) { const d = document.createElement('div'); d.textContent = v == null ? '' : v; return d.innerHTML; }

  async function fetchFragment(url, selector) {
    const res = await fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-Peek': '1' } });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
    const node = doc.querySelector(selector || '#peek-content') || doc.querySelector('.page-body');
    if (!node) throw new Error('Nothing to show');
    node.querySelectorAll('script').forEach(function (s) { s.remove(); });
    const heading = doc.querySelector('.page-header .page-title') || doc.querySelector('.topbar-title');
    return { html: node.innerHTML, title: heading ? heading.textContent.trim() : '' };
  }

  // ---- confirm ---------------------------------------------------------------------------
  function confirmDialog(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      const danger = (opts.tone || 'danger') === 'danger';
      const modal = el('div', 'modal fade dp-modal dp-confirm' + (danger ? ' is-danger' : ''));
      modal.tabIndex = -1;
      modal.setAttribute('aria-modal', 'true');
      modal.setAttribute('role', 'alertdialog');
      modal.innerHTML =
        '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
        '<div class="modal-body d-flex gap-3">' +
          '<span class="dp-confirm-icon"><i class="bi ' + (danger ? 'bi-exclamation-triangle' : 'bi-question-circle') + '"></i></span>' +
          '<div><h2 class="modal-title mb-1">' + escapeHtml(opts.title || 'Are you sure?') + '</h2>' +
          '<div class="text-muted">' + escapeHtml(opts.message || '') + '</div></div></div>' +
        '<div class="modal-footer"><button type="button" class="btn btn-outline-secondary btn-sm" data-cancel>' +
          escapeHtml(opts.cancelLabel || 'Cancel') + '</button>' +
        '<button type="button" class="btn btn-' + (danger ? 'danger' : 'primary') + ' btn-sm" data-ok>' +
          escapeHtml(opts.confirmLabel || (danger ? 'Delete' : 'Confirm')) + '</button></div></div></div>';
      document.body.appendChild(modal);
      const instance = global.bootstrap.Modal.getOrCreateInstance(modal);
      let answer = false;
      modal.querySelector('[data-ok]').addEventListener('click', function () { answer = true; instance.hide(); });
      modal.querySelector('[data-cancel]').addEventListener('click', function () { instance.hide(); });
      modal.addEventListener('shown.bs.modal', function () {
        // Focus the safe choice for destructive prompts.
        modal.querySelector(danger ? '[data-cancel]' : '[data-ok]').focus();
      });
      modal.addEventListener('hidden.bs.modal', function () { instance.dispose(); modal.remove(); resolve(answer); });
      instance.show();
    });
  }

  // ---- drawer ------------------------------------------------------------------------------
  let drawer = null;
  async function openDrawer(opts) {
    opts = opts || {};
    if (drawer) { drawer.instance.hide(); }
    const node = el('div', 'offcanvas offcanvas-end dp-drawer' + (opts.large ? ' dp-drawer-lg' : ''));
    node.tabIndex = -1;
    node.setAttribute('aria-labelledby', 'dp-drawer-title');
    node.innerHTML =
      '<div class="offcanvas-header"><h2 class="dp-drawer-title" id="dp-drawer-title"></h2>' +
      '<a class="btn btn-outline-secondary btn-sm dp-open-full" hidden><i class="bi bi-box-arrow-up-right me-1"></i>Open</a>' +
      '<button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button></div>' +
      '<div class="offcanvas-body"></div>';
    document.body.appendChild(node);
    const body = node.querySelector('.offcanvas-body');
    node.querySelector('.dp-drawer-title').textContent = opts.title || '';
    if (opts.url) {
      const full = node.querySelector('.dp-open-full');
      full.href = opts.url;
      full.hidden = false;
      body.innerHTML = '<div role="status" aria-busy="true"><span class="sk sk-line sk-title"></span><span class="sk sk-line"></span><span class="sk sk-line w-75"></span><span class="sk sk-block d-block mt-3"></span></div>';
    } else {
      body.innerHTML = opts.html || '';
    }
    const instance = global.bootstrap.Offcanvas.getOrCreateInstance(node);
    drawer = { node: node, instance: instance };
    node.addEventListener('hidden.bs.offcanvas', function () {
      instance.dispose(); node.remove(); if (drawer && drawer.node === node) drawer = null;
    });
    instance.show();
    if (opts.url) {
      try {
        const frag = await fetchFragment(opts.url, opts.selector);
        body.innerHTML = frag.html;
        if (!opts.title && frag.title) node.querySelector('.dp-drawer-title').textContent = frag.title;
      } catch (err) {
        // The peek is a convenience; the real page is the fallback.
        instance.hide();
        window.location.assign(opts.url);
      }
    }
    return instance;
  }

  // ---- modal ---------------------------------------------------------------------------------
  async function openModal(opts) {
    opts = opts || {};
    const modal = el('div', 'modal fade dp-modal');
    modal.tabIndex = -1;
    modal.setAttribute('aria-labelledby', 'dp-modal-title');
    modal.innerHTML =
      '<div class="modal-dialog modal-dialog-centered modal-dialog-scrollable"><div class="modal-content">' +
      '<div class="modal-header"><h2 class="modal-title" id="dp-modal-title"></h2>' +
      '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div>' +
      '<div class="modal-body"></div></div></div>';
    document.body.appendChild(modal);
    modal.querySelector('.modal-title').textContent = opts.title || '';
    const body = modal.querySelector('.modal-body');
    const instance = global.bootstrap.Modal.getOrCreateInstance(modal);
    modal.addEventListener('hidden.bs.modal', function () { instance.dispose(); modal.remove(); });
    if (opts.url) {
      body.innerHTML = '<div role="status" aria-busy="true"><span class="sk sk-line sk-title"></span><span class="sk sk-line"></span><span class="sk sk-line w-75"></span></div>';
      instance.show();
      try {
        const frag = await fetchFragment(opts.url, opts.selector);
        body.innerHTML = frag.html;
        const first = body.querySelector('input:not([type=hidden]), select, textarea');
        if (first) first.focus();
      } catch (err) {
        instance.hide();
        window.location.assign(opts.url);
      }
    } else {
      body.innerHTML = opts.html || '';
      instance.show();
    }
    return instance;
  }

  // ---- declarative hooks ----------------------------------------------------------------------
  function plainClick(e) { return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey; }

  document.addEventListener('click', function (e) {
    const peek = e.target.closest && e.target.closest('[data-peek]');
    if (peek && plainClick(e)) {
      e.preventDefault();
      openDrawer({ url: peek.getAttribute('href') || peek.dataset.peek, title: peek.dataset.peekTitle,
                   large: peek.hasAttribute('data-peek-large'), selector: peek.dataset.peekSelector });
      return;
    }
    const modal = e.target.closest && e.target.closest('[data-modal]');
    if (modal && plainClick(e)) {
      e.preventDefault();
      openModal({ url: modal.getAttribute('href') || modal.dataset.modal, title: modal.dataset.modalTitle,
                  selector: modal.dataset.modalSelector });
      return;
    }
    const ask = e.target.closest && e.target.closest('[data-confirm]');
    if (ask && !ask.dataset.confirmed && !(ask.tagName === 'FORM')) {
      e.preventDefault();
      confirmDialog({ title: ask.dataset.confirmTitle, message: ask.dataset.confirm,
                      confirmLabel: ask.dataset.confirmLabel, tone: ask.dataset.confirmTone }).then(function (ok) {
        if (!ok) return;
        ask.dataset.confirmed = '1';
        if (ask.tagName === 'A') window.location.assign(ask.href);
        else ask.click();
        window.setTimeout(function () { delete ask.dataset.confirmed; }, 0);
      });
    }
  });

  // <form data-confirm="…"> asks on submit.
  document.addEventListener('submit', function (e) {
    const form = e.target;
    if (!form.matches || !form.matches('form[data-confirm]') || form.dataset.confirmed) return;
    e.preventDefault();
    confirmDialog({ title: form.dataset.confirmTitle, message: form.dataset.confirm,
                    confirmLabel: form.dataset.confirmLabel, tone: form.dataset.confirmTone }).then(function (ok) {
      if (!ok) {
        // feedback.js runs before this handler and marks the submit button busy.
        // Since the browser submission was cancelled, there is no navigation
        // event to clear that state; restore it so the form remains usable.
        form.querySelectorAll('button.is-busy').forEach(function (button) {
          button.classList.remove('is-busy');
          button.removeAttribute('aria-busy');
        });
        return;
      }
      form.dataset.confirmed = '1';
      // requestSubmit() (not the native submit()) — it re-fires the
      // 'submit' event, which is what a page's own AJAX submit listener
      // (e.g. session_detail.html's live-ajax-form handler) needs to see
      // in order to actually run. The old submit() call bypassed that
      // event entirely, so confirming never reached an AJAX-intercepted
      // form's real handler at all.
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    });
  });

  global.DevPerf = Object.assign(global.DevPerf || {}, {
    confirm: confirmDialog, openDrawer: openDrawer, openModal: openModal,
  });
})(window);
