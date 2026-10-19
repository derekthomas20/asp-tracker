/* Biosimilar Net Cost Recovery calculator (tools/ncr-calculator/).
   Copied into public/ by scripts/build_site.py. The math ("core math" below)
   is ported unchanged from reference/ncr-calculator.html.

   Payment limits and billing units come from the CMS file, written into the
   page as JSON by build_site.py. 340B purchasing is layered on top through
   costed(), which hands the unchanged math a blended price per package. Visitors' prices, overrides and settings are
   saved only in their own browser (localStorage) and are never sent anywhere. */
(function () {
"use strict";
const DATA = JSON.parse(document.getElementById("ncr-data").textContent);
const KEY = "rxcurve-ncr-v2";
const $ = id => document.getElementById(id);

/* ---------- saved entries (this browser only) ---------- */
let saved = {};
try { saved = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { saved = {}; }
if (typeof saved !== "object" || Array.isArray(saved)) saved = {};
const defaultSettings = () => ({medShare: 60, commPct: 125, seq: true, wasteBill: true, mdvReuse: false, erosion: 0, annual: 200,
  is340: false, share340: 50});

// Runtime markets: the CMS data plus whatever this visitor entered.
// p.limit / k.price / p.rebate are strings; "" means not entered.
const markets = {};
DATA.markets.forEach(d => {
  const s = (saved.markets && saved.markets[d.id]) || {};
  const sp = s.products || {};
  markets[d.id] = {
    // unitMg and k.mg keep the prototype's names but are in the market's
    // measure (mg, mcg or units), so the ported math needs no change.
    id: d.id, name: d.name, unitMg: d.unit_size, unitName: d.unit_name, unitLabel: d.unit_label, doseSource: d.dose_source,
    dose: Object.assign({}, d.dose, s.dose || {}),
    example: !!s.example,
    products: d.products.map(p => {
      const e = sp[p.key] || {}, prices = e.prices || {}, prices340 = e.prices340 || {};
      const override = typeof e.limit === "string" ? e.limit : null;
      return {
        key: p.key, name: p.name, hcpcs: p.hcpcs || "", ref: p.reference, status: p.status,
        cmsLimit: p.cms_limit, cmsDosage: p.cms_dosage, cmsDescription: p.cms_description,
        exampleRebate: p.example_rebate_pct,
        limit: override !== null ? override : (p.cms_limit || ""),
        rebate: typeof e.rebate === "string" ? e.rebate : "",
        pkgs: p.packages.map(k => ({key: k.key, mg: k.amount, type: k.type, examplePrice: k.example_price,
          price: typeof prices[k.key] === "string" ? prices[k.key] : "",
          price340: typeof prices340[k.key] === "string" ? prices340[k.key] : ""}))
      };
    })
  };
});
const state = {
  market: markets[saved.market] ? saved.market : DATA.markets[0].id,
  settings: Object.assign(defaultSettings(), saved.settings || {})
};

const overridden = p => p.limit !== (p.cmsLimit || "");
function save() {
  const out = {market: state.market, settings: state.settings, markets: {}};
  Object.values(markets).forEach(m => {
    const products = {};
    m.products.forEach(p => {
      const e = {}, prices = {}, prices340 = {};
      if (overridden(p)) e.limit = p.limit;
      if (p.rebate !== "") e.rebate = p.rebate;
      p.pkgs.forEach(k => { if (k.price !== "") prices[k.key] = k.price; if (k.price340 !== "") prices340[k.key] = k.price340; });
      if (Object.keys(prices).length) e.prices = prices;
      if (Object.keys(prices340).length) e.prices340 = prices340;
      if (Object.keys(e).length) products[p.key] = e;
    });
    out.markets[m.id] = {dose: m.dose, example: m.example, products};
  });
  try { localStorage.setItem(KEY, JSON.stringify(out)); } catch (e) {}
}

const M = () => markets[state.market];
const n = v => { const x = parseFloat(v); return isFinite(x) ? x : 0; };
const has = v => v !== "" && isFinite(parseFloat(v));
const hasLimit = p => has(p.limit);
// 340B: the share of doses bought at the 340B price (0 when 340B is off).
const share340 = () => state.settings.is340 ? Math.min(100, Math.max(0, n(state.settings.share340))) / 100 : 0;
const needOther = () => share340() < 1, need340 = () => share340() > 0;
const priced = k => (!needOther() || has(k.price)) && (!need340() || has(k.price340));
// The product as the ported math sees it. With 340B off it is the product
// itself. With 340B on, each package's price becomes its cost per package
// (rebate already applied, so rebate is 0): the 340B price, the other
// price, or the mix of the two by the 340B share.
function costed(p, channel) {
  if (!state.settings.is340) return p;
  const sh = channel === "other" ? 0 : channel === "340b" ? 1 : share340(), rf = 1 - n(p.rebate) / 100;
  return Object.assign({}, p, {rebate: 0, pkgs: p.pkgs.map(k => Object.assign({}, k,
    {price: (1 - sh) * n(k.price) * rf + sh * n(k.price340)}))});
}
const ready = p => hasLimit(p) && p.pkgs.length > 0 && p.pkgs.every(priced);
const money = (v, d = 2) => (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const signed = (v, d = 0) => (v > 0 ? "+" : "") + money(v, d);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;"}[c]));
const units = (m, k) => n(k.mg) / m.unitMg;
const amt = (m, v) => n(v).toLocaleString("en-US", {maximumFractionDigits: 3}) + " " + m.unitName;
const vialName = k => k.type === "MDV" ? "Multi-dose" : "Single-dose";
const enterPrice = '<span class="ncr-enter">Enter price</span>';
const enterLimit = '<span class="ncr-enter">Enter limit</span>';

/* ---------- core math (unchanged from the prototype) ---------- */
function blend(){const s=state.settings,ms=Math.min(100,Math.max(0,n(s.medShare)))/100;
  return ms*(s.seq?1-0.8*0.02:1)+(1-ms)*n(s.commPct)/100;}
function payPerUnit(p){return n(p.limit)*(1+n(state.settings.erosion)/100)*blend();}
function netPerUnit(p,k,m){const u=units(m,k);return u>0? n(k.price)*(1-n(p.rebate)/100)/u : 0;}
function doseMg(m){const d=m.dose;return d.basis==='mgkg'?n(d.value)*n(d.weight):d.basis==='mgm2'?n(d.value)*n(d.bsa):n(d.value);}
function doseUnits(m){const mg=doseMg(m);return mg>0?Math.ceil(mg/m.unitMg-1e-9):0;}

function bestCombo(p,m,D){
  const s=state.settings, pk=p.pkgs.filter(k=>units(m,k)>0);
  if(!pk.length||D<=0) return null;
  const maxC=pk.map(k=>Math.min(60,Math.ceil(D/units(m,k))));
  let best=null, counts=new Array(pk.length).fill(0), guard=0;
  const evalC=()=>{
    let sdvU=0,mdvU=0,sdvCost=0,mdvCost=0,mdvNet=0,vials=0;
    pk.forEach((k,i)=>{const c=counts[i];if(!c)return;vials+=c;const u=units(m,k)*c,cost=n(k.price)*(1-n(p.rebate)/100)*c;
      if(k.type==='MDV'){mdvU+=u;mdvCost+=cost;}else{sdvU+=u;sdvCost+=cost;}});
    if(sdvU+mdvU<D-1e-9) return;
    let sdvUsed,mdvUsed;
    if(s.mdvReuse){sdvUsed=Math.min(sdvU,D);mdvUsed=D-sdvUsed;}else{mdvUsed=Math.min(mdvU,D);sdvUsed=D-mdvUsed;}
    const sdvWaste=sdvU-sdvUsed, mdvLeft=mdvU-mdvUsed;
    const discarded=sdvWaste+(s.mdvReuse?0:mdvLeft);
    const mdvAvg=mdvU>0?mdvCost/mdvU:0;
    const cost=sdvCost+(s.mdvReuse?mdvUsed*mdvAvg:mdvCost);
    const billed=D+(s.wasteBill?sdvWaste:0);
    const pay=billed*payPerUnit(p);
    const r={counts:counts.slice(),discarded,cost,billed,pay,ncr:pay-cost,vials,saved:s.mdvReuse?mdvLeft:0};
    if(!best||r.discarded<best.discarded-1e-9||(Math.abs(r.discarded-best.discarded)<1e-9&&(r.cost<best.cost-1e-9||(Math.abs(r.cost-best.cost)<1e-9&&r.vials<best.vials)))) best=r;
  };
  (function rec(i){if(guard>250000)return;if(i===pk.length){guard++;evalC();return;}for(let c=0;c<=maxC[i];c++){counts[i]=c;rec(i+1);}})(0);
  if(best) best.desc=pk.map((k,i)=>best.counts[i]?best.counts[i]+' × '+amt(m,k.mg)+(k.type==='MDV'?' MDV':''):'').filter(Boolean).join(' + ');
  return best;
}

/* ---------- rendering ---------- */
function renderControls() {
  const sel = $("market");
  sel.innerHTML = Object.values(markets).map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("");
  sel.value = state.market;
  const m = M(), s = state.settings;
  $("doseBasis").value = m.dose.basis; $("doseVal").value = m.dose.value; $("weight").value = m.dose.weight; $("bsa").value = m.dose.bsa;
  $("medShare").value = s.medShare; $("commPct").value = s.commPct; $("seq").checked = s.seq; $("erosion").value = s.erosion;
  $("wasteBill").checked = s.wasteBill; $("mdvReuse").checked = s.mdvReuse; $("annual").value = s.annual;
  $("is340").checked = s.is340; $("share340").value = s.share340;
  $("unitLine").innerHTML = `HCPCS billing unit: <b>${esc(m.unitLabel)}</b> (CMS ${esc(DATA.quarter)})`;
  $("doseSource").textContent = "Default dose: " + m.doseSource + ". Change it to match your patients.";
  doseVis();
}
function doseVis() {
  const b = M().dose.basis;
  $("wtL").hidden = b !== "mgkg"; $("bsaL").hidden = b !== "mgm2";
  const u = M().unitName;
  $("doseValL").firstElementChild.textContent = b === "mgkg" ? `Dose (${u}/kg)` : b === "mgm2" ? `Dose (${u}/m²)` : `Dose (${u})`;
}

function renderPricing() {
  const m = M();
  $("priceBody").innerHTML = m.products.map(p => {
    const head = `<tr class="ncr-prod"><td colspan="9"><div class="ncr-prod-bar">
      <span class="ncr-name">${esc(p.name)}</span>
      <span class="tag${p.ref ? " ncr-ref" : ""}">${p.ref ? "Reference" : "Biosimilar"}</span>
      <span class="ncr-code">${p.hcpcs ? esc(p.hcpcs) : "No HCPCS code"}</span>
      <label>Payment limit / unit $<input class="ncr-lim" type="number" step="0.001" min="0" inputmode="decimal" data-p="${p.key}" data-f="limit" value="${esc(p.limit)}"></label>
      <label>Rebate %<input class="ncr-reb" type="number" step="0.1" min="0" max="100" inputmode="decimal" data-p="${p.key}" data-f="rebate" value="${esc(p.rebate)}"></label>
      <span class="ncr-status" id="st-${p.key}"></span>
    </div></td></tr>`;
    const rows = p.pkgs.map(k => `<tr id="row-${p.key}-${k.key}">
      <td>${esc(amt(m, k.mg))}</td>
      <td class="num" data-o="units"></td>
      <td>${vialName(k)}</td>
      <td class="num"><input class="ncr-price" type="number" step="0.01" min="0" inputmode="decimal" data-p="${p.key}" data-k="${k.key}" data-f="price" value="${esc(k.price)}" placeholder="Enter price" aria-label="${esc(p.name)} ${esc(amt(m, k.mg))} price per package"></td>
      <td class="num c340"><input class="ncr-price" type="number" step="0.01" min="0" inputmode="decimal" data-p="${p.key}" data-k="${k.key}" data-f="price340" value="${esc(k.price340)}" placeholder="Enter 340B price" aria-label="${esc(p.name)} ${esc(amt(m, k.mg))} 340B price per package"></td>
      <td class="num" data-o="net"></td><td class="num" data-o="pay"></td><td class="num" data-o="ncr"></td><td class="num" data-o="mg%"></td></tr>`).join("");
    return head + rows;
  }).join("");
}

// The label beside each payment limit: where it came from, and a reset
// button when the visitor has changed it.
function renderStatus(p) {
  const el = $("st-" + p.key);
  if (!el) return;
  const q = esc(DATA.quarter);
  let html;
  if (p.status === "cms" && !overridden(p)) html = `<span class="ncr-chip">CMS ${q}</span>`;
  else if (p.status === "cms") html = `<span class="ncr-chip warn">Changed by you</span> CMS ${q}: $${esc(p.cmsLimit)} <button type="button" class="ncr-link" data-act="reset" data-p="${p.key}">Reset</button>`;
  else {
    const why = p.status === "missing" ? "Not in the current CMS file" : p.status === "no_limit" ? "No payment limit in the CMS file" : "No HCPCS code";
    html = `<span class="ncr-chip warn">${why}</span>` + (hasLimit(p)
      ? ` Typed by you <button type="button" class="ncr-link" data-act="reset" data-p="${p.key}">Clear</button>`
      : " Type a limit to include it");
  }
  el.innerHTML = html;
  const input = el.parentNode.querySelector('input[data-f="limit"]');
  if (input) input.classList.toggle("ncr-changed", p.status === "cms" ? overridden(p) : hasLimit(p));
}

// Packages bought in a year for one product: vials per dose × doses per
// year. With multi-dose reuse, multi-dose vials are counted by the drug
// actually used, so those counts are approximate.
function perYear(p, c, m, annual) {
  const pk = p.pkgs.filter(k => units(m, k) > 0);
  let mdvU = 0;
  pk.forEach((k, i) => { if (k.type === "MDV") mdvU += units(m, k) * c.counts[i]; });
  const used = state.settings.mdvReuse && mdvU > 0 ? (mdvU - c.saved) / mdvU : 1;
  return pk.map((k, i) => {
    if (!c.counts[i]) return "";
    const mdv = k.type === "MDV", count = Math.ceil(c.counts[i] * annual * (mdv ? used : 1) - 1e-9);
    return (mdv && used < 1 ? "≈" : "") + count.toLocaleString("en-US") + " × " + amt(m, k.mg) + (mdv ? " MDV" : "");
  }).filter(Boolean).join(" + ");
}

function update() {
  const m = M(), s = state.settings;
  $("erosionOut").textContent = (s.erosion > 0 ? "+" : "") + s.erosion + "%";
  const mg = doseMg(m), D = doseUnits(m);
  $("doseLine").innerHTML = `<b>${esc(amt(m, mg))}</b> = <b>${D}</b> billing units`;
  $("exampleNote").hidden = !m.example;
  $("exampleNote").textContent = `Example prices are loaded for ${m.name}. They are made up for illustration and are not real contracts.`;
  const on340 = s.is340, sh = share340(), mixed = on340 && sh > 0 && sh < 1;
  $("share340L").hidden = $("note340").hidden = !on340;
  $("priceTable").classList.toggle("no340", !on340);
  $("priceHead").textContent = on340 ? "Other patients $ / pkg" : "Contract $ / pkg";

  // per package outputs
  let bestUnit = null;
  m.products.forEach(p => {
    renderStatus(p);
    const pay = payPerUnit(p), cp = costed(p);
    p.pkgs.forEach((k, i) => {
      const row = $("row-" + p.key + "-" + k.key); if (!row) return;
      const u = units(m, k), net = netPerUnit(cp, cp.pkgs[i], m), ncr = pay - net;
      const o = f => row.querySelector(`[data-o="${f}"]`);
      o("units").textContent = u ? (+u.toFixed(2)).toLocaleString("en-US") : "—";
      o("pay").innerHTML = hasLimit(p) ? money(pay) : enterLimit;
      if (!priced(k)) { o("net").innerHTML = enterPrice; o("ncr").innerHTML = enterPrice; o("mg%").innerHTML = enterPrice; return; }
      o("net").textContent = u ? money(net) : "—";
      o("ncr").innerHTML = !hasLimit(p) ? enterLimit : u ? `<span class="${ncr < 0 ? "ncr-neg" : "ncr-pos"}">${money(ncr)}</span>` : "—";
      o("mg%").innerHTML = !hasLimit(p) ? enterLimit : u && net > 0 ? ((ncr / net) * 100).toFixed(1) + "%" : "—";
      if (hasLimit(p) && u && (!bestUnit || ncr > bestUnit.ncr)) bestUnit = {p, k, ncr};
    });
  });
  document.querySelectorAll("#priceBody tr").forEach(r => r.classList.remove("ncr-best"));
  if (bestUnit) { const r = $("row-" + bestUnit.p.key + "-" + bestUnit.k.key); if (r) r.classList.add("ncr-best"); }

  // per dose: only products with a limit and a price for every package
  const results = channel => m.products.filter(ready).map(p => ({p, c: bestCombo(costed(p, channel), m, D)}))
    .filter(r => r.c).sort((a, b) => b.c.ncr - a.c.ncr);
  const res = results("mix");
  const waiting = m.products.filter(p => !ready(p));
  const lead = res[0], second = res[1], annual = n(s.annual);
  $("doseBody").innerHTML = res.map((r, i) => {
    const c = r.c, gap = (c.ncr - lead.c.ncr) * annual;
    return `<tr class="${i === 0 ? "ncr-best" : ""}"><td><b>${esc(r.p.name)}</b> <span class="muted">${esc(r.p.hcpcs)}</span></td>
      <td>${esc(c.desc)}${c.saved ? ` <span class="ncr-chip warn">${+c.saved.toFixed(2)} u saved</span>` : ""}</td>
      <td class="num">${D}</td><td class="num">${+c.discarded.toFixed(2)}</td><td class="num">${+c.billed.toFixed(2)}</td>
      <td class="num">${money(c.cost)}</td><td class="num">${money(c.pay)}</td>
      <td class="num"><span class="${c.ncr < 0 ? "ncr-neg" : "ncr-pos"}">${money(c.ncr)}</span></td>
      <td class="num">${money(c.ncr * annual, 0)}</td>
      <td class="num">${i === 0 ? '<span class="ncr-chip">Leader</span>' : `<span class="ncr-neg">${money(gap, 0)}</span>`}</td></tr>`;
  }).join("") + waiting.map(p => `<tr><td><b>${esc(p.name)}</b> <span class="muted">${esc(p.hcpcs)}</span></td>
      <td colspan="9">${hasLimit(p) ? enterPrice : enterLimit}</td></tr>`).join("")
    || `<tr><td colspan="10" class="muted">Enter a dose and at least one package with a strength to see results.</td></tr>`;
  if (!D) $("doseBody").innerHTML = `<tr><td colspan="10" class="muted">Enter a dose to see results.</td></tr>`;

  // verdict
  $("verdictSub").textContent = D ? `${m.name}, ${D} billing units per dose, ${annual.toLocaleString("en-US")} doses a year`
    + (on340 ? `, ${Math.round(sh * 100)}% bought at 340B.` : ".") : "";
  let summary = "";
  if (lead && lead.c.ncr < 0) {
    summary = `At these prices no product is paid enough to cover its cost. ${lead.p.name} loses the least: ${money(-lead.c.ncr)} per dose.`;
  } else if (lead) {
    const gap = second ? lead.c.ncr - second.c.ncr : 0;
    summary = `At these prices, ${lead.p.name} recovers the most: ${money(lead.c.ncr)} per dose`
      + (second ? `, ${money(gap)} more than ${second.p.name}` : "")
      + `. Over ${annual.toLocaleString("en-US")} doses a year that is ${money(lead.c.ncr * annual, 0)}`
      + (second ? `, or ${money(gap * annual, 0)} more` : "") + ".";
  }
  if (lead && mixed) {
    const l340 = results("340b")[0], lOther = results("other")[0];
    if (l340 && lOther) summary += l340.p === lOther.p
      ? ` ${l340.p.name} leads for both 340B doses and other doses.`
      : ` It differs by channel: ${l340.p.name} leads for 340B doses (${money(l340.c.ncr)} per dose) and ${lOther.p.name} for other doses (${money(lOther.c.ncr)} per dose).`;
  }
  $("summary").textContent = summary;
  $("summary").hidden = !summary;
  if (lead) {
    const neg = res.filter(r => r.c.ncr < 0).length;
    $("leadCard").innerHTML = `<div class="ncr-eyebrow">Best recovery per dose</div>
      <div class="ncr-lead-name">${esc(lead.p.name)} <span class="muted">${esc(lead.p.hcpcs)}</span></div>
      <div class="ncr-lead-val">${money(lead.c.ncr)}<small>per dose</small></div>
      <div class="ncr-stats">
        <div><div class="k">Ahead of ${second ? esc(second.p.name) : "next"}</div><div class="v">${second ? signed(lead.c.ncr - second.c.ncr, 2) + " / dose" : "—"}</div></div>
        <div><div class="k">Annual difference</div><div class="v">${second ? signed((lead.c.ncr - second.c.ncr) * annual) : "—"}</div></div>
        <div><div class="k">Vials used</div><div class="v">${esc(lead.c.desc)}</div></div>
        <div><div class="k">Underwater products</div><div class="v">${neg ? `<span class="ncr-chip neg">${neg} below cost</span>` : "None"}</div></div>
      </div>` + (waiting.length ? `<p class="ncr-calc">${waiting.length} product${waiting.length > 1 ? "s" : ""} not compared yet: enter ${waiting.some(p => !hasLimit(p)) ? "a limit or " : ""}${on340 ? "prices" : "contract prices"}.</p>` : "");
    const vals = res.map(r => r.c.ncr), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = (hi - lo) || 1, z = (0 - lo) / span * 100;
    $("bars").innerHTML = `<div class="ncr-eyebrow">NCR per dose by product</div>` + res.map((r, i) => {
      const v = r.c.ncr, a = (Math.min(v, 0) - lo) / span * 100, w = Math.abs(v) / span * 100;
      return `<div class="ncr-bar"><div class="nm" title="${esc(r.p.name)}">${esc(r.p.name)}<span>${r.p.ref ? "ref" : ""}</span></div>
        <div class="ncr-track"><div class="ncr-fill ${v < 0 ? "neg" : i === 0 ? "lead" : ""}" style="left:${a}%;width:${Math.max(w, .5)}%"></div>${lo < 0 ? `<div class="ncr-zero" style="left:${z}%"></div>` : ""}</div>
        <div class="val ${v < 0 ? "ncr-neg" : ""}">${money(v)}</div></div>`;
    }).join("");
  } else {
    $("leadCard").innerHTML = `<div class="ncr-eyebrow">Best recovery per dose</div>
      <p>Enter ${on340 ? "prices" : "contract prices"} for at least one product${on340 ? "" : ", or click <b>Load example prices</b>,"} to compare products.</p>`;
    $("bars").innerHTML = "";
  }

  // a year of purchases: packages by strength, spend, payment and NCR
  $("buySub").textContent = "Packages needed for the doses given per year, by strength, if every dose used that product."
    + (on340 ? ` About ${Math.round(sh * 100)}% of them are bought at the 340B price.` : "");
  $("buyBody").innerHTML = res.map((r, i) => `<tr class="${i === 0 ? "ncr-best" : ""}"><td><b>${esc(r.p.name)}</b> <span class="muted">${esc(r.p.hcpcs)}</span></td>
      <td>${esc(perYear(r.p, r.c, m, annual)) || "—"}</td>
      <td class="num">${money(r.c.cost * annual, 0)}</td><td class="num">${money(r.c.pay * annual, 0)}</td>
      <td class="num"><span class="${r.c.ncr < 0 ? "ncr-neg" : "ncr-pos"}">${money(r.c.ncr * annual, 0)}</span></td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">Enter prices and doses given per year to see a year of purchases.</td></tr>`;

  // breakeven: the price each package needs to match the best NCR per unit.
  // It solves for the price you negotiate: the 340B price when every dose
  // is bought at 340B, otherwise the other-patients (contract) price.
  const solve340 = on340 && sh >= 1;
  $("beCurHead").textContent = solve340 ? "Current 340B $ / pkg" : on340 ? "Current other $ / pkg" : "Current $ / pkg";
  $("beSub").textContent = `The ${solve340 ? "340B price" : on340 ? "price for other patients" : "contract price"} each package would need for its NCR per unit to match the leader’s best package. Use it as a negotiation target.`;
  if (bestUnit) {
    const target = bestUnit.ncr;
    $("beBody").innerHTML = m.products.flatMap(p => {
      const pay = payPerUnit(p), rf = 1 - n(p.rebate) / 100;
      return p.pkgs.filter(k => units(m, k) > 0).map(k => {
        const u = units(m, k), isLead = k === bestUnit.k;
        // With 340B off (sh = 0) this is the prototype's (pay − target) × units ÷ (1 − rebate).
        const be = solve340 ? (pay - target) * u : rf > 0 ? ((pay - target) * u - sh * n(k.price340)) / ((1 - sh) * rf) : 0;
        const cur = solve340 ? n(k.price340) : n(k.price), diff = be - cur;
        // a 340B mix needs the 340B price to solve for the other price
        if (hasLimit(p) && !solve340 && need340() && !has(k.price340)) return `<tr><td><b>${esc(p.name)}</b></td><td>${esc(amt(m, k.mg))} ${k.type === "MDV" ? "multi-dose" : "single-dose"}</td><td class="num">${solve340 || has(k.price) ? money(cur) : enterPrice}</td><td class="num">${enterPrice}</td><td class="num">—</td><td></td></tr>`;
        const pkg = `${esc(amt(m, k.mg))} ${k.type === "MDV" ? "multi-dose" : "single-dose"}`;
        if (!hasLimit(p)) return `<tr><td><b>${esc(p.name)}</b></td><td>${pkg}</td><td class="num">${priced(k) ? money(cur) : enterPrice}</td><td class="num">${enterLimit}</td><td class="num">—</td><td></td></tr>`;
        return `<tr class="${isLead ? "ncr-best" : ""}"><td><b>${esc(p.name)}</b></td><td>${pkg}</td>
          <td class="num">${priced(k) ? money(cur) : enterPrice}</td><td class="num">${be <= 0 ? '<span class="ncr-neg">Not reachable</span>' : money(be)}</td>
          <td class="num">${isLead ? '<span class="ncr-chip">Sets the target</span>' : be <= 0 || !priced(k) ? "—" : `<span class="${diff < 0 ? "ncr-neg" : "ncr-pos"}">${signed(diff, 2)} (${cur ? ((diff / cur) * 100).toFixed(1) : "0"}%)</span>`}</td>
          <td>${isLead || !priced(k) ? "" : diff >= 0 ? '<span class="ncr-chip">Already ahead</span>' : ""}</td></tr>`;
      });
    }).join("");
  } else $("beBody").innerHTML = `<tr><td colspan="6" class="muted">Enter prices to see breakeven targets.</td></tr>`;
  save();
}

function toast(id, text, ms) { const t = $(id); t.textContent = text; clearTimeout(t._t); t._t = setTimeout(() => { t.textContent = ""; }, ms || 3000); }

/* ---------- events ---------- */
const findP = key => M().products.find(p => p.key === key);
$("priceBody").addEventListener("input", e => {
  const t = e.target, p = findP(t.dataset.p); if (!p || !t.dataset.f) return;
  if (t.dataset.k) { const k = p.pkgs.find(x => x.key === t.dataset.k); if (k) k[t.dataset.f] = t.value; }
  else p[t.dataset.f] = t.value;
  update();
});
$("priceBody").addEventListener("click", e => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const p = findP(b.dataset.p); if (!p) return;
  if (b.dataset.act === "reset") {
    p.limit = p.cmsLimit || "";
    const input = b.closest(".ncr-prod-bar").querySelector('input[data-f="limit"]');
    if (input) { input.value = p.limit; input.focus(); }
  }
  update();
});

$("loadExamples").addEventListener("click", () => {
  const m = M();
  m.products.forEach(p => { p.rebate = p.exampleRebate ? String(p.exampleRebate) : ""; p.pkgs.forEach(k => { k.price = k.examplePrice == null ? "" : String(k.examplePrice); }); });
  m.example = true;
  renderPricing(); update();
  toast("toast", "Example prices loaded for " + m.name + "." + (state.settings.is340 ? " There are no example 340B prices." : ""), 4000);
});
$("clearPrices").addEventListener("click", () => {
  const m = M();
  m.products.forEach(p => { p.rebate = ""; p.pkgs.forEach(k => { k.price = ""; k.price340 = ""; }); });
  m.example = false;
  renderPricing(); update();
  toast("toast", "Prices and rebates cleared for " + m.name + ".");
});

$("market").addEventListener("change", e => { state.market = e.target.value; renderControls(); renderPricing(); update(); });
$("doseBasis").addEventListener("change", e => { M().dose.basis = e.target.value; doseVis(); update(); });
[["doseVal", "value"], ["weight", "weight"], ["bsa", "bsa"]].forEach(([id, f]) => $(id).addEventListener("input", e => { M().dose[f] = n(e.target.value); update(); }));
["medShare", "commPct", "erosion", "annual", "share340"].forEach(id => $(id).addEventListener("input", e => { state.settings[id] = n(e.target.value); update(); }));
["seq", "wasteBill", "mdvReuse", "is340"].forEach(id => $(id).addEventListener("change", e => { state.settings[id] = e.target.checked; update(); }));

$("copyBtn").addEventListener("click", () => {
  const rows = [["Product", "HCPCS", "Vials used", "Administered", "Discarded", "Billed units", "Acquisition", "Payment", "NCR/dose", "NCR/year", "Packages/year"]];
  const m = M(), D = doseUnits(m), a = n(state.settings.annual);
  m.products.filter(ready).map(p => ({p, c: bestCombo(costed(p), m, D)})).filter(r => r.c).sort((x, y) => y.c.ncr - x.c.ncr)
    .forEach(({p, c}) => rows.push([p.name, p.hcpcs, c.desc, D, +c.discarded.toFixed(2), +c.billed.toFixed(2), c.cost.toFixed(2), c.pay.toFixed(2), c.ncr.toFixed(2), (c.ncr * a).toFixed(0), perYear(p, c, m, a)]));
  if (state.settings.is340) rows.push([], ["340B share of doses", Math.round(share340() * 100) + "%"]);
  const txt = rows.map(r => r.join("\t")).join("\n");
  const done = () => toast("copyToast", "Copied. Paste into Excel or Sheets.", 3500);
  const fallback = () => {
    const ta = document.createElement("textarea"); ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    if (ok) done(); else toast("copyToast", "Copy is blocked here. Select the table and copy it instead.", 3500);
  };
  try { navigator.clipboard.writeText(txt).then(done, fallback); } catch (e) { fallback(); }
});

renderControls(); renderPricing(); update();
})();
