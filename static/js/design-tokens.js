(function () {
  const root = document.documentElement;

  window.token = function (name) {
    return getComputedStyle(root).getPropertyValue(name).trim();
  };

  // Mirrors core.utils.SCORE_BANDS — core/tests/test_score_bands.py fails if
  // these thresholds drift from it.
  window.scoreTone = function (score) {
    if (score >= 3.5) return 'success';
    if (score >= 2.5) return 'info';
    if (score >= 1.5) return 'warn';
    return 'danger';
  };
})();
