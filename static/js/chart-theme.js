/*
 * F-11 Chart style guide. Loaded after Chart.js and design-tokens.js.
 * Sets Chart.js defaults from the design tokens so every chart shares font,
 * grid, tooltip and motion, and gives pages a tone-based palette instead of
 * hand-picked colors.
 *
 *   DevPerfCharts.palette(n)            n categorical colors (accent first)
 *   DevPerfCharts.tone('success')       'success' | 'info' | 'warn' | 'danger' | 'neutral' | 'high'
 *   DevPerfCharts.alpha(color, 0.15)    translucent fill for area/bars
 *   DevPerfCharts.sparkline(canvas, values, tone)
 *   DevPerfCharts.legend(container, [{label, color}])
 *   DevPerfCharts.markEmpty(canvas, isEmpty)   toggles the .chart-wrap empty state
 * Markup: <div class="chart-wrap"><canvas></canvas><div class="chart-empty">…</div></div>
 */
(function (global) {
  'use strict';
  const token = global.token || function () { return ''; };

  const CATEGORICAL = ['--accent', '--success', '--high', '--brand-bright', '--warn', '--text-muted'];
  const TONES = {
    success: '--success', info: '--accent', warn: '--warn',
    danger: '--danger', neutral: '--text-subtle', high: '--high',
  };

  function palette(n) {
    const out = [];
    for (let i = 0; i < n; i++) out.push(token(CATEGORICAL[i % CATEGORICAL.length]));
    return out;
  }
  function tone(name) { return token(TONES[name] || TONES.neutral); }

  function alpha(color, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec((color || '').trim());
    if (!m) return color;
    const n = parseInt(m[1], 16);
    return 'rgba(' + (n >> 16) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  function isEmpty(chart) {
    const sets = chart.data && chart.data.datasets ? chart.data.datasets : [];
    return !sets.some(function (ds) {
      return (ds.data || []).some(function (v) {
        return v !== null && v !== undefined && !(typeof v === 'number' && v === 0);
      });
    });
  }

  function markEmpty(canvas, empty) {
    const wrap = canvas && canvas.closest ? canvas.closest('.chart-wrap') : null;
    if (wrap) wrap.classList.toggle('is-empty', !!empty);
  }

  function legend(container, items) {
    container.classList.add('chart-legend');
    container.innerHTML = '';
    items.forEach(function (item) {
      const el = document.createElement('span');
      const sw = document.createElement('span');
      sw.className = 'swatch';
      sw.style.setProperty('--swatch', item.color);
      el.appendChild(sw);
      el.appendChild(document.createTextNode(item.label));
      container.appendChild(el);
    });
  }

  function sparkline(canvas, values, toneName) {
    if (!global.Chart) return null;
    const color = tone(toneName || 'info');
    return new global.Chart(canvas, {
      type: 'line',
      data: { labels: values.map(function (_, i) { return i; }),
              datasets: [{ data: values, borderColor: color, backgroundColor: alpha(color, 0.12), fill: true, borderWidth: 2 }] },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
        elements: { point: { radius: 0 } },
      },
    });
  }

  function applyDefaults() {
    const C = global.Chart;
    if (!C) return;
    const reduce = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const d = C.defaults;
    d.font.family = token('--font-ui') || d.font.family;
    d.font.size = 12;
    d.color = token('--text-muted');
    d.borderColor = token('--border');
    d.responsive = true;
    d.maintainAspectRatio = false;
  
    if (reduce) {
      d.animation = false;
    } else {
      Object.assign(d.animation, { duration: 400, easing: 'easeOutQuart' });
    }
    d.plugins.legend.position = 'bottom';
    d.plugins.legend.labels.usePointStyle = true;
    d.plugins.legend.labels.boxWidth = 8;
    d.plugins.legend.labels.padding = 16;
    d.plugins.tooltip.backgroundColor = token('--ink');
    d.plugins.tooltip.titleColor = token('--surface');
    d.plugins.tooltip.bodyColor = token('--surface');
    d.plugins.tooltip.padding = 10;
    d.plugins.tooltip.cornerRadius = 8;
    d.plugins.tooltip.boxPadding = 4;
    d.elements.line.tension = 0.35;
    d.elements.line.borderWidth = 2;
    d.elements.point.radius = 0;
    d.elements.point.hoverRadius = 4;
    d.elements.bar.borderRadius = 4;
    d.elements.arc.borderWidth = 2;
    d.elements.arc.borderColor = token('--surface');
    if (d.scale) {
      d.scale.grid.color = token('--border');
      d.scale.border.display = false;
      d.scale.ticks.color = token('--text-muted');
    }
    // Empty charts show the .chart-empty message instead of a blank canvas.
    C.register({
      id: 'devperfEmpty',
      afterInit: function (chart) { markEmpty(chart.canvas, isEmpty(chart)); },
      afterUpdate: function (chart) { markEmpty(chart.canvas, isEmpty(chart)); },
    });
  }

  applyDefaults();
  global.DevPerfCharts = { palette: palette, tone: tone, alpha: alpha, sparkline: sparkline,
                            legend: legend, markEmpty: markEmpty };
})(window);
