/* Biosimilar switching calculator. Copied into public/ by
   scripts/build_site.py. Units, payment limits and coinsurance come from the
   page (written there by the build script from the CMS files); this file only
   does the what-if arithmetic the visitor asks for. */
(function () {
  var data = JSON.parse(document.getElementById("switch-data").textContent);
  var $ = function (id) { return document.getElementById(id); };
  var money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var whole = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
  // Payment limits as CMS gives them (up to 3 decimal places), not rounded to cents.
  var limitMoney = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 3 });
  function signedMoney(v) { return (v > 0 ? "+" : v < 0 ? "−" : "") + money.format(Math.abs(v)); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  var market, products;

  function fillMarkets() {
    var want = new URLSearchParams(location.search).get("market");
    data.markets.forEach(function (m) {
      var o = document.createElement("option");
      o.value = m.id; o.textContent = m.name;
      $("market").appendChild(o);
    });
    if (want && data.markets.some(function (m) { return m.id === want; })) $("market").value = want;
  }

  function chooseMarket() {
    market = data.markets.filter(function (m) { return m.id === $("market").value; })[0];
    products = market.products.map(function (p) {
      return { code: p.code, name: p.name, role: p.role, units: Number(p.units), limit: Number(p.limit), co: Number(p.coinsurance) };
    });
    var target = $("target");
    target.innerHTML = "";
    // Cheapest biosimilar first: the usual switching target.
    products.filter(function (p) { return p.role === "biosimilar"; })
      .sort(function (a, b) { return a.limit - b.limit; })
      .forEach(function (p) {
        var o = document.createElement("option");
        o.value = p.code; o.textContent = p.name + " (" + p.code + ") · " + limitMoney.format(p.limit) + " per " + market.unit;
        target.appendChild(o);
      });
    var lo = $("leftOut");
    lo.hidden = !market.left_out.length;
    lo.textContent = market.left_out.length ? "Left out: " + market.left_out.join("; ") + "." : "";
    render();
  }

  function render() {
    var s = Number($("shift").value) / 100;
    $("shiftOut").textContent = $("shift").value + "%";
    var ref = products.filter(function (p) { return p.role === "reference"; })[0];
    var tgtCode = $("target").value;
    var moved = ref.units * s;
    var rows = products.map(function (p) {
      var after = p.units + (p === ref ? -moved : p.code === tgtCode ? moved : 0);
      var before$ = p.units * p.limit, after$ = after * p.limit;
      return { p: p, after: after, before$: before$, after$: after$,
               coBefore: before$ * p.co / 100, coAfter: after$ * p.co / 100 };
    });
    function sum(f) { return rows.reduce(function (t, r) { return t + f(r); }, 0); }
    var allowedBefore = sum(function (r) { return r.before$; }), allowedAfter = sum(function (r) { return r.after$; });
    var coBefore = sum(function (r) { return r.coBefore; }), coAfter = sum(function (r) { return r.coAfter; });
    var dAllowed = allowedAfter - allowedBefore, dCo = coAfter - coBefore, dMedicare = dAllowed - dCo;
    var totalUnits = sum(function (r) { return r.p.units; });
    var bioBefore = sum(function (r) { return r.p.role === "biosimilar" ? r.p.units : 0; });
    var bioAfter = sum(function (r) { return r.p.role === "biosimilar" ? r.after : 0; });
    var tgt = products.filter(function (p) { return p.code === tgtCode; })[0];

    $("unitLine").textContent = whole.format(moved) + " of " + whole.format(ref.units) + " " + ref.name +
      " billing units (" + market.unit + " each) move to " + tgt.name + ". Each unit moved changes the allowed amount by " +
      signedMoney(tgt.limit - ref.limit) + ".";
    $("resultSub").textContent = "At " + data.quarter + " payment limits, using " + data.year + " units";
    var verb = dAllowed < 0 ? "lower" : dAllowed > 0 ? "higher" : "unchanged";
    $("summary").textContent = dAllowed === 0 ? "The allowed amount does not change." :
      "Moving " + $("shift").value + "% of " + ref.name + " units to " + tgt.name + " makes the yearly allowed amount " +
      signedMoney(dAllowed) + " (" + verb + "): " + signedMoney(dMedicare) + " for Medicare and " + signedMoney(dCo) + " for patients.";

    var cards = [
      ["Allowed amount", allowedBefore, allowedAfter, dAllowed],
      ["Medicare pays", allowedBefore - coBefore, allowedAfter - coAfter, dMedicare],
      ["Patients pay (coinsurance)", coBefore, coAfter, dCo]
    ].map(function (c) {
      return '<div class="switch-card"><span class="label">' + c[0] + '</span><span class="value ' +
        (c[3] < 0 ? "down" : c[3] > 0 ? "up" : "") + '">' + signedMoney(c[3]) + '</span><span class="sub">' +
        money.format(c[1]) + " → " + money.format(c[2]) + "</span></div>";
    });
    cards.push('<div class="switch-card"><span class="label">Biosimilar share of units</span><span class="value">' +
      (100 * bioAfter / totalUnits).toFixed(1) + '%</span><span class="sub">was ' + (100 * bioBefore / totalUnits).toFixed(1) + "% in " + data.year + "</span></div>");
    $("cards").innerHTML = cards.join("");

    $("detailBody").innerHTML = rows.map(function (r) {
      var d = r.after$ - r.before$;
      return "<tr><td>" + esc(r.p.name) + (r.p.role === "reference" ? ' <span class="tag">reference</span>' : "") + "</td><td>" + r.p.code +
        '</td><td class="num">' + limitMoney.format(r.p.limit) + '</td><td class="num">' + r.p.co + '%</td><td class="num">' + whole.format(r.p.units) +
        '</td><td class="num">' + whole.format(r.after) + '</td><td class="num">' + money.format(r.before$) +
        '</td><td class="num">' + money.format(r.after$) + '</td><td class="num ' + (d < 0 ? "down" : d > 0 ? "up" : "") + '">' +
        (d === 0 ? "–" : signedMoney(d)) + "</td></tr>";
    }).join("") + '<tr class="strong"><td>Total</td><td></td><td></td><td></td><td class="num">' + whole.format(totalUnits) +
      '</td><td class="num">' + whole.format(totalUnits) + '</td><td class="num">' + money.format(allowedBefore) +
      '</td><td class="num">' + money.format(allowedAfter) + '</td><td class="num">' + signedMoney(dAllowed) + "</td></tr>";
  }

  fillMarkets();
  $("market").addEventListener("change", function () {
    history.replaceState(null, "", "?market=" + encodeURIComponent($("market").value));
    chooseMarket();
  });
  $("target").addEventListener("change", render);
  $("shift").addEventListener("input", render);
  chooseMarket();
})();
