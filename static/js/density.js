/*
 * F-7 Density. "comfortable" (default) or "compact", applied as
 * html[data-density] so the tokens in _design_system.html do the work.
 * base.html sets the attribute in <head> before first paint; this file wires
 * the toggle buttons and exposes DevPerfDensity for the command palette.
 * Stored in localStorage for now; a server-side UserPreference is a follow-up.
 */
(function (global) {
  'use strict';
  const KEY = 'devperf.density';

  function read() {
    try { return localStorage.getItem(KEY) === 'compact' ? 'compact' : 'comfortable'; }
    catch (e) { return 'comfortable'; }
  }

  function paint(value) {
    if (value === 'compact') document.documentElement.setAttribute('data-density', 'compact');
    else document.documentElement.removeAttribute('data-density');
    document.querySelectorAll('[data-density-toggle]').forEach(function (btn) {
      const compact = value === 'compact';
      btn.setAttribute('aria-pressed', String(compact));
      btn.title = compact ? 'Density: compact (click for comfortable)' : 'Density: comfortable (click for compact)';
      const icon = btn.querySelector('i');
      if (icon) icon.className = 'bi ' + (compact ? 'bi-arrows-collapse' : 'bi-arrows-expand');
    });
  }

  function set(value) {
    value = value === 'compact' ? 'compact' : 'comfortable';
    try { localStorage.setItem(KEY, value); } catch (e) { /* private mode: applies for this page only */ }
    paint(value);
    window.dispatchEvent(new CustomEvent('devperf:density', { detail: value }));
  }

  function toggle() { set(read() === 'compact' ? 'comfortable' : 'compact'); }

  document.addEventListener('click', function (e) {
    const btn = e.target.closest && e.target.closest('[data-density-toggle]');
    if (btn) { e.preventDefault(); toggle(); }
  });
  paint(read());

  global.DevPerfDensity = { get: read, set: set, toggle: toggle };
})(window);
