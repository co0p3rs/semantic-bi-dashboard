"use strict";

// No framework, no innerHTML with data: every value reaches the DOM through
// textContent or an SVG attribute, which keeps the strict CSP meaningful.

const LABELS = {
  net_revenue: "Net revenue",
  orders: "Orders",
  average_order_value: "AOV",
  new_customers: "New customers",
  mer: "MER",
  cac: "CAC",
  conversion_rate: "Conversion",
  refund_rate: "Refund rate",
  ad_spend: "Ad spend",
};
// Lower is better for these; a rise is shown in red.
const INVERTED = new Set(["cac", "refund_rate"]);
const UNITS = {
  net_revenue: "currency", average_order_value: "currency", cac: "currency", ad_spend: "currency",
  orders: "count", new_customers: "count", mer: "ratio", conversion_rate: "percent", refund_rate: "percent",
};

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const money2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const count = new Intl.NumberFormat("en-US");

function fmt(metric, value) {
  if (value === null || value === undefined) return "n/a";
  switch (UNITS[metric]) {
    case "currency": return Math.abs(value) < 1000 ? money2.format(value) : money.format(value);
    case "percent": return (value * 100).toFixed(2) + "%";
    case "ratio": return value.toFixed(2) + "x";
    default: return count.format(Math.round(value));
  }
}

function csrf() {
  const match = document.cookie.match(/(?:^|; )nbi_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function api(path, { method = "GET", body } = {}) {
  const headers = { "content-type": "application/json" };
  if (method !== "GET") headers["x-csrf-token"] = csrf();
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  if (res.status === 401) { showLogin(); throw new Error("signed out"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

// ---------------------------------------------------------------- state

const state = { days: 30, dataEnd: null };

function isoDaysBefore(iso, days) {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- views

function showLogin() {
  $("dash").hidden = true;
  $("who").hidden = true;
  $("login").hidden = false;
}

async function showDashboard() {
  const me = await api("/api/me");
  $("login").hidden = true;
  $("who").hidden = false;
  $("dash").hidden = false;
  $("who-email").textContent = me.email;
  $("who-role").textContent = me.role;
  if (!state.dataEnd) {
    const health = await fetch("/healthz").then((r) => r.json());
    state.dataEnd = Object.values(health.sources).sort().at(-1);
  }
  await load();
}

async function load() {
  const end = state.dataEnd;
  const start = isoDaysBefore(end, state.days - 1);
  $("range-label").textContent = `${start} → ${end}`;
  const data = await api("/api/v1/dashboard/overview", { method: "POST", body: { start_date: start, end_date: end } });
  renderFreshness(data.meta.freshness);
  renderTiles(data.tiles, data.previous_range);
  renderChart(data.series, data.series_metrics);
  renderProducts(data.by_product);
  const c = data.meta.cache;
  $("cache").textContent = `cache ${c.hit ? "hit" : "miss"} · generations ${Object.entries(c.generations).map(([k, v]) => `${k}:${v}`).join(" ")}`;
}

function renderFreshness(freshness) {
  const box = $("freshness");
  box.hidden = freshness.complete;
  box.textContent = freshness.complete ? "" : "Partial data: " + freshness.warnings.join("; ") + ".";
}

function renderTiles(tiles, previousRange) {
  const root = $("tiles");
  root.replaceChildren();
  for (const [metric, t] of Object.entries(tiles)) {
    const tile = el("div", { class: "tile" });
    tile.append(el("div", { class: "label" }, LABELS[metric] || metric));
    tile.append(el("div", { class: "value" }, fmt(metric, t.current)));
    let cls = "flat";
    let text = "no prior data";
    if (t.pct_change !== null) {
      const good = INVERTED.has(metric) ? t.pct_change < 0 : t.pct_change > 0;
      cls = t.pct_change === 0 ? "flat" : good ? "up" : "down";
      text = `${t.pct_change > 0 ? "▲" : t.pct_change < 0 ? "▼" : "•"} ${(Math.abs(t.pct_change) * 100).toFixed(1)}% vs prior period`;
      tile.title = `Prior period: ${previousRange[0]} → ${previousRange[1]}`;
    }
    tile.append(el("div", { class: `delta ${cls}` }, text));
    root.append(tile);
  }
}

function renderChart(rows, metrics) {
  const root = $("chart");
  root.replaceChildren();
  const legend = $("legend");
  legend.replaceChildren();
  metrics.forEach((m, i) => {
    const item = el("span");
    item.append(el("i", { class: `k${i}` }), document.createTextNode(LABELS[m] || m));
    legend.append(item);
  });
  if (!rows.length) return;

  const W = 1000, H = 260, pad = { l: 56, r: 12, t: 12, b: 26 };
  const max = Math.max(1, ...rows.flatMap((r) => metrics.map((m) => r[m] || 0)));
  const x = (i) => pad.l + (i * (W - pad.l - pad.r)) / Math.max(1, rows.length - 1);
  const y = (v) => H - pad.b - (v / max) * (H - pad.t - pad.b);
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img" });

  for (let g = 0; g <= 4; g++) {
    const v = (max * g) / 4;
    chart.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: y(v), y2: y(v) }));
    const label = svg("text", { class: "axis", x: pad.l - 8, y: y(v) + 4, "text-anchor": "end" });
    label.textContent = money.format(v);
    chart.append(label);
  }
  const step = Math.ceil(rows.length / 8);
  rows.forEach((r, i) => {
    if (i % step) return;
    const label = svg("text", { class: "axis", x: x(i), y: H - 6, "text-anchor": "middle" });
    label.textContent = r.bucket.slice(5);
    chart.append(label);
  });
  metrics.forEach((m, i) => {
    // null = not synced yet: lift the pen instead of drawing a fake drop to zero.
    let pen = "M";
    const d = rows.map((r, j) => {
      if (r[m] === null || r[m] === undefined) { pen = "M"; return ""; }
      const seg = `${pen}${x(j).toFixed(1)},${y(r[m]).toFixed(1)}`;
      pen = "L";
      return seg;
    }).join("");
    chart.append(svg("path", { d, class: `s${i}`, fill: "none", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
  });
  root.append(chart);
}

function renderProducts(rows) {
  const table = $("products");
  table.replaceChildren();
  if (!rows.length) return;
  const cols = Object.keys(rows[0]).filter((k) => k !== "bucket");
  const head = el("tr");
  cols.forEach((c) => head.append(el("th", {}, c === "product" ? "Product" : LABELS[c] || c)));
  const thead = el("thead");
  thead.append(head);
  table.append(thead);
  const body = el("tbody");
  rows.forEach((r) => {
    const tr = el("tr");
    cols.forEach((c) => tr.append(el("td", {}, c === "product" ? r[c] : fmt(c, r[c]))));
    body.append(tr);
  });
  table.append(body);
}

// ---------------------------------------------------------------- events

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    $("login-error").textContent = "";
    try {
      await api("/api/auth/login", { method: "POST", body: { email: form.get("email"), password: form.get("password") } });
      event.target.reset();
      await showDashboard();
    } catch (err) {
      $("login-error").textContent = err.message;
    }
  });

  $("logout").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
    showLogin();
  });

  document.querySelectorAll(".ranges button").forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll(".ranges button").forEach((b) => b.classList.toggle("active", b === button));
      state.days = Number(button.dataset.days);
      await load();
    });
  });

  showDashboard().catch(showLogin);
});
