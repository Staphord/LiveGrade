/* Small page behaviours that used to be inline onclick/onchange/onload attributes.
   A Content-Security-Policy that blocks inline handlers cannot run those, so the markup
   now carries a data-attribute and this file does the work, for elements that are on the
   page at load and for ones a live refresh inserts later. */
(function () {
  'use strict';

  // <select data-autosubmit>: submit its form as soon as the choice changes.
  document.addEventListener('change', function (event) {
    var select = event.target.closest && event.target.closest('[data-autosubmit]');
    if (select && select.form) select.form.submit();
  });

  document.addEventListener('click', function (event) {
    if (!event.target.closest) return;

    // <button data-toggle-panel="id">: collapse/expand the panel with that id.
    var toggle = event.target.closest('[data-toggle-panel]');
    if (toggle) {
      var panel = document.getElementById(toggle.getAttribute('data-toggle-panel'));
      if (!panel) return;
      var open = panel.classList.toggle('is-collapsed') === false;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      return;
    }

    // <button data-set-next-first>: the group has to be made "next" before it can be started.
    var setNext = event.target.closest('[data-set-next-first]');
    if (setNext && typeof window.showSetNextFirst === 'function') window.showSetNextFirst(setNext);
  });

  // <img data-fade-in>: show the picture once it has loaded. 'load' does not bubble, so it is
  // caught in the capture phase; pictures that finished before this ran are marked here.
  document.addEventListener('load', function (event) {
    var img = event.target;
    if (img && img.matches && img.matches('img[data-fade-in]')) img.classList.add('is-loaded');
  }, true);
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('img[data-fade-in]').forEach(function (img) {
      if (img.complete && img.naturalWidth) img.classList.add('is-loaded');
    });
  });
})();
