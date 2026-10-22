/* Biosimilar Net Cost Recovery calculator (tools/ncr-calculator/).
   Copied into public/ by scripts/build_site.py. The math ("core math" below)
   is ported unchanged from reference/ncr-calculator.html.

   Payment limits and billing units come from the CMS file, written into the
   page as JSON by build_site.py. 340B purchasing is layered on top through
   costed(), which hands the unchanged math a blended price per package. Visitors' prices, overrides and settings are
   saved only in their own browser (localStorage) and are never sent anywhere.

   Besides the biosimilar markets, visitors can build comparisons of their
   own from any product in the CMS crosswalk (window.NCR_CATALOG, written to
   products.js by build_site.py). Products in such a comparison can have
   different billing units, so each one carries its own unit and dose; pm()
   hands the unchanged math a one-product "market" with them. */
(function () {
"use strict";
const DATA = JSON.parse(document.getElementById("ncr-data").textContent);
const CATALOG = window.NCR_CATALOG || [];
const CAT = {};
CATALOG.forEach(c => { CAT[c.key] = c; });
const KEY = "rxcurve-ncr-v2";
const $ = id => document.getElementById(id);
const n = v => { const x = parseFloat(v); return isFinite(x) ? x : 0; };
const has = v => v !== "" && v != null && isFinite(parseFloat(v));

/* ---------- saved entries (this browser only) ---------- */
let saved = {};
try { saved = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { saved = {}; }
if (typeof saved !== "object" || Array.isArray(saved)) saved = {};
const defaultSettings = () => ({medShare: 60, commPct: 125, seq: true, wasteBill: true, mdvReuse: false, erosion: 0, annual: 200,
  is340: false, share340: 50, volBasis: "doses", annualUnits: null, annualPacks: null});
// A new comparison of your own starts with the first market's patient weight and BSA.
const defaultPatient = () => ({weight: DATA.markets[0].dose.weight, bsa: DATA.markets[0].dose.bsa});

// One runtime product from market or catalog data (d) plus what this visitor
// saved for it (e). p.limit / k.price / p.rebate are strings; "" means not entered.
function makeProduct(d, e) {
  e = e || {};
  const prices = e.prices || {}, prices340 = e.prices340 || {}, types = e.types || {};
  const override = typeof e.limit === "string" ? e.limit : null;
  return {
    key: d.key, name: d.name, hcpcs: d.hcpcs || "", ref: d.reference, status: d.status,
    cmsLimit: d.cms_limit, cmsDosage: d.cms_dosage,
    exampleRebate: d.example_rebate_pct,
    limit: override !== null ? override : (d.cms_limit || ""),
    rebate: typeof e.rebate === "string" ? e.rebate : "",
    include: e.off !== true,
    pkgs: d.packages.map(k => ({key: k.key, mg: k.amount, units: k.units,
      type: ["SDV", "MDV", "OFF"].includes(types[k.key]) ? types[k.key] : k.type, examplePrice: k.example_price,
      price: typeof prices[k.key] === "string" ? prices[k.key] : "",
      price340: typeof prices340[k.key] === "string" ? prices340[k.key] : ""}))
  };
}
// A product in a comparison of your own: it also has its own billing unit and dose.
function customProduct(c, e) {
  e = e || {};
  const p = makeProduct(c, e), d = Object.assign({basis: "flat", value: 0}, c.dose || {}, e.dose || {});
  return Object.assign(p, {role: c.role, description: c.description, unitMg: c.unit_size, unitName: c.unit_name,
    unitLabel: c.unit_label, flatOnly: c.unit_name === "billing units",
    dose: {basis: c.unit_name === "billing units" ? "flat" : d.basis, value: n(d.value)},
    // this product's own yearly volume; "" means use the shared figure
    annual: typeof e.annual === "string" ? e.annual : "", annualUnits: typeof e.annualUnits === "string" ? e.annualUnits : "",
    annualPacks: typeof e.annualPacks === "string" ? e.annualPacks : ""});
}

// Runtime markets: the CMS data plus whatever this visitor entered.
const markets = {};
DATA.markets.forEach(d => {
  const s = (saved.markets && saved.markets[d.id]) || {};
  const sp = s.products || {};
  markets[d.id] = {
    // unitMg and k.mg keep the prototype's names but are in the market's
    // measure (mg, mcg or units), so the ported math needs no change.
    id: d.id, name: d.name, unitMg: d.unit_size, unitName: d.unit_name, unitLabel: d.unit_label,
    dose: Object.assign({}, d.dose, s.dose || {}),
    example: !!s.example, packsOf: typeof s.packsOf === "string" ? s.packsOf : "",
    products: d.products.map(p => makeProduct(p, sp[p.key]))
  };
});
// Comparisons of your own. A saved product no longer in the CMS crosswalk is
// kept as saved (gone) and named on the page instead of being dropped.
function makeCustom(id, s) {
  const list = Array.isArray(s.products) ? s.products.filter(e => e && typeof e.key === "string") : [];
  markets[id] = {
    id, custom: true, name: typeof s.name === "string" && s.name.trim() ? s.name : "My comparison",
    dose: Object.assign({basis: "flat", value: 0}, defaultPatient(),
      has(s.weight) ? {weight: n(s.weight)} : {}, has(s.bsa) ? {bsa: n(s.bsa)} : {}),
    example: !!s.example, packsOf: typeof s.packsOf === "string" ? s.packsOf : "",
    products: list.filter(e => CAT[e.key]).map(e => customProduct(CAT[e.key], e)),
    gone: list.filter(e => !CAT[e.key])
  };
  return markets[id];
}
if (saved.customs && typeof saved.customs === "object") Object.keys(saved.customs).forEach(id => {
  if (/^c[a-z0-9]+$/.test(id) && saved.customs[id] && typeof saved.customs[id] === "object") makeCustom(id, saved.customs[id]);
});
const state = {
  market: markets[saved.market] ? saved.market : DATA.markets[0].id,
  settings: Object.assign(defaultSettings(), saved.settings || {})
};
if (!["units", "packs"].includes(state.settings.volBasis)) state.settings.volBasis = "doses";

const overridden = p => p.limit !== (p.cmsLimit || "");
function savedProduct(p, custom) {
  const e = {}, prices = {}, prices340 = {};
  if (overridden(p)) e.limit = p.limit;
  if (p.rebate !== "") e.rebate = p.rebate;
  if (!p.include) e.off = true;
  p.pkgs.forEach(k => { if (k.price !== "") prices[k.key] = k.price; if (k.price340 !== "") prices340[k.key] = k.price340; });
  if (Object.keys(prices).length) e.prices = prices;
  if (Object.keys(prices340).length) e.prices340 = prices340;
  if (custom) {
    e.key = p.key; e.name = p.name; e.dose = p.dose;
    if (p.annual !== "") e.annual = p.annual;
    if (p.annualUnits !== "") e.annualUnits = p.annualUnits;
    if (p.annualPacks !== "") e.annualPacks = p.annualPacks;
    e.types = {}; p.pkgs.forEach(k => { e.types[k.key] = k.type; });
  }
  return e;
}
function save() {
  const out = {market: state.market, settings: state.settings, markets: {}, customs: {}};
  Object.values(markets).forEach(m => {
    if (m.custom) {
      out.customs[m.id] = {name: m.name, weight: m.dose.weight, bsa: m.dose.bsa, example: m.example, packsOf: m.packsOf,
        products: m.products.map(p => savedProduct(p, true)).concat(m.gone)};
      return;
    }
    const products = {};
    m.products.forEach(p => { const e = savedProduct(p, false); if (Object.keys(e).length) products[p.key] = e; });
    out.markets[m.id] = {dose: m.dose, example: m.example, packsOf: m.packsOf, products};
  });
  try { localStorage.setItem(KEY, JSON.stringify(out)); } catch (e) {}
}

const M = () => markets[state.market];
const hasLimit = p => has(p.limit);
// The billing unit and dose a product is measured in: its market's, or in a
// comparison of your own, the product's (with the comparison's weight and BSA).
const pm = (m, p) => !m.custom ? m : {unitMg: p.unitMg, unitName: p.unitName,
  dose: {basis: p.dose.basis, value: p.dose.value, weight: m.dose.weight, bsa: m.dose.bsa}};
const used = k => k.type !== "OFF";
// Yearly volume is entered as doses, billing units administered, or
// packages bought.
const byUnits = () => state.settings.volBasis === "units";
const byPacks = () => state.settings.volBasis === "packs";
const volField = () => byUnits() ? "annualUnits" : byPacks() ? "annualPacks" : "annual";
const VOL = {annual: ["Doses per year", "Doses / year"], annualUnits: ["Billing units per year", "Billing units / year"],
  annualPacks: ["Packages per year", "Packages / year"]};
// Doses a year from one product's billing units or packages a year, using
// its billing units per dose (D) or the packages its dose uses (combo c).
function toDoses(v, p, D, c, q) {
  if (byUnits()) return D > 0 ? v / D : 0;
  if (!byPacks()) return v;
  const per = c ? packsPerDose(p, c, q) : 0;
  return per > 0 ? v / per : 0;
}
// The product whose packages the shared packages-a-year figure counts.
const packsProduct = m => m.products.find(p => p.key === m.packsOf) || m.products[0];
// Doses a year for one product: its own figure in a comparison of your own,
// else the shared one. Shared packages are the packs product's, turned into
// doses with that product's vials per dose, and every product gets those doses.
function yearDoses(m, p, D, c, q) {
  const own = m.custom ? p[volField()] : "";
  if (has(own)) return toDoses(n(own), p, D, c, q);
  const v = n(state.settings[volField()]);
  return byPacks() ? sharedPackDoses(m) : toDoses(v, p, D, c, q);
}
function sharedPackDoses(m) {
  if (packDoses === null) {
    const r = packsProduct(m), rq = r && pm(m, r), rD = r ? doseUnits(rq) : 0;
    packDoses = r ? toDoses(n(state.settings.annualPacks), r, rD, bestCombo(costed(r), rq, rD), rq) : 0;
  }
  return packDoses;
}
let packDoses = null;  // the shared packages a year as doses; reset by update()
const qty = v => v.toLocaleString("en-US", {maximumFractionDigits: 1});
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
const ready = p => p.include && hasLimit(p) && p.pkgs.some(used) && p.pkgs.every(k => !used(k) || priced(k));
const money = (v, d = 2) => (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const signed = (v, d = 0) => (v > 0 ? "+" : "") + money(v, d);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;"}[c]));
// A package marked "Not used" holds no billing units, so the math never picks it.
const units = (m, k) => used(k) ? n(k.mg) / m.unitMg : 0;
const amt = (m, v) => n(v).toLocaleString("en-US", {maximumFractionDigits: 3}) + " " + m.unitName;
const vialName = k => k.type === "MDV" ? "Multi-dose" : k.type === "OFF" ? "Not used" : "Single-dose";
const enterDose = '<span class="ncr-enter">Enter dose</span>';
const BASIS = {mgkg: "per kg", mgm2: "per m²", flat: "flat"};
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
  const sel = $("market"), own = Object.values(markets).filter(m => m.custom);
  sel.innerHTML = `<optgroup label="Biosimilar markets">${Object.values(markets).filter(m => !m.custom).map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("")}</optgroup>`
    + (own.length ? `<optgroup label="Your comparisons">${own.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("")}</optgroup>` : "");
  sel.value = state.market;
  const m = M(), s = state.settings;
  $("doseBasis").value = m.dose.basis; $("doseVal").value = m.dose.value; $("weight").value = m.dose.weight; $("bsa").value = m.dose.bsa;
  $("medShare").value = s.medShare; $("commPct").value = s.commPct; $("seq").checked = s.seq; $("erosion").value = s.erosion;
  $("wasteBill").checked = s.wasteBill; $("mdvReuse").checked = s.mdvReuse;
  $("volBasis").value = s.volBasis; $("annual").value = s[volField()] == null ? "" : s[volField()];
  $("annualL").textContent = VOL[volField()][0];
  renderPacksOf();
  $("is340").checked = s.is340; $("share340").value = s.share340;
  $("unitLine").innerHTML = m.custom
    ? `Billing units come from each product’s HCPCS code (CMS ${esc(DATA.quarter)}). Set each product’s dose under <b>Your prices by package</b>.`
    : `Billing unit: <b>${esc(m.unitLabel)}</b> (CMS ${esc(DATA.quarter)})`;
  $("doseBasisL").hidden = $("doseValL").hidden = !!m.custom;
  $("ownTools").hidden = !m.custom;
  $("startOwn").hidden = !!m.custom;
  $("deleteOwn").hidden = !m.custom;
  if (m.custom) {
    $("ownName").value = m.name;
    $("addMarket").innerHTML = `<option value="">Choose a market…</option>` + Object.values(markets).filter(x => !x.custom)
      .map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join("");
    renderFound();
  }
  doseVis();
}
function doseVis() {
  const m = M(), b = m.dose.basis;
  if (m.custom) {
    $("wtL").hidden = !m.products.some(p => p.dose.basis === "mgkg");
    $("bsaL").hidden = !m.products.some(p => p.dose.basis === "mgm2");
    return;
  }
  $("wtL").hidden = b !== "mgkg"; $("bsaL").hidden = b !== "mgm2";
  const u = m.unitName;
  $("doseValL").firstElementChild.textContent = b === "mgkg" ? `Dose (${u}/kg)` : b === "mgm2" ? `Dose (${u}/m²)` : `Dose (${u})`;
}

// With volume in packages: which product's packages the shared figure counts.
function renderPacksOf() {
  const m = M(), r = packsProduct(m);
  $("packsOfL").hidden = !byPacks() || !m.products.length;
  $("packsOf").innerHTML = m.products.map(p => `<option value="${p.key}">${esc(p.name)}</option>`).join("");
  if (r) $("packsOf").value = r.key;
}

// Product search for comparisons of your own: every word typed must appear
// in the product's name, HCPCS code or CMS description.
const FOUND_MAX = 25;
function renderFound() {
  const m = M(), words = $("findProduct").value.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) { $("found").innerHTML = ""; $("foundNote").textContent = ""; return; }
  const hits = CATALOG.filter(c => { const t = (c.name + " " + c.hcpcs + " " + c.description).toLowerCase(); return words.every(w => t.includes(w)); });
  $("foundNote").textContent = !hits.length ? "No product matches. Try a brand name, a generic name or a HCPCS code."
    : hits.length > FOUND_MAX ? `Showing ${FOUND_MAX} of ${hits.length.toLocaleString("en-US")} matches. Type more to narrow them.` : "";
  $("found").innerHTML = hits.slice(0, FOUND_MAX).map(c => {
    const inIt = m.products.some(p => p.key === c.key);
    const limit = c.cms_limit ? `$${esc(c.cms_limit)} per ${esc(c.unit_label)}` : c.status === "missing" ? "Not in the current CMS file" : "No payment limit";
    return `<li><span><b>${esc(c.name)}</b> <span class="ncr-code">${esc(c.hcpcs)}</span>${c.role ? ` <span class="tag${c.reference ? " ncr-ref" : ""}">${c.reference ? "Reference" : "Biosimilar"}</span>` : ""}
      <small>${esc(c.description)} · ${limit}</small></span>
      ${inIt ? '<span class="ncr-chip">Added</span>' : `<button type="button" class="quiet" data-add="${esc(c.key)}">Add</button>`}</li>`;
  }).join("");
}

function renderPricing() {
  const m = M();
  $("priceBody").innerHTML = m.products.map(p => {
    const q = pm(m, p);
    const tag = m.custom && !p.role ? "" : `<span class="tag${p.ref ? " ncr-ref" : ""}">${p.ref ? "Reference" : "Biosimilar"}</span>`;
    const u = esc(q.unitName);
    const dose = !m.custom ? "" : `<span class="ncr-unit">Billing unit: ${esc(p.unitLabel)}</span>
      <label>Dose<select data-p="${p.key}" data-f="doseBasis" aria-label="${esc(p.name)} dose basis">${Object.keys(BASIS).filter(b => b === "flat" || !p.flatOnly)
        .map(b => `<option value="${b}"${p.dose.basis === b ? " selected" : ""}>${BASIS[b]}</option>`).join("")}</select></label>
      <label><input class="ncr-reb" type="number" step="0.1" min="0" inputmode="decimal" data-p="${p.key}" data-f="doseValue" value="${p.dose.value || ""}" placeholder="Dose" aria-label="${esc(p.name)} dose">${p.dose.basis === "mgkg" ? u + "/kg" : p.dose.basis === "mgm2" ? u + "/m²" : u}</label>
      <label>${VOL[volField()][1]}<input class="ncr-reb ncr-yr" type="number" step="1" min="0" inputmode="decimal" data-p="${p.key}" data-f="${volField()}" value="${esc(p[volField()])}"></label>
      <button type="button" class="ncr-link" data-act="remove" data-p="${p.key}">Remove</button>`;
    const head = `<tr class="ncr-prod" id="prod-${p.key}"><td colspan="9"><div class="ncr-prod-bar">
      <label class="ncr-inc"><input type="checkbox" data-p="${p.key}" data-f="include"${p.include ? " checked" : ""} aria-label="Include ${esc(p.name)} in the comparison">Include</label>
      <span class="ncr-name">${esc(p.name)}</span>
      ${tag}
      <span class="ncr-code">${p.hcpcs ? esc(p.hcpcs) : "No HCPCS code"}</span>
      <label>Payment limit / unit $<input class="ncr-lim" type="number" step="0.001" min="0" inputmode="decimal" data-p="${p.key}" data-f="limit" value="${esc(p.limit)}"></label>
      <label>Rebate %<input class="ncr-reb" type="number" step="0.1" min="0" max="100" inputmode="decimal" data-p="${p.key}" data-f="rebate" value="${esc(p.rebate)}"></label>
      <span class="ncr-status" id="st-${p.key}"></span>
      ${dose}
    </div></td></tr>`;
    const vial = k => !m.custom ? vialName(k) : `<select data-p="${p.key}" data-k="${k.key}" data-f="type" aria-label="${esc(p.name)} ${esc(amt(q, k.mg))} package type">`
      + ["SDV", "MDV", "OFF"].map(t => `<option value="${t}"${k.type === t ? " selected" : ""}>${vialName({type: t})}</option>`).join("") + "</select>";
    const rows = p.pkgs.map(k => `<tr id="row-${p.key}-${k.key}">
      <td>${esc(amt(q, k.mg))}</td>
      <td class="num" data-o="units"></td>
      <td>${vial(k)}</td>
      <td class="num"><input class="ncr-price" type="number" step="0.01" min="0" inputmode="decimal" data-p="${p.key}" data-k="${k.key}" data-f="price" value="${esc(k.price)}" placeholder="Enter price" aria-label="${esc(p.name)} ${esc(amt(q, k.mg))} price per package"></td>
      <td class="num c340"><input class="ncr-price" type="number" step="0.01" min="0" inputmode="decimal" data-p="${p.key}" data-k="${k.key}" data-f="price340" value="${esc(k.price340)}" placeholder="Enter 340B price" aria-label="${esc(p.name)} ${esc(amt(q, k.mg))} 340B price per package"></td>
      <td class="num" data-o="net"></td><td class="num" data-o="pay"></td><td class="num" data-o="ncr"></td><td class="num" data-o="mg%"></td></tr>`).join("");
    return head + rows;
  }).join("") || (m.custom ? `<tr><td colspan="9" class="muted">No products yet. Search for products under <b>Market, dose and payer</b>, or add a whole market.</td></tr>` : "");
  const gone = m.custom && m.gone.length ? `Saved but no longer in the CMS crosswalk, so left out: ${m.gone.map(e => esc(e.name || e.key)).join(", ")}.` : "";
  $("goneNote").innerHTML = gone; $("goneNote").hidden = !gone;
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
// The share of each opened multi-dose vial a dose uses up (1 without reuse).
function mdvUsed(p, c, m) {
  let mdvU = 0;
  p.pkgs.filter(k => units(m, k) > 0).forEach((k, i) => { if (k.type === "MDV") mdvU += units(m, k) * c.counts[i]; });
  return state.settings.mdvReuse && mdvU > 0 ? (mdvU - c.saved) / mdvU : 1;
}
// Packages one dose uses up, counted the same way as perYear().
function packsPerDose(p, c, m) {
  const used = mdvUsed(p, c, m);
  return p.pkgs.filter(k => units(m, k) > 0).reduce((t, k, i) => t + c.counts[i] * (k.type === "MDV" ? used : 1), 0);
}
function perYear(p, c, m, annual) {
  const pk = p.pkgs.filter(k => units(m, k) > 0), used = mdvUsed(p, c, m);
  return pk.map((k, i) => {
    if (!c.counts[i]) return "";
    const mdv = k.type === "MDV", count = Math.ceil(c.counts[i] * annual * (mdv ? used : 1) - 1e-9);
    return (mdv && used < 1 ? "≈" : "") + count.toLocaleString("en-US") + " × " + amt(m, k.mg) + (mdv ? " MDV" : "");
  }).filter(Boolean).join(" + ");
}

function update() {
  packDoses = null;
  const m = M(), s = state.settings;
  $("erosionOut").textContent = (s.erosion > 0 ? "+" : "") + s.erosion + "%";
  $("moreSum").textContent = "· Commercial " + n(s.commPct) + "% of limit"
    + (s.seq ? " · sequestration on" : "") + (n(s.erosion) ? " · limit change " + (s.erosion > 0 ? "+" : "") + s.erosion + "%" : "")
    + (s.wasteBill ? " · JW on" : "") + (s.mdvReuse ? " · multi-dose reuse" : "")
    + (s.is340 ? " · 340B " + Math.round(share340() * 100) + "%" : "");
  const mg = doseMg(m), D = doseUnits(m);
  $("doseLine").innerHTML = m.custom ? `<b>${m.products.length}</b> product${m.products.length === 1 ? "" : "s"}`
    : `Dose: <b>${esc(amt(m, mg))}</b> = <b>${D}</b> billing units`;
  $("exampleNote").hidden = !m.example;
  $("exampleNote").textContent = `Example prices are loaded for ${m.name}. They are made up for illustration and are not real contracts.`;
  const on340 = s.is340, sh = share340(), mixed = on340 && sh > 0 && sh < 1;
  $("share340L").hidden = $("note340").hidden = !on340;
  $("priceTable").classList.toggle("no340", !on340);
  $("priceHead").textContent = on340 ? "Other patients $ / pkg" : "Contract $ / pkg";

  // per package outputs. NCR per unit is compared across products only
  // when they all use the same billing unit.
  const included = m.products.filter(p => p.include);
  const oneUnit = !m.custom || new Set(included.map(p => p.unitMg + " " + p.unitName)).size <= 1;
  let bestUnit = null;
  m.products.forEach(p => {
    renderStatus(p);
    const q = pm(m, p);
    const head = $("prod-" + p.key); if (head) head.classList.toggle("ncr-off", !p.include);
    p.pkgs.forEach(k => { const r = $("row-" + p.key + "-" + k.key); if (r) r.classList.toggle("ncr-off", !p.include || !used(k)); });
    const pay = payPerUnit(p), cp = costed(p);
    p.pkgs.forEach((k, i) => {
      const row = $("row-" + p.key + "-" + k.key); if (!row) return;
      const u = units(q, k), net = netPerUnit(cp, cp.pkgs[i], q), ncr = pay - net;
      const o = f => row.querySelector(`[data-o="${f}"]`);
      o("units").textContent = n(k.mg) ? (+(n(k.mg) / q.unitMg).toFixed(2)).toLocaleString("en-US") : "—";
      if (!used(k)) { ["net", "pay", "ncr", "mg%"].forEach(f => { o(f).textContent = "—"; }); return; }
      o("pay").innerHTML = hasLimit(p) ? money(pay) : enterLimit;
      if (!priced(k)) { o("net").innerHTML = enterPrice; o("ncr").innerHTML = enterPrice; o("mg%").innerHTML = enterPrice; return; }
      o("net").textContent = u ? money(net) : "—";
      o("ncr").innerHTML = !hasLimit(p) ? enterLimit : u ? `<span class="${ncr < 0 ? "ncr-neg" : "ncr-pos"}">${money(ncr)}</span>` : "—";
      o("mg%").innerHTML = !hasLimit(p) ? enterLimit : u && net > 0 ? ((ncr / net) * 100).toFixed(1) + "%" : "—";
      if (oneUnit && p.include && hasLimit(p) && u && (!bestUnit || ncr > bestUnit.ncr)) bestUnit = {p, k, ncr};
    });
  });
  document.querySelectorAll("#priceBody tr").forEach(r => r.classList.remove("ncr-best"));
  if (bestUnit) { const r = $("row-" + bestUnit.p.key + "-" + bestUnit.k.key); if (r) r.classList.add("ncr-best"); }

  // per dose: only products with a limit, a dose and a price for every
  // package. y is doses a year and yr the NCR a year; products are ranked by
  // yr, which gives the per-dose order whenever every product has the same y.
  const results = channel => m.products.filter(ready).map(p => {
    const q = pm(m, p), Dp = doseUnits(q), c = bestCombo(costed(p, channel), q, Dp), y = yearDoses(m, p, Dp, c, q);
    return {p, q, D: Dp, c, y, yr: c ? c.ncr * y : 0};
  }).filter(r => r.c).sort((a, b) => (b.yr - a.yr) || (b.c.ncr - a.c.ncr));
  const res = results("mix");
  const waiting = included.filter(p => !res.some(r => r.p === p));
  const needs = p => !hasLimit(p) ? enterLimit : m.custom && !doseUnits(pm(m, p)) ? enterDose : enterPrice;
  const off = m.products.filter(p => !p.include);
  const lead = res[0], second = res[1];
  // Every product given the same number of doses a year: results read per dose.
  const sameYear = res.every(r => Math.abs(r.y - res[0].y) < 1e-9), annual = lead ? lead.y : 0;
  // What was administered: the dose rounded up to whole billing units.
  const given = r => r.q.unitName === "billing units" ? `${r.D}` : `${esc(amt(r.q, r.D * r.q.unitMg))} <span class="muted">(${r.D} u)</span>`;
  $("doseBody").innerHTML = res.map((r, i) => {
    const c = r.c;
    return `<tr class="${i === 0 ? "ncr-best" : ""}"><td><b>${esc(r.p.name)}</b> <span class="muted">${esc(r.p.hcpcs)}</span></td>
      <td>${esc(c.desc)}${c.saved ? ` <span class="ncr-chip warn">${+c.saved.toFixed(2)} u saved</span>` : ""}</td>
      <td class="num">${given(r)}</td><td class="num">${+c.discarded.toFixed(2)}</td><td class="num">${+c.billed.toFixed(2)}</td>
      <td class="num">${money(c.cost)}</td><td class="num">${money(c.pay)}</td>
      <td class="num"><span class="${c.ncr < 0 ? "ncr-neg" : "ncr-pos"}">${money(c.ncr)}</span></td>
      <td class="num">${qty(r.y)}</td>
      <td class="num">${money(r.yr, 0)}</td>
      <td class="num">${i === 0 ? '<span class="ncr-chip">Leader</span>' : `<span class="ncr-neg">${money(r.yr - lead.yr, 0)}</span>`}</td></tr>`;
  }).join("") + waiting.map(p => `<tr><td><b>${esc(p.name)}</b> <span class="muted">${esc(p.hcpcs)}</span></td>
      <td colspan="10">${needs(p)}</td></tr>`).join("")
    || `<tr><td colspan="11" class="muted">${m.custom ? "Add products to compare." : "Enter a dose and at least one package with a strength to see results."}</td></tr>`;
  if (!D && !m.custom) $("doseBody").innerHTML = `<tr><td colspan="11" class="muted">Enter a dose to see results.</td></tr>`;

  // verdict
  const shared = n(s[volField()]), ownVol = m.custom && m.products.some(p => has(p[volField()]));
  document.querySelectorAll("#priceBody .ncr-yr").forEach(i => { i.placeholder = qty(shared); });
  $("volNote").hidden = !(byUnits() && m.custom && !oneUnit);
  const pr = packsProduct(m);
  const volText = (byUnits() ? `${qty(shared)} billing units a year` : byPacks() ? `${qty(shared)} packages of ${pr ? pr.name : "the first product"} a year (${qty(sharedPackDoses(m))} doses)`
    : `${qty(shared)} doses a year`) + (ownVol ? " unless set for a product" : "");
  $("verdictSub").textContent = m.custom || D ? `${m.name}, ${m.custom ? "each product at its own dose" : D + " billing units per dose"}, ${volText}`
    + (on340 ? `, ${Math.round(sh * 100)}% bought at 340B.` : ".") : "";
  let summary = "";
  if (lead && !sameYear) {
    const gap = second ? lead.yr - second.yr : 0;
    summary = lead.yr < 0
      ? `At these prices no product is paid enough to cover its cost. ${lead.p.name} loses the least: ${money(-lead.yr, 0)} a year (${qty(lead.y)} doses).`
      : `At these prices and doses a year, ${lead.p.name} recovers the most: ${money(lead.yr, 0)} a year (${money(lead.c.ncr)} per dose × ${qty(lead.y)} doses)`
        + (second ? `, ${money(gap, 0)} more than ${second.p.name}` : "") + ".";
  } else if (lead && lead.c.ncr < 0) {
    summary = `At these prices no product is paid enough to cover its cost. ${lead.p.name} loses the least: ${money(-lead.c.ncr)} per dose.`;
  } else if (lead) {
    const gap = second ? lead.c.ncr - second.c.ncr : 0;
    summary = `At these prices, ${lead.p.name} recovers the most: ${money(lead.c.ncr)} per dose`
      + (second ? `, ${money(gap)} more than ${second.p.name}` : "")
      + `. Over ${qty(annual)} doses a year that is ${money(lead.c.ncr * annual, 0)}`
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
  const offNote = off.length ? `<p class="ncr-calc">Left out by you: ${off.map(p => esc(p.name)).join(", ")}.</p>` : "";
  if (lead) {
    const neg = res.filter(r => r.c.ncr < 0).length;
    $("leadCard").innerHTML = (sameYear ? `<div class="ncr-eyebrow">Best recovery per dose</div>
      <div class="ncr-lead-name">${esc(lead.p.name)} <span class="muted">${esc(lead.p.hcpcs)}</span></div>
      <div class="ncr-lead-val">${money(lead.c.ncr)}<small>per dose</small></div>
      <div class="ncr-stats">
        <div><div class="k">Ahead of ${second ? esc(second.p.name) : "next"}</div><div class="v">${second ? signed(lead.c.ncr - second.c.ncr, 2) + " / dose" : "—"}</div></div>
        <div><div class="k">Annual difference</div><div class="v">${second ? signed((lead.c.ncr - second.c.ncr) * annual) : "—"}</div></div>`
      : `<div class="ncr-eyebrow">Best recovery per year</div>
      <div class="ncr-lead-name">${esc(lead.p.name)} <span class="muted">${esc(lead.p.hcpcs)}</span></div>
      <div class="ncr-lead-val">${money(lead.yr, 0)}<small>per year</small></div>
      <div class="ncr-stats">
        <div><div class="k">Ahead of ${second ? esc(second.p.name) : "next"}</div><div class="v">${second ? signed(lead.yr - second.yr) + " / year" : "—"}</div></div>
        <div><div class="k">Per dose</div><div class="v">${money(lead.c.ncr)} × ${qty(lead.y)} doses</div></div>`) + `
        <div><div class="k">Vials used</div><div class="v">${esc(lead.c.desc)}</div></div>
        <div><div class="k">Underwater products</div><div class="v">${neg ? `<span class="ncr-chip neg">${neg} below cost</span>` : "None"}</div></div>
      </div>` + (waiting.length ? `<p class="ncr-calc">${waiting.length} product${waiting.length > 1 ? "s" : ""} not compared yet: enter ${waiting.some(p => !hasLimit(p)) ? "a limit or " : ""}${m.custom && waiting.some(p => !doseUnits(pm(m, p))) ? "a dose or " : ""}${on340 ? "prices" : "contract prices"}.</p>` : "") + offNote;
    const val = r => sameYear ? r.c.ncr : r.yr;
    const vals = res.map(val), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = (hi - lo) || 1, z = (0 - lo) / span * 100;
    $("bars").innerHTML = `<div class="ncr-eyebrow">NCR per ${sameYear ? "dose" : "year"} by product</div>` + res.map((r, i) => {
      const v = val(r), a = (Math.min(v, 0) - lo) / span * 100, w = Math.abs(v) / span * 100;
      return `<div class="ncr-bar"><div class="nm" title="${esc(r.p.name)}">${esc(r.p.name)}<span>${r.p.ref ? "ref" : ""}</span></div>
        <div class="ncr-track"><div class="ncr-fill ${v < 0 ? "neg" : i === 0 ? "lead" : ""}" style="left:${a}%;width:${Math.max(w, .5)}%"></div>${lo < 0 ? `<div class="ncr-zero" style="left:${z}%"></div>` : ""}</div>
        <div class="val ${v < 0 ? "ncr-neg" : ""}">${money(v, sameYear ? 2 : 0)}</div></div>`;
    }).join("");
  } else {
    $("leadCard").innerHTML = !m.products.length
      ? `<div class="ncr-eyebrow">Best recovery per dose</div><p>This comparison has no products yet. Search for products under <b>Market, dose and payer</b> and click <b>Add</b>.</p>`
      : off.length === m.products.length
      ? `<div class="ncr-eyebrow">Best recovery per dose</div><p>Every product is left out. Tick <b>Include</b> beside a product under <b>Your prices by package</b> to compare it.</p>`
      : `<div class="ncr-eyebrow">Best recovery per dose</div>
      <p>Enter ${m.custom ? "a dose and " : ""}${on340 ? "prices" : "contract prices"} for at least one product${on340 ? "" : ", or click <b>Load example prices</b>,"} to compare products.</p>`;
    $("bars").innerHTML = "";
  }

  // a year of purchases: packages by strength, spend, payment and NCR
  $("buySub").textContent = "Packages needed for the doses given per year, by strength, if every dose used that product."
    + (byUnits() ? " Doses a year are the billing units a year divided by the billing units in one dose." : "")
    + (byPacks() ? " Doses a year are the packages a year divided by the packages one dose uses." : "")
    + (on340 ? ` About ${Math.round(sh * 100)}% of them are bought at the 340B price.` : "");
  $("buyBody").innerHTML = res.map((r, i) => `<tr class="${i === 0 ? "ncr-best" : ""}"><td><b>${esc(r.p.name)}</b> <span class="muted">${esc(r.p.hcpcs)}</span></td>
      <td>${esc(perYear(r.p, r.c, r.q, r.y)) || "—"}</td>
      <td class="num">${money(r.c.cost * r.y, 0)}</td><td class="num">${money(r.c.pay * r.y, 0)}</td>
      <td class="num"><span class="${r.yr < 0 ? "ncr-neg" : "ncr-pos"}">${money(r.yr, 0)}</span></td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">Enter prices and doses given per year to see a year of purchases.</td></tr>`;

  // breakeven: the price each package needs to match the best NCR per unit.
  // It solves for the price you negotiate: the 340B price when every dose
  // is bought at 340B, otherwise the other-patients (contract) price.
  const solve340 = on340 && sh >= 1;
  $("beCurHead").textContent = solve340 ? "Current 340B $ / pkg" : on340 ? "Current other $ / pkg" : "Current $ / pkg";
  $("beSub").textContent = `The ${solve340 ? "340B price" : on340 ? "price for other patients" : "contract price"} each package would need for its NCR per unit to match the leader’s best package. Use it as a negotiation target.`;
  if (m.custom) {
    // Products here can differ in billing unit and dose, so they are matched
    // per dose: the cost of a dose is linear in the package prices, and the
    // vials used don't change when every price moves by the same percent, so
    // every package moves by (payment − leader's NCR) ÷ acquisition cost − 1.
    // With different doses a year, the target per dose is the leader's NCR a
    // year spread over this product's doses.
    $("beSub").textContent = `The ${solve340 ? "340B price" : on340 ? "price" : "contract price"} each package would need for the product’s NCR per ${sameYear ? "dose" : "year"} to match the leader’s, if all its package prices moved by the same percent`
      + (mixed ? " (its 340B prices and its prices for other patients alike)" : "") + ". Use it as a negotiation target.";
    $("beBody").innerHTML = lead ? res.flatMap(r => {
      const target = sameYear ? lead.c.ncr : r.y > 0 ? lead.yr / r.y : null, isLead = r === lead;
      if (target === null) return [`<tr><td><b>${esc(r.p.name)}</b></td><td colspan="5" class="muted">No doses a year entered.</td></tr>`];
      const f = r.c.cost > 0 ? (r.c.pay - target) / r.c.cost : 0;
      return r.p.pkgs.filter(used).map(k => {
        const cur = solve340 ? n(k.price340) : n(k.price), be = cur * f, diff = be - cur;
        return `<tr class="${isLead ? "ncr-best" : ""}"><td><b>${esc(r.p.name)}</b></td><td>${esc(amt(r.q, k.mg))} ${k.type === "MDV" ? "multi-dose" : "single-dose"}</td>
          <td class="num">${money(cur)}</td><td class="num">${isLead ? money(cur) : f <= 0 ? '<span class="ncr-neg">Not reachable</span>' : money(be)}</td>
          <td class="num">${isLead ? '<span class="ncr-chip">Sets the target</span>' : f <= 0 ? "—" : `<span class="${diff < 0 ? "ncr-neg" : "ncr-pos"}">${signed(diff, 2)} (${((f - 1) * 100).toFixed(1)}%)</span>`}</td>
          <td>${isLead ? "" : f >= 1 ? '<span class="ncr-chip">Already ahead</span>' : ""}</td></tr>`;
      });
    }).join("") : `<tr><td colspan="6" class="muted">Enter doses and prices to see breakeven targets.</td></tr>`;
  } else if (bestUnit) {
    const target = bestUnit.ncr;
    $("beBody").innerHTML = m.products.filter(p => p.include).flatMap(p => {
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
  if (t.dataset.f === "doseBasis") { p.dose.basis = t.value; renderPricing(); doseVis(); }
  else if (t.dataset.f === "doseValue") p.dose.value = n(t.value);
  else if (t.dataset.k) { const k = p.pkgs.find(x => x.key === t.dataset.k); if (k) k[t.dataset.f] = t.value; }
  else p[t.dataset.f] = t.type === "checkbox" ? t.checked : t.value;
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
  if (b.dataset.act === "remove") {
    const m = M();
    m.products = m.products.filter(x => x !== p);
    renderPricing(); renderFound(); renderPacksOf(); doseVis();
    toast("toast", p.name + " removed.");
  }
  update();
});

/* ---------- comparisons of your own ---------- */
// A market's product as saved in a comparison: same prices, limit, rebate,
// vial types and dose; packages the market doesn't use are marked unused.
function fromMarket(mk, p) {
  const c = CAT[p.key]; if (!c) return null;
  const e = savedProduct(p, false);
  const byUnits = {}; p.pkgs.forEach(k => { byUnits[k.units] = k; });
  e.key = p.key; e.dose = {basis: mk.dose.basis, value: mk.dose.value};
  e.types = {}; e.prices = {}; e.prices340 = {};
  c.packages.forEach(ck => {
    const k = byUnits[ck.units];
    e.types[ck.key] = k ? k.type : "OFF";
    if (k && k.price !== "") e.prices[ck.key] = k.price;
    if (k && k.price340 !== "") e.prices340[ck.key] = k.price340;
  });
  return customProduct(c, e);
}
function addMarket(m, mk) {
  const before = m.products.length;
  mk.products.forEach(p => { if (!m.products.some(x => x.key === p.key)) { const x = fromMarket(mk, p); if (x) m.products.push(x); } });
  return m.products.length - before;
}
function newOwn(from) {
  const id = "c" + Date.now().toString(36);
  const names = new Set(Object.values(markets).map(x => x.name));
  let name = from ? from.name + " and more" : "My comparison", i = 2;
  while (names.has(name)) name = (from ? from.name + " and more" : "My comparison") + " " + i++;
  const m = makeCustom(id, Object.assign({name}, from ? {weight: from.dose.weight, bsa: from.dose.bsa} : {}));
  if (from) addMarket(m, from);
  state.market = id;
  renderControls(); renderPricing(); update();
  $("findProduct").focus();
}
$("startOwn").addEventListener("click", () => newOwn(M()));
$("newOwn").addEventListener("click", () => newOwn(null));
$("deleteOwn").addEventListener("click", () => {
  const m = M();
  if (!m.custom || !window.confirm(`Delete “${m.name}” and the prices entered in it? This can’t be undone.`)) return;
  delete markets[m.id];
  state.market = DATA.markets[0].id;
  renderControls(); renderPricing(); update();
});
$("ownName").addEventListener("input", e => {
  const m = M(); if (!m.custom) return;
  m.name = e.target.value.trim() || "My comparison";
  const o = $("market").querySelector(`option[value="${m.id}"]`); if (o) o.textContent = m.name;
  update();
});
$("findProduct").addEventListener("input", renderFound);
$("found").addEventListener("click", e => {
  const b = e.target.closest("button[data-add]"), m = M(); if (!b || !m.custom) return;
  const c = CAT[b.dataset.add]; if (!c || m.products.some(p => p.key === c.key)) return;
  m.products.push(customProduct(c));
  renderPricing(); renderFound(); renderPacksOf(); doseVis(); update();
  toast("findToast", c.name + " added" + (c.dose ? "." : ". Enter its dose under Your prices by package."), 4000);
});
$("addMarket").addEventListener("change", e => {
  const m = M(), mk = markets[e.target.value]; if (!m.custom || !mk) return;
  const added = addMarket(m, mk);
  e.target.value = "";
  renderPricing(); renderFound(); renderPacksOf(); doseVis(); update();
  toast("findToast", added ? `${added} product${added > 1 ? "s" : ""} from ${mk.name} added.` : `Every product in ${mk.name} is already here.`, 4000);
});

$("loadExamples").addEventListener("click", () => {
  const m = M();
  if (!m.products.some(p => p.pkgs.some(k => k.examplePrice != null))) {
    toast("toast", "There are no example prices for these products. Example prices exist only for products in some biosimilar markets.", 5000);
    return;
  }
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

$("market").addEventListener("change", e => { state.market = e.target.value; $("findProduct").value = ""; renderControls(); renderPricing(); update(); });
$("doseBasis").addEventListener("change", e => { M().dose.basis = e.target.value; doseVis(); update(); });
[["doseVal", "value"], ["weight", "weight"], ["bsa", "bsa"]].forEach(([id, f]) => $(id).addEventListener("input", e => { M().dose[f] = n(e.target.value); update(); }));
$("annual").addEventListener("input", e => { state.settings[volField()] = n(e.target.value); update(); });
$("packsOf").addEventListener("change", e => { M().packsOf = e.target.value; update(); });
// Switching to billing units or packages for the first time starts from the
// doses already entered: doses × the billing units in one dose (the first
// product's, in a comparison of your own), or × the packages the packs
// product's dose uses.
$("volBasis").addEventListener("change", e => {
  const s = state.settings, m = M();
  s.volBasis = ["units", "packs"].includes(e.target.value) ? e.target.value : "doses";
  if (byUnits() && s.annualUnits == null) {
    const p = m.custom ? m.products.find(x => doseUnits(pm(m, x)) > 0) : null;
    s.annualUnits = n(s.annual) * (m.custom ? (p ? doseUnits(pm(m, p)) : 1) : doseUnits(m));
  }
  if (byPacks() && s.annualPacks == null) {
    const r = packsProduct(m), rq = r && pm(m, r), rD = r ? doseUnits(rq) : 0, c = r ? bestCombo(costed(r), rq, rD) : null;
    s.annualPacks = Math.round(n(s.annual) * (c ? packsPerDose(r, c, rq) : 1));
  }
  renderControls(); renderPricing(); update();
});
["medShare", "commPct", "erosion", "share340"].forEach(id => $(id).addEventListener("input", e => { state.settings[id] = n(e.target.value); update(); }));
["seq", "wasteBill", "mdvReuse", "is340"].forEach(id => $(id).addEventListener("change", e => { state.settings[id] = e.target.checked; update(); }));

$("copyBtn").addEventListener("click", () => {
  const rows = [["Product", "HCPCS", "Vials used", "Administered (billing units)", "Discarded", "Billed units", "Acquisition", "Payment", "NCR/dose", "Doses/year", "NCR/year", "Packages/year"]];
  const m = M();
  m.products.filter(ready).map(p => { const q = pm(m, p), D = doseUnits(q), c = bestCombo(costed(p), q, D), y = yearDoses(m, p, D, c, q); return {p, q, D, c, y, yr: c ? c.ncr * y : 0}; })
    .filter(r => r.c).sort((a, b) => (b.yr - a.yr) || (b.c.ncr - a.c.ncr))
    .forEach(({p, q, D, c, y, yr}) => rows.push([p.name, p.hcpcs, c.desc, D, +c.discarded.toFixed(2), +c.billed.toFixed(2), c.cost.toFixed(2), c.pay.toFixed(2), c.ncr.toFixed(2), +y.toFixed(1), yr.toFixed(0), perYear(p, c, q, y)]));
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
