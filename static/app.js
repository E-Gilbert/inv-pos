/* CFC POS + Smart Inventory — front end.
 * Plain JavaScript, no build step and no internet needed: it runs fully on the LAN.
 * Quantities are stored in g / ml / pcs. Staff see kg / L when counting stock
 * and receiving deliveries, and g / ml in recipes (per plate).
 */
"use strict";

const S = {
  token: null, staff: null, state: null, menu: { categories: [], items: [] },
  cart: [], cat: "all", view: "pos", setupTab: "menu", reportDay: null,
};
const $app = document.getElementById("app");

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "KES " + Number(n || 0).toLocaleString("en-KE", { maximumFractionDigits: 0 });
const big = { g: ["kg", 1000], ml: ["L", 1000], pcs: ["pcs", 1] };
const bigUnit = (u) => big[u][0];
const toBig = (q, u) => (q == null ? null : q / big[u][1]);
const fromBig = (q, u) => (q === "" || q == null ? null : Number(q) * big[u][1]);
function fmtQty(q, u, signed = false) {
  if (q == null) return "–";
  const v = toBig(q, u);
  const s = Number(v.toFixed(u === "pcs" ? 0 : 2)).toLocaleString("en-KE", { maximumFractionDigits: 3 });
  return (signed && v > 0 ? "+" : "") + s + " " + bigUnit(u);
}
const isManager = () => S.staff && ["manager", "admin"].includes(S.staff.role);
const isAdmin = () => S.staff && S.staff.role === "admin";
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.token) headers["X-Token"] = S.token;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  let res;
  try { res = await fetch(path, { ...opts, headers }); }
  catch { throw new Error("Can't reach the POS server. Check the network cable / Wi-Fi."); }
  if (res.status === 401 && path !== "/api/login") { logout(true); throw new Error("Session expired. Log in again."); }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res;
}

function toast(msg, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), bad ? 6000 : 2500);
}
const fail = (e) => toast(e.message || String(e), true);

function modal(html, cls = "") {
  closeModal();
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  bg.id = "modal";
  bg.innerHTML = `<div class="modal ${cls}">${html}</div>`;
  bg.addEventListener("click", (e) => { if (e.target === bg) closeModal(); });
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

// ------------------------------------------------------------------ session
async function boot() {
  S.token = store.get("cfc_token");
  if (!S.token) return renderLogin();
  try { await refreshState(); await loadMenu(); render(); }
  catch { renderLogin(); }
}
async function refreshState() {
  S.state = await api("/api/state");
  S.staff = S.state.staff;
}
async function loadMenu() { S.menu = await api("/api/menu"); }
function logout(silent) {
  if (!silent && S.token) api("/api/logout", { method: "POST" }).catch(() => {});
  S.token = null; S.staff = null; S.cart = [];
  store.set("cfc_token", null);
  renderLogin();
}

function renderLogin() {
  let pin = "";
  $app.innerHTML = `
    <div class="login"><div class="card">
      <h1>City Food Court</h1><div class="muted">Enter your PIN</div>
      <div class="pin-dots" id="dots"></div>
      <div class="pinpad">
        ${[1,2,3,4,5,6,7,8,9].map((n) => `<button data-k="${n}">${n}</button>`).join("")}
        <button data-k="clr">C</button><button data-k="0">0</button><button data-k="ok" style="background:var(--brand);color:#fff">OK</button>
      </div>
      <div class="err" id="err"></div>
    </div></div>`;
  const dots = () => { document.getElementById("dots").innerHTML = Array.from({ length: Math.max(4, pin.length) }, (_, i) => `<span class="${i < pin.length ? "on" : ""}"></span>`).join(""); };
  const submit = async () => {
    if (pin.length < 4) return;
    try {
      const r = await api("/api/login", { method: "POST", json: { pin } });
      S.token = r.token; store.set("cfc_token", r.token);
      S.view = "pos";
      await refreshState(); await loadMenu(); render();
    } catch (e) { pin = ""; dots(); document.getElementById("err").textContent = e.message; }
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

// ------------------------------------------------------------------ shell
const VIEWS = [
  ["pos", "POS", () => true],
  ["orders", "Orders", () => true],
  ["stock", "Stock", () => true],
  ["day", "Open / Close day", isManager],
  ["reports", "Reports", isManager],
  ["setup", "Setup", isManager],
];

function render() {
  const day = S.state.day;
  $app.innerHTML = `
    <header class="top">
      <div class="brand">${esc(S.state.business_name)}</div>
      <nav>${VIEWS.filter((v) => v[2]()).map(([k, l]) => `<button data-v="${k}" class="${S.view === k ? "active" : ""}">${l}</button>`).join("")}</nav>
      <div class="who"><span>${esc(S.staff.name)} · ${esc(S.staff.role)}</span><button id="logout">Log out</button></div>
    </header>
    <div class="daybar ${day ? "open" : "closed"}">
      ${day ? `<b>Day open:</b> ${esc(day.business_date)} <span class="muted small">since ${esc(day.opened_at?.slice(11, 16) || "")}</span>`
            : `<b>No day open.</b> Sales and stock entries are locked until a manager opens the day.
               ${isManager() ? `<button class="btn" id="goopen">Open day</button>` : ""}`}
    </div>
    <main id="view"></main>`;
  $app.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => { S.view = b.dataset.v; render(); }));
  document.getElementById("logout").onclick = () => logout();
  document.getElementById("goopen")?.addEventListener("click", () => { S.view = "day"; render(); });
  const v = document.getElementById("view");
  ({ pos: viewPOS, orders: viewOrders, stock: viewStock, day: viewDay, reports: viewReports, setup: viewSetup }[S.view] || viewPOS)(v);
}

// ------------------------------------------------------------------ POS
function viewPOS(el) {
  const cats = S.menu.categories.filter((c) => S.menu.items.some((i) => i.category_id === c.id));
  const items = S.menu.items.filter((i) => S.cat === "all" || i.category_id === S.cat);
  el.innerHTML = `
    <div class="pos">
      <div>
        <div class="cats">
          <button data-c="all" class="${S.cat === "all" ? "active" : ""}">All</button>
          ${cats.map((c) => `<button data-c="${c.id}" class="${S.cat === c.id ? "active" : ""}">${esc(c.name)}</button>`).join("")}
        </div>
        <div class="items">
          ${items.map((i) => `<button class="item" data-i="${i.id}">
              <span class="n">${esc(i.name)}</span>
              <span><span class="p">${money(i.price)}</span>${i.recipe_lines ? "" : `<div class="nr">no recipe – stock not tracked</div>`}</span>
            </button>`).join("") || `<div class="empty">No menu items yet. ${isManager() ? "Add them in Setup or import the Excel sheet." : ""}</div>`}
        </div>
      </div>
      <div class="panel cart" id="cart"></div>
    </div>`;
  el.querySelectorAll(".cats button").forEach((b) => b.addEventListener("click", () => { S.cat = b.dataset.c === "all" ? "all" : Number(b.dataset.c); viewPOS(el); }));
  el.querySelectorAll(".item").forEach((b) => b.addEventListener("click", () => addToCart(Number(b.dataset.i))));
  renderCart();
}

function addToCart(id) {
  const line = S.cart.find((l) => l.id === id);
  if (line) line.qty++;
  else { const it = S.menu.items.find((i) => i.id === id); S.cart.push({ id, name: it.name, price: it.price, qty: 1 }); }
  renderCart();
}
const cartTotal = () => S.cart.reduce((s, l) => s + l.price * l.qty, 0);

function renderCart() {
  const el = document.getElementById("cart");
  if (!el) return;
  const total = cartTotal();
  el.innerHTML = `
    <h3>Current order</h3>
    <div class="lines">
      ${S.cart.map((l, i) => `
        <div class="line">
          <div><div><b>${esc(l.name)}</b></div><div class="muted small">${money(l.price)} each</div></div>
          <div class="qty"><button data-m="${i}">−</button><b>${l.qty}</b><button data-p="${i}">+</button></div>
          <div class="num"><b>${money(l.price * l.qty)}</b></div>
        </div>`).join("") || `<div class="empty">Tap items to add them</div>`}
    </div>
    <div class="total"><span>Total</span><span class="num">${money(total)}</span></div>
    ${S.state.day ? `
      <div class="paybtns">
        <button class="btn big ok" data-pay="cash" ${S.cart.length ? "" : "disabled"}>Cash</button>
        <button class="btn big ok" data-pay="mpesa" ${S.cart.length ? "" : "disabled"}>M-Pesa</button>
        <button class="btn big ok" data-pay="card" ${S.cart.length ? "" : "disabled"}>Card</button>
      </div>` : `<div class="note">Open the day to start selling.</div>`}
    ${S.cart.length ? `<button class="btn block danger" id="clear" style="margin-top:8px">Clear order</button>` : ""}`;
  el.querySelectorAll("[data-p]").forEach((b) => b.addEventListener("click", () => { S.cart[b.dataset.p].qty++; renderCart(); }));
  el.querySelectorAll("[data-m]").forEach((b) => b.addEventListener("click", () => {
    const l = S.cart[b.dataset.m]; l.qty--; if (l.qty <= 0) S.cart.splice(b.dataset.m, 1); renderCart();
  }));
  el.querySelectorAll("[data-pay]").forEach((b) => b.addEventListener("click", () => payModal(b.dataset.pay)));
  document.getElementById("clear")?.addEventListener("click", () => { S.cart = []; renderCart(); });
}

function payModal(method) {
  const total = cartTotal();
  const quick = [...new Set([total, Math.ceil(total / 100) * 100, Math.ceil(total / 500) * 500, Math.ceil(total / 1000) * 1000])];
  const body = {
    cash: `<label>Cash received</label><input id="tend" type="number" inputmode="numeric" autofocus>
           <div class="quick">${quick.map((q) => `<button class="btn" data-q="${q}">${q.toLocaleString()}</button>`).join("")}</div>
           <div style="margin-top:10px;font-size:18px">Change: <b id="chg">–</b></div>`,
    mpesa: `<div class="info">Ask the customer to pay <b>${money(total)}</b>, then type the confirmation code from the M-Pesa message.</div>
            <label>M-Pesa code (10 characters)</label><input id="ref" maxlength="10" autofocus style="text-transform:uppercase;font-size:20px;letter-spacing:2px">`,
    card: `<div class="info">Charge <b>${money(total)}</b> on the card machine, then confirm.</div>
           <label>Card slip / approval no. (optional)</label><input id="ref" autofocus>`,
  }[method];
  const m = modal(`
    <h2>${{ cash: "Cash", mpesa: "M-Pesa", card: "Card" }[method]} payment · ${money(total)}</h2>
    ${body}
    <div class="err" id="perr"></div>
    <div class="actions"><button class="btn" id="cancel">Cancel</button><button class="btn primary big" id="confirm">Confirm payment</button></div>`);
  const tend = m.querySelector("#tend");
  const upd = () => { const v = Number(tend.value); m.querySelector("#chg").textContent = v >= total ? money(v - total) : "–"; };
  tend?.addEventListener("input", upd);
  m.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("click", () => { tend.value = b.dataset.q; upd(); }));
  m.querySelector("#cancel").onclick = closeModal;
  const confirm = async () => {
    const btn = m.querySelector("#confirm");
    btn.disabled = true;
    try {
      const order = await api("/api/orders", { method: "POST", json: {
        items: S.cart.map((l) => ({ menu_item_id: l.id, qty: l.qty })),
        payment_method: method,
        amount_tendered: method === "cash" ? Number(tend.value || 0) : null,
        payment_ref: m.querySelector("#ref")?.value || null,
      } });
      S.cart = [];
      renderCart();
      receiptModal(order);
    } catch (e) { m.querySelector("#perr").textContent = e.message; btn.disabled = false; }
  };
  m.querySelector("#confirm").onclick = confirm;
  m.querySelectorAll("input").forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") confirm(); }));
}

function receiptHTML(o) {
  const L = (a, b) => `<div class="r"><span>${a}</span><span>${b}</span></div>`;
  return `<div class="receipt">
    <div class="c"><b style="font-size:15px">${esc(S.state.business_name)}</b><br>Kisumu</div><hr>
    ${L("Receipt #" + o.number, esc(o.business_date))}
    ${L(esc(o.created_at.slice(11, 16)), "Served by " + esc(o.cashier))}<hr>
    ${o.items.map((i) => L(`${i.qty} × ${esc(i.name)}`, Number(i.price * i.qty).toLocaleString())).join("")}<hr>
    ${L("<b>TOTAL</b>", "<b>" + money(o.total) + "</b>")}
    ${L("Paid by", { cash: "Cash", mpesa: "M-Pesa", card: "Card" }[o.payment_method])}
    ${o.payment_method === "cash" ? L("Cash", money(o.amount_tendered)) + L("Change", money(o.change)) : ""}
    ${o.payment_ref ? L("Ref", esc(o.payment_ref)) : ""}
    ${o.status === "void" ? `<hr><div class="c"><b>*** VOID ***</b><br>${esc(o.void_reason)}</div>` : ""}
    <hr><div class="c">${esc(S.state.receipt_footer)}</div>
  </div>`;
}

function receiptModal(o) {
  const m = modal(`
    <h2>${o.status === "void" ? "Voided order" : "Paid ✓"} ${o.payment_method === "cash" && o.status !== "void" ? `· Change ${money(o.change)}` : ""}</h2>
    ${receiptHTML(o)}
    <div class="actions"><button class="btn" id="print">Print receipt</button><button class="btn primary big" id="done" autofocus>New order</button></div>`);
  m.querySelector("#print").onclick = () => printHTML(receiptHTML(o), true);
  m.querySelector("#done").onclick = closeModal;
}

// ------------------------------------------------------------------ orders
async function viewOrders(el) {
  el.innerHTML = `<div class="panel"><h2>Today's orders</h2><div id="ol" class="tablewrap">Loading…</div></div>`;
  try {
    const rows = await api("/api/orders");
    const paid = rows.filter((r) => r.status === "paid");
    document.getElementById("ol").innerHTML = rows.length ? `
      <div class="kpis">
        <div class="kpi"><div class="l">Orders</div><div class="v">${paid.length}</div></div>
        <div class="kpi"><div class="l">Sales</div><div class="v">${money(paid.reduce((s, r) => s + r.total, 0))}</div></div>
        <div class="kpi"><div class="l">Voids</div><div class="v">${rows.length - paid.length}</div></div>
      </div>
      <table><thead><tr><th>#</th><th>Time</th><th>Items</th><th>Paid by</th><th class="num">Total</th><th>Cashier</th><th></th></tr></thead><tbody>
      ${rows.map((r) => `<tr class="${r.status === "void" ? "void" : ""}">
        <td>${r.number}</td><td>${esc(r.created_at.slice(11, 16))}</td><td class="small">${esc(r.summary)}</td>
        <td>${esc(r.payment_method)}${r.payment_ref ? `<div class="small muted">${esc(r.payment_ref)}</div>` : ""}</td>
        <td class="num">${money(r.total)}</td><td>${esc(r.cashier)}</td>
        <td style="white-space:nowrap"><button class="btn" data-r="${r.id}">Receipt</button>
          ${isManager() && r.status === "paid" ? `<button class="btn danger" data-v="${r.id}">Void</button>` : ""}
          ${r.status === "void" ? `<span class="tag grey" title="${esc(r.void_reason)}">void</span>` : ""}</td>
      </tr>`).join("")}</tbody></table>` : `<div class="empty">No orders yet today.</div>`;
    el.querySelectorAll("[data-r]").forEach((b) => b.addEventListener("click", async () => receiptModal(await api("/api/orders/" + b.dataset.r))));
    el.querySelectorAll("[data-v]").forEach((b) => b.addEventListener("click", () => {
      const m = modal(`<h2>Void order</h2><div class="note">Voiding returns the order's ingredients to stock and removes it from sales. This is logged with your name.</div>
        <label>Reason</label><input id="vr" autofocus placeholder="e.g. customer cancelled, wrong item rung up">
        <div class="err" id="verr"></div>
        <div class="actions"><button class="btn" id="c">Cancel</button><button class="btn primary" id="ok">Void order</button></div>`);
      m.querySelector("#c").onclick = closeModal;
      m.querySelector("#ok").onclick = async () => {
        try { await api(`/api/orders/${b.dataset.v}/void`, { method: "POST", json: { reason: m.querySelector("#vr").value } }); closeModal(); toast("Order voided"); viewOrders(el); }
        catch (e) { m.querySelector("#verr").textContent = e.message; }
      };
    }));
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ stock
async function viewStock(el) {
  el.innerHTML = `<div id="st">Loading…</div>`;
  try {
    const [stock, log, ings] = await Promise.all([api("/api/stock"), api("/api/stock/log"), api("/api/ingredients")]);
    const active = ings.filter((i) => i.active);
    const low = stock.lines.filter((l) => l.par_level > 0 && l.expected < l.par_level);
    const opts = active.map((i) => `<option value="${i.id}" data-u="${i.unit}">${esc(i.name)} (${bigUnit(i.unit)})</option>`).join("");
    document.getElementById("st").innerHTML = `
      ${!stock.day ? `<div class="note">No day is open. Stock shows once a day is opened.</div>` : ""}
      <div class="grid2">
        <div class="panel"><h3>Record waste / spoilage</h3>
          <div class="small muted">Record it when it happens, so it doesn't show up as missing stock at closing.</div>
          <div class="row"><div><label>Ingredient</label><select id="wi">${opts}</select></div>
            <div><label>Quantity (<span id="wu">${active[0] ? bigUnit(active[0].unit) : ""}</span>)</label><input id="wq" type="number" step="any" inputmode="decimal"></div></div>
          <div class="row"><div><label>Reason</label><select id="wr">${S.state.waste_reasons.map((r) => `<option>${esc(r)}</option>`).join("")}</select></div>
            <div><label>Note</label><input id="wn" placeholder="optional"></div></div>
          <div style="margin-top:10px"><button class="btn primary" id="wsave" ${stock.day ? "" : "disabled"}>Save waste</button></div>
        </div>
        <div class="panel"><h3>Receive delivery</h3>
          ${isManager() ? `
          <div class="small muted">Enter what actually arrived (weigh it), not what's on the invoice.</div>
          <div class="row"><div><label>Ingredient</label><select id="di">${opts}</select></div>
            <div><label>Quantity (<span id="du">${active[0] ? bigUnit(active[0].unit) : ""}</span>)</label><input id="dq" type="number" step="any" inputmode="decimal"></div></div>
          <div class="row"><div><label>Supplier / invoice no.</label><input id="dn" placeholder="optional"></div></div>
          <div style="margin-top:10px"><button class="btn primary" id="dsave" ${stock.day ? "" : "disabled"}>Save delivery</button></div>`
          : `<div class="muted">A manager receives deliveries.</div>`}
        </div>
      </div>
      <div class="panel">
        <h3>Stock right now ${low.length ? `<span class="tag warn">${low.length} below reorder level</span>` : ""}</h3>
        <div class="small muted" style="margin-bottom:8px">Expected = opening + delivered − used by sales − waste. This is what <i>should</i> be in the store; the real count happens at closing.</div>
        <div class="tablewrap"><table><thead><tr><th>Ingredient</th><th class="num">Opening</th><th class="num">Delivered</th><th class="num">Used by sales</th><th class="num">Waste</th><th class="num">Expected now</th><th></th></tr></thead><tbody>
        ${stock.lines.map((l) => `<tr>
          <td>${esc(l.name)} ${l.count_daily ? "" : `<span class="tag grey">weekly</span>`}</td>
          <td class="num">${fmtQty(l.opening, l.unit)}</td><td class="num">${fmtQty(l.delivered, l.unit)}</td>
          <td class="num">${fmtQty(l.sold, l.unit)}</td><td class="num">${fmtQty(l.wasted, l.unit)}</td>
          <td class="num"><b>${fmtQty(l.expected, l.unit)}</b></td>
          <td>${l.expected < 0 ? `<span class="tag bad">negative – delivery missing?</span>` : l.par_level > 0 && l.expected < l.par_level ? `<span class="tag warn">reorder</span>` : ""}</td>
        </tr>`).join("")}</tbody></table></div>
      </div>
      <div class="panel"><h3>Today's deliveries & waste</h3>
        ${log.length ? `<div class="tablewrap"><table><thead><tr><th>Time</th><th>Type</th><th>Ingredient</th><th class="num">Qty</th><th>Reason / note</th><th>By</th></tr></thead><tbody>
        ${log.map((m) => `<tr><td>${esc(m.created_at.slice(11, 16))}</td><td><span class="tag ${m.type === "waste" ? "warn" : "ok"}">${m.type}</span></td>
          <td>${esc(m.name)}</td><td class="num">${fmtQty(Math.abs(m.qty), m.unit)}</td><td>${esc([m.reason, m.note].filter(Boolean).join(" – "))}</td><td>${esc(m.staff)}</td></tr>`).join("")}
        </tbody></table></div>` : `<div class="muted">Nothing recorded yet today.</div>`}
      </div>`;
    const unitOf = (sel) => sel.selectedOptions[0]?.dataset.u;
    const bindUnit = (s, u) => { const sel = document.getElementById(s); sel?.addEventListener("change", () => { document.getElementById(u).textContent = bigUnit(unitOf(sel)); }); };
    bindUnit("wi", "wu"); bindUnit("di", "du");
    const save = async (kind, p) => {
      const sel = document.getElementById(p + "i");
      const qty = fromBig(document.getElementById(p + "q").value, unitOf(sel));
      try {
        await api("/api/stock/movement", { method: "POST", json: {
          ingredient_id: Number(sel.value), kind, qty,
          reason: kind === "waste" ? document.getElementById("wr").value : null,
          note: document.getElementById(p + "n").value || null,
        } });
        toast(kind === "waste" ? "Waste recorded" : "Delivery recorded");
        viewStock(el);
      } catch (e) { fail(e); }
    };
    document.getElementById("wsave").onclick = () => save("waste", "w");
    document.getElementById("dsave")?.addEventListener("click", () => save("delivery", "d"));
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------ open / close day
async function viewDay(el) {
  if (!S.state.day) return viewOpenDay(el);
  return viewCloseDay(el);
}

async function viewOpenDay(el) {
  const rows = await api("/api/day/opening-preview").catch(fail);
  if (!rows) return;
  const first = S.state.first_day;
  const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  el.innerHTML = `<div class="panel">
    <h2>Open the day</h2>
    ${first ? `<div class="note"><b>First day:</b> count everything in the store and enter it below. From tomorrow, opening stock is carried over automatically from the previous night's closing count.</div>`
            : `<div class="info">Opening stock is carried over from last night's closing count. Only change a number if the morning count is different, and the difference will be on record.</div>`}
    <div class="row" style="max-width:300px"><div><label>Business date</label><input id="bd" type="date" value="${today}"></div></div>
    <div class="tablewrap" style="margin-top:12px"><table><thead><tr><th>Ingredient</th><th class="num">Carried over</th><th style="width:180px">Opening count</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${esc(r.name)} ${r.count_daily ? "" : `<span class="tag grey">weekly</span>`}</td>
      <td class="num">${r.carry_over == null ? "–" : fmtQty(r.carry_over, r.unit)}</td>
      <td><div class="row" style="flex-wrap:nowrap;gap:6px"><input data-o="${r.id}" data-u="${r.unit}" type="number" step="any" inputmode="decimal"
        value="${r.carry_over == null ? "" : toBig(r.carry_over, r.unit)}"><span class="shrink small muted">${bigUnit(r.unit)}</span></div></td></tr>`).join("")}
    </tbody></table></div>
    <div class="err" id="oerr"></div>
    <div style="margin-top:12px"><button class="btn primary big" id="open">Open day</button></div>
  </div>`;
  document.getElementById("open").onclick = async () => {
    const counts = {};
    el.querySelectorAll("[data-o]").forEach((i) => { if (i.value !== "") counts[i.dataset.o] = fromBig(i.value, i.dataset.u); });
    try {
      await api("/api/day/open", { method: "POST", json: { business_date: document.getElementById("bd").value, opening_counts: counts } });
      await refreshState(); toast("Day opened"); S.view = "pos"; render();
    } catch (e) { document.getElementById("oerr").textContent = e.message; }
  };
}

async function viewCloseDay(el) {
  const stock = await api("/api/stock").catch(fail);
  if (!stock) return;
  const pct = S.state.tolerance_pct;
  const tol = (l) => (l.unit === "pcs" ? 0 : Math.max(l.opening + l.delivered, 0) * pct / 100);
  el.innerHTML = `<div class="panel">
    <h2>Close the day · ${esc(stock.day.business_date)}</h2>
    <div class="info">Count what is <b>physically left</b> and enter it. The system compares it with what should be left.
      Differences bigger than ${pct}% (any difference for items counted in pieces) need a reason before the day can close.
      What you count tonight becomes tomorrow's opening stock (carried over).</div>
    <div class="tablewrap"><table><thead><tr>
      <th>Ingredient</th><th class="num">Opening</th><th class="num">+ Delivered</th><th class="num">− Sales</th><th class="num">− Waste</th>
      <th class="num">Should be left</th><th style="width:150px">Counted</th><th class="num">Difference</th><th style="width:190px">Reason</th><th style="width:170px">Note</th></tr></thead><tbody>
    ${stock.lines.map((l) => `<tr data-row="${l.id}">
      <td><b>${esc(l.name)}</b> ${l.count_daily ? "" : `<span class="tag grey" title="Leave blank if not counted today">weekly</span>`}</td>
      <td class="num">${fmtQty(l.opening, l.unit)}</td><td class="num">${fmtQty(l.delivered, l.unit)}</td>
      <td class="num">${fmtQty(l.sold, l.unit)}</td><td class="num">${fmtQty(l.wasted, l.unit)}</td>
      <td class="num"><b>${fmtQty(l.expected, l.unit)}</b></td>
      <td><div class="row" style="flex-wrap:nowrap;gap:6px"><input data-c="${l.id}" type="number" step="any" inputmode="decimal" placeholder="${l.count_daily ? "count" : "optional"}"><span class="shrink small muted">${bigUnit(l.unit)}</span></div></td>
      <td class="num" data-var="${l.id}">–</td>
      <td><select data-r="${l.id}"><option value="">—</option>${S.state.variance_reasons.map((r) => `<option>${esc(r)}</option>`).join("")}</select></td>
      <td><input data-n="${l.id}" placeholder="explain"></td>
    </tr>`).join("")}</tbody></table></div>
    <label>Closing notes (optional)</label><textarea id="cn" rows="2"></textarea>
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
  document.getElementById("close").onclick = async () => {
    const counts = {};
    stock.lines.forEach((l) => {
      const v = el.querySelector(`[data-c="${l.id}"]`).value;
      counts[l.id] = { counted: v === "" ? null : fromBig(v, l.unit), reason: el.querySelector(`[data-r="${l.id}"]`).value, note: el.querySelector(`[data-n="${l.id}"]`).value };
    });
    if (!confirm("Close the day? Sales will be locked for this date.")) return;
    try {
      const rep = await api("/api/day/close", { method: "POST", json: { counts, notes: document.getElementById("cn").value || null } });
      await refreshState();
      S.view = "reports"; S.reportDay = rep.day.id; render();
      toast("Day closed");
    } catch (e) { document.getElementById("cerr").textContent = e.message; window.scrollTo(0, document.body.scrollHeight); }
  };
}

// ------------------------------------------------------------------ reports
async function viewReports(el) {
  if (S.reportDay) return viewReport(el, S.reportDay);
  const days = await api("/api/days").catch(fail);
  if (!days) return;
  el.innerHTML = `<div class="panel"><h2>Daily reports</h2>
    ${days.length ? `<div class="tablewrap"><table><thead><tr><th>Date</th><th>Status</th><th class="num">Sales</th><th class="num">Stock differences</th><th></th></tr></thead><tbody>
    ${days.map((d) => `<tr><td>${esc(d.business_date)}</td><td><span class="tag ${d.status === "open" ? "ok" : "grey"}">${d.status}</span></td>
      <td class="num">${money(d.sales)}</td><td class="num">${d.status === "closed" ? d.variances : "–"}</td>
      <td><button class="btn" data-d="${d.id}">View</button></td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">No days yet.</div>`}
  </div>`;
  el.querySelectorAll("[data-d]").forEach((b) => b.addEventListener("click", () => { S.reportDay = Number(b.dataset.d); viewReports(el); }));
}

function reportHTML(r) {
  const d = r.day;
  const flagged = r.stock.filter((l) => l.flag);
  return `
    <h2>${esc(S.state.business_name)} · Day report ${esc(d.business_date)} ${d.status === "open" ? `<span class="tag ok">still open</span>` : ""}</h2>
    <div class="small muted" style="margin-bottom:12px">Opened ${esc(d.opened_at || "")} by ${esc(d.opened_by_name || "")}${d.closed_at ? ` · Closed ${esc(d.closed_at)} by ${esc(d.closed_by_name || "")}` : ""}</div>
    <div class="kpis">
      <div class="kpi"><div class="l">Sales</div><div class="v">${money(r.sales.total)}</div></div>
      <div class="kpi"><div class="l">Orders</div><div class="v">${r.sales.orders}</div></div>
      ${r.sales.by_method.map((m) => `<div class="kpi"><div class="l">${esc(m.payment_method)}</div><div class="v">${money(m.amt)}</div></div>`).join("")}
      <div class="kpi"><div class="l">Recorded waste</div><div class="v">${money(r.waste_value)}</div></div>
      ${d.status === "closed" ? `<div class="kpi ${r.shortage_value > 0 ? "bad" : ""}"><div class="l">Stock shortages (value)</div><div class="v">${money(r.shortage_value)}</div></div>` : ""}
    </div>
    ${r.items_without_recipe.length ? `<div class="note">Sold without a recipe (stock not tracked): ${r.items_without_recipe.map(esc).join(", ")}</div>` : ""}
    ${d.notes ? `<div class="info"><b>Closing notes:</b> ${esc(d.notes)}</div>` : ""}
    <h3>Stock reconciliation ${flagged.length ? `<span class="tag bad">${flagged.length} outside tolerance</span>` : d.status === "closed" ? `<span class="tag ok">all within tolerance</span>` : ""}</h3>
    <div class="tablewrap"><table><thead><tr><th>Ingredient</th><th class="num">Opening</th><th class="num">+ Delivered</th><th class="num">− Sales</th><th class="num">− Waste</th>
      <th class="num">Should be left</th><th class="num">Counted</th><th class="num">Difference</th><th class="num">Value</th><th>Reason</th></tr></thead><tbody>
    ${r.stock.map((l) => `<tr class="${l.flag ? "flag" : ""}"><td>${esc(l.name)}${l.below_par ? ` <span class="tag warn">reorder</span>` : ""}</td>
      <td class="num">${fmtQty(l.opening, l.unit)}</td><td class="num">${fmtQty(l.delivered, l.unit)}</td><td class="num">${fmtQty(l.sold, l.unit)}</td>
      <td class="num">${fmtQty(l.wasted, l.unit)}</td><td class="num">${fmtQty(l.expected, l.unit)}</td><td class="num">${fmtQty(l.counted, l.unit)}</td>
      <td class="num">${l.variance == null ? "–" : fmtQty(l.variance, l.unit, true)}</td>
      <td class="num">${l.variance_value == null ? "–" : money(l.variance_value)}</td>
      <td class="small">${esc([l.reason, l.note].filter(Boolean).join(" – "))}</td></tr>`).join("")}</tbody></table></div>
    <div class="grid2" style="margin-top:14px">
      <div><h3>Items sold</h3><table><thead><tr><th>Item</th><th class="num">Qty</th><th class="num">Amount</th></tr></thead><tbody>
        ${r.items.map((i) => `<tr><td>${esc(i.name)}</td><td class="num">${i.qty}</td><td class="num">${money(i.amt)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">None</td></tr>`}</tbody></table></div>
      <div><h3>Waste recorded</h3><table><thead><tr><th>Ingredient</th><th class="num">Qty</th><th>Reason</th><th>By</th></tr></thead><tbody>
        ${r.waste.map((w) => `<tr><td>${esc(w.name)}</td><td class="num">${fmtQty(w.qty, w.unit)}</td><td class="small">${esc([w.reason, w.note].filter(Boolean).join(" – "))}</td><td>${esc(w.staff)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`}</tbody></table>
        <h3 style="margin-top:14px">Voided orders</h3><table><thead><tr><th>#</th><th class="num">Amount</th><th>Reason</th><th>By</th></tr></thead><tbody>
        ${r.sales.voids.map((v) => `<tr><td>${v.number}</td><td class="num">${money(v.total)}</td><td class="small">${esc(v.void_reason)}</td><td>${esc(v.voided_by)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None</td></tr>`}</tbody></table>
      </div>
    </div>`;
}

async function viewReport(el, id) {
  const r = await api("/api/reports/day/" + id).catch(fail);
  if (!r) return;
  el.innerHTML = `<div class="row" style="margin-bottom:12px"><div class="shrink"><button class="btn" id="back">← All days</button></div>
    <div class="shrink"><button class="btn" id="pr">Print report</button></div></div>
    <div class="panel">${reportHTML(r)}</div>`;
  document.getElementById("back").onclick = () => { S.reportDay = null; viewReports(el); };
  document.getElementById("pr").onclick = () => printHTML(reportHTML(r));
}

// ------------------------------------------------------------------ setup
async function viewSetup(el) {
  const tabs = [["menu", "Menu & recipes"], ["ingredients", "Ingredients"], ["excel", "Excel import / export"]];
  if (isAdmin()) tabs.push(["staff", "Staff"], ["settings", "Settings"]);
  el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button data-t="${k}" class="${S.setupTab === k ? "active" : ""}">${l}</button>`).join("")}</div><div id="sv"></div>`;
  el.querySelectorAll("[data-t]").forEach((b) => b.addEventListener("click", () => { S.setupTab = b.dataset.t; viewSetup(el); }));
  const sv = document.getElementById("sv");
  try {
    await ({ menu: setupMenu, ingredients: setupIngredients, excel: setupExcel, staff: setupStaff, settings: setupSettings }[S.setupTab])(sv);
  } catch (e) { fail(e); }
}

async function setupMenu(sv) {
  const [items, ings] = await Promise.all([api("/api/menu-items"), api("/api/ingredients")]);
  sv.innerHTML = `<div class="panel">
    <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Menu & recipes</h2><div class="shrink"><button class="btn primary" id="add">+ Add menu item</button></div></div>
    <div class="small muted" style="margin-bottom:10px">Food cost uses the ingredient costs. Food cost % = cost of one plate ÷ selling price.</div>
    <div class="tablewrap"><table><thead><tr><th>Item</th><th>Category</th><th class="num">Price</th><th>Recipe (per plate)</th><th class="num">Food cost</th><th class="num">%</th><th></th></tr></thead><tbody>
    ${items.map((i) => {
      const pc = i.price ? (i.food_cost / i.price) * 100 : 0;
      return `<tr class="${i.active ? "" : "void"}"><td><b>${esc(i.name)}</b></td><td>${esc(i.category || "")}</td><td class="num">${money(i.price)}</td>
        <td class="small">${i.recipe.length ? i.recipe.map((r) => `${esc(r.name)} ${r.qty}${r.unit}`).join(", ") : `<span class="tag warn">no recipe</span>`}</td>
        <td class="num">${money(i.food_cost)}</td><td class="num"><span class="tag ${pc > 40 ? "bad" : pc > 32 ? "warn" : "ok"}">${pc.toFixed(0)}%</span></td>
        <td><button class="btn" data-e="${i.id}">Edit</button></td></tr>`;
    }).join("")}</tbody></table></div></div>`;
  const edit = (item) => {
    const it = item || { name: "", category: "", price: "", active: 1, recipe: [] };
    let lines = it.recipe.map((r) => ({ ingredient_id: r.ingredient_id, qty: r.qty }));
    const act = ings.filter((i) => i.active);
    const m = modal(`<h2>${item ? "Edit" : "New"} menu item</h2>
      <div class="row"><div><label>Name</label><input id="mn" value="${esc(it.name)}"></div><div><label>Category</label><input id="mc" value="${esc(it.category || "")}" list="catlist"></div></div>
      <datalist id="catlist">${S.menu.categories.map((c) => `<option>${esc(c.name)}</option>`).join("")}</datalist>
      <div class="row"><div><label>Price (KES)</label><input id="mp" type="number" value="${it.price}"></div>
        <div><label>Status</label><select id="ma"><option value="1">On menu</option><option value="0" ${it.active ? "" : "selected"}>Hidden</option></select></div></div>
      <h3 style="margin-top:16px">Recipe – raw quantity for ONE plate</h3>
      <div class="small muted">Use raw weights (before cooking), so cooking loss is included.</div>
      <div id="rl"></div><button class="btn" id="addl" style="margin-top:8px">+ Ingredient</button>
      <div class="err" id="merr"></div>
      <div class="actions"><button class="btn" id="c">Cancel</button><button class="btn primary" id="s">Save</button></div>`, "wide");
    const drawLines = () => {
      m.querySelector("#rl").innerHTML = lines.map((l, i) => `<div class="row" style="margin-top:6px">
        <div><select data-li="${i}">${act.map((a) => `<option value="${a.id}" ${a.id === l.ingredient_id ? "selected" : ""}>${esc(a.name)} (${a.unit})</option>`).join("")}</select></div>
        <div style="max-width:140px"><input data-lq="${i}" type="number" step="any" value="${l.qty}" placeholder="qty"></div>
        <div class="shrink"><button class="btn danger" data-ld="${i}">✕</button></div></div>`).join("") || `<div class="muted small" style="margin-top:6px">No ingredients: this item won't affect stock.</div>`;
      m.querySelectorAll("[data-li]").forEach((s) => s.addEventListener("change", () => { lines[s.dataset.li].ingredient_id = Number(s.value); }));
      m.querySelectorAll("[data-lq]").forEach((s) => s.addEventListener("input", () => { lines[s.dataset.lq].qty = s.value; }));
      m.querySelectorAll("[data-ld]").forEach((s) => s.addEventListener("click", () => { lines.splice(s.dataset.ld, 1); drawLines(); }));
    };
    drawLines();
    m.querySelector("#addl").onclick = () => { if (act[0]) { lines.push({ ingredient_id: act[0].id, qty: "" }); drawLines(); } };
    m.querySelector("#c").onclick = closeModal;
    m.querySelector("#s").onclick = async () => {
      const body = { name: m.querySelector("#mn").value, category: m.querySelector("#mc").value, price: Number(m.querySelector("#mp").value),
        active: m.querySelector("#ma").value === "1", recipe: lines.map((l) => ({ ingredient_id: l.ingredient_id, qty: Number(l.qty) })) };
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
  const ings = await api("/api/ingredients");
  sv.innerHTML = `<div class="panel">
    <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Ingredients (raw materials)</h2><div class="shrink"><button class="btn primary" id="add">+ Add ingredient</button></div></div>
    <div class="tablewrap"><table><thead><tr><th>Name</th><th>Unit</th><th class="num">Cost</th><th class="num">Reorder level</th><th>Count</th><th></th></tr></thead><tbody>
    ${ings.map((i) => `<tr class="${i.active ? "" : "void"}"><td><b>${esc(i.name)}</b></td><td>${i.unit}</td>
      <td class="num">${money(i.cost_per_unit * big[i.unit][1])} / ${bigUnit(i.unit)}</td><td class="num">${fmtQty(i.par_level, i.unit)}</td>
      <td>${i.count_daily ? `<span class="tag ok">daily</span>` : `<span class="tag grey">weekly</span>`}</td>
      <td><button class="btn" data-e="${i.id}">Edit</button></td></tr>`).join("")}</tbody></table></div></div>`;
  const edit = (it) => {
    const i = it || { name: "", unit: "g", cost_per_unit: 0, par_level: 0, count_daily: 1, active: 1 };
    const m = modal(`<h2>${it ? "Edit" : "New"} ingredient</h2>
      <div class="row"><div><label>Name</label><input id="n" value="${esc(i.name)}"></div>
        <div><label>Measured in</label><select id="u">${["g", "ml", "pcs"].map((u) => `<option value="${u}" ${i.unit === u ? "selected" : ""}>${{ g: "weight (kg / g)", ml: "volume (L / ml)", pcs: "pieces" }[u]}</option>`).join("")}</select></div></div>
      <div class="row"><div><label>Cost per <span class="bu">${bigUnit(i.unit)}</span> (KES)</label><input id="c" type="number" step="any" value="${+(i.cost_per_unit * big[i.unit][1]).toFixed(4)}"></div>
        <div><label>Reorder below (<span class="bu">${bigUnit(i.unit)}</span>)</label><input id="p" type="number" step="any" value="${toBig(i.par_level, i.unit)}"></div></div>
      <div class="row"><div><label>Counted at closing</label><select id="d"><option value="1">Every day (meat, rice, oil, drinks)</option><option value="0" ${i.count_daily ? "" : "selected"}>Weekly (spices, onions…)</option></select></div>
        <div><label>Status</label><select id="a"><option value="1">Active</option><option value="0" ${i.active ? "" : "selected"}>Inactive</option></select></div></div>
      <div class="err" id="ierr"></div>
      <div class="actions"><button class="btn" id="x">Cancel</button><button class="btn primary" id="s">Save</button></div>`);
    m.querySelector("#u").onchange = () => m.querySelectorAll(".bu").forEach((s) => (s.textContent = bigUnit(m.querySelector("#u").value)));
    m.querySelector("#x").onclick = closeModal;
    m.querySelector("#s").onclick = async () => {
      const unit = m.querySelector("#u").value;
      const body = { name: m.querySelector("#n").value, unit, cost_per_unit: Number(m.querySelector("#c").value || 0) / big[unit][1],
        par_level: fromBig(m.querySelector("#p").value || 0, unit), count_daily: m.querySelector("#d").value === "1", active: m.querySelector("#a").value === "1" };
      try { await api(it ? `/api/ingredients/${it.id}` : "/api/ingredients", { method: it ? "PUT" : "POST", json: body }); closeModal(); toast("Saved"); setupIngredients(sv); }
      catch (e) { m.querySelector("#ierr").textContent = e.message; }
    };
  };
  document.getElementById("add").onclick = () => edit(null);
  sv.querySelectorAll("[data-e]").forEach((b) => b.addEventListener("click", () => edit(ings.find((i) => i.id === Number(b.dataset.e)))));
}

async function setupExcel(sv) {
  sv.innerHTML = `<div class="panel"><h2>Excel import / export</h2>
    <p>Load ingredients, menu items and recipes from a spreadsheet. Download the file first: it contains everything currently in the system
       (or examples if empty) and a <b>How to fill</b> sheet.</p>
    <button class="btn" id="dl">Download Excel (current data / template)</button>
    <h3 style="margin-top:20px">Import</h3>
    <div class="note">Importing replaces the recipe of every dish listed in the file. If any row has a problem, <b>nothing</b> is saved and you'll see the list of errors.</div>
    <div class="row"><div><input type="file" id="f" accept=".xlsx"></div><div class="shrink"><button class="btn primary" id="up">Import</button></div></div>
    <div class="err" id="xerr"></div><div id="xok"></div></div>`;
  document.getElementById("dl").onclick = async () => {
    try {
      const res = await api("/api/excel");
      const url = URL.createObjectURL(await res.blob());
      const a = Object.assign(document.createElement("a"), { href: url, download: "cfc-recipes.xlsx" });
      a.click(); setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) { fail(e); }
  };
  document.getElementById("up").onclick = async () => {
    const f = document.getElementById("f").files[0];
    document.getElementById("xerr").textContent = ""; document.getElementById("xok").textContent = "";
    if (!f) return (document.getElementById("xerr").textContent = "Choose a file first.");
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await api("/api/excel", { method: "POST", body: fd });
      document.getElementById("xok").innerHTML = `<div class="info" style="margin-top:10px">Imported ✓ ${r.ingredients} ingredients, ${r.menu_items} menu items, ${r.recipe_lines} recipe lines.</div>`;
      await loadMenu();
    } catch (e) { document.getElementById("xerr").textContent = e.message; }
  };
}

async function setupStaff(sv) {
  const staff = await api("/api/staff");
  sv.innerHTML = `<div class="panel">
    <div class="row" style="margin-bottom:10px"><h2 style="margin:0">Staff</h2><div class="shrink"><button class="btn primary" id="add">+ Add staff</button></div></div>
    <div class="info">Cashiers sell, record waste and see stock. Managers also open/close the day, receive deliveries, void orders, see reports and edit the menu. Admins also manage staff and settings.</div>
    <table><thead><tr><th>Name</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>
    ${staff.map((s) => `<tr class="${s.active ? "" : "void"}"><td>${esc(s.name)}</td><td>${s.role}</td><td>${s.active ? "active" : "disabled"}</td><td><button class="btn" data-e="${s.id}">Edit</button></td></tr>`).join("")}
    </tbody></table></div>`;
  const edit = (st) => {
    const s = st || { name: "", role: "cashier", active: 1 };
    const m = modal(`<h2>${st ? "Edit" : "New"} staff</h2>
      <label>Name</label><input id="n" value="${esc(s.name)}">
      <label>PIN (4–6 digits)${st ? " – leave blank to keep the current PIN" : ""}</label><input id="p" inputmode="numeric" maxlength="6">
      <div class="row"><div><label>Role</label><select id="r">${["cashier", "manager", "admin"].map((r) => `<option ${s.role === r ? "selected" : ""}>${r}</option>`).join("")}</select></div>
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
  sv.innerHTML = `<div class="panel" style="max-width:560px"><h2>Settings</h2>
    <label>Business name (receipts & reports)</label><input id="bn" value="${esc(S.state.business_name)}">
    <label>Receipt footer</label><input id="rf" value="${esc(S.state.receipt_footer)}">
    <label>Stock tolerance (%)</label><input id="tp" type="number" step="0.5" value="${S.state.tolerance_pct}">
    <div class="small muted" style="margin-top:4px">Differences smaller than this % of the day's available stock count as normal measuring noise and need no reason. Pieces always need to match exactly.</div>
    <div style="margin-top:14px"><button class="btn primary" id="s">Save settings</button></div></div>`;
  document.getElementById("s").onclick = async () => {
    try {
      await api("/api/settings", { method: "PUT", json: { business_name: document.getElementById("bn").value, receipt_footer: document.getElementById("rf").value, variance_tolerance_pct: Number(document.getElementById("tp").value) } });
      await refreshState(); toast("Saved"); render();
    } catch (e) { fail(e); }
  };
}

boot();
