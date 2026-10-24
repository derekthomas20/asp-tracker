/* US state map for the biosimilar group pages. Copied into public/ by
   scripts/build_site.py. The numbers come from each page (written there by
   the build script); this file only draws them.

   State shapes: us-atlas states-albers-10m.json (already projected, so no
   map projection is needed), drawn with d3-geo and topojson-client.

   drawStateMap(container, data, opts)
     data   {FIPS code: [services, biosimilar share or null, name]} (numbers as text)
     opts   {unitLabel: "billing units"} */
(function () {
  var ATLAS = "https://cdn.jsdelivr.net/npm/us-atlas@3.0.1/states-albers-10m.json";
  var SVG = "http://www.w3.org/2000/svg";

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function hexToRgb(hex) {
    var n = parseInt(hex.replace("#", ""), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  // t in [0, 1] -> color along the --map-low, --map-mid, --map-high stops
  // (pale to navy on the light page; dark to bright in dark mode).
  function ramp(t, stops) {
    var seg = Math.min(stops.length - 2, Math.floor(t * (stops.length - 1)));
    var u = t * (stops.length - 1) - seg;
    var a = stops[seg], b = stops[seg + 1];
    return "rgb(" + [0, 1, 2].map(function (i) { return Math.round(a[i] + (b[i] - a[i]) * u); }).join(",") + ")";
  }
  var whole = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });  // whole units

  var MEASURES = {
    services: {
      value: function (d) { return d ? Number(d[0]) : null; },
      // Square root spreads the colors: a few large states would otherwise
      // leave every other state the same pale shade.
      scale: Math.sqrt,
      text: function (v, opts) { return whole.format(v) + " " + opts.unitLabel; }
    },
    share: {
      value: function (d) { return d && d[1] !== null ? Number(d[1]) : null; },
      scale: function (v) { return v; },
      text: function (v) { return v.toFixed(1) + "% biosimilar"; }
    }
  };

  window.drawStateMap = function (container, data, opts) {
    if (!container || !window.d3 || !window.topojson) return;
    opts = opts || {};
    var stops, empty;
    var svg = container.querySelector("svg.us-map");
    var info = container.querySelector(".map-info");
    var legendLo = container.querySelector(".map-legend .lo");
    var legendHi = container.querySelector(".map-legend .hi");
    var bar = container.querySelector(".map-legend .bar");
    function colors() {
      stops = [css("--map-low") || "#e6f2f0", css("--map-mid") || "#0f7c7a", css("--map-high") || "#0f2a3d"].map(hexToRgb);
      empty = css("--map-empty") || "#e4e6e1";
      bar.style.background = "linear-gradient(to right, " + [0, 0.5, 1].map(function (t) { return ramp(t, stops); }).join(", ") + ")";
    }
    colors();
    // The theme button: repaint in the new theme's colors.
    document.addEventListener("rxcurve:theme", function () { colors(); paint(); });
    var paths = {};
    var measure = "services";
    var defaultInfo = info.textContent;

    function paint() {
      var m = MEASURES[measure];
      var values = Object.keys(data).map(function (k) { return m.value(data[k]); })
        .filter(function (v) { return v !== null; });
      var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
      var slo = m.scale(lo), shi = m.scale(hi);
      Object.keys(paths).forEach(function (id) {
        var v = m.value(data[id]);
        var p = paths[id];
        p.el.setAttribute("fill", v === null ? empty : ramp(shi > slo ? (m.scale(v) - slo) / (shi - slo) : 1, stops));
        p.title.textContent = p.name + ": " + (v === null ? "no data" : m.text(v, opts));
      });
      legendLo.textContent = values.length ? m.text(lo, opts) : "";
      legendHi.textContent = values.length ? m.text(hi, opts) : "";
    }

    fetch(ATLAS).then(function (r) { return r.json(); }).then(function (us) {
      var path = d3.geoPath();
      var states = topojson.feature(us, us.objects.states).features;
      states.forEach(function (f) {
        var el = document.createElementNS(SVG, "path");
        el.setAttribute("d", path(f));
        el.setAttribute("class", "state");
        var title = document.createElementNS(SVG, "title");
        el.appendChild(title);
        svg.appendChild(el);
        paths[f.id] = { el: el, title: title, name: f.properties.name };
        el.addEventListener("mouseenter", function () { info.textContent = title.textContent; });
        el.addEventListener("mouseleave", function () { info.textContent = defaultInfo; });
        el.addEventListener("click", function () { info.textContent = title.textContent; });  // phones have no hover
      });
      var borders = document.createElementNS(SVG, "path");
      borders.setAttribute("d", path(topojson.mesh(us, us.objects.states, function (a, b) { return a !== b; })));
      borders.setAttribute("class", "borders");
      svg.appendChild(borders);
      paint();
    }).catch(function () {
      info.textContent = "The map could not be loaded. The same figures are in the table below.";
    });

    container.querySelectorAll("[data-measure]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        measure = btn.getAttribute("data-measure");
        container.querySelectorAll("[data-measure]").forEach(function (b) {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        paint();
      });
    });
  };
})();
