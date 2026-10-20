/* Line charts in the site's style, drawn with Chart.js.
   Copied into public/ by scripts/build_site.py. The numbers come from each
   page (written there by the build script); this file only draws them. */
(function () {
  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // Exact dollars as CMS gives them, up to 3 decimal places.
  var dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 3 });
  // Axis labels only: whole dollars for big amounts so the axis stays short.
  function axisDollars(v) {
    var digits = Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 1 ? 2 : 3;
    return "$" + v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  window.drawLimitChart = function (canvas, labels, rawValues, opts) {
    if (!window.Chart || !canvas) return;
    opts = opts || {};
    var values = rawValues.map(function (v) { return v === null ? null : Number(v); });
    var last = values.length - 1;
    var line = css(opts.colorVar || "--color-navy");
    var muted = css("--color-neutral-700");
    var text = css("--color-text");
    var divider = css("--color-divider");
    var font = { family: css("--font-body") || "sans-serif", size: 12 };

    // Same padding rule as the design: 12% of the range above and below.
    var shown = values.filter(function (v) { return v !== null; });
    var lo = Math.min.apply(null, shown), hi = Math.max.apply(null, shown);
    var pad = (hi - lo || hi * 0.1 || 1) * 0.12;

    function draw() {
      new Chart(canvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [{
            data: values, borderColor: line, backgroundColor: line,
            borderWidth: opts.width || 2.5, spanGaps: false, tension: 0,
            borderJoinStyle: "round",
            // Only the latest point gets a dot, as in the design.
            pointRadius: values.map(function (_, i) { return i === last ? 4 : 0; }),
            pointHoverRadius: 5
          }]
        },
        options: {
          maintainAspectRatio: false,
          animation: false,
          layout: { padding: { top: 6, right: 8 } },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: css("--color-surface"), titleColor: text, bodyColor: text,
              borderColor: divider, borderWidth: 1, cornerRadius: 2, displayColors: false,
              titleFont: font, bodyFont: { family: font.family, size: 13, weight: "600" },
              callbacks: { label: function (c) { return dollars.format(c.parsed.y); } }
            }
          },
          scales: {
            x: {
              grid: { display: false }, border: { display: false },
              ticks: {
                font: function (c) { return { family: font.family, size: 12, weight: c.index === last ? "600" : "400" }; },
                color: function (c) { return c.index === last ? text : muted; },
                maxRotation: 0, autoSkip: false,
                // Label every Nth quarter counting back from the latest, so
                // the latest quarter's label is always shown.
                callback: function (v, i) {
                  var step = labels.length <= 8 ? 1 : Math.ceil(labels.length / (canvas.clientWidth < 500 ? 5 : 9));
                  return (last - i) % step === 0 ? labels[i] : "";
                }
              }
            },
            y: {
              min: Math.max(0, lo - pad), max: hi + pad,
              border: { display: false },
              grid: { color: divider, drawTicks: false },
              ticks: { count: 4, padding: 10, font: font, color: muted, callback: axisDollars }
            }
          }
        }
      });
    }
    // Wait for the web font so the axis labels are measured in it.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(draw); else draw();
  };

  // Several lines on one chart: the reference product as a thick navy line,
  // biosimilars in teal, light teal, then colors that differ in lightness. series: [{label, values, colorVar, width, dash}]
  // opts.format "percent" draws shares (0-100%) with a dot on every year.
  window.drawGroupChart = function (canvas, labels, series, opts) {
    if (!window.Chart || !canvas) return;
    opts = opts || {};
    var pct = opts.format === "percent";
    var fmt = pct ? function (v) { return v.toFixed(1) + "%"; } : function (v) { return dollars.format(v); };
    var muted = css("--color-neutral-700"), text = css("--color-text"), divider = css("--color-divider");
    var font = { family: css("--font-body") || "sans-serif", size: 12 };
    var last = labels.length - 1;
    var all = [];
    var datasets = series.map(function (s) {
      var values = s.values.map(function (v) { return v === null ? null : Number(v); });
      values.forEach(function (v) { if (v !== null) all.push(v); });
      var color = css(s.colorVar);
      return {
        label: s.label, data: values, borderColor: color, backgroundColor: color,
        borderWidth: s.width, spanGaps: false, tension: 0, borderJoinStyle: "round",
        borderDash: s.dash ? [6, 4] : [],
        pointRadius: values.map(function (_, i) { return pct ? 2.5 : i === last ? s.width + 1 : 0; }),
        pointHoverRadius: 4, order: s.width > 2 ? 0 : 1
      };
    });
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    var pad = (hi - lo || hi * 0.1 || 1) * 0.12;
    function draw() {
      new Chart(canvas, {
        type: "line",
        data: { labels: labels, datasets: datasets },
        options: {
          maintainAspectRatio: false, animation: false,
          interaction: { mode: "index", intersect: false },
          layout: { padding: { top: 6, right: 8 } },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: css("--color-surface"), titleColor: text, bodyColor: text,
              borderColor: divider, borderWidth: 1, cornerRadius: 2, boxPadding: 4,
              titleFont: font, bodyFont: font,
              filter: function (c) { return c.parsed.y !== null; },
              callbacks: { label: function (c) { return " " + c.dataset.label + ": " + fmt(c.parsed.y); } }
            }
          },
          scales: {
            x: {
              grid: { display: false }, border: { display: false },
              ticks: {
                font: function (c) { return { family: font.family, size: 12, weight: c.index === last ? "600" : "400" }; },
                color: function (c) { return c.index === last ? text : muted; },
                maxRotation: 0, autoSkip: false,
                callback: function (v, i) {
                  var step = labels.length <= 8 ? 1 : Math.ceil(labels.length / (canvas.clientWidth < 500 ? 5 : 9));
                  return (last - i) % step === 0 ? labels[i] : "";
                }
              }
            },
            y: {
              min: pct ? 0 : Math.max(0, lo - pad), max: pct ? 100 : hi + pad, border: { display: false },
              grid: { color: divider, drawTicks: false },
              ticks: { count: pct ? 5 : 4, padding: 10, font: font, color: muted,
                       callback: pct ? function (v) { return v + "%"; } : axisDollars }
            }
          }
        }
      });
    }
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(draw); else draw();
  };

  // Short dollar amounts for bar axes: $1.2B, $450M, $12K.
  function shortDollars(v) {
    var a = Math.abs(v);
    if (a >= 1e9) return "$" + (v / 1e9).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "B";
    if (a >= 1e6) return "$" + (v / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "M";
    if (a >= 1e3) return "$" + (v / 1e3).toLocaleString("en-US", { maximumFractionDigits: 0 }) + "K";
    return "$" + v.toLocaleString("en-US");
  }
  var cents = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // Stacked bars, one stack per year. series: [{label, values, colorVar}],
  // drawn bottom to top in the order given. opts.format: "dollars" or "percent"
  // (percent stacks run to 100).
  window.drawStackedBars = function (canvas, labels, series, opts) {
    if (!window.Chart || !canvas) return;
    opts = opts || {};
    var pct = opts.format === "percent";
    var muted = css("--color-neutral-700"), text = css("--color-text"), divider = css("--color-divider");
    var font = { family: css("--font-body") || "sans-serif", size: 12 };
    var datasets = series.map(function (s) {
      var color = css(s.colorVar);
      return {
        label: s.label, backgroundColor: color, borderColor: css("--color-surface"), borderWidth: 1,
        data: s.values.map(function (v) { return v === null ? null : Number(v); }),
        maxBarThickness: 56
      };
    });
    function draw() {
      new Chart(canvas, {
        type: "bar",
        data: { labels: labels, datasets: datasets },
        options: {
          maintainAspectRatio: false, animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: css("--color-surface"), titleColor: text, bodyColor: text,
              borderColor: divider, borderWidth: 1, cornerRadius: 2, boxPadding: 4,
              titleFont: font, bodyFont: font, itemSort: function (a, b) { return b.datasetIndex - a.datasetIndex; },
              filter: function (c) { return c.parsed.y !== null; },
              callbacks: {
                label: function (c) {
                  return " " + c.dataset.label + ": " + (pct ? c.parsed.y.toFixed(1) + "%" : cents.format(c.parsed.y));
                }
              }
            }
          },
          scales: {
            x: { stacked: true, grid: { display: false }, border: { display: false },
                 ticks: { font: font, color: muted, maxRotation: 0, autoSkip: false } },
            y: {
              stacked: true, min: 0, max: pct ? 100 : undefined, border: { display: false },
              grid: { color: divider, drawTicks: false },
              ticks: { count: pct ? 5 : undefined, maxTicksLimit: 5, padding: 10, font: font, color: muted,
                       callback: function (v) { return pct ? v + "%" : shortDollars(v); } }
            }
          }
        }
      });
    }
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(draw); else draw();
  };
})();
