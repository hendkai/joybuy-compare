"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const eur = (v) => v == null ? "–" : v.toLocaleString("de-DE", { style: "currency", currency: "EUR" });
const pct = (v) => v == null ? "–" : `${v > 0 ? "" : ""}${Math.round(v)} %`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const KIND = { exact: "gleiche Packung", unit: "per Grundpreis", loose: "ähnlich" };

const state = {
  q: "", category: "", keyword: "", compare: "all", min_savings: 0, min_discount: 0,
  price_min: "", price_max: "", in_stock: false, sort: "savings", shops: [],
  offset: 0, total: 0, loading: false, done: false,
};
let SHOPS = [];
const shopInfo = (n) => SHOPS.find((s) => s.name === n) || { label: n, color: "#888" };

// ---------- URL state ----------
const KEYS = ["q", "category", "keyword", "compare", "min_savings", "min_discount", "price_min", "price_max", "in_stock", "sort", "shops"];
function readUrl() {
  const p = new URLSearchParams(location.hash.slice(1));
  for (const k of KEYS) {
    if (!p.has(k)) continue;
    const v = p.get(k);
    if (k === "shops") state.shops = v ? v.split(",") : [];
    else if (k === "in_stock") state.in_stock = v === "1";
    else if (k === "min_savings" || k === "min_discount") state[k] = Number(v) || 0;
    else state[k] = v;
  }
}
function writeUrl() {
  const p = new URLSearchParams();
  for (const k of KEYS) {
    let v = state[k];
    if (k === "shops") v = state.shops.length && state.shops.length < SHOPS.length ? state.shops.join(",") : "";
    if (k === "in_stock") v = state.in_stock ? "1" : "";
    if (k === "compare" && v === "all") v = "";
    if (k === "sort" && v === "savings") v = "";
    if (v && v !== 0) p.set(k, v);
  }
  history.replaceState(null, "", "#" + p.toString());
}

function apiParams(extra = {}) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.category) p.set("category", state.category);
  if (state.keyword) p.set("keyword", state.keyword);
  p.set("compare", state.compare);
  if (state.min_savings > 0) p.set("min_savings", state.min_savings);
  if (state.min_discount > 0) p.set("min_discount", state.min_discount);
  if (state.price_min !== "") p.set("price_min", state.price_min);
  if (state.price_max !== "") p.set("price_max", state.price_max);
  if (state.in_stock) p.set("in_stock", "true");
  if (state.shops.length) p.set("shops", state.shops.join(","));
  p.set("sort", state.sort);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}

// ---------- facets & stats ----------
async function loadFacets() {
  const shopsParam = state.shops.length ? `?shops=${state.shops.join(",")}` : "";
  const f = await (await fetch(`/api/facets${shopsParam}`)).json();
  SHOPS = f.shops;
  if (!state.shops.length) state.shops = SHOPS.map((s) => s.name);
  $("#shopFilters").innerHTML = SHOPS.map((s) => `
    <label class="shopchk"><input type="checkbox" value="${s.name}" ${state.shops.includes(s.name) ? "checked" : ""}>
      <i style="background:${s.color}"></i>${esc(s.label)}<small>${s.matches}</small></label>`).join("");
  $$("#shopFilters input").forEach((i) => i.onchange = () => {
    state.shops = $$("#shopFilters input:checked").map((x) => x.value);
    if (!state.shops.length) { i.checked = true; state.shops = [i.value]; }
    refresh(true);
  });
  $("#categories").innerHTML = `<li data-v="" class="${state.category ? "" : "active"}">Alle Kategorien</li>` +
    f.categories.map((c) => `<li data-v="${esc(c.name)}" class="${c.name === state.category ? "active" : ""}">
      <span>${esc(c.name)}</span><small>${c.cheaper ? `<span style="color:var(--good)">${c.cheaper}↓</span> · ` : ""}${c.n}</small></li>`).join("");
  $$("#categories li").forEach((li) => li.onclick = () => { state.category = li.dataset.v; refresh(true); });
  $("#keywords").innerHTML = `<li data-v="" class="${state.keyword ? "" : "active"}">Alle</li>` +
    f.keywords.map((k) => `<li data-v="${esc(k.name)}" class="${k.name === state.keyword ? "active" : ""}"><span>${esc(k.name)}</span><small>${k.n}</small></li>`).join("");
  $$("#keywords li").forEach((li) => li.onclick = () => { state.keyword = li.dataset.v; refresh(true); });

  const s = f.stats;
  const share = s.matched ? Math.round((s.cheaper / s.matched) * 100) : null;
  $("#stats").innerHTML = `
    <div class="stat"><b>${s.products ?? 0}</b><span>Joybuy-Produkte</span></div>
    <div class="stat click" data-cmp="compared"><b>${s.matched ?? 0}</b><span>mit Vergleichspreis (${s.compared ?? 0} geprüft)</span></div>
    <div class="stat good click" data-cmp="cheaper"><b>${s.cheaper ?? 0}${share != null ? ` <small>(${share} %)</small>` : ""}</b><span>Joybuy günstiger</span></div>
    <div class="stat bad click" data-cmp="pricier"><b>${s.pricier ?? 0}</b><span>woanders günstiger</span></div>
    <div class="stat ${s.median_savings > 0 ? "good" : s.median_savings < 0 ? "bad" : ""}"><b>${s.median_savings != null ? Math.abs(Math.round(s.median_savings)) + " %" : "–"}</b><span>${s.median_savings == null ? "Median-Differenz" : s.median_savings >= 0 ? "Joybuy im Median günstiger" : "Joybuy im Median teurer"}</span></div>
    <div class="stat click" data-sort="discount"><b>${s.discounted ?? 0}</b><span>mit Joybuy-Rabatt</span></div>`;
  $$("#stats .click").forEach((el) => el.onclick = () => {
    if (el.dataset.cmp) state.compare = el.dataset.cmp;
    if (el.dataset.sort) { state.sort = el.dataset.sort; state.min_discount = 5; }
    syncControls(); refresh(true);
  });
}

// ---------- product grid ----------
function verdict(p) {
  if (p.savings_pct == null) return null;
  const sh = shopInfo(p.best_shop).label;
  if (p.savings_pct > 0.5) return { cls: "good", short: `${Math.round(p.savings_pct)} % günstiger`, long: `Joybuy ist ${eur(p.savings_eur)} (${Math.round(p.savings_pct)} %) günstiger als ${sh}` };
  if (p.savings_pct < -0.5) return { cls: "bad", short: `${Math.round(-p.savings_pct)} % teurer`, long: `${sh} ist ${eur(-p.savings_eur)} (${Math.round(-p.savings_pct)} %) günstiger als Joybuy` };
  return { cls: "neutral", short: "gleich teuer", long: `Gleicher Preis wie bei ${sh}` };
}

function card(p) {
  const v = verdict(p);
  const offers = (p.offers || []).slice(0, 3).map((o) => {
    const cls = o.comparable_price < p.price ? "cheaper" : "pricier";
    const si = shopInfo(o.shop);
    return `<div class="cmprow ${cls}"><span class="shop"><i style="background:${si.color}"></i>${esc(si.label.split(" ")[0])}${o.match_kind !== "exact" ? `<span class="kind" title="${KIND[o.match_kind]}">${o.match_kind === "unit" ? "€/kg" : "≈"}</span>` : ""}</span><b>${eur(o.comparable_price)}</b></div>`;
  }).join("");
  return `<article class="card" data-sku="${p.sku}">
    <div class="badges">${p.discount_pct ? `<span class="badge disc">−${Math.round(p.discount_pct)} %</span>` : "<span></span>"}
      ${v ? `<span class="badge ${v.cls}">${v.short}</span>` : ""}</div>
    <div class="img"><img loading="lazy" src="${esc(p.image || "")}" alt="" onerror="this.style.visibility='hidden'"></div>
    <div class="body">
      <div class="brandname">${esc(p.brand || p.category || "")}</div>
      <div class="title" title="${esc(p.title)}">${esc(p.title)}</div>
      <div class="priceline"><span class="price">${eur(p.price)}</span>${p.list_price ? `<span class="strike">${eur(p.list_price)}</span>` : ""}</div>
      <div class="unit">${esc(p.unit_price_text || p.qty_label || "")}</div>
      <div class="meta">${p.rating ? `<span>★ ${p.rating}</span>` : ""}${p.sales_text ? `<span>${esc(p.sales_text)}</span>` : ""}${p.in_stock ? "" : `<span style="color:var(--bad)">nicht auf Lager</span>`}</div>
      <div class="cmp">${offers || `<span class="none">${p.compared_at ? "kein Vergleichsangebot gefunden" : "noch nicht verglichen"}</span>`}</div>
    </div></article>`;
}

async function loadPage() {
  if (state.loading || state.done) return;
  state.loading = true;
  const grid = $("#grid");
  if (state.offset === 0) grid.innerHTML = Array(8).fill('<div class="skeleton"></div>').join("");
  try {
    const r = await (await fetch(`/api/products?${apiParams({ offset: state.offset, limit: 48 })}`)).json();
    if (state.offset === 0) grid.innerHTML = "";
    grid.insertAdjacentHTML("beforeend", r.items.map(card).join(""));
    state.total = r.total;
    state.offset += r.items.length;
    state.done = state.offset >= r.total || r.items.length === 0;
    $("#resultInfo").textContent = `${r.total.toLocaleString("de-DE")} Produkte`;
    $("#empty").classList.toggle("hidden", r.total > 0);
  } finally {
    state.loading = false;
  }
}

function chips() {
  const c = [];
  if (state.q) c.push(["q", `„${state.q}“`]);
  if (state.category) c.push(["category", state.category]);
  if (state.keyword) c.push(["keyword", `Suchbegriff: ${state.keyword}`]);
  if (state.compare !== "all") c.push(["compare", { cheaper: "Joybuy günstiger", pricier: "Joybuy teurer", compared: "mit Vergleich", uncompared: "unverglichen" }[state.compare]]);
  if (state.min_savings) c.push(["min_savings", `≥ ${state.min_savings} % Ersparnis`]);
  if (state.min_discount) c.push(["min_discount", `≥ ${state.min_discount} % Rabatt`]);
  if (state.price_min !== "" || state.price_max !== "") c.push(["price", `${state.price_min || 0} – ${state.price_max || "∞"} €`]);
  if (state.in_stock) c.push(["in_stock", "auf Lager"]);
  $("#activeChips").innerHTML = c.map(([k, l]) => `<span class="chip" data-k="${k}">${esc(l)}</span>`).join("");
  $$("#activeChips .chip").forEach((el) => el.onclick = () => {
    const k = el.dataset.k;
    if (k === "price") { state.price_min = ""; state.price_max = ""; }
    else if (k === "compare") state.compare = "all";
    else if (k === "in_stock") state.in_stock = false;
    else if (k.startsWith("min_")) state[k] = 0;
    else state[k] = "";
    syncControls(); refresh(true);
  });
}

function syncControls() {
  $("#q").value = state.q;
  $$("input[name=compare]").forEach((r) => r.checked = r.value === state.compare);
  $("#minSavings").value = state.min_savings; $("#minSavingsOut").textContent = state.min_savings ? `≥ ${state.min_savings} %` : "aus";
  $("#minDiscount").value = state.min_discount; $("#minDiscountOut").textContent = state.min_discount ? `≥ ${state.min_discount} %` : "aus";
  $("#priceMin").value = state.price_min; $("#priceMax").value = state.price_max;
  $("#inStock").checked = state.in_stock; $("#sort").value = state.sort;
}

let facetTimer;
function refresh(withFacets = false) {
  state.offset = 0; state.done = false;
  writeUrl(); chips();
  loadPage();
  if (withFacets) { clearTimeout(facetTimer); facetTimer = setTimeout(loadFacets, 50); }
}

// ---------- detail modal ----------
function sparkline(history, product) {
  const series = {};
  for (const h of history) (series[h.shop] ||= []).push(h);
  const all = history.filter((h) => h.price != null);
  if (all.length < 2) return "";
  const t0 = Math.min(...all.map((h) => h.ts)), t1 = Math.max(Date.now() / 1000, ...all.map((h) => h.ts));
  const p0 = Math.min(...all.map((h) => h.price)) * 0.95, p1 = Math.max(...all.map((h) => h.price)) * 1.05;
  const X = (t) => 10 + ((t - t0) / Math.max(1, t1 - t0)) * 580, Y = (p) => 110 - ((p - p0) / Math.max(0.01, p1 - p0)) * 100;
  const color = (s) => s === "joybuy" ? "#e4002b" : shopInfo(s).color;
  let paths = "";
  for (const [s, pts] of Object.entries(series)) {
    let d = "", last = null;
    for (const h of pts) { d += `${d ? "L" : "M"}${X(h.ts).toFixed(1)},${Y(h.price).toFixed(1)} `; last = h; }
    d += `L590,${Y(last.price).toFixed(1)}`;
    paths += `<path d="${d}" fill="none" stroke="${color(s)}" stroke-width="2.5"/>`;
  }
  const legend = Object.keys(series).map((s) => `<span><i style="background:${color(s)}"></i>${s === "joybuy" ? "Joybuy" : esc(shopInfo(s).label)}</span>`).join("");
  return `<div class="hist"><h4 style="margin:0 0 6px">Preisverlauf</h4><svg viewBox="0 0 600 120" preserveAspectRatio="none">${paths}</svg><div class="legend">${legend}</div></div>`;
}

async function openDetail(sku) {
  const dlg = $("#detail");
  dlg.innerHTML = `<div style="padding:40px" class="muted">Lade …</div>`;
  dlg.showModal();
  const d = await (await fetch(`/api/products/${encodeURIComponent(sku)}`)).json();
  const p = d.product;
  const v = verdict(p);
  const searched = Object.fromEntries(d.searches.map((s) => [s.shop, s]));
  const rows = [`<tr class="jb"><td>Joybuy</td><td><span class="otitle">${esc(p.title)}</span></td><td class="num">${eur(p.price)}</td><td class="num">${eur(p.price)}</td><td><a href="${esc(p.url)}" target="_blank" rel="noopener">öffnen ↗</a></td></tr>`];
  for (const o of d.offers) {
    const si = shopInfo(o.shop);
    const diff = o.comparable_price - p.price;
    rows.push(`<tr><td><span class="shop"><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${si.color}"></i> ${esc(si.label)}</span></td>
      <td><span class="otitle">${esc(o.title)}</span><span class="kind">${KIND[o.match_kind]} · ${Math.round(o.score)}%</span>${o.ean ? ` <span class="kind">EAN ${esc(o.ean)}</span>` : ""}</td>
      <td class="num">${eur(o.price)}<br><small class="muted">${esc(o.unit_price_text || "")}</small></td>
      <td class="num" style="color:${diff < 0 ? "var(--bad)" : "var(--good)"}">${eur(o.comparable_price)}<br><small>${diff >= 0 ? "+" : ""}${eur(diff)}</small></td>
      <td>${o.url ? `<a href="${esc(o.url)}" target="_blank" rel="noopener">öffnen ↗</a>` : ""}</td></tr>`);
  }
  for (const s of SHOPS) {
    if (d.offers.some((o) => o.shop === s.name)) continue;
    const st = searched[s.name];
    rows.push(`<tr class="muted"><td>${esc(s.label)}</td><td colspan="4"><small>${st ? (st.status === "error" ? "Fehler bei der Suche" : `kein passendes Produkt für „${esc(st.query)}“`) : "nicht gesucht"}</small></td></tr>`);
  }
  dlg.innerHTML = `<button class="close" onclick="this.closest('dialog').close()">✕</button>
    <div class="wrap">
      <div><div class="bigimg"><img src="${esc(p.image || "")}" alt=""></div></div>
      <div>
        <div class="brandname muted">${esc(p.brand || "")} · ${esc(p.category || "")}</div>
        <h2>${esc(p.title)}</h2>
        <div class="priceline"><span class="price">${eur(p.price)}</span>${p.list_price ? `<span class="strike">UVP ${eur(p.list_price)}</span>` : ""}
          ${p.discount_pct ? `<span class="badge disc">−${Math.round(p.discount_pct)} %</span>` : ""}</div>
        <div class="unit">${esc([p.unit_price_text, p.qty_label, p.discount_text, p.seller].filter(Boolean).join(" · "))}</div>
        <div class="verdict ${v ? v.cls : "neutral"}">${v ? v.long : (p.compared_at ? "Kein vergleichbares Angebot gefunden." : "Noch nicht verglichen.")}</div>
        <table class="offers"><thead><tr><th>Shop</th><th>Artikel</th><th class="num">Preis</th><th class="num">Vergleichbar*</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>
        <p class="muted" style="font-size:12px">* auf die Joybuy-Packungsgröße umgerechnet, falls die Packung abweicht (Grundpreis). idealo zeigt den günstigsten Händlerpreis ohne Versand. Zuletzt verglichen: ${p.compared_at ? new Date(p.compared_at * 1000).toLocaleString("de-DE") : "nie"}.</p>
        ${sparkline(d.history, p)}
        <div class="btnrow"><a class="primary" href="${esc(p.url)}" target="_blank" rel="noopener">Bei Joybuy ansehen ↗</a>
          <button id="recompare">Jetzt neu vergleichen</button></div>
      </div>
    </div>`;
  $("#recompare").onclick = async () => {
    const r = await fetch("/api/refresh", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sku }) });
    alert(r.ok ? "Vergleich gestartet – Status oben." : "Es läuft bereits eine Aktualisierung."); pollStatus();
  };
}

// ---------- refresh / status ----------
let pollTimer;
async function pollStatus() {
  clearTimeout(pollTimer);
  const s = await (await fetch("/api/status")).json();
  const bar = $("#statusBar");
  if (s.running) {
    const prog = s.total ? Math.round((s.done / s.total) * 100) : 0;
    bar.className = "statusbar";
    bar.innerHTML = `<b>Aktualisierung läuft</b><span>${esc(s.step || s.phase || "")}</span>${s.total ? `<div class="progress"><i style="width:${prog}%"></i></div><span>${s.done}/${s.total}</span>` : ""}`;
    $("#refreshBtn").disabled = true;
    pollTimer = setTimeout(() => { pollStatus(); if (Math.random() < 0.34) { loadFacets(); } }, 3000);
  } else {
    $("#refreshBtn").disabled = false;
    if (s.error) { bar.className = "statusbar error"; bar.innerHTML = `<b>Fehler:</b> ${esc(s.error)}`; }
    else bar.className = "statusbar hidden";
    if (bar.dataset.wasRunning === "1") { loadFacets(); refresh(); }
  }
  bar.dataset.wasRunning = s.running ? "1" : "0";
}

function bind() {
  $("#searchForm").onsubmit = (e) => { e.preventDefault(); state.q = $("#q").value.trim(); refresh(true); };
  $$("input[name=compare]").forEach((r) => r.onchange = () => { state.compare = r.value; refresh(); });
  $("#minSavings").oninput = (e) => { state.min_savings = +e.target.value; $("#minSavingsOut").textContent = state.min_savings ? `≥ ${state.min_savings} %` : "aus"; };
  $("#minSavings").onchange = () => { if (state.min_savings && state.compare === "all") state.compare = "cheaper"; syncControls(); refresh(); };
  $("#minDiscount").oninput = (e) => { state.min_discount = +e.target.value; $("#minDiscountOut").textContent = state.min_discount ? `≥ ${state.min_discount} %` : "aus"; };
  $("#minDiscount").onchange = () => refresh();
  let pt; const onPrice = () => { clearTimeout(pt); pt = setTimeout(() => { state.price_min = $("#priceMin").value; state.price_max = $("#priceMax").value; refresh(); }, 400); };
  $("#priceMin").oninput = onPrice; $("#priceMax").oninput = onPrice;
  $("#inStock").onchange = (e) => { state.in_stock = e.target.checked; refresh(); };
  $("#sort").onchange = (e) => { state.sort = e.target.value; refresh(); };
  $("#resetFilters").onclick = () => {
    Object.assign(state, { q: "", category: "", keyword: "", compare: "all", min_savings: 0, min_discount: 0, price_min: "", price_max: "", in_stock: false, sort: "savings", shops: SHOPS.map((s) => s.name) });
    syncControls(); refresh(true);
  };
  $("#filterToggle").onclick = () => $("#sidebar").classList.toggle("open");
  $("#grid").onclick = (e) => { const c = e.target.closest(".card"); if (c) openDetail(c.dataset.sku); };
  $("#detail").onclick = (e) => { if (e.target.id === "detail") e.target.close(); };
  $("#refreshBtn").onclick = () => $("#refreshDlg").showModal();
  $("#refreshForm").onsubmit = async (e) => {
    if (e.submitter?.value !== "ok") return;
    const body = {
      keywords: $("#rfKeywords").value.split("\n").map((s) => s.trim()).filter(Boolean),
      pages: +$("#rfPages").value || 2,
      limit: +$("#rfLimit").value || null,
      shops: state.shops.join(","),
    };
    const r = await fetch("/api/refresh", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) alert((await r.json()).detail || "Fehler");
    pollStatus();
  };
  new IntersectionObserver((en) => { if (en[0].isIntersecting) loadPage(); }, { rootMargin: "600px" }).observe($("#sentinel"));
}

(async function init() {
  readUrl();
  bind();
  await loadFacets();
  syncControls();
  refresh();
  pollStatus();
})();
