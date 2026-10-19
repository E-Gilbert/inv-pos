/* CFC INV-POS — front end.
 * Plain JavaScript, no build step and nothing loaded from the internet: it runs fully on the LAN.
 * Quantities are stored in g / ml / pcs. Staff see kg / L when counting stock and receiving,
 * and g / ml in recipes (per plate).
 *
 * The server enforces every permission; the UI only hides what a role can't use.
 * Every checkout carries a client_ref so a retry after a network drop can never double-charge.
 */
"use strict";

const S = {
  token: null, staff: null, state: null, perms: new Set(), menu: { categories: [], items: [] },
  cart: [], discount: null, cat: "all", view: "dash", setupTab: "menu", reportTab: "daily", reportDay: null,
  stockFilter: "all", stockSearch: "", pending: null,
};
const $app = document.getElementById("app");

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cur = () => (S.state?.settings?.currency || "KES");
const money = (n) => cur() + " " + Number(n || 0).toLocaleString("en-KE", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const big = { g: ["kg", 1000], ml: ["L", 1000], pcs: ["pcs", 1] };
const bigUnit = (u) => (big[u] || ["", 1])[0];
const toBig = (q, u) => (q == null ? null : q / (big[u] || ["", 1])[1]);
const fromBig = (q, u) => (q === "" || q == null ? null : Number(q) * (big[u] || ["", 1])[1]);
function fmtQty(q, u, signed = false) {
  if (q == null) return "–";
  const v = toBig(q, u);
  const s = Number(v.toFixed(u === "pcs" ? 2 : 3)).toLocaleString("en-KE", { maximumFractionDigits: 3 });
  return (signed && v > 0 ? "+" : "") + s + " " + bigUnit(u);
}
const can = (p) => S.perms.has(p);
const isManager = () => can("reports.view");
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const daysAgo = (n) => new Date(Date.now() - n * 864e5 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const uuid = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") :
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};
function terminalId() {
  let t = store.get("cfc_terminal");
  if (!t) { t = "T-" + uuid().slice(0, 12); store.set("cfc_terminal", t); }
  return t;
}
const STATUS_LABEL = { out: "Out of stock", critical: "Critical", low: "Low stock", healthy: "Healthy", inactive: "Inactive" };
const STATUS_COLOUR = { out: "red", critical: "red", low: "amber", healthy: "green", inactive: "grey" };
const stBadge = (s) => `<span class="st ${STATUS_COLOUR[s] || "grey"}">${esc(STATUS_LABEL[s] || s)}</span>`;
const incomingBadge = (q, u) => (q > 0 ? `<span class="st blue">Incoming ${fmtQty(q, u)}</span>` : "");

class NetError extends Error {}
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}), "X-Terminal": terminalId() };
  if (S.token) headers["X-Token"] = S.token;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), opts.timeout || 20000);
  let res;
  try { res = await fetch(path, { ...opts, headers, signal: ctl.signal }); }
  catch { throw new NetError("Can't reach the POS server. Check the network cable / Wi-Fi."); }
  finally { clearTimeout(timer); }
  if (res.status === 401 && path !== "/api/login") { logout(true); throw new Error("Session expired. Log in again."); }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    if (res.status === 403 && /Change your PIN/.test(msg)) { renderChangePin(true); }
    const e = new Error(msg); e.status = res.status; throw e;
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res;
}

function toast(msg, bad = false) {
  let box = document.getElementById("toasts");
  if (!box) { box = document.createElement("div"); box.id = "toasts"; document.body.appendChild(box); }
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.setAttribute("role", bad ? "alert" : "status");
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => t.remove(), bad ? 7000 : 2500);
}
const fail = (e) => toast(e.message || String(e), true);

function modal(html, cls = "") {
  closeModal();
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.id = "modal";
  bg.innerHTML = `<div class="modal ${cls}" role="dialog" aria-modal="true">${html}</div>`;
  bg.addEventListener("click", (e) => { if (e.target === bg && !bg.dataset.locked) closeModal(); });
  document.body.appendChild(bg);
  const f = bg.querySelector("[autofocus]");
  if (f) setTimeout(() => f.focus(), 30);
  return bg;
}
const closeModal = () => document.getElementById("modal")?.remove();

function printHTML(html, receiptMode = false) {
  const area = document.getElementById("print-area");
  area.className = receiptMode ? "receipt-mode" : "";
  area.innerHTML = html;
  window.print();
}

async function download(path, name) {
  try {
    const res = await api(path);
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) { fail(e); }
}

// Connectivity indicator (LAN server reachability, not internet).
let offlineShown = false;
async function ping() {
  try { await fetch("/api/health", { cache: "no-store" }); if (offlineShown) { document.getElementById("offline")?.remove(); offlineShown = false; } }
  catch {
    if (!offlineShown) {
      const d = document.createElement("div"); d.id = "offline"; d.className = "offline";
      d.textContent = "No connection to the POS server — sales are paused until it's back. Nothing is lost.";
      document.body.appendChild(d); offlineShown = true;
    }
  }
}
setInterval(ping, 15000);
window.addEventListener("online", ping);
window.addEventListener("offline", ping);

// ------------------------------------------------------------------ session
async function boot() {
  S.token = store.get("cfc_token");
  if (!S.token) return renderLogin();
  try { await refreshState(); if (S.staff.must_change_pin) return renderChangePin(true); await loadMenu(); render(); }
  catch { renderLogin(); }
}
async function refreshState() {
  S.state = await api("/api/state");
  S.staff = S.state.staff;
  S.perms = new Set(S.state.permissions);
}
async function loadMenu() { if (can("pos.sell") || can("stock.view")) S.menu = await api("/api/menu"); }
function logout(silent) {
  if (!silent && S.token) api("/api/logout", { method: "POST" }).catch(() => {});
  S.token = null; S.staff = null; S.cart = []; S.discount = null; S.perms = new Set();
  store.set("cfc_token", null);
  closeModal();
  renderLogin();
}

function renderLogin() {
  let pin = "";
  $app.innerHTML = `
    <div class="login"><div class="card">
      <h1>${esc(S.state?.business_name || "City Food Court")}</h1><div class="muted">Enter your PIN</div>
      <div class="pin-dots" id="dots" aria-live="polite"></div>
      <div class="pinpad">
        ${[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => `<button data-k="${n}" aria-label="${n}">${n}</button>`).join("")}
        <button data-k="clr" aria-label="Clear">C</button><button data-k="0" aria-label="0">0</button><button data-k="ok" aria-label="OK" style="background:var(--brand);color:#fff">OK</button>
      </div>
      <div class="err" id="err" role="alert"></div>
    </div></div>`;
  const dots = () => { document.getElementById("dots").innerHTML = Array.from({ length: Math.max(4, pin.length) }, (_, i) => `<span class="${i < pin.length ? "on" : ""}"></span>`).join(""); };
  let busy = false;
  const submit = async () => {
    if (pin.length < 4 || busy) return;
    busy = true;
    try {
      const r = await api("/api/login", { method: "POST", json: { pin } });
      S.token = r.token; store.set("cfc_token", r.token);
      await refreshState();
      if (S.staff.must_change_pin) return renderChangePin(true, pin);
      S.view = can("pos.sell") ? "pos" : "dash";
      await loadMenu(); render();
    } catch (e) { pin = ""; dots(); document.getElementById("err").textContent = e.message; }
    finally { busy = false; }
  };
  const press = (k) => {
    document.getElementById("err").textContent = "";
    if (k === "clr") pin = "";
    else if (k === "ok") return submit();
    else if (pin.length < 6) pin += k;
    dots();
  };
  $app.querySelectorAll(".pinpad button").forEach((b) => b.addEventListener("click", () => press(b.dataset.k)));
  document.onkeydown = (e) => {
    if (S.token) return;
    if (/^\d$/.test(e.key)) press(e.key);
    else if (e.key === "Backspace") { pin = pin.slice(0, -1); dots(); }
    else if (e.key === "Enter") submit();
  };
  dots();
}

function renderChangePin(forced, current = "") {
  const m = modal(`<h2>${forced ? "Choose your own PIN" : "Change PIN"}</h2>
    ${forced ? `<div class="note">For security you must replace this PIN before continuing.</div>` : ""}
    <label>Current PIN</label><input id="cp" type="password" inputmode="numeric" maxlength="6" value="${esc(current)}" ${current ? "" : "autofocus"}>
    <label>New PIN (4–6 digits${isManager() ? ", not repeated or sequential" : ""})</label><input id="np" type="password" inputmode="numeric" maxlength="6" ${current ? "autofocus" : ""}>
    <label>Repeat new PIN</label><input id="np2" type="password" inputmode="numeric" maxlength="6">
    <div class="err" id="perr"></div>
    <div class="actions">${forced ? `<button class="btn" id="lo">Log out</button>` : `<button class="btn" id="x">Cancel</button>`}<button class="btn primary" id="s">Save PIN</button></div>`);
  if (forced) m.dataset.locked = "1";
  m.querySelector("#lo")?.addEventListener("click", () => logout());
  m.querySelector("#x")?.addEventListener("click", closeModal);
  m.querySelector("#s").onclick = async () => {
    const np = m.querySelector("#np").value;
    if (np !== m.querySelector("#np2").value) return (m.querySelector("#perr").textContent = "The new PINs don't match.");
    try {
      await api("/api/me/pin", { method: "POST", json: { current_pin: m.querySelector("#cp").value, new_pin: np } });
      closeModal(); toast("PIN changed");
      await refreshState(); await loadMenu(); S.view = can("pos.sell") ? "pos" : "dash"; render();
    } catch (e) { m.querySelector("#perr").textContent = e.message; }
  };
}

// ------------------------------------------------------------------ shell
const VIEWS = [
  ["dash", "Dashboard", () => can("stock.view")],
  ["pos", "POS", () => can("pos.sell")],
  ["orders", "Orders", () => can("pos.sell")],
  ["stock", "Stock", () => can("stock.view")],
  ["purchasing", "Purchasing", () => can("purchasing")],
  ["day", "Open / Close day", () => can("day.manage")],
  ["reports", "Reports", () => can("reports.view")],
  ["setup", "Setup", () => can("catalog.edit") || can("staff.manage")],
];

function licenceBanner() {
  const l = S.state.licence || {};
  if (!l.enforced) return isManager() ? `<div class="banner dev">Licence: development mode (not enforced)${l.state && l.state !== "unlicensed" ? " · " + esc(l.message || "") : ""}</div>` : "";
  if (["unlicensed", "invalid", "revoked", "expired", "wrong_server"].includes(l.state)) return `<div class="banner bad">${esc(l.message)} New sales are paused; records and reports remain available.</div>`;
  if (l.state === "grace" || (l.days_left != null && l.days_left <= 14)) return `<div class="banner warn">Licence: ${esc(l.message)}</div>`;
  return "";
}

function render() {
  const day = S.state.day;
  if (!VIEWS.find((v) => v[0] === S.view && v[2]())) S.view = (VIEWS.find((v) => v[2]()) || ["dash"])[0];
  const sh = S.state.shift;
  $app.innerHTML = `
    <header class="top">
      <div class="brand">${esc(S.state.business_name)}</div>
      <nav aria-label="Main">${VIEWS.filter((v) => v[2]()).map(([k, l]) => `<button data-v="${k}" class="${S.view === k ? "active" : ""}">${l}</button>`).join("")}</nav>
      <div class="who">
        ${can("shift.own") && day ? `<button id="shiftbtn" title="Cash shift">${sh ? "Shift ✓" : "Open shift"}</button>` : ""}
        <span>${esc(S.staff.name)} · ${esc(S.staff.role.replace("_", " "))}</span>
        <button id="pinbtn" title="Change PIN">PIN</button><button id="logout">Log out</button></div>
    </header>
    ${licenceBanner()}
    <div class="daybar ${day ? "open" : "closed"}">
      ${day ? `<b>Day open:</b> ${esc(day.business_date)} <span class="muted small">since ${esc(day.opened_at?.slice(11, 16) || "")}</span>`
            : `<b>No day open.</b> Sales and stock entries are locked until a manager opens the day.
               ${can("day.manage") ? `<button class="btn" id="goopen">Open day</button>` : ""}`}
    </div>
    <main id="view"></main>`;
  $app.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => { S.view = b.dataset.v; S.reportDay = null; render(); }));
  document.getElementById("logout").onclick = () => logout();
  document.getElementById("pinbtn").onclick = () => renderChangePin(false);
  document.getElementById("shiftbtn")?.addEventListener("click", shiftModal);
  document.getElementById("goopen")?.addEventListener("click", () => { S.view = "day"; render(); });
  document.onkeydown = globalKeys;
  const v = document.getElementById("view");
  ({ dash: viewDashboard, pos: viewPOS, orders: viewOrders, stock: viewStock, purchasing: viewPurchasing,
     day: viewDay, reports: viewReports, setup: viewSetup }[S.view] || viewDashboard)(v);
}

// Windows keyboard workflow: F2 search/scan, F4 cash, F7 M-Pesa, F8 card, Esc close dialog.
function globalKeys(e) {
  if (!S.token) return;
  if (e.key === "Escape" && document.getElementById("modal") && !document.getElementById("modal").dataset.locked) { closeModal(); return; }
  if (S.view !== "pos" || document.getElementById("modal")) return;
  const map = { F4: "cash", F7: "mpesa", F8: "card" };
  if (e.key === "F2") { e.preventDefault(); document.getElementById("scan")?.focus(); }
  else if (map[e.key] && S.cart.length && S.state.day) { e.preventDefault(); payModal(map[e.key]); }
}

// ------------------------------------------------------------------ dashboard
async function viewDashboard(el) {
  el.innerHTML = `<div class="empty">Loading…</div>`;
  let d;
  try { d = await api("/api/dashboard"); } catch (e) { el.innerHTML = ""; return fail(e); }
  const c = d.stock_counts;
  const card = (cls, v, l, f) => `<button class="statcard ${cls}" data-f="${f}"><div class="v">${v}</div><div class="small">${l}</div></button>`;
  const t = d.today;
  const maxTrend = Math.max(1, ...(d.trend || []).map((x) => x.sales));
  el.innerHTML = `
    ${t ? `<div class="kpis">
      <div class="kpi"><div class="l">Sales today (collected)</div><div class="v">${money(t.collected - t.refunds)}</div></div>
      <div class="kpi"><div class="l">Orders</div><div class="v">${t.orders}</div></div>
      <div class="kpi"><div class="l">Gross profit (est.)</div><div class="v">${money(t.gross_profit)}</div></div>
      <div class="kpi"><div class="l">Stock value</div><div class="v">${money(d.valuation)}</div></div>
    </div>` : ""}
    <h3>Stock status</h3>
    <div class="statcards">
      ${card("red", c.out + c.critical, "Critical / out of stock", "red")}
      ${card("amber", c.low, "Low stock", "amber")}
      ${card("green", c.healthy, "Healthy", "green")}
      ${card("blue", d.incoming_items, "Incoming (ordered)", "blue")}
      ${card("grey", c.inactive, "Inactive", "grey")}
    </div>
    <div class="grid2">
      <div class="panel"><h3>Needs attention</h3>
        ${d.alerts.length ? `<div class="tablewrap"><table><thead><tr><th>Item</th><th>Status</th><th class="num">On hand</th><th class="num">Reorder at</th><th class="num">Suggested order</th></tr></thead><tbody>
          ${d.alerts.map((a) => `<tr class="st-row-${STATUS_COLOUR[a.status]}"><td>${esc(a.name)}</td><td>${stBadge(a.status)} ${incomingBadge(a.incoming, a.unit)}</td>
            <td class="num">${fmtQty(a.on_hand, a.unit)}</td><td class="num">${fmtQty(a.par_level, a.unit)}</td>
            <td class="num">${a.suggested_order != null ? fmtQty(a.suggested_order, a.unit) : `<span class="muted small">needs 3+ days of history</span>`}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="muted">Nothing is low. 👍</div>`}
      </div>
      <div class="panel">
        ${d.trend ? `<h3>Sales, recent days</h3><div class="bars" aria-label="Sales by day">${d.trend.map((x) =>
          `<div style="height:${Math.round((x.sales / maxTrend) * 100)}%" title="${esc(x.business_date)}: ${money(x.sales)}${x.source === "workbook" ? " (imported – sold on the other POS)" : ""}"><span>${esc(x.business_date.slice(8))}</span></div>`).join("")}</div>
          <div class="small muted" style="margin-top:22px">Imported workbook days show 0: those sales were rung up on the other POS.</div>` : ""}
        ${d.open_shifts ? `<h3 style="margin-top:14px">Open cash shifts</h3>${d.open_shifts.length ? `<table><tbody>${d.open_shifts.map((s) =>
          `<tr><td>${esc(s.staff_name)}</td><td class="small muted">since ${esc(s.opened_at.slice(11, 16))}</td><td class="num">${money(s.expected_cash_now)} expected in drawer</td></tr>`).join("")}</tbody></table>` : `<div class="muted">None</div>`}` : ""}
        ${d.open_pos != null ? `<div class="small muted" style="margin-top:10px">${d.open_pos} purchase order(s) waiting for delivery.</div>` : ""}
      </div>
    </div>`;
  el.querySelectorAll("[data-f]").forEach((b) => b.addEventListener("click", () => { S.stockFilter = b.dataset.f; S.view = "stock"; render(); }));
}

// ------------------------------------------------------------------ POS
function viewPOS(el) {
  const cats = S.menu.categories.filter((c) => S.menu.items.some((i) => i.category_id === c.id));
  const items = S.menu.items.filter((i) => S.cat === "all" || i.category_id === S.cat);
  const needShift = S.state.settings.require_shift === "1" && !S.state.shift && S.state.day;
  el.innerHTML = `
    ${needShift ? `<div class="note"><b>Open your cash shift to start selling.</b> <button class="btn primary" id="os">Open shift</button></div>` : ""}
    <div class="pos">
      <div>
        <div class="scan"><input id="scan" placeholder="Scan barcode or type item name / SKU, then Enter  (F2)" autocomplete="off" aria-label="Scan or search"></div>
        <div class="cats">
          <button data-c="all" class="${S.cat === "all" ? "active" : ""}">All</button>
          ${cats.map((c) => `<button data-c="${c.id}" class="${S.cat === c.id ? "active" : ""}">${esc(c.name)}</button>`).join("")}
        </div>
        <div class="items" id="items"></div>
        <div class="small muted keys-hint" style="margin-top:8px">Keys: <span class="kbd">F2</span> scan/search · <span class="kbd">F4</span> cash · <span class="kbd">F7</span> M-Pesa · <span class="kbd">F8</span> card · <span class="kbd">Esc</span> close</div>
      </div>
      <div class="panel cart" id="cart"></div>
    </div>`;
  const drawItems = (list) => {
    document.getElementById("items").innerHTML = list.map((i) => `<button class="item ${i.stock_status === "out" ? "out" : ""}" data-i="${i.id}">
        <span class="n">${esc(i.name)}</span>
        <span><span class="p">${money(i.price)}</span>
          ${i.stock_status ? `<div class="stk">${stBadge(i.stock_status)}</div>` : ""}
          ${i.recipe_lines ? "" : `<div class="nr">no recipe – stock not tracked</div>`}</span>
      </button>`).join("") || `<div class="empty">No menu items. ${can("catalog.edit") ? "Add them in Setup or import the Excel sheet." : ""}</div>`;
    el.querySelectorAll(".item").forEach((b) => b.addEventListener("click", () => addToCart(Number(b.dataset.i))));
  };
  drawItems(items);
  document.getElementById("os")?.addEventListener("click", shiftModal);
  el.querySelectorAll(".cats button").forEach((b) => b.addEventListener("click", () => { S.cat = b.dataset.c === "all" ? "all" : Number(b.dataset.c); viewPOS(el); }));
  const scan = document.getElementById("scan");
  scan.addEventListener("input", () => {
    const q = scan.value.trim().toLowerCase();
    drawItems(q ? S.menu.items.filter((i) => i.name.toLowerCase().includes(q) || (i.sku || "").toLowerCase() === q || i.barcode === q) : items);
  });
  scan.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const q = scan.value.trim();
    if (!q) return;
    const lq = q.toLowerCase();
    const hit = S.menu.items.find((i) => i.barcode === q) || S.menu.items.find((i) => (i.sku || "").toLowerCase() === lq)
      || (() => { const m = S.menu.items.filter((i) => i.name.toLowerCase().includes(lq)); return m.length === 1 ? m[0] : null; })();
    if (hit) { addToCart(hit.id); scan.value = ""; drawItems(items); }
    else toast(`No single item matches "${q}"`, true);
  });
  if (matchMedia("(pointer: fine)").matches) scan.focus();  // keyboard/scanner tills; don't pop the keyboard on tablets
  renderCart();
}

function cartChanged() { S.pending = null; renderCart(); }
function addToCart(id) {
  const line = S.cart.find((l) => l.id === id);
  if (line) line.qty++;
  else { const it = S.menu.items.find((i) => i.id === id); S.cart.push({ id, name: it.name, price: it.price, qty: 1 }); }
  cartChanged();
}

// Display-only estimate; the server recalculates everything from its own prices.
function cartTotals() {
  const sub = S.cart.reduce((s, l) => s + l.price * l.qty, 0);
  let disc = 0;
  if (S.discount) disc = S.discount.type === "pct" ? Math.round(sub * S.discount.value) / 100 : Math.min(S.discount.value, sub);
  const net = sub - disc;
  const rate = Number(S.state.settings.tax_rate_pct || 0);
  const incl = S.state.settings.tax_inclusive === "1";
  const tax = rate > 0 ? (incl ? net * rate / (100 + rate) : net * rate / 100) : 0;
  return { sub, disc, tax: Math.round(tax * 100) / 100, total: Math.round((incl || !rate ? net : net + tax) * 100) / 100, rate, incl };
}

function renderCart() {
  const el = document.getElementById("cart");
  if (!el) return;
  const t = cartTotals();
  let bar = document.getElementById("cartbar");
  if (!bar) { bar = document.createElement("button"); bar.id = "cartbar"; bar.className = "cartbar"; document.getElementById("view").appendChild(bar);
    bar.addEventListener("click", () => el.scrollIntoView({ behavior: "smooth" })); }
  bar.textContent = S.cart.length ? `View order · ${S.cart.reduce((n, l) => n + l.qty, 0)} items · ${money(t.total)}` : "";
  bar.hidden = !S.cart.length;
  const label = S.state.settings.tax_label || "VAT";
  el.innerHTML = `
    <h3>Current order</h3>
    <div class="lines">
      ${S.cart.map((l, i) => `
        <div class="line">
          <div><div><b>${esc(l.name)}</b></div><div class="muted small">${money(l.price)} each</div></div>
          <div class="qty"><button data-m="${i}" aria-label="One less">−</button><b>${l.qty}</b><button data-p="${i}" aria-label="One more">+</button></div>
          <div class="num"><b>${money(l.price * l.qty)}</b></div>
        </div>`).join("") || `<div class="empty">Tap or scan items to add them</div>`}
    </div>
    ${t.disc ? `<div class="row small" style="justify-content:space-between;display:flex"><span>Discount (${esc(S.discount.reason || "")})</span><span class="num">−${money(t.disc)}</span></div>` : ""}
    ${t.rate ? `<div class="row small muted" style="justify-content:space-between;display:flex"><span>${esc(label)} ${t.rate}% ${t.incl ? "(included)" : ""}</span><span class="num">${money(t.tax)}</span></div>` : ""}
    <div class="total"><span>Total</span><span class="num">${money(t.total)}</span></div>
    ${S.pending ? `<div class="note"><b>Last payment didn't get a reply.</b> Retrying is safe — it can't charge twice.
        <button class="btn primary" id="retry">Retry payment</button></div>` : ""}
    ${S.state.day ? `
      <div class="paybtns">
        <button class="btn big ok" data-pay="cash" ${S.cart.length ? "" : "disabled"}>Cash</button>
        <button class="btn big ok" data-pay="mpesa" ${S.cart.length ? "" : "disabled"}>M-Pesa</button>
        <button class="btn big ok" data-pay="card" ${S.cart.length ? "" : "disabled"}>Card</button>
      </div>` : `<div class="note">Open the day to start selling.</div>`}
    ${S.cart.length ? `<div class="row" style="margin-top:8px"><button class="btn" id="disc">${S.discount ? "Change discount" : "Discount"}</button>
       <button class="btn danger" id="clear">Clear order</button></div>` : ""}`;
  el.querySelectorAll("[data-p]").forEach((b) => b.addEventListener("click", () => { S.cart[b.dataset.p].qty++; cartChanged(); }));
  el.querySelectorAll("[data-m]").forEach((b) => b.addEventListener("click", () => {
    const l = S.cart[b.dataset.m]; l.qty--; if (l.qty <= 0) S.cart.splice(b.dataset.m, 1); cartChanged();
  }));
  el.querySelectorAll("[data-pay]").forEach((b) => b.addEventListener("click", () => payModal(b.dataset.pay)));
  document.getElementById("clear")?.addEventListener("click", () => { S.cart = []; S.discount = null; cartChanged(); });
  document.getElementById("disc")?.addEventListener("click", discountModal);
  document.getElementById("retry")?.addEventListener("click", () => submitOrder(S.pending.body));
}

function discountModal() {
  const d = S.discount || { type: "pct", value: "", reason: "" };
  const lim = Number(S.state.settings.cashier_max_discount_pct || 0);
  const m = modal(`<h2>Discount</h2>
    <div class="row"><div><label>Type</label><select id="dt"><option value="pct" ${d.type === "pct" ? "selected" : ""}>Percent %</option><option value="amount" ${d.type === "amount" ? "selected" : ""}>Amount (${esc(cur())})</option></select></div>
      <div><label>Value</label><input id="dv" type="number" min="0" step="any" inputmode="decimal" value="${esc(d.value)}" autofocus></div></div>
    <label>Reason (required)</label><input id="dr" value="${esc(d.reason)}" maxlength="200" placeholder="e.g. regular customer, complaint">
    ${isManager() ? "" : `<div class="small muted" style="margin-top:6px">Discounts over ${lim}% need a manager to enter their PIN at payment.</div>`}
    <div class="actions">${S.discount ? `<button class="btn danger" id="rm">Remove discount</button>` : ""}<button class="btn" id="x">Cancel</button><button class="btn primary" id="ok">Apply</button></div>`);
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#rm")?.addEventListener("click", () => { S.discount = null; closeModal(); cartChanged(); });
  m.querySelector("#ok").onclick = () => {
    const v = Number(m.querySelector("#dv").value), reason = m.querySelector("#dr").value.trim();
    if (!(v > 0)) return toast("Enter a discount value", true);
    if (!reason) return toast("Give a reason", true);
    S.discount = { type: m.querySelector("#dt").value, value: v, reason };
    closeModal(); cartChanged();
  };
}

function needsApproval() {
  if (!S.discount || isManager()) return false;
  const t = cartTotals();
  return t.sub > 0 && (t.disc / t.sub) * 100 > Number(S.state.settings.cashier_max_discount_pct || 0) + 1e-9;
}

function payModal(method) {
  if (S.state.settings.require_shift === "1" && !S.state.shift) return shiftModal();
  const total = cartTotals().total;
  const quick = [...new Set([total, Math.ceil(total / 100) * 100, Math.ceil(total / 500) * 500, Math.ceil(total / 1000) * 1000])];
  const body = {
    cash: `<label>Cash received</label><input id="tend" type="number" inputmode="decimal" min="0" step="any" autofocus>
           <div class="quick">${quick.map((q) => `<button class="btn" data-q="${q}">${q.toLocaleString()}</button>`).join("")}</div>
           <div style="margin-top:10px;font-size:18px">Change: <b id="chg">–</b></div>`,
    mpesa: `<div class="info">Ask the customer to pay <b>${money(total)}</b>. Check the M-Pesa message on the business phone, then type its confirmation code.
              <br><span class="small">The code is checked for format and re-use only; it is not verified with Safaricom.</span></div>
            <label>M-Pesa code (10 characters)</label><input id="ref" maxlength="10" autofocus autocomplete="off" style="text-transform:uppercase;font-size:20px;letter-spacing:2px">`,
    card: `<div class="info">Charge <b>${money(total)}</b> on the card machine and wait for "APPROVED", then confirm.</div>
           <label>Card slip / approval no. (optional)</label><input id="ref" maxlength="40" autofocus>`,
  }[method];
  const m = modal(`
    <h2>${{ cash: "Cash", mpesa: "M-Pesa", card: "Card" }[method]} payment · ${money(total)}</h2>
    ${body}
    ${needsApproval() ? `<label>Manager PIN to approve the discount</label><input id="apin" type="password" inputmode="numeric" maxlength="6">` : ""}
    <div class="err" id="perr"></div>
    <div class="actions"><button class="btn" id="cancel">Cancel</button><button class="btn primary big" id="confirm">Confirm payment</button></div>`);
  const tend = m.querySelector("#tend");
  const upd = () => { const v = Number(tend.value); m.querySelector("#chg").textContent = v >= total ? money(v - total) : "–"; };
  tend?.addEventListener("input", upd);
  m.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("click", () => { tend.value = b.dataset.q; upd(); }));
  m.querySelector("#cancel").onclick = closeModal;
  const confirm = () => {
    if (S.pending) return submitOrder(S.pending.body, m);  // never create a second key for the same cart
    const body = {
      items: S.cart.map((l) => ({ menu_item_id: l.id, qty: l.qty })),
      payment_method: method,
      amount_tendered: method === "cash" ? Number(tend.value || 0) : null,
      payment_ref: m.querySelector("#ref")?.value || null,
      discount: S.discount, approver_pin: m.querySelector("#apin")?.value || null,
      client_ref: uuid(),
    };
    submitOrder(body, m);
  };
  m.querySelector("#confirm").onclick = confirm;
  m.querySelectorAll("input").forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") confirm(); }));
}

let submitting = false;
async function submitOrder(body, m) {
  if (submitting) return;
  submitting = true;
  const btn = m?.querySelector("#confirm");
  if (btn) btn.disabled = true;
  try {
    const order = await api("/api/orders", { method: "POST", json: body, timeout: 15000 });
    S.cart = []; S.discount = null; S.pending = null;
    renderCart();
    receiptModal(order);
    refreshState().then(() => { if (S.view === "pos") document.querySelector("#shiftbtn") && (document.querySelector("#shiftbtn").textContent = "Shift ✓"); }).catch(() => {});
  } catch (e) {
    if (e instanceof NetError) {
      S.pending = { body };  // same client_ref on retry -> server returns the original sale if it went through
      closeModal(); renderCart();
      toast("No reply from the server. Press 'Retry payment' when the connection is back — it can't charge twice.", true);
    } else {
      if (e.status === 409) S.pending = null;
      if (m && document.body.contains(m)) { m.querySelector("#perr").textContent = e.message; if (btn) btn.disabled = false; }
      else fail(e);
      if (e.status !== 409 && body.client_ref) body.client_ref = uuid();  // rejected = nothing saved; a fresh key is safe
    }
  } finally { submitting = false; }
}

function receiptHTML(o) {
  const L = (a, b) => `<div class="r"><span>${a}</span><span>${b}</span></div>`;
  const s = S.state.settings;
  return `<div class="receipt">
    <div class="c"><b style="font-size:15px">${esc(S.state.business_name)}</b><br>${esc(o.business_address || s.business_address || "")}
      ${o.business_pin ? `<br>PIN: ${esc(o.business_pin)}` : ""}</div><hr>
    ${L("Receipt #" + o.number, esc(o.business_date))}
    ${L(esc((o.created_at || "").slice(11, 16)), "Served by " + esc(o.cashier))}<hr>
    ${o.items.map((i) => L(`${i.qty} × ${esc(i.name)}`, Number(i.price * i.qty).toLocaleString())).join("")}<hr>
    ${o.discount ? L("Subtotal", money(o.subtotal)) + L("Discount", "−" + money(o.discount)) : ""}
    ${o.tax ? L(`${esc(o.tax_label || "VAT")} ${o.tax_rate}%${o.tax_inclusive ? " incl." : ""}`, money(o.tax)) : ""}
    ${L("<b>TOTAL</b>", "<b>" + money(o.total) + "</b>")}
    ${L("Paid by", { cash: "Cash", mpesa: "M-Pesa", card: "Card" }[o.payment_method])}
    ${o.payment_method === "cash" ? L("Cash", money(o.amount_tendered)) + L("Change", money(o.change)) : ""}
    ${o.payment_ref ? L("Ref", esc(o.payment_ref)) : ""}
    ${o.refunded ? `<hr>${L("Refunded", "−" + money(o.refunded))}` : ""}
    ${o.status === "void" ? `<hr><div class="c"><b>*** VOID ***</b><br>${esc(o.void_reason)}</div>` : ""}
    <hr><div class="c">${esc(S.state.receipt_footer)}</div>
    ${o.tax ? `<div class="c small">Not a KRA eTIMS fiscal receipt</div>` : ""}
  </div>`;
}

function receiptModal(o) {
  const m = modal(`
    <h2>${o.status === "void" ? "Voided order" : o.duplicate ? "Already paid ✓ (no second charge)" : "Paid ✓"} ${o.payment_method === "cash" && o.status !== "void" ? `· Change ${money(o.change)}` : ""}</h2>
    ${receiptHTML(o)}
    <div class="actions"><button class="btn" id="print">Print receipt</button><button class="btn primary big" id="done" autofocus>New order</button></div>`);
  m.querySelector("#print").onclick = () => printHTML(receiptHTML(o), true);
  m.querySelector("#done").onclick = () => { closeModal(); document.getElementById("scan")?.focus(); };
}

// ------------------------------------------------------------------ shifts
async function shiftModal() {
  await refreshState().catch(() => {});
  const sh = S.state.shift;
  if (!sh) {
    const m = modal(`<h2>Open cash shift</h2>
      <div class="info">Count the cash in your drawer before your first sale (the float). At the end you count again and the system shows any difference.</div>
      <label>Opening float (${esc(cur())})</label><input id="fl" type="number" min="0" step="any" inputmode="decimal" value="0" autofocus>
      <div class="err" id="serr"></div>
      <div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="ok">Open shift</button></div>`);
    m.querySelector("#x").onclick = closeModal;
    m.querySelector("#ok").onclick = async () => {
      try { await api("/api/shifts/open", { method: "POST", json: { opening_float: Number(m.querySelector("#fl").value || 0) } }); closeModal(); await refreshState(); toast("Shift opened"); render(); }
      catch (e) { m.querySelector("#serr").textContent = e.message; }
    };
    return;
  }
  const m = modal(`<h2>Your shift</h2>
    <table><tbody>
      <tr><td>Opened</td><td class="num">${esc(sh.opened_at)}</td></tr>
      <tr><td>Float</td><td class="num">${money(sh.opening_float)}</td></tr>
      <tr><td>Cash sales</td><td class="num">${money(sh.sales_by_method.cash)}</td></tr>
      <tr><td>Cash refunds paid out</td><td class="num">−${money(sh.refunds_by_method.cash)}</td></tr>
      <tr><td><b>Cash expected in drawer</b></td><td class="num"><b>${money(sh.expected_cash_now)}</b></td></tr>
      <tr><td class="muted">M-Pesa / Card (not in drawer)</td><td class="num muted">${money(sh.sales_by_method.mpesa)} / ${money(sh.sales_by_method.card)}</td></tr>
      <tr><td class="muted">Orders</td><td class="num muted">${sh.orders}</td></tr>
    </tbody></table>
    <h3 style="margin-top:14px">Close shift</h3>
    <label>Cash counted in drawer</label><input id="cc" type="number" min="0" step="any" inputmode="decimal">
    <label>Note (required if it doesn't match)</label><input id="cn" maxlength="300">
    <div class="err" id="serr"></div>
    <div class="actions"><button class="btn" id="x">Keep shift open</button><button class="btn primary" id="ok">Close shift</button></div>`);
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#ok").onclick = async () => {
    const v = m.querySelector("#cc").value;
    if (v === "") return (m.querySelector("#serr").textContent = "Count the cash first.");
    try {
      const r = await api(`/api/shifts/${sh.id}/close`, { method: "POST", json: { counted_cash: Number(v), notes: m.querySelector("#cn").value || null } });
      closeModal(); await refreshState(); render();
      toast(r.cash_variance ? `Shift closed. Cash ${r.cash_variance > 0 ? "over" : "short"} by ${money(Math.abs(r.cash_variance))}` : "Shift closed. Cash matches ✓", !!r.cash_variance);
    } catch (e) { m.querySelector("#serr").textContent = e.message; }
  };
}

// ------------------------------------------------------------------ orders, voids, refunds
async function viewOrders(el) {
  el.innerHTML = `<div class="panel"><h2>Today's orders</h2>
    ${can("pos.refund") ? `<div class="row" style="max-width:520px;margin-bottom:10px"><div><input id="oq" placeholder="Older order: M-Pesa code, or 2026-10-18#12"></div><div class="shrink"><button class="btn" id="og">Find</button></div></div>` : ""}
    <div id="ol" class="tablewrap">Loading…</div></div>`;
  document.getElementById("og")?.addEventListener("click", async () => {
    try {
      const rows = await api("/api/orders/search?q=" + encodeURIComponent(document.getElementById("oq").value.trim()));
      if (!rows.length) return toast("No order found", true);
      receiptActions(rows[0], el);
    } catch (e) { fail(e); }
  });
  try {
    const rows = await api("/api/orders");
    const paid = rows.filter((r) => r.status === "paid");
    document.getElementById("ol").innerHTML = rows.length ? `
      <div class="kpis">
        <div class="kpi"><div class="l">Orders</div><div class="v">${paid.length}</div></div>
        <div class="kpi"><div class="l">Sales (after refunds)</div><div class="v">${money(paid.reduce((s, r) => s + r.total - r.refunded, 0))}</div></div>
        <div class="kpi"><div class="l">Voids</div><div class="v">${rows.length - paid.length}</div></div>
      </div>
      <table><thead><tr><th>#</th><th>Time</th><th>Items</th><th>Paid by</th><th class="num">Total</th><th>Cashier</th><th></th></tr></thead><tbody>
      ${rows.map((r) => `<tr class="${r.status === "void" ? "void" : ""}">
        <td>${r.number}</td><td>${esc(r.created_at.slice(11, 16))}</td><td class="small">${esc(r.summary)}</td>
        <td>${esc(r.payment_method)}${r.payment_ref ? `<div class="small muted">${esc(r.payment_ref)}</div>` : ""}</td>
        <td class="num">${money(r.total)}${r.discount ? `<div class="small muted">disc ${money(r.discount)}</div>` : ""}${r.refunded ? `<div class="small" style="color:var(--bad)">refunded ${money(r.refunded)}</div>` : ""}</td>
        <td>${esc(r.cashier)}</td>
        <td style="white-space:nowrap"><button class="btn" data-r="${r.id}">Open</button>
          ${r.status === "void" ? `<span class="tag grey" title="${esc(r.void_reason)}">void</span>` : ""}</td>
      </tr>`).join("")}</tbody></table>` : `<div class="empty">No orders yet today.</div>`;
    el.querySelectorAll("[data-r]").forEach((b) => b.addEventListener("click", async () => {
      try { receiptActions(await api("/api/orders/" + b.dataset.r), el); } catch (e) { fail(e); }
    }));
  } catch (e) { fail(e); }
}

function receiptActions(o, el) {
  const canVoid = can("pos.void") && o.status === "paid" && !o.refunded && S.state.day && o.day_id === S.state.day.id;
  const canRefund = o.status === "paid" && o.items.some((i) => i.refundable_qty > 0) && S.state.day;
  const m = modal(`${receiptHTML(o)}
    ${o.refunds?.length ? `<h3 style="margin-top:12px">Refunds</h3>${o.refunds.map((r) => `<div class="small">${esc(r.created_at)} · ${money(r.amount)} ${esc(r.method)} · ${esc(r.reason)}${r.restocked ? " · restocked" : ""}</div>`).join("")}` : ""}
    <div class="actions"><button class="btn" id="pr">Print</button>
      ${canVoid ? `<button class="btn danger" id="vo">Void</button>` : ""}
      ${canRefund ? `<button class="btn danger" id="rf">Refund…</button>` : ""}
      <button class="btn primary" id="x">Close</button></div>`);
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#pr").onclick = () => printHTML(receiptHTML(o), true);
  m.querySelector("#vo")?.addEventListener("click", () => voidModal(o, el));
  m.querySelector("#rf")?.addEventListener("click", () => refundModal(o, el));
}

function voidModal(o, el) {
  const m = modal(`<h2>Void order #${o.number}</h2><div class="note">Voiding cancels the whole sale, returns its ingredients to stock and removes it from sales. Logged with your name.</div>
    <label>Reason</label><input id="vr" autofocus maxlength="200" placeholder="e.g. customer cancelled, wrong item rung up">
    <div class="err" id="verr"></div>
    <div class="actions"><button class="btn" id="c">Cancel</button><button class="btn primary" id="ok">Void order</button></div>`);
  m.querySelector("#c").onclick = closeModal;
  m.querySelector("#ok").onclick = async () => {
    try { await api(`/api/orders/${o.id}/void`, { method: "POST", json: { reason: m.querySelector("#vr").value } }); closeModal(); toast("Order voided"); viewOrders(el); }
    catch (e) { m.querySelector("#verr").textContent = e.message; }
  };
}

function refundModal(o, el) {
  const key = uuid();
  const m = modal(`<h2>Refund · receipt #${o.number} (${esc(o.business_date)})</h2>
    <table><thead><tr><th>Item</th><th class="num">Sold</th><th class="num">Can refund</th><th style="width:110px">Refund qty</th></tr></thead><tbody>
      ${o.items.map((i) => `<tr><td>${esc(i.name)}</td><td class="num">${i.qty}</td><td class="num">${i.refundable_qty}</td>
        <td><input type="number" min="0" max="${i.refundable_qty}" step="1" data-ri="${i.id}" value="0" ${i.refundable_qty ? "" : "disabled"}></td></tr>`).join("")}
    </tbody></table>
    <div class="row"><div><label>Reason</label><select id="rr">${S.state.refund_reasons.map((r) => `<option>${esc(r)}</option>`).join("")}</select></div>
      <div><label>Money returned by</label><select id="rm">${["cash", "mpesa", "card"].map((x) => `<option value="${x}" ${x === o.payment_method ? "selected" : ""}>${x}</option>`).join("")}</select></div></div>
    <label><input type="checkbox" id="rs" style="width:auto;min-height:0"> Put the items back in stock (only if unused, e.g. sealed drinks)</label>
    ${isManager() ? "" : `<label>Manager PIN to approve</label><input id="ap" type="password" inputmode="numeric" maxlength="6">`}
    <div class="small muted">Refund amount is the share of what the customer actually paid (after any discount).</div>
    <div class="err" id="rerr"></div>
    <div class="actions"><button class="btn" id="c">Cancel</button><button class="btn primary" id="ok">Refund</button></div>`, "wide");
  m.querySelector("#c").onclick = closeModal;
  m.querySelector("#ok").onclick = async () => {
    const lines = [...m.querySelectorAll("[data-ri]")].map((i) => ({ order_item_id: Number(i.dataset.ri), qty: Number(i.value || 0) })).filter((l) => l.qty > 0);
    if (!lines.length) return (m.querySelector("#rerr").textContent = "Enter a quantity to refund.");
    try {
      const r = await api(`/api/orders/${o.id}/refund`, { method: "POST", json: {
        lines, reason: m.querySelector("#rr").value, restock: m.querySelector("#rs").checked, method: m.querySelector("#rm").value,
        approver_pin: m.querySelector("#ap")?.value || null, client_ref: key } });
      closeModal(); toast(`Refunded. Total refunded on this order: ${money(r.refunded)}`); await refreshState(); viewOrders(el);
    } catch (e) { m.querySelector("#rerr").textContent = e.message; }
  };
}

// ------------------------------------------------------------------ stock
async function viewStock(el) {
  el.innerHTML = `<div id="st">Loading…</div>`;
  try {
    const [ov, stock, log] = await Promise.all([api("/api/stock/overview"), api("/api/stock"), api("/api/stock/log")]);
    const active = ov.filter((i) => i.active);
    const lines = Object.fromEntries((stock.lines || []).map((l) => [l.id, l]));
    const opts = active.map((i) => `<option value="${i.id}" data-u="${i.unit}">${esc(i.name)} (${bigUnit(i.unit)})</option>`).join("");
    const counts = { all: ov.length, red: 0, amber: 0, green: 0, blue: 0, grey: 0 };
    ov.forEach((o) => { counts[STATUS_COLOUR[o.status]]++; if (o.incoming > 0 && o.active) counts.blue++; });
    const chip = (k, label) => `<button data-sf="${k}" class="${S.stockFilter === k ? "active" : ""}">${k === "all" ? "" : `<span class="st ${k}">${label}</span>`}${k === "all" ? "All" : ""} ${counts[k]}</button>`;
    document.getElementById("st").innerHTML = `
      ${!stock.day ? `<div class="note">No day is open: showing last night's counted stock. Recording waste, deliveries and adjustments needs an open day.</div>` : ""}
      <div class="grid2">
        <div class="panel"><h3>Record waste / spoilage</h3>
          <div class="small muted">Record it when it happens (e.g. chapati that overstayed), so it doesn't show up as missing stock at closing.</div>
          <div class="row"><div><label>Item</label><select id="wi">${opts}</select></div>
            <div><label>Quantity (<span id="wu">${active[0] ? bigUnit(active[0].unit) : ""}</span>)</label><input id="wq" type="number" min="0" step="any" inputmode="decimal"></div></div>
          <div class="row"><div><label>Reason</label><select id="wr">${S.state.waste_reasons.map((r) => `<option>${esc(r)}</option>`).join("")}</select></div>
            <div><label>Note</label><input id="wn" placeholder="optional" maxlength="300"></div></div>
          <div style="margin-top:10px"><button class="btn primary" id="wsave" ${stock.day ? "" : "disabled"}>Save waste</button></div>
        </div>
        <div class="panel"><h3>Receive goods (no purchase order)</h3>
          ${can("stock.receive") ? `
          <div class="small muted">Enter what actually arrived (weigh / count it), not what's on the invoice. Deliveries against a purchase order are received under Purchasing.</div>
          <div class="row"><div><label>Item</label><select id="di">${opts}</select></div>
            <div><label>Quantity (<span id="du">${active[0] ? bigUnit(active[0].unit) : ""}</span>)</label><input id="dq" type="number" min="0" step="any" inputmode="decimal"></div></div>
          <div class="row"><div><label>Cost per <span id="dcu">${active[0] ? bigUnit(active[0].unit) : ""}</span> (optional)</label><input id="dc" type="number" min="0" step="any" inputmode="decimal"></div>
            <div><label>Supplier / invoice no.</label><input id="dn" placeholder="optional" maxlength="300"></div></div>
          <div style="margin-top:10px"><button class="btn primary" id="dsave" ${stock.day ? "" : "disabled"}>Save delivery</button></div>`
          : `<div class="muted">A manager or stock controller receives deliveries.</div>`}
          ${can("stock.adjust") ? `<h3 style="margin-top:16px">Adjust stock (manager)</h3>
          <div class="row"><div><label>Item</label><select id="ai">${opts}</select></div>
            <div><label>+ add / − remove (<span id="au">${active[0] ? bigUnit(active[0].unit) : ""}</span>)</label><input id="aq" type="number" step="any" inputmode="decimal"></div></div>
          <div class="row"><div><label>Reason</label><select id="ar">${S.state.adjust_reasons.map((r) => `<option>${esc(r)}</option>`).join("")}</select></div>
            <div><label>Note</label><input id="an" maxlength="300"></div></div>
          <div style="margin-top:10px"><button class="btn" id="asave" ${stock.day ? "" : "disabled"}>Save adjustment</button></div>` : ""}
        </div>
      </div>
      <div class="panel">
        <h3>Stock levels</h3>
        <div class="legend"><span class="st red">Critical / out</span><span class="st amber">Low (at or below reorder level)</span><span class="st green">Healthy</span><span class="st blue">Incoming – ordered, not yet here, never counted as stock</span><span class="st grey">Inactive</span></div>
        <div class="chips">${chip("all")}${chip("red", "Critical")}${chip("amber", "Low")}${chip("green", "Healthy")}${chip("blue", "Incoming")}${chip("grey", "Inactive")}</div>
        <input id="ss" placeholder="Search item…" value="${esc(S.stockSearch)}" style="max-width:320px;margin-bottom:8px">
        <div class="small muted" style="margin-bottom:8px">On hand = opening + received + adjustments − used by sales − waste (what <i>should</i> be there; the real count happens at closing).</div>
        <div class="tablewrap" id="stt"></div>
      </div>
      <div class="panel"><h3>Today's deliveries, waste & adjustments</h3>
        ${log.length ? `<div class="tablewrap"><table><thead><tr><th>Time</th><th>Type</th><th>Item</th><th class="num">Qty</th><th>Reason / note</th><th>By</th></tr></thead><tbody>
        ${log.map((m) => `<tr><td>${esc(m.created_at.slice(11, 16))}</td><td><span class="tag ${m.type === "waste" ? "warn" : m.type === "adjustment" ? "grey" : "ok"}">${m.type}</span></td>
          <td>${esc(m.name)}</td><td class="num">${fmtQty(m.qty, m.unit, true)}</td><td>${esc([m.reason, m.note].filter(Boolean).join(" – "))}</td><td>${esc(m.staff)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="muted">Nothing recorded yet today.</div>`}
        ${can("stock.receive") ? `<button class="btn" id="hist" style="margin-top:10px">Full movement history…</button>` : ""}
      </div>`;
    const drawTable = () => {
      const q = S.stockSearch.toLowerCase();
      const rows = ov.filter((o) => (S.stockFilter === "all" || (S.stockFilter === "blue" ? o.incoming > 0 && o.active : STATUS_COLOUR[o.status] === S.stockFilter))
        && (!q || o.name.toLowerCase().includes(q) || (o.sku || "").toLowerCase().includes(q)));
      document.getElementById("stt").innerHTML = `<table><thead><tr><th>Item</th><th>Status</th><th class="num">On hand</th><th class="num">Used today</th>
        <th class="num">Reorder at</th><th class="num">Avg / day</th><th class="num">Suggested order</th>${isManager() ? `<th class="num">Value</th>` : ""}</tr></thead><tbody>
        ${rows.map((o) => { const l = lines[o.id]; return `<tr class="st-row-${STATUS_COLOUR[o.status]}">
          <td><b>${esc(o.name)}</b>${o.sku ? ` <span class="small muted">${esc(o.sku)}</span>` : ""} ${o.count_daily ? "" : `<span class="tag grey">weekly</span>`}</td>
          <td>${stBadge(o.status)} ${incomingBadge(o.incoming, o.unit)}</td>
          <td class="num"><b>${fmtQty(o.on_hand, o.unit)}</b>${o.on_hand < 0 ? `<div class="small" style="color:var(--bad)">negative – delivery missing?</div>` : ""}</td>
          <td class="num">${l ? fmtQty(l.sold + l.wasted, o.unit) : "–"}</td>
          <td class="num">${o.par_level ? fmtQty(o.par_level, o.unit) : "–"}</td>
          <td class="num">${o.avg_daily_usage != null ? fmtQty(o.avg_daily_usage, o.unit) : "–"}</td>
          <td class="num">${o.suggested_order != null ? fmtQty(o.suggested_order, o.unit) : "–"}</td>
          ${isManager() ? `<td class="num">${money(o.value)}</td>` : ""}</tr>`; }).join("") || `<tr><td colspan="8" class="muted">No items match.</td></tr>`}
      </tbody></table>`;
    };
    drawTable();
    el.querySelectorAll("[data-sf]").forEach((b) => b.addEventListener("click", () => { S.stockFilter = b.dataset.sf; viewStock(el); }));
    document.getElementById("ss").addEventListener("input", (e) => { S.stockSearch = e.target.value; drawTable(); });
    const unitOf = (sel) => sel.selectedOptions[0]?.dataset.u;
    const bindUnit = (s, ...us) => { const sel = document.getElementById(s); sel?.addEventListener("change", () => us.forEach((u) => { document.getElementById(u).textContent = bigUnit(unitOf(sel)); })); };
    bindUnit("wi", "wu"); bindUnit("di", "du", "dcu"); bindUnit("ai", "au");
    const save = async (kind, p, btn) => {
      const sel = document.getElementById(p + "i");
      const u = unitOf(sel);
      const raw = document.getElementById(p + "q").value;
      if (raw === "") return toast("Enter a quantity", true);
      const costRaw = p === "d" ? document.getElementById("dc").value : "";
      btn.disabled = true;
      try {
        await api("/api/stock/movement", { method: "POST", json: {
          ingredient_id: Number(sel.value), kind, qty: fromBig(raw, u),
          reason: kind === "waste" ? document.getElementById("wr").value : kind === "adjustment" ? document.getElementById("ar").value : null,
          note: document.getElementById(p + "n").value || null,
          unit_cost: costRaw === "" ? null : Number(costRaw) / big[u][1],
          client_ref: uuid(),
        } });
        toast({ waste: "Waste recorded", delivery: "Delivery recorded", adjustment: "Adjustment saved" }[kind]);
        viewStock(el);
      } catch (e) { fail(e); btn.disabled = false; }
    };
    document.getElementById("wsave").onclick = (e) => save("waste", "w", e.target);
    document.getElementById("dsave")?.addEventListener("click", (e) => save("delivery", "d", e.target));
    document.getElementById("asave")?.addEventListener("click", (e) => save("adjustment", "a", e.target));
    document.getElementById("hist")?.addEventListener("click", () => historyModal(active));
  } catch (e) { fail(e); }
}

async function historyModal(items) {
  const m = modal(`<h2>Stock movement history</h2>
    <div class="row"><div><label>Item</label><select id="hi"><option value="">All items</option>${items.map((i) => `<option value="${i.id}">${esc(i.name)}</option>`).join("")}</select></div>
      <div><label>Type</label><select id="ht"><option value="">All</option>${["delivery", "sale", "void", "refund", "waste", "adjustment", "external_sale"].map((t) => `<option>${t}</option>`).join("")}</select></div>
      <div><label>From</label><input id="hf" type="date" value="${daysAgo(7)}"></div><div><label>To</label><input id="hto" type="date" value="${today()}"></div>
      <div class="shrink"><button class="btn primary" id="hg">Show</button></div></div>
    <div id="hr" class="tablewrap" style="margin-top:10px"></div>
    <div class="actions">${can("data.export") ? `<button class="btn" id="hx">Export CSV</button>` : ""}<button class="btn" id="x">Close</button></div>`, "wide");
  const q = () => new URLSearchParams(Object.entries({ ingredient_id: m.querySelector("#hi").value, type: m.querySelector("#ht").value,
    date_from: m.querySelector("#hf").value, date_to: m.querySelector("#hto").value }).filter(([, v]) => v)).toString();
  const go = async () => {
    try {
      const rows = await api("/api/stock/movements?" + q());
      m.querySelector("#hr").innerHTML = `<table><thead><tr><th>Date</th><th>Time</th><th>Type</th><th>Item</th><th class="num">Qty</th><th>Ref / reason</th><th>By</th></tr></thead><tbody>
        ${rows.map((r) => `<tr><td>${esc(r.business_date)}</td><td>${esc((r.created_at || "").slice(11, 16))}</td><td>${esc(r.type)}</td><td>${esc(r.name)}</td>
          <td class="num">${fmtQty(r.qty, r.unit, true)}</td><td class="small">${r.order_number ? "Receipt #" + r.order_number + " " : ""}${esc([r.reason, r.note].filter(Boolean).join(" – "))}</td><td>${esc(r.staff || "")}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">None</td></tr>`}</tbody></table>`;
    } catch (e) { fail(e); }
  };
  m.querySelector("#hg").onclick = go;
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#hx")?.addEventListener("click", () => download("/api/export/movements.csv?" + q(), "stock-movements.csv"));
  go();
}

// ------------------------------------------------------------------ purchasing
async function viewPurchasing(el) {
  el.innerHTML = `<div id="pu">Loading…</div>`;
  try {
    const [pos, sups, ov] = await Promise.all([api("/api/purchase-orders"), api("/api/suppliers"), api("/api/stock/overview")]);
    const ST = { draft: "grey", ordered: "blue", partial: "amber", received: "green", cancelled: "grey" };
    document.getElementById("pu").innerHTML = `
      <div class="panel"><div class="row" style="margin-bottom:10px"><h2 style="margin:0">Purchase orders</h2>
        <div class="shrink"><button class="btn primary" id="np" ${sups.some((s) => s.active) ? "" : "disabled title='Add a supplier first'"}>+ New purchase order</button></div></div>
        <div class="small muted" style="margin-bottom:8px">Ordered stock shows as <span class="st blue">Incoming</span> and is <b>not</b> counted as stock until it is received.</div>
        ${pos.length ? `<div class="tablewrap"><table><thead><tr><th>#</th><th>Supplier</th><th>Status</th><th>Items</th><th class="num">Value ordered</th><th class="num">Received</th><th>Expected</th><th></th></tr></thead><tbody>
          ${pos.map((p) => `<tr><td>${p.id}</td><td>${esc(p.supplier)}</td><td><span class="st ${ST[p.status]}">${p.status}</span></td>
            <td class="small">${p.lines.map((l) => `${esc(l.name)} ${fmtQty(l.qty_received, l.unit)}/${fmtQty(l.qty_ordered, l.unit)}`).join(", ")}</td>
            <td class="num">${money(p.value_ordered)}</td><td class="num">${money(p.value_received)}</td><td>${esc(p.expected_date || "")}</td>
            <td style="white-space:nowrap">${p.status === "draft" ? `<button class="btn" data-ed="${p.id}">Edit</button> <button class="btn primary" data-or="${p.id}">Mark ordered</button>` : ""}
              ${["ordered", "partial"].includes(p.status) && can("stock.receive") ? `<button class="btn primary" data-rc="${p.id}">Receive</button>` : ""}
              ${!["received", "cancelled"].includes(p.status) ? `<button class="btn danger" data-cx="${p.id}">${p.status === "partial" ? "Close short" : "Cancel"}</button>` : ""}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="empty">No purchase orders yet.</div>`}
      </div>
      <div class="panel"><div class="row" style="margin-bottom:10px"><h2 style="margin:0">Suppliers</h2><div class="shrink"><button class="btn" id="ns">+ Add supplier</button></div></div>
        <table><thead><tr><th>Name</th><th>Phone</th><th>Email</th><th>Notes</th><th></th></tr></thead><tbody>
        ${sups.map((s) => `<tr class="${s.active ? "" : "void"}"><td>${esc(s.name)}</td><td>${esc(s.phone || "")}</td><td>${esc(s.email || "")}</td><td class="small">${esc(s.notes || "")}</td>
          <td><button class="btn" data-se="${s.id}">Edit</button></td></tr>`).join("") || `<tr><td colspan="5" class="muted">None yet.</td></tr>`}</tbody></table></div>`;
    const reload = () => viewPurchasing(el);
    const act = async (id, a) => { try { await api(`/api/purchase-orders/${id}/${a}`, { method: "POST" }); reload(); } catch (e) { fail(e); } };
    el.querySelectorAll("[data-or]").forEach((b) => b.addEventListener("click", () => act(b.dataset.or, "order")));
    el.querySelectorAll("[data-cx]").forEach((b) => b.addEventListener("click", () => { if (confirm("Close this purchase order? Anything not yet received will no longer be expected.")) act(b.dataset.cx, "cancel"); }));
    el.querySelectorAll("[data-rc]").forEach((b) => b.addEventListener("click", () => receiveModal(pos.find((p) => p.id === Number(b.dataset.rc)), reload)));
    el.querySelectorAll("[data-ed]").forEach((b) => b.addEventListener("click", () => poModal(sups, ov, pos.find((p) => p.id === Number(b.dataset.ed)), reload)));
    document.getElementById("np").onclick = () => poModal(sups, ov, null, reload);
    document.getElementById("ns").onclick = () => supplierModal(null, reload);
    el.querySelectorAll("[data-se]").forEach((b) => b.addEventListener("click", () => supplierModal(sups.find((s) => s.id === Number(b.dataset.se)), reload)));
  } catch (e) { fail(e); }
}

function supplierModal(s, done) {
  const x = s || { name: "", phone: "", email: "", notes: "", active: 1 };
  const m = modal(`<h2>${s ? "Edit" : "New"} supplier</h2>
    <label>Name</label><input id="n" value="${esc(x.name)}" maxlength="100" autofocus>
    <div class="row"><div><label>Phone</label><input id="p" value="${esc(x.phone || "")}" maxlength="40"></div><div><label>Email</label><input id="e" value="${esc(x.email || "")}" maxlength="100"></div></div>
    <label>Notes</label><input id="no" value="${esc(x.notes || "")}" maxlength="300">
    <label>Status</label><select id="a"><option value="1">Active</option><option value="0" ${x.active ? "" : "selected"}>Inactive</option></select>
    <div class="err" id="err"></div><div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="s">Save</button></div>`);
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#s").onclick = async () => {
    const body = { name: m.querySelector("#n").value, phone: m.querySelector("#p").value || null, email: m.querySelector("#e").value || null,
      notes: m.querySelector("#no").value || null, active: m.querySelector("#a").value === "1" };
    try { await api(s ? `/api/suppliers/${s.id}` : "/api/suppliers", { method: s ? "PUT" : "POST", json: body }); closeModal(); done(); }
    catch (e) { m.querySelector("#err").textContent = e.message; }
  };
}

function poModal(sups, ov, po, done) {
  const items = ov.filter((i) => i.active);
  let lines = po ? po.lines.map((l) => ({ ingredient_id: l.ingredient_id, qty: toBig(l.qty_ordered, l.unit), cost: l.unit_cost * big[l.unit][1] }))
    : items.filter((i) => ["out", "critical", "low"].includes(i.status)).map((i) => ({ ingredient_id: i.id, qty: i.suggested_order != null ? toBig(i.suggested_order, i.unit) : "", cost: (i.cost_per_unit || 0) * big[i.unit][1] }));
  const m = modal(`<h2>${po ? "Edit" : "New"} purchase order</h2>
    <div class="row"><div><label>Supplier</label><select id="su">${sups.filter((s) => s.active).map((s) => `<option value="${s.id}" ${po && po.supplier_id === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
      <div><label>Expected delivery</label><input id="ed" type="date" value="${esc(po?.expected_date || "")}"></div></div>
    ${po ? "" : `<div class="small muted" style="margin-top:6px">Pre-filled with low / critical items and suggested quantities where there is enough history. Check every line.</div>`}
    <div id="pl"></div><button class="btn" id="al" style="margin-top:8px">+ Item</button>
    <label>Notes</label><input id="pn" value="${esc(po?.notes || "")}" maxlength="300">
    <div class="err" id="err"></div><div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="s">Save draft</button></div>`, "wide");
  const draw = () => {
    m.querySelector("#pl").innerHTML = lines.map((l, i) => { const it = items.find((x) => x.id === l.ingredient_id) || items[0]; return `<div class="row" style="margin-top:6px">
      <div><select data-li="${i}">${items.map((a) => `<option value="${a.id}" ${a.id === l.ingredient_id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></div>
      <div style="max-width:150px"><input data-lq="${i}" type="number" min="0" step="any" value="${esc(l.qty)}" placeholder="qty (${bigUnit(it.unit)})" aria-label="Quantity in ${bigUnit(it.unit)}"></div>
      <div style="max-width:170px"><input data-lc="${i}" type="number" min="0" step="any" value="${esc(l.cost)}" placeholder="cost per ${bigUnit(it.unit)}" aria-label="Cost per ${bigUnit(it.unit)}"></div>
      <div class="shrink small muted">${bigUnit(it.unit)}</div><div class="shrink"><button class="btn danger" data-ld="${i}" aria-label="Remove">✕</button></div></div>`; }).join("") || `<div class="muted small" style="margin-top:6px">No items yet.</div>`;
    m.querySelectorAll("[data-li]").forEach((s) => s.addEventListener("change", () => { lines[s.dataset.li].ingredient_id = Number(s.value); draw(); }));
    m.querySelectorAll("[data-lq]").forEach((s) => s.addEventListener("input", () => { lines[s.dataset.lq].qty = s.value; }));
    m.querySelectorAll("[data-lc]").forEach((s) => s.addEventListener("input", () => { lines[s.dataset.lc].cost = s.value; }));
    m.querySelectorAll("[data-ld]").forEach((s) => s.addEventListener("click", () => { lines.splice(s.dataset.ld, 1); draw(); }));
  };
  draw();
  m.querySelector("#al").onclick = () => {
    const free = items.find((x) => !lines.some((l) => l.ingredient_id === x.id));
    if (free) { lines.push({ ingredient_id: free.id, qty: "", cost: ((free.cost_per_unit || 0) * big[free.unit][1]) || "" }); draw(); }
  };
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#s").onclick = async () => {
    const out = lines.map((l) => { const u = items.find((x) => x.id === l.ingredient_id).unit;
      return { ingredient_id: l.ingredient_id, qty_ordered: fromBig(l.qty, u), unit_cost: Number(l.cost || 0) / big[u][1] }; });
    if (out.some((l) => !(l.qty_ordered > 0))) return (m.querySelector("#err").textContent = "Every line needs a quantity.");
    const body = { supplier_id: Number(m.querySelector("#su").value), lines: out, expected_date: m.querySelector("#ed").value || null, notes: m.querySelector("#pn").value || null };
    try { await api(po ? `/api/purchase-orders/${po.id}` : "/api/purchase-orders", { method: po ? "PUT" : "POST", json: body }); closeModal(); toast("Saved as draft. Mark it ordered when sent."); done(); }
    catch (e) { m.querySelector("#err").textContent = e.message; }
  };
}

function receiveModal(po, done) {
  const key = uuid();
  const m = modal(`<h2>Receive PO #${po.id} · ${esc(po.supplier)}</h2>
    <div class="info">Enter what physically arrived now. Partial deliveries are fine; the rest stays incoming.</div>
    <table><thead><tr><th>Item</th><th class="num">Ordered</th><th class="num">Already received</th><th style="width:140px">Received now</th><th style="width:140px">Cost per unit</th></tr></thead><tbody>
    ${po.lines.map((l) => `<tr><td>${esc(l.name)}</td><td class="num">${fmtQty(l.qty_ordered, l.unit)}</td><td class="num">${fmtQty(l.qty_received, l.unit)}</td>
      <td><input data-rq="${l.id}" data-u="${l.unit}" type="number" min="0" step="any" value="${l.outstanding ? toBig(l.outstanding, l.unit) : 0}" ${l.outstanding ? "" : "disabled"}></td>
      <td><input data-rcost="${l.id}" data-u="${l.unit}" type="number" min="0" step="any" value="${+(l.unit_cost * big[l.unit][1]).toFixed(4)}"></td></tr>`).join("")}
    </tbody></table>
    <label>Delivery note / invoice no.</label><input id="rn" maxlength="300">
    <div class="err" id="err"></div><div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="s">Receive into stock</button></div>`, "wide");
  m.querySelector("#x").onclick = closeModal;
  m.querySelector("#s").onclick = async (e) => {
    const lines = [...m.querySelectorAll("[data-rq]")].filter((i) => !i.disabled && Number(i.value) > 0).map((i) => ({
      line_id: Number(i.dataset.rq), qty: fromBig(i.value, i.dataset.u),
      unit_cost: Number(m.querySelector(`[data-rcost="${i.dataset.rq}"]`).value || 0) / big[i.dataset.u][1] }));
    if (!lines.length) return (m.querySelector("#err").textContent = "Enter at least one received quantity.");
    e.target.disabled = true;
    try { await api(`/api/purchase-orders/${po.id}/receive`, { method: "POST", json: { lines, note: m.querySelector("#rn").value || null, client_ref: key } }); closeModal(); toast("Received into stock"); done(); }
    catch (err) { m.querySelector("#err").textContent = err.message; e.target.disabled = false; }
  };
}

// ------------------------------------------------------------------ open / close day
async function viewDay(el) {
  if (!S.state.day) return viewOpenDay(el);
  return viewCloseDay(el);
}

async function viewOpenDay(el) {
  const prev = await api("/api/day/opening-preview").catch(fail);
  if (!prev) return;
  const rows = prev.rows;
  const first = S.state.first_day;
  el.innerHTML = `<div class="panel">
    <h2>Open the day</h2>
    ${first ? `<div class="note"><b>First day:</b> count everything in the store and enter it below. From tomorrow, opening stock is carried over automatically from the previous night's closing count.</div>`
            : `<div class="info">Opening stock is carried over from the last closing count${prev.last_day ? ` (<b>${esc(prev.last_day.business_date)}</b>${prev.last_day.source === "workbook" ? ", imported from the workbook" : ""})` : ""}. Only change a number if the morning count is different; the change is recorded.</div>`}
    <div class="row" style="max-width:300px"><div><label>Business date</label><input id="bd" type="date" value="${today()}"></div></div>
    <div class="tablewrap" style="margin-top:12px"><table><thead><tr><th>Item</th><th class="num">Carried over</th><th style="width:180px">Opening count</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${esc(r.name)} ${r.count_daily ? "" : `<span class="tag grey">weekly</span>`}</td>
      <td class="num">${r.carry_over == null ? "–" : fmtQty(r.carry_over, r.unit)}</td>
      <td><div class="row" style="flex-wrap:nowrap;gap:6px"><input data-o="${r.id}" data-u="${r.unit}" type="number" min="0" step="any" inputmode="decimal"
        value="${r.carry_over == null ? "" : toBig(r.carry_over, r.unit)}" aria-label="Opening count for ${esc(r.name)}"><span class="shrink small muted">${bigUnit(r.unit)}</span></div></td></tr>`).join("")}
    </tbody></table></div>
    <div class="err" id="oerr"></div>
    <div style="margin-top:12px"><button class="btn primary big" id="open">Open day</button></div>
  </div>`;
  document.getElementById("open").onclick = async (e) => {
    const counts = {};
    el.querySelectorAll("[data-o]").forEach((i) => { if (i.value !== "") counts[i.dataset.o] = fromBig(i.value, i.dataset.u); });
    e.target.disabled = true;
    try {
      await api("/api/day/open", { method: "POST", json: { business_date: document.getElementById("bd").value, opening_counts: counts } });
      await refreshState(); toast("Day opened"); S.view = can("pos.sell") ? "pos" : "dash"; render();
    } catch (err) { document.getElementById("oerr").textContent = err.message; e.target.disabled = false; }
  };
}

async function viewCloseDay(el) {
  const stock = await api("/api/stock").catch(fail);
  if (!stock) return;
  const pct = S.state.tolerance_pct;
  const tol = (l) => (l.unit === "pcs" ? 0 : Math.max(l.opening + l.delivered + l.adjusted, 0) * pct / 100);
  el.innerHTML = `<div class="panel">
    <h2>Close the day · ${esc(stock.day.business_date)}</h2>
    <div class="info">Count what is <b>physically left</b> and enter it. The system compares it with what should be left.
      Differences bigger than ${pct}% (any difference for items counted in pieces) need a reason before the day can close.
      All cash shifts must be closed first. Tonight's count becomes tomorrow's opening stock.</div>
    <div class="tablewrap"><table><thead><tr>
      <th>Item</th><th class="num">Opening</th><th class="num">+ Received</th><th class="num">± Adjusted</th><th class="num">− Sales</th><th class="num">− Waste</th>
      <th class="num">Should be left</th><th style="width:150px">Counted</th><th class="num">Difference</th><th style="width:190px">Reason</th><th style="width:170px">Note</th></tr></thead><tbody>
    ${stock.lines.map((l) => `<tr data-row="${l.id}">
      <td><b>${esc(l.name)}</b> ${l.count_daily ? "" : `<span class="tag grey" title="Leave blank if not counted today">weekly</span>`}</td>
      <td class="num">${fmtQty(l.opening, l.unit)}</td><td class="num">${fmtQty(l.delivered, l.unit)}</td><td class="num">${fmtQty(l.adjusted, l.unit, true)}</td>
      <td class="num">${fmtQty(l.sold, l.unit)}</td><td class="num">${fmtQty(l.wasted, l.unit)}</td>
      <td class="num"><b>${fmtQty(l.expected, l.unit)}</b></td>
      <td><div class="row" style="flex-wrap:nowrap;gap:6px"><input data-c="${l.id}" type="number" min="0" step="any" inputmode="decimal" placeholder="${l.count_daily ? "count" : "optional"}" aria-label="Counted ${esc(l.name)}"><span class="shrink small muted">${bigUnit(l.unit)}</span></div></td>
      <td class="num" data-var="${l.id}">–</td>
      <td><select data-r="${l.id}" aria-label="Reason"><option value="">—</option>${S.state.variance_reasons.map((r) => `<option>${esc(r)}</option>`).join("")}</select></td>
      <td><input data-n="${l.id}" placeholder="explain" maxlength="300"></td>
    </tr>`).join("")}</tbody></table></div>
    <label>Closing notes (optional)</label><textarea id="cn" rows="2" maxlength="1000"></textarea>
    <div class="err" id="cerr"></div>
    <div class="row" style="margin-top:10px"><div class="shrink"><button class="btn primary big" id="close">Close day & see report</button></div>
      <div class="small muted">After closing, sales are locked for this date.</div></div>
  </div>`;
  const byId = Object.fromEntries(stock.lines.map((l) => [l.id, l]));
  const update = (id) => {
    const l = byId[id];
    const inp = el.querySelector(`[data-c="${id}"]`);
    const cell = el.querySelector(`[data-var="${id}"]`);
    const row = el.querySelector(`[data-row="${id}"]`);
    const reason = el.querySelector(`[data-r="${id}"]`).value;
    if (inp.value === "") { cell.textContent = "–"; row.className = ""; return; }
    const v = fromBig(inp.value, l.unit) - l.expected;
    const out = Math.abs(v) > tol(l) + 1e-9;
    cell.innerHTML = Math.abs(v) < 1e-9 ? `<span class="tag ok">exact</span>` : `<b style="color:${out ? "var(--bad)" : "var(--muted)"}">${fmtQty(v, l.unit, true)}</b>`;
    row.className = out && !reason ? "flag" : Math.abs(v) < 1e-9 || !out ? "okrow" : "";
  };
  el.querySelectorAll("[data-c],[data-r]").forEach((i) => i.addEventListener("input", () => update(i.dataset.c || i.dataset.r)));
  document.getElementById("close").onclick = async (e) => {
    const counts = {};
    stock.lines.forEach((l) => {
      const v = el.querySelector(`[data-c="${l.id}"]`).value;
      counts[l.id] = { counted: v === "" ? null : fromBig(v, l.unit), reason: el.querySelector(`[data-r="${l.id}"]`).value, note: el.querySelector(`[data-n="${l.id}"]`).value };
    });
    if (!confirm("Close the day? Sales will be locked for this date.")) return;
    e.target.disabled = true;
    try {
      const rep = await api("/api/day/close", { method: "POST", json: { counts, notes: document.getElementById("cn").value || null } });
      await refreshState();
      S.view = "reports"; S.reportTab = "daily"; S.reportDay = rep.day.id; render();
      toast("Day closed");
    } catch (err) { document.getElementById("cerr").textContent = err.message; e.target.disabled = false; window.scrollTo(0, document.body.scrollHeight); }
  };
}

// ------------------------------------------------------------------ reports
async function viewReports(el) {
  const tabs = [["daily", "Daily"], ["period", "Sales & profit"], ["valuation", "Stock valuation"]];
  el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-t="${k}" class="${S.reportTab === k ? "active" : ""}">${l}</button>`).join("")}</div><div id="rv"></div>`;
  el.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => { S.reportTab = b.dataset.t; S.reportDay = null; viewReports(el); }));
  const rv = document.getElementById("rv");
  if (S.reportTab === "period") return reportPeriod(rv);
  if (S.reportTab === "valuation") return reportValuation(rv);
  if (S.reportDay) return viewReport(rv, S.reportDay, el);
  const days = await api("/api/days").catch(fail);
  if (!days) return;
  rv.innerHTML = `<div class="panel"><h2>Daily reports</h2>
    ${days.length ? `<div class="tablewrap"><table><thead><tr><th>Date</th><th>Status</th><th class="num">Sales (net of refunds)</th><th class="num">Stock differences</th><th></th></tr></thead><tbody>
    ${days.map((d) => `<tr><td>${esc(d.business_date)}${d.covers_to ? ` → ${esc(d.covers_to)}` : ""}</td>
      <td><span class="tag ${d.status === "open" ? "ok" : "grey"}">${d.status}</span>${d.source === "workbook" ? ` <span class="tag warn" title="Imported from the stock-movement workbook; sales were on the other POS">imported</span>` : ""}</td>
      <td class="num">${money(d.sales)}</td><td class="num">${d.status === "closed" ? d.variances : "–"}</td>
      <td><button class="btn" data-d="${d.id}">View</button></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No days yet.</div>`}
  </div>`;
  rv.querySelectorAll("[data-d]").forEach((b) => b.addEventListener("click", () => { S.reportDay = Number(b.dataset.d); viewReports(el); }));
}

function reportHTML(r) {
  const d = r.day;
  const s = r.summary;
  const flagged = r.stock.filter((l) => l.flag);
  return `
    <h2>${esc(S.state.business_name)} · Day report ${esc(d.business_date)}${d.covers_to ? " → " + esc(d.covers_to) : ""} ${d.status === "open" ? `<span class="tag ok">still open</span>` : ""}</h2>
    <div class="small muted" style="margin-bottom:12px">${d.source === "workbook" ? esc(d.notes || "Imported") + ". Sales figures come from the other POS (\"system sales\")." :
      `Opened ${esc(d.opened_at || "")} by ${esc(d.opened_by_name || "")}${d.closed_at ? ` · Closed ${esc(d.closed_at)} by ${esc(d.closed_by_name || "")}` : ""}`}</div>
    <div class="kpis">
      <div class="kpi"><div class="l">Collected</div><div class="v">${money(s.collected)}</div></div>
      <div class="kpi"><div class="l">Refunds</div><div class="v">${money(s.refunds)}</div></div>
      <div class="kpi"><div class="l">Net sales ex ${esc(S.state.settings.tax_label || "tax")}</div><div class="v">${money(s.net_sales_ex_tax)}</div></div>
      <div class="kpi"><div class="l">Cost of goods sold</div><div class="v">${money(s.cogs)}</div></div>
      <div class="kpi"><div class="l">Gross profit</div><div class="v">${money(s.gross_profit)}</div></div>
      <div class="kpi"><div class="l">Orders / voids</div><div class="v">${s.orders} / ${s.voids}</div></div>
      ${s.discounts ? `<div class="kpi"><div class="l">Discounts given</div><div class="v">${money(s.discounts)}</div></div>` : ""}
      ${r.sales.by_method.map((m) => `<div class="kpi"><div class="l">${esc(m.payment_method)}</div><div class="v">${money(m.amt)}</div></div>`).join("")}
      <div class="kpi"><div class="l">Recorded waste</div><div class="v">${money(r.waste_value)}</div></div>
      ${d.status === "closed" ? `<div class="kpi ${r.shortage_value > 0 ? "bad" : ""}"><div class="l">Stock shortages (value)</div><div class="v">${money(r.shortage_value)}</div></div>` : ""}
    </div>
    <div class="small muted">Gross profit = net sales − ingredient cost of what was sold. It is not net profit (wages, rent, utilities are not in this system).</div>
    ${r.items_without_recipe.length ? `<div class="note">Sold without a recipe (stock and cost not tracked): ${r.items_without_recipe.map(esc).join(", ")}</div>` : ""}
    ${s.uncosted_ingredients.length ? `<div class="note">No cost set for: ${s.uncosted_ingredients.map(esc).join(", ")} — cost of goods is understated.</div>` : ""}
    ${d.notes && d.source !== "workbook" ? `<div class="info"><b>Closing notes:</b> ${esc(d.notes)}</div>` : ""}
    <h3>Stock reconciliation ${flagged.length ? `<span class="tag bad">${flagged.length} outside tolerance</span>` : d.status === "closed" ? `<span class="tag ok">all within tolerance</span>` : ""}</h3>
    <div class="tablewrap"><table><thead><tr><th>Item</th><th class="num">Opening</th><th class="num">+ Received</th><th class="num">± Adj.</th><th class="num">− Sales</th><th class="num">− Waste</th>
      <th class="num">Should be left</th><th class="num">Counted</th><th class="num">Difference</th><th class="num">Value</th><th>Reason</th></tr></thead><tbody>
    ${r.stock.map((l) => `<tr class="${l.flag ? "flag" : ""}"><td>${esc(l.name)}${l.below_par ? ` <span class="st amber">reorder</span>` : ""}</td>
      <td class="num">${fmtQty(l.opening, l.unit)}</td><td class="num">${fmtQty(l.delivered, l.unit)}</td><td class="num">${fmtQty(l.adjusted || 0, l.unit, true)}</td><td class="num">${fmtQty(l.sold, l.unit)}</td>
      <td class="num">${fmtQty(l.wasted, l.unit)}</td><td class="num">${fmtQty(l.expected, l.unit)}</td><td class="num">${fmtQty(l.counted, l.unit)}</td>
      <td class="num">${l.variance == null ? "–" : fmtQty(l.variance, l.unit, true)}</td>
      <td class="num">${l.variance_value == null ? "–" : money(l.variance_value)}</td>
      <td class="small">${esc([l.reason, l.note].filter(Boolean).join(" – "))}</td></tr>`).join("")}</tbody></table></div>
    <div class="grid2" style="margin-top:14px">
      <div><h3>Items sold</h3><table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">At list price</th></tr></thead><tbody>
        ${r.items.map((i) => `<tr><td>${esc(i.name)}</td><td class="num">${i.qty}</td><td class="num">${money(i.amt)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">None</td></tr>`}</tbody></table>
        <h3 style="margin-top:14px">Cash shifts</h3><table><thead><tr><th>Cashier</th><th class="num">Expected</th><th class="num">Counted</th><th class="num">Over / short</th></tr></thead><tbody>
        ${r.shifts.map((x) => `<tr><td>${esc(x.staff_name)}${x.status === "open" ? ` <span class="tag ok">open</span>` : ""}</td><td class="num">${money(x.expected_cash ?? x.expected_cash_now)}</td>
          <td class="num">${x.counted_cash == null ? "–" : money(x.counted_cash)}</td><td class="num" style="${x.cash_variance ? "color:var(--bad)" : ""}">${x.cash_variance == null ? "–" : money(x.cash_variance)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`}</tbody></table></div>
      <div><h3>Waste recorded</h3><table><thead><tr><th>Item</th><th class="num">Qty</th><th>Reason</th><th>By</th></tr></thead><tbody>
        ${r.waste.map((w) => `<tr><td>${esc(w.name)}</td><td class="num">${fmtQty(w.qty, w.unit)}</td><td class="small">${esc([w.reason, w.note].filter(Boolean).join(" – "))}</td><td>${esc(w.staff)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`}</tbody></table>
        <h3 style="margin-top:14px">Adjustments</h3><table><tbody>
        ${r.adjustments.map((w) => `<tr><td>${esc(w.name)}</td><td class="num">${fmtQty(w.qty, w.unit, true)}</td><td class="small">${esc([w.reason, w.note].filter(Boolean).join(" – "))}</td><td>${esc(w.staff)}</td></tr>`).join("") || `<tr><td class="muted">None</td></tr>`}</tbody></table>
        <h3 style="margin-top:14px">Voids & refunds</h3><table><tbody>
        ${r.sales.voids.map((v) => `<tr><td>Void #${v.number}</td><td class="num">${money(v.total)}</td><td class="small">${esc(v.void_reason)}</td><td>${esc(v.voided_by)}</td></tr>`).join("")}
        ${r.refunds.map((v) => `<tr><td>Refund #${v.number}</td><td class="num">${money(v.amount)} ${esc(v.method)}</td><td class="small">${esc(v.reason)}${v.restocked ? " (restocked)" : ""}</td><td>${esc(v.approved_by || v.staff)}</td></tr>`).join("")}
        ${!r.sales.voids.length && !r.refunds.length ? `<tr><td class="muted">None</td></tr>` : ""}</tbody></table>
      </div>
    </div>`;
}

async function viewReport(rv, id, el) {
  const r = await api("/api/reports/day/" + id).catch(fail);
  if (!r) return;
  rv.innerHTML = `<div class="row" style="margin-bottom:12px"><div class="shrink"><button class="btn" id="back">← All days</button></div>
    <div class="shrink"><button class="btn" id="pr">Print report</button></div></div>
    <div class="panel">${reportHTML(r)}</div>`;
  document.getElementById("back").onclick = () => { S.reportDay = null; viewReports(el); };
  document.getElementById("pr").onclick = () => printHTML(reportHTML(r));
}

async function reportPeriod(rv) {
  rv.innerHTML = `<div class="panel"><div class="row" style="max-width:640px"><div><label>From</label><input id="pf" type="date" value="${daysAgo(6)}"></div>
    <div><label>To</label><input id="pt" type="date" value="${today()}"></div><div class="shrink"><button class="btn primary" id="pg">Show</button></div>
    <div class="shrink"><button class="btn" id="px">Items CSV</button></div></div><div id="po" style="margin-top:14px"></div></div>`;
  const go = async () => {
    const f = document.getElementById("pf").value, t = document.getElementById("pt").value;
    try {
      const r = await api(`/api/reports/period?date_from=${f}&date_to=${t}`);
      document.getElementById("po").innerHTML = `
        <div class="kpis">
          <div class="kpi"><div class="l">Days</div><div class="v">${r.days}</div></div>
          <div class="kpi"><div class="l">Gross sales (list)</div><div class="v">${money(r.gross_sales)}</div></div>
          <div class="kpi"><div class="l">Discounts</div><div class="v">${money(r.discounts)}</div></div>
          <div class="kpi"><div class="l">Refunds</div><div class="v">${money(r.refunds)}</div></div>
          <div class="kpi"><div class="l">${esc(S.state.settings.tax_label || "Tax")} collected</div><div class="v">${money(r.tax)}</div></div>
          <div class="kpi"><div class="l">Net sales ex tax</div><div class="v">${money(r.net_sales_ex_tax)}</div></div>
          <div class="kpi"><div class="l">COGS</div><div class="v">${money(r.cogs)}</div></div>
          <div class="kpi"><div class="l">Gross profit</div><div class="v">${money(r.gross_profit)}${r.gross_margin_pct != null ? ` <span class="small">${r.gross_margin_pct}%</span>` : ""}</div></div>
          <div class="kpi"><div class="l">Purchases received</div><div class="v">${money(r.purchases_total)}</div></div>
          <div class="kpi"><div class="l">Waste (cost)</div><div class="v">${money(r.waste_value)}</div></div>
          <div class="kpi bad"><div class="l">Stock shortages (cost)</div><div class="v">${money(r.shortage_value)}</div></div>
        </div>
        <div class="small muted">Gross profit excludes overheads (wages, rent, utilities): it is not net profit. Imported workbook days add stock movement but no sales money.</div>
        ${r.uncosted_ingredients.length ? `<div class="note">No cost set for ${r.uncosted_ingredients.map(esc).join(", ")}: COGS is understated.</div>` : ""}
        <div class="grid2" style="margin-top:12px">
          <div><h3>Items sold</h3><table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">At list price</th></tr></thead><tbody>
            ${r.items.map((i) => `<tr><td>${esc(i.name)}</td><td class="num">${i.qty}</td><td class="num">${money(i.amt)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">None</td></tr>`}</tbody></table>
            <h3 style="margin-top:14px">Purchases received</h3><table><thead><tr><th>Supplier</th><th>Item</th><th class="num">Qty</th><th class="num">Value</th></tr></thead><tbody>
            ${r.purchases.map((p) => `<tr><td class="small">${esc(p.supplier)}</td><td>${esc(p.name)}</td><td class="num">${fmtQty(p.qty, p.unit)}</td><td class="num">${p.uncosted ? `<span class="muted small">no cost</span>` : money(p.value)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`}</tbody></table></div>
          <div><h3>Stock differences (counted − expected)</h3><table><thead><tr><th>Item</th><th class="num">Total</th><th class="num">Days off</th><th class="num">Value</th></tr></thead><tbody>
            ${r.variance.map((v) => `<tr class="${v.qty < -1e-9 ? "flag" : ""}"><td>${esc(v.name)}</td><td class="num">${fmtQty(v.qty, v.unit, true)}</td><td class="num">${v.days_off}</td><td class="num">${money(v.value)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`}</tbody></table>
            <h3 style="margin-top:14px">Waste</h3><table><tbody>
            ${r.waste.map((w) => `<tr><td>${esc(w.name)}</td><td class="num">${fmtQty(w.qty, w.unit)}</td><td class="num">${money(w.value)}</td></tr>`).join("") || `<tr><td class="muted">None</td></tr>`}</tbody></table></div>
        </div>`;
    } catch (e) { fail(e); }
  };
  document.getElementById("pg").onclick = go;
  document.getElementById("px").onclick = () => download(`/api/export/sales.csv?date_from=${document.getElementById("pf").value}&date_to=${document.getElementById("pt").value}`, "sales-items.csv");
  go();
}

async function reportValuation(rv) {
  const r = await api("/api/reports/valuation").catch(fail);
  if (!r) return;
  rv.innerHTML = `<div class="panel"><div class="row" style="margin-bottom:10px"><h2 style="margin:0">Stock valuation · ${money(r.total)}</h2>
      <div class="shrink"><button class="btn" id="vx">Export CSV</button></div></div>
    <div class="small muted">On-hand quantity × current cost per unit. Incoming (ordered) stock is not included. Negative stock is valued at 0.</div>
    ${r.uncosted.length ? `<div class="note">No cost set: ${r.uncosted.map(esc).join(", ")}</div>` : ""}
    ${r.negative.length ? `<div class="note">Negative on-hand (a delivery or count is missing): ${r.negative.map(esc).join(", ")}</div>` : ""}
    <div class="tablewrap"><table><thead><tr><th>Item</th><th>Status</th><th class="num">On hand</th><th class="num">Cost / unit</th><th class="num">Value</th></tr></thead><tbody>
    ${r.lines.map((o) => `<tr class="st-row-${STATUS_COLOUR[o.status]}"><td>${esc(o.name)}</td><td>${stBadge(o.status)} ${incomingBadge(o.incoming, o.unit)}</td><td class="num">${fmtQty(o.on_hand, o.unit)}</td>
      <td class="num">${money((o.cost_per_unit || 0) * big[o.unit][1])} / ${bigUnit(o.unit)}</td><td class="num">${money(o.value)}</td></tr>`).join("")}</tbody></table></div></div>`;
  document.getElementById("vx").onclick = () => download("/api/export/stock.csv", "stock.csv");
}

// ------------------------------------------------------------------ setup
async function viewSetup(el) {
  const tabs = [];
  if (can("catalog.edit")) tabs.push(["menu", "Menu & recipes"], ["ingredients", "Items & thresholds"]);
  if (can("data.import")) tabs.push(["excel", "Import / export"]);
  if (can("staff.manage")) tabs.push(["staff", "Staff"]);
  if (can("settings.edit")) tabs.push(["settings", "Settings"]);
  if (can("audit.view")) tabs.push(["audit", "Audit log"]);
  if (can("licence.manage")) tabs.push(["licence", "Licence & devices"]);
  if (!tabs.find((t) => t[0] === S.setupTab)) S.setupTab = tabs[0][0];
  el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-t="${k}" class="${S.setupTab === k ? "active" : ""}">${l}</button>`).join("")}</div><div id="sv"></div>`;
  el.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => { S.setupTab = b.dataset.t; viewSetup(el); }));
  const sv = document.getElementById("sv");
  try {
    await ({ menu: setupMenu, ingredients: setupIngredients, excel: setupExcel, staff: setupStaff, settings: setupSettings,
             audit: setupAudit, licence: setupLicence }[S.setupTab])(sv);
  } catch (e) { fail(e); }
}

async function setupMenu(sv) {
  const [items, ings] = await Promise.all([api("/api/menu-items"), api("/api/ingredients")]);
  sv.innerHTML = `<div class="panel">
    <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Menu & recipes</h2><div class="shrink"><button class="btn primary" id="add">+ Add menu item</button></div></div>
    <div class="small muted" style="margin-bottom:10px">Food cost uses ingredient costs (purchase cost, not selling price). Food cost % = cost of one plate ÷ selling price.</div>
    <div class="tablewrap"><table><thead><tr><th>Item</th><th>Category</th><th>SKU / barcode</th><th class="num">Price</th><th>Recipe (per plate)</th><th class="num">Food cost</th><th class="num">%</th><th></th></tr></thead><tbody>
    ${items.map((i) => {
      const pc = i.price ? (i.food_cost / i.price) * 100 : 0;
      return `<tr class="${i.active ? "" : "void"}"><td><b>${esc(i.name)}</b></td><td>${esc(i.category || "")}</td><td class="small">${esc([i.sku, i.barcode].filter(Boolean).join(" · "))}</td><td class="num">${money(i.price)}</td>
        <td class="small">${i.recipe.length ? i.recipe.map((r) => `${esc(r.name)} ${r.qty}${r.unit}`).join(", ") : `<span class="tag warn">no recipe</span>`}</td>
        <td class="num">${money(i.food_cost)}</td><td class="num"><span class="tag ${pc > 40 ? "bad" : pc > 32 ? "warn" : "ok"}">${pc.toFixed(0)}%</span></td>
        <td><button class="btn" data-e="${i.id}">Edit</button></td></tr>`;
    }).join("")}</tbody></table></div></div>`;
  const edit = (item) => {
    const it = item || { name: "", category: "", price: "", active: 1, recipe: [], sku: "", barcode: "" };
    let lines = it.recipe.map((r) => ({ ingredient_id: r.ingredient_id, qty: r.qty }));
    const act = ings.filter((i) => i.active);
    const m = modal(`<h2>${item ? "Edit" : "New"} menu item</h2>
      <div class="row"><div><label>Name</label><input id="mn" value="${esc(it.name)}" maxlength="80"></div><div><label>Category</label><input id="mc" value="${esc(it.category || "")}" list="catlist" maxlength="40"></div></div>
      <datalist id="catlist">${S.menu.categories.map((c) => `<option>${esc(c.name)}</option>`).join("")}</datalist>
      <div class="row"><div><label>Selling price (${esc(cur())})</label><input id="mp" type="number" min="0" step="any" value="${esc(it.price)}"></div>
        <div><label>Status</label><select id="ma"><option value="1">On menu</option><option value="0" ${it.active ? "" : "selected"}>Hidden</option></select></div></div>
      <div class="row"><div><label>SKU (optional, unique)</label><input id="ms" value="${esc(it.sku || "")}" maxlength="40"></div>
        <div><label>Barcode (optional – scan it here)</label><input id="mb" value="${esc(it.barcode || "")}" maxlength="40"></div></div>
      <h3 style="margin-top:16px">Recipe – raw quantity for ONE plate</h3>
      <div class="small muted">Use raw weights (before cooking), so cooking loss is included. Resale items (a soda) use 1 pcs of the stock item.</div>
      <div id="rl"></div><button class="btn" id="addl" style="margin-top:8px">+ Ingredient</button>
      <div class="err" id="merr"></div>
      <div class="actions"><button class="btn" id="c">Cancel</button><button class="btn primary" id="s">Save</button></div>`, "wide");
    m.querySelector("#mb").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });  // scanners send Enter
    const drawLines = () => {
      m.querySelector("#rl").innerHTML = lines.map((l, i) => `<div class="row" style="margin-top:6px">
        <div><select data-li="${i}">${act.map((a) => `<option value="${a.id}" ${a.id === l.ingredient_id ? "selected" : ""}>${esc(a.name)} (${a.unit})</option>`).join("")}</select></div>
        <div style="max-width:140px"><input data-lq="${i}" type="number" min="0" step="any" value="${esc(l.qty)}" placeholder="qty"></div>
        <div class="shrink"><button class="btn danger" data-ld="${i}" aria-label="Remove">✕</button></div></div>`).join("") || `<div class="muted small" style="margin-top:6px">No ingredients: this item won't affect stock.</div>`;
      m.querySelectorAll("[data-li]").forEach((s) => s.addEventListener("change", () => { lines[s.dataset.li].ingredient_id = Number(s.value); }));
      m.querySelectorAll("[data-lq]").forEach((s) => s.addEventListener("input", () => { lines[s.dataset.lq].qty = s.value; }));
      m.querySelectorAll("[data-ld]").forEach((s) => s.addEventListener("click", () => { lines.splice(s.dataset.ld, 1); drawLines(); }));
    };
    drawLines();
    m.querySelector("#addl").onclick = () => {
      const free = act.find((x) => !lines.some((l) => l.ingredient_id === x.id));
      if (free) { lines.push({ ingredient_id: free.id, qty: "" }); drawLines(); }
    };
    m.querySelector("#c").onclick = closeModal;
    m.querySelector("#s").onclick = async () => {
      const body = { name: m.querySelector("#mn").value, category: m.querySelector("#mc").value, price: Number(m.querySelector("#mp").value),
        active: m.querySelector("#ma").value === "1", sku: m.querySelector("#ms").value || null, barcode: m.querySelector("#mb").value || null,
        recipe: lines.map((l) => ({ ingredient_id: l.ingredient_id, qty: Number(l.qty) })) };
      try {
        await api(item ? `/api/menu-items/${item.id}` : "/api/menu-items", { method: item ? "PUT" : "POST", json: body });
        closeModal(); toast("Saved"); await loadMenu(); setupMenu(sv);
      } catch (e) { m.querySelector("#merr").textContent = e.message; }
    };
  };
  document.getElementById("add").onclick = () => edit(null);
  sv.querySelectorAll("[data-e]").forEach((b) => b.addEventListener("click", () => edit(items.find((i) => i.id === Number(b.dataset.e)))));
}

async function setupIngredients(sv) {
  const [ings, sups] = await Promise.all([api("/api/ingredients"), can("purchasing") ? api("/api/suppliers") : []]);
  sv.innerHTML = `<div class="panel">
    <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Stock items (ingredients & resale goods)</h2><div class="shrink"><button class="btn primary" id="add">+ Add item</button></div></div>
    <div class="small muted" style="margin-bottom:8px"><span class="st amber">Low</span> at or below the reorder level · <span class="st red">Critical</span> at or below the critical level (default ${esc(S.state.settings.critical_pct_of_reorder)}% of reorder level) or out of stock.</div>
    <div class="tablewrap"><table><thead><tr><th>Name</th><th>SKU</th><th>Unit</th><th class="num">Purchase cost</th><th class="num">Reorder at</th><th class="num">Critical at</th><th>Count</th><th></th></tr></thead><tbody>
    ${ings.map((i) => `<tr class="${i.active ? "" : "void"}"><td><b>${esc(i.name)}</b>${i.category ? ` <span class="small muted">${esc(i.category)}</span>` : ""}</td><td class="small">${esc(i.sku || "")}</td><td>${i.unit}</td>
      <td class="num">${money(i.cost_per_unit * big[i.unit][1])} / ${bigUnit(i.unit)}</td><td class="num">${fmtQty(i.par_level, i.unit)}</td><td class="num">${i.critical_level ? fmtQty(i.critical_level, i.unit) : "auto"}</td>
      <td>${i.count_daily ? `<span class="tag ok">daily</span>` : `<span class="tag grey">weekly</span>`}</td>
      <td><button class="btn" data-e="${i.id}">Edit</button></td></tr>`).join("")}</tbody></table></div></div>`;
  const edit = (it) => {
    const i = it || { name: "", unit: "g", cost_per_unit: 0, par_level: 0, critical_level: 0, count_daily: 1, active: 1, sku: "", barcode: "", category: "", supplier_id: null };
    const m = modal(`<h2>${it ? "Edit" : "New"} stock item</h2>
      <div class="row"><div><label>Name</label><input id="n" value="${esc(i.name)}" maxlength="80"></div>
        <div><label>Measured in</label><select id="u">${["g", "ml", "pcs"].map((u) => `<option value="${u}" ${i.unit === u ? "selected" : ""}>${{ g: "weight (kg / g)", ml: "volume (L / ml)", pcs: "pieces" }[u]}</option>`).join("")}</select></div></div>
      <div class="row"><div><label>Purchase cost per <span class="bu">${bigUnit(i.unit)}</span> (${esc(cur())})</label><input id="c" type="number" min="0" step="any" value="${+(i.cost_per_unit * big[i.unit][1]).toFixed(4)}"></div>
        <div><label>Reorder at (<span class="bu">${bigUnit(i.unit)}</span>)</label><input id="p" type="number" min="0" step="any" value="${toBig(i.par_level, i.unit)}"></div>
        <div><label>Critical at (<span class="bu">${bigUnit(i.unit)}</span>, 0 = auto)</label><input id="cr" type="number" min="0" step="any" value="${toBig(i.critical_level || 0, i.unit)}"></div></div>
      <div class="row"><div><label>SKU</label><input id="sk" value="${esc(i.sku || "")}" maxlength="40"></div><div><label>Barcode</label><input id="bc" value="${esc(i.barcode || "")}" maxlength="40"></div>
        <div><label>Category</label><input id="ca" value="${esc(i.category || "")}" maxlength="40"></div></div>
      <div class="row"><div><label>Counted at closing</label><select id="d"><option value="1">Every day (meat, rice, oil, drinks)</option><option value="0" ${i.count_daily ? "" : "selected"}>Weekly (spices, onions…)</option></select></div>
        <div><label>Main supplier</label><select id="sp"><option value="">—</option>${sups.map((s) => `<option value="${s.id}" ${s.id === i.supplier_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
        <div><label>Status</label><select id="a"><option value="1">Active</option><option value="0" ${i.active ? "" : "selected"}>Inactive / discontinued</option></select></div></div>
      <div class="err" id="ierr"></div>
      <div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="s">Save</button></div>`, "wide");
    m.querySelector("#bc").addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    m.querySelector("#u").onchange = () => m.querySelectorAll(".bu").forEach((s) => (s.textContent = bigUnit(m.querySelector("#u").value)));
    m.querySelector("#x").onclick = closeModal;
    m.querySelector("#s").onclick = async () => {
      const unit = m.querySelector("#u").value;
      const body = { name: m.querySelector("#n").value, unit, cost_per_unit: Number(m.querySelector("#c").value || 0) / big[unit][1],
        par_level: fromBig(m.querySelector("#p").value || 0, unit), critical_level: fromBig(m.querySelector("#cr").value || 0, unit),
        count_daily: m.querySelector("#d").value === "1", active: m.querySelector("#a").value === "1",
        sku: m.querySelector("#sk").value || null, barcode: m.querySelector("#bc").value || null, category: m.querySelector("#ca").value || null,
        supplier_id: m.querySelector("#sp").value ? Number(m.querySelector("#sp").value) : null };
      try { await api(it ? `/api/ingredients/${it.id}` : "/api/ingredients", { method: it ? "PUT" : "POST", json: body }); closeModal(); toast("Saved"); setupIngredients(sv); }
      catch (e) { m.querySelector("#ierr").textContent = e.message; }
    };
  };
  document.getElementById("add").onclick = () => edit(null);
  sv.querySelectorAll("[data-e]").forEach((b) => b.addEventListener("click", () => edit(ings.find((i) => i.id === Number(b.dataset.e)))));
}

async function setupExcel(sv) {
  sv.innerHTML = `<div class="panel"><h2>Menu, recipes & items (Excel / CSV)</h2>
    <p>Download the file first: it contains everything currently in the system (or examples if empty) and a <b>How to fill</b> sheet.
       A CSV with the Ingredients columns also works for stock items.</p>
    <div class="row" style="max-width:640px"><div class="shrink"><button class="btn" id="dl">Download Excel</button></div>
      ${can("data.export") ? `<div class="shrink"><button class="btn" id="dls">Stock CSV</button></div>` : ""}</div>
    <h3 style="margin-top:20px">Import</h3>
    <div class="note">Check first: nothing is saved until you press <b>Import</b>, and if any row has a problem nothing is saved. Importing replaces the recipe of every dish in the file; an item's unit can't change once it has stock history.</div>
    <div class="row"><div><input type="file" id="f" accept=".xlsx,.csv"></div><div class="shrink"><button class="btn" id="pv">Check file</button></div><div class="shrink"><button class="btn primary" id="up" disabled>Import</button></div></div>
    <div class="err" id="xerr"></div><div id="xok"></div></div>
    <div class="panel"><h2>Import stock history (daily stock-movement workbook)</h2>
      <p class="small">For sheets with <i>opening / additions / closing / actual sales / system sales / variance</i>, one sheet per day (e.g. "oct 2026 stock movement.xlsx").
        Each sheet becomes a closed, read-only day; the last closing becomes the next opening stock. The workbook itself is never changed.</p>
      <div class="row"><div><input type="file" id="wf" accept=".xlsx"></div>
        <div class="shrink"><label>Month (if not in file name)</label><input id="wm" type="month" style="width:170px"></div>
        <div class="shrink"><button class="btn" id="wa">Analyse workbook</button></div></div>
      <div class="err" id="werr"></div><div id="wout"></div></div>`;
  document.getElementById("dl").onclick = () => download("/api/excel", "cfc-recipes.xlsx");
  document.getElementById("dls")?.addEventListener("click", () => download("/api/export/stock.csv", "stock.csv"));
  const send = async (preview) => {
    const f = document.getElementById("f").files[0];
    document.getElementById("xerr").textContent = ""; document.getElementById("xok").textContent = "";
    if (!f) return (document.getElementById("xerr").textContent = "Choose a file first.");
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await api("/api/excel" + (preview ? "?preview=true" : ""), { method: "POST", body: fd });
      document.getElementById("xok").innerHTML = `<div class="info" style="margin-top:10px">${preview ? "File is OK. It would import" : "Imported ✓"}
        ${r.ingredients} items (${r.new_ingredients} new, ${r.updated} updated), ${r.menu_items} menu items, ${r.recipe_lines} recipe lines.
        ${r.changes?.length ? `<br><b>Changes to existing prices/costs:</b> ${r.changes.map((c) => esc(`${c.menu_item || c.ingredient}: ${c.field} ${c.old} → ${c.new}`)).join("; ")}` : ""}</div>`;
      document.getElementById("up").disabled = !preview;
      if (!preview) await loadMenu();
    } catch (e) { document.getElementById("xerr").textContent = e.message; document.getElementById("up").disabled = true; }
  };
  document.getElementById("f").onchange = () => { document.getElementById("up").disabled = true; };
  document.getElementById("pv").onclick = () => send(true);
  document.getElementById("up").onclick = () => send(false);
  document.getElementById("wa").onclick = () => analyseWorkbook();
}

async function analyseWorkbook() {
  const f = document.getElementById("wf").files[0];
  const out = document.getElementById("wout"), err = document.getElementById("werr");
  err.textContent = ""; out.innerHTML = "";
  if (!f) return (err.textContent = "Choose the workbook first.");
  const fd = new FormData(); fd.append("file", f);
  const wm = document.getElementById("wm").value;
  const qs = wm ? `year=${Number(wm.slice(0, 4))}&month=${Number(wm.slice(5, 7))}` : "";
  let r;
  try { r = await api("/api/import/stock-workbook" + (qs ? "?" + qs : ""), { method: "POST", body: fd }); } catch (e) { return (err.textContent = e.message); }
  const lvl = { error: "bad", warning: "warn", info: "grey" };
  const units = ["", "kg", "g", "L", "ml", "pcs"];
  out.innerHTML = `
    ${r.already_imported ? `<div class="note">This exact file was already imported on ${esc(r.already_imported)}. Importing again is blocked.</div>` : ""}
    <div class="info">Period: <b>${r.month ? esc(r.year + "-" + String(r.month).padStart(2, "0")) : "unknown — choose the month above and analyse again"}</b> ·
      ${r.sheets.length} sheets (${r.sheets.map((s) => esc(s.name)).join(", ")}) ·
      <span class="tag bad">${r.counts.error} errors</span> <span class="tag warn">${r.counts.warning} warnings</span> <span class="tag grey">${r.counts.info} notes</span></div>
    <h3>1. Confirm the unit each item is counted in</h3>
    <div class="small muted" style="margin-bottom:6px">Suggestions are only suggestions (whole numbers everywhere → pieces). Nothing is imported until every unit is confirmed.</div>
    <div class="tablewrap"><table><thead><tr><th>Workbook item</th><th>Unit in workbook</th><th>Matches existing item</th><th class="num">Period usage</th><th class="num">POS sold</th><th class="num">Variance</th></tr></thead><tbody>
      ${r.items.map((i) => `<tr><td><b>${esc(i.item)}</b></td>
        <td><select data-wu="${esc(i.key)}" aria-label="Unit for ${esc(i.item)}">${units.map((u) => `<option value="${u}" ${u && u === i.suggested_unit ? "selected" : ""}>${u || "— choose —"}</option>`).join("")}</select>
          ${i.suggested_unit ? `<div class="small muted">suggested</div>` : `<div class="small" style="color:var(--bad)">no safe suggestion</div>`}</td>
        <td class="small">${i.existing ? `${esc(i.existing.name)} (${esc(i.existing.unit)})` : "new item"}</td>
        <td class="num">${i.total_actual}</td><td class="num">${i.total_system}</td>
        <td class="num" style="${i.total_variance < 0 ? "color:var(--bad)" : ""}">${i.total_variance > 0 ? "+" : ""}${i.total_variance}</td></tr>`).join("")}
    </tbody></table></div>
    <h3 style="margin-top:14px">2. Review discrepancies</h3>
    <div class="tablewrap" style="max-height:340px;overflow:auto"><table><thead><tr><th>Level</th><th>Sheet</th><th>Item</th><th>Finding</th></tr></thead><tbody>
      ${r.issues.filter((i) => i.code !== "split_count").map((i) => `<tr><td><span class="tag ${lvl[i.level]}">${i.level}</span></td><td>${esc(i.sheet || "")}</td><td>${esc(i.item || "")}</td><td class="small">${esc(i.message)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="err" id="wcerr"></div>
    <div style="margin-top:12px"><button class="btn primary" id="wc" ${r.counts.error || r.already_imported || !r.month ? "disabled" : ""}>3. Import as history</button></div>`;
  document.getElementById("wc")?.addEventListener("click", async (e) => {
    const mapping = {};
    let missing = [];
    out.querySelectorAll("[data-wu]").forEach((s) => { if (s.value) mapping[s.dataset.wu] = { unit: s.value }; else missing.push(s.dataset.wu); });
    if (missing.length) return (document.getElementById("wcerr").textContent = "Choose a unit for: " + missing.join(", "));
    if (!confirm("Import these days as closed history? This can't be undone from the app (a database backup is recommended first).")) return;
    const fd2 = new FormData(); fd2.append("file", f); fd2.append("mapping", JSON.stringify(mapping));
    e.target.disabled = true;
    try {
      const res = await api("/api/import/stock-workbook?commit=true" + (qs ? "&" + qs : ""), { method: "POST", body: fd2 });
      out.innerHTML = `<div class="info">Imported ✓ ${res.days} days, ${res.lines} stock lines, ${res.movements} movements. New items: ${res.new_ingredients.map(esc).join(", ") || "none"}.
        Set purchase costs and reorder levels under <b>Items & thresholds</b> so status colours and valuation work.</div>`;
      await refreshState();
    } catch (x) { document.getElementById("wcerr").textContent = x.message; e.target.disabled = false; }
  });
}

async function setupStaff(sv) {
  const staff = await api("/api/staff");
  const ROLE = { cashier: "Cashier", stock_controller: "Stock controller", manager: "Manager", admin: "Admin" };
  sv.innerHTML = `<div class="panel">
    <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Staff</h2><div class="shrink"><button class="btn primary" id="add">+ Add staff</button></div></div>
    <div class="info"><b>Cashier</b>: sell, cash shift, record waste, see stock. <b>Stock controller</b>: suppliers, purchase orders, receive goods, waste (no selling).
      <b>Manager</b>: everything above plus open/close day, voids, refunds, discounts, adjustments, menu & prices, reports, imports. <b>Admin</b>: also staff, settings, audit log, licence.
      New or reset PINs must be changed by the person at their next login.</div>
    <table><thead><tr><th>Name</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>
    ${staff.map((s) => `<tr class="${s.active ? "" : "void"}"><td>${esc(s.name)}</td><td>${esc(ROLE[s.role] || s.role)}</td><td>${s.active ? "active" : "disabled"}${s.must_change_pin ? ` <span class="tag warn">PIN change pending</span>` : ""}</td><td><button class="btn" data-e="${s.id}">Edit</button></td></tr>`).join("")}
    </tbody></table></div>`;
  const edit = (st) => {
    const s = st || { name: "", role: "cashier", active: 1 };
    const m = modal(`<h2>${st ? "Edit" : "New"} staff</h2>
      <label>Name</label><input id="n" value="${esc(s.name)}" maxlength="60">
      <label>${st ? "Reset PIN (leave blank to keep)" : "Temporary PIN (4–6 digits)"}</label><input id="p" type="password" inputmode="numeric" maxlength="6" autocomplete="new-password">
      <div class="row"><div><label>Role</label><select id="r">${Object.entries(ROLE).map(([k, v]) => `<option value="${k}" ${s.role === k ? "selected" : ""}>${v}</option>`).join("")}</select></div>
        <div><label>Status</label><select id="a"><option value="1">Active</option><option value="0" ${s.active ? "" : "selected"}>Disabled</option></select></div></div>
      <div class="err" id="serr"></div>
      <div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="s">Save</button></div>`);
    m.querySelector("#x").onclick = closeModal;
    m.querySelector("#s").onclick = async () => {
      const body = { name: m.querySelector("#n").value, pin: m.querySelector("#p").value, role: m.querySelector("#r").value, active: m.querySelector("#a").value === "1" };
      try { await api(st ? `/api/staff/${st.id}` : "/api/staff", { method: st ? "PUT" : "POST", json: body }); closeModal(); toast("Saved"); setupStaff(sv); }
      catch (e) { m.querySelector("#serr").textContent = e.message; }
    };
  };
  document.getElementById("add").onclick = () => edit(null);
  sv.querySelectorAll("[data-e]").forEach((b) => b.addEventListener("click", () => edit(staff.find((x) => x.id === Number(b.dataset.e)))));
}

async function setupSettings(sv) {
  const s = S.state.settings;
  const sel = (id, opts, v) => `<select id="${id}">${opts.map(([k, l]) => `<option value="${k}" ${String(v) === k ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  sv.innerHTML = `<div class="panel" style="max-width:760px"><h2>Settings</h2>
    <h3>Business & receipts</h3>
    <div class="row"><div><label>Business name</label><input id="bn" value="${esc(s.business_name)}" maxlength="80"></div><div><label>Address / town</label><input id="ba" value="${esc(s.business_address)}" maxlength="120"></div></div>
    <div class="row"><div><label>KRA PIN on receipts (optional)</label><input id="bp" value="${esc(s.business_pin)}" maxlength="20"></div><div><label>Receipt footer</label><input id="rf" value="${esc(s.receipt_footer)}" maxlength="200"></div></div>
    <h3 style="margin-top:16px">Tax</h3>
    <div class="note">Set this only after confirming the business's VAT position with its accountant. These receipts are not KRA eTIMS fiscal receipts.</div>
    <div class="row"><div><label>Tax rate % (0 = off)</label><input id="tr" type="number" min="0" max="50" step="0.01" value="${esc(s.tax_rate_pct)}"></div>
      <div><label>Menu prices…</label>${sel("ti", [["1", "include tax"], ["0", "exclude tax (added at checkout)"]], s.tax_inclusive)}</div>
      <div><label>Tax label</label><input id="tl" value="${esc(s.tax_label)}" maxlength="12"></div></div>
    <h3 style="margin-top:16px">Selling & cash</h3>
    <div class="row"><div><label>Cashier may discount up to % without a manager</label><input id="md" type="number" min="0" max="100" step="1" value="${esc(s.cashier_max_discount_pct)}"></div>
      <div><label>Cash shift before selling</label>${sel("rs", [["1", "required"], ["0", "optional"]], s.require_shift)}</div>
      <div><label>Log out after idle (minutes)</label><input id="si" type="number" min="5" max="1440" value="${esc(s.session_idle_minutes)}"></div></div>
    <h3 style="margin-top:16px">Stock</h3>
    <div class="row"><div><label>Closing tolerance (%)</label><input id="tp" type="number" step="0.5" min="0" max="20" value="${esc(s.variance_tolerance_pct)}"></div>
      <div><label>Critical = % of reorder level (if no own level)</label><input id="cp" type="number" min="0" max="100" value="${esc(s.critical_pct_of_reorder)}"></div>
      <div><label>Reorder suggestion covers (days)</label><input id="rc" type="number" min="0" max="60" value="${esc(s.reorder_cover_days)}"></div></div>
    <div class="row"><div><label>Purchase cost updates when goods are received</label>${sel("cm", [["manual", "Never – I set costs manually"], ["last_purchase", "Use the last purchase cost"], ["weighted_average", "Weighted average cost"]], s.cost_method)}</div></div>
    <div class="err" id="serr"></div>
    <div style="margin-top:14px"><button class="btn primary" id="s">Save settings</button></div></div>`;
  document.getElementById("s").onclick = async () => {
    const v = (id) => document.getElementById(id).value;
    try {
      await api("/api/settings", { method: "PUT", json: {
        business_name: v("bn"), business_address: v("ba"), business_pin: v("bp"), receipt_footer: v("rf"),
        variance_tolerance_pct: Number(v("tp")), tax_rate_pct: Number(v("tr") || 0), tax_inclusive: v("ti") === "1", tax_label: v("tl") || "VAT",
        cashier_max_discount_pct: Number(v("md") || 0), critical_pct_of_reorder: Number(v("cp") || 0), reorder_cover_days: Number(v("rc") || 0),
        cost_method: v("cm"), require_shift: v("rs") === "1", session_idle_minutes: Number(v("si") || 720) } });
      await refreshState(); toast("Saved"); render();
    } catch (e) { document.getElementById("serr").textContent = e.message; }
  };
}

async function setupAudit(sv) {
  sv.innerHTML = `<div class="panel"><div class="row" style="max-width:520px;margin-bottom:10px"><div><input id="af" placeholder="Filter by action, e.g. order.refund, staff, settings"></div><div class="shrink"><button class="btn" id="ag">Filter</button></div></div><div id="al" class="tablewrap"></div></div>`;
  const go = async () => {
    const rows = await api("/api/audit?limit=500" + (document.getElementById("af").value ? "&action=" + encodeURIComponent(document.getElementById("af").value) : "")).catch(fail);
    if (!rows) return;
    document.getElementById("al").innerHTML = `<table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Details</th><th>IP</th></tr></thead><tbody>
      ${rows.map((a) => `<tr><td class="small">${esc(a.at)}</td><td>${esc(a.staff || "—")}</td><td><span class="tag grey">${esc(a.action)}</span></td>
        <td class="small" style="max-width:520px;word-break:break-word">${esc(a.details || "")}</td><td class="small muted">${esc(a.ip || "")}</td></tr>`).join("")}</tbody></table>`;
  };
  document.getElementById("ag").onclick = go;
  go();
}

async function setupLicence(sv) {
  const l = await api("/api/licence");
  const STC = { valid: "green", grace: "amber", expired: "red", revoked: "red", invalid: "red", unlicensed: "grey", wrong_server: "red" };
  sv.innerHTML = `<div class="panel" style="max-width:860px"><h2>Licence</h2>
    <div class="${l.enforced ? "info" : "note"}">Mode: <b>${l.enforced ? "ENFORCED (production)" : "Development — licence is checked and shown but never blocks anything"}</b>.
      ${l.enforced ? "" : "Production enforcement is switched on only by the vendor with the CFC_LICENCE_MODE=enforce server setting."}</div>
    <table><tbody>
      <tr><td>Status</td><td><span class="st ${STC[l.state] || "grey"}">${esc(l.state)}</span> ${esc(l.message || "")}</td></tr>
      ${l.customer ? `<tr><td>Licensed to</td><td>${esc(l.customer)} · plan ${esc(l.plan || "")} · id ${esc(l.lid)}</td></tr>
      <tr><td>Expires</td><td>${esc(l.expires)}${l.days_left != null ? ` (${l.days_left} days)` : ""} · grace ${esc(l.grace_days)} days</td></tr>
      <tr><td>Selling devices</td><td>${esc(l.terminals)} used of ${esc(l.max_devices || "unlimited")}</td></tr>` : ""}
      <tr><td>This server's id</td><td><code>${esc(l.server_id)}</code></td></tr>
      ${l.dev_key ? `<tr><td>Public key</td><td class="small">development test key (production key not installed)</td></tr>` : ""}
    </tbody></table>
    <label>Install or renew licence key</label><textarea id="lk" rows="3" spellcheck="false"></textarea>
    <div class="row" style="margin-top:8px"><div class="shrink"><button class="btn primary" id="li">Install licence</button></div><div class="shrink"><button class="btn" id="lr">Apply revocation list</button></div></div>
    <div class="err" id="lerr"></div>
    <h3 style="margin-top:18px">Devices</h3>
    <div class="small muted">Every till/tablet/PC that has used this server. Only devices that have made a sale count towards the licence.</div>
    <table><thead><tr><th>Device</th><th>Label</th><th>Last seen</th><th>Browser</th><th></th></tr></thead><tbody>
      ${l.devices.map((d) => `<tr class="${d.active ? "" : "void"}"><td class="small"><code>${esc(d.id)}</code>${d.id === terminalId() ? " (this device)" : ""}</td><td>${esc(d.label || "")}</td>
        <td class="small">${esc(d.last_seen)}</td><td class="small muted">${esc((d.user_agent || "").slice(0, 60))}</td>
        <td><button class="btn" data-dl="${esc(d.id)}" data-a="${d.active ? 0 : 1}" data-label="${esc(d.label || "")}">${d.active ? "Deactivate" : "Activate"}</button></td></tr>`).join("")}
    </tbody></table></div>`;
  const send = async (path) => {
    try { await api(path, { method: "POST", json: { token: document.getElementById("lk").value.trim() } }); toast("Done"); await refreshState(); setupLicence(sv); }
    catch (e) { document.getElementById("lerr").textContent = e.message; }
  };
  document.getElementById("li").onclick = () => send("/api/licence");
  document.getElementById("lr").onclick = () => send("/api/licence/revocations");
  sv.querySelectorAll("[data-dl]").forEach((b) => b.addEventListener("click", async () => {
    const label = prompt("Label for this device (e.g. Till 1)", b.dataset.label) ?? b.dataset.label;
    try { await api("/api/devices/" + encodeURIComponent(b.dataset.dl), { method: "PUT", json: { label, active: b.dataset.a === "1" } }); setupLicence(sv); } catch (e) { fail(e); }
  }));
}

boot();
