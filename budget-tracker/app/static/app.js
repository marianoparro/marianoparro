// Family Budget frontend: plain JavaScript, no framework.
// Pattern: fetch JSON from the FastAPI endpoints, keep it in `state`,
// and re-render the parts of the page that depend on it.

const state = {
  config: null,     // GET /config  — categories + optional income/fixed costs
  months: [],       // GET /months  — one row per month, for the trend chart
  month: null,      // selected "YYYY-MM"
  summary: null,    // GET /summary/{month}
  txs: [],          // GET /transactions?month=
  filter: "",       // "" = all, "__uncat", "__oneoff", or a category name
  search: "",
};

const TAXES = "Taxes";  // a set-aside, never charged to the card
const FLAG_PCT = 30;    // same threshold the API uses for over_target

// ---------- small helpers ----------
const $ = (sel) => document.querySelector(sel);
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const usd2 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const money = (n) => usd0.format(n);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const monthName = (m, style = "short") =>
  new Date(`${m}-15T12:00:00`).toLocaleDateString("en-US", { month: style, year: style === "long" ? "numeric" : undefined });
const shortDate = (d) => new Date(`${d}T12:00:00`).toLocaleDateString("en-US", { month: "short", day: "numeric" });

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

// Status badge: always an icon + words, so it never relies on color alone.
function badge(variancePct) {
  if (variancePct == null) return "";
  const pct = Math.abs(Math.round(variancePct));
  if (variancePct > FLAG_PCT) return `<span class="badge critical">▲ ${pct}% over</span>`;
  if (variancePct > 0) return `<span class="badge warning">▲ ${pct}% over</span>`;
  return `<span class="badge good">✓ ${pct}% under</span>`;
}

// ---------- tooltip ----------
const tip = $("#tooltip");
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 12, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + pad, y = evt.clientY - h - pad;
  if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
  if (y < 8) y = evt.clientY + pad;
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTip = () => { tip.hidden = true; };

// ---------- loading ----------
async function init() {
  [state.config, state.months] = await Promise.all([api("/config"), api("/months")]);
  if (state.config.user) $("#who").textContent = `Hi, ${state.config.user}`;
  if (state.config.assistant_enabled) { $("#ask").hidden = false; loadChat(); }
  if (!state.months.length) {
    $("#tiles").innerHTML = `<div class="tile"><div class="label">No data yet</div><div class="sub">Import a Chase CSV below to get started.</div></div>`;
    renderFilterOptions();
    return;
  }
  const fromHash = location.hash.slice(1);
  const known = state.months.map((m) => m.month);
  await selectMonth(known.includes(fromHash) ? fromHash : known[known.length - 1]);
}

async function selectMonth(month) {
  state.month = month;
  history.replaceState(null, "", `#${month}`);
  [state.summary, state.txs] = await Promise.all([
    api(`/summary/${month}`),
    api(`/transactions?month=${month}`),
  ]);
  renderMonthSelect();
  renderTiles();
  renderTrend();
  renderCategories();
  renderOneoffs();
  renderFilterOptions();
  renderTransactions();
}

async function refreshAll() {
  state.months = await api("/months");
  const known = state.months.map((m) => m.month);
  await selectMonth(known.includes(state.month) ? state.month : known[known.length - 1]);
}

// ---------- header + tiles ----------
function renderMonthSelect() {
  $("#month-select").innerHTML = [...state.months].reverse()
    .map((m) => `<option value="${m.month}" ${m.month === state.month ? "selected" : ""}>${monthName(m.month, "long")}</option>`)
    .join("");
}

// Partial months: the first month (data starts mid-month) and the latest one
// (still in progress). Their totals aren't comparable, so we label them.
function partialNote(month) {
  const row = state.months.find((m) => m.month === month);
  if (!row) return "";
  const [y, mo] = month.split("-").map(Number);
  const lastDay = new Date(y, mo, 0).getDate();
  const isFirst = row === state.months[0], isLatest = row === state.months.at(-1);
  if (isFirst && Number(row.first_date.slice(8, 10)) > 3) return `partial — data starts ${shortDate(row.first_date)}`;
  if (isLatest && Number(row.last_date.slice(8, 10)) < lastDay) return `in progress — through ${shortDate(row.last_date)}`;
  return "";
}

// Top row reads as the money flow, left to right:
//   Income − Fixed − Variable − One-offs (− tax set-aside) = Saved
// Each big monthly number has its yearly equivalent in small text underneath.
function renderTiles() {
  const s = state.summary;
  const c = state.config;
  const hasPlan = c.monthly_net_income != null && c.monthly_fixed_costs != null;
  const variance = s.variable_target ? ((s.variable_total - s.variable_target) / s.variable_target) * 100 : null;
  const uncatCount = state.txs.filter((t) => !t.category).length;
  const note = partialNote(s.month);
  const year = s.month.slice(0, 4);
  const oneoffsYtd = oneoffsYearToDate();

  const tile = (label, value, yearly, sub = "") => `<div class="tile">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="yearly">${yearly}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ""}
    </div>`;

  const tiles = [];
  if (hasPlan) {
    tiles.push(tile("Income (net)", money(c.monthly_net_income), `${money(c.monthly_net_income * 12)} / year`));
    tiles.push(tile("Fixed costs", money(c.monthly_fixed_costs), `${money(c.monthly_fixed_costs * 12)} / year`,
      c.fixed_costs.length ? `${c.fixed_costs.length} items · <a href="#flow" id="see-fixed">see breakdown</a>` : ""));
  }
  tiles.push(tile("Variable spend (card)", money(s.variable_total),
    `≈ ${money(s.variable_total * 12)} / year at this pace`,
    `target ${money(s.variable_target)} · ${badge(variance)}` +
    (uncatCount ? `<br>incl. ${money(s.uncategorized_total)} uncategorized · <a href="#" id="fix-uncat">categorize</a>` : "") +
    (note ? `<br>Month ${note}` : "")));
  tiles.push(tile("One-offs", money(s.oneoff_total),
    `${money(oneoffsYtd)} so far in ${year}`,
    `${s.oneoffs.length} charge${s.oneoffs.length === 1 ? "" : "s"}, not in averages`));
  if (hasPlan) {
    const saved = savedThisMonth();
    const goal = c.yearly_savings_goal;
    const perYear = savedPerYear();
    const status = goal == null ? "" : perYear >= goal
      ? `<span class="badge good">✓ on pace for ${money(goal)}</span>`
      : `<span class="badge ${perYear >= 0 ? "warning" : "critical"}">▼ ${money(goal - perYear)} short of ${money(goal)}</span>`;
    tiles.push(tile("Saved this month", money(saved), `≈ ${money(savedPerYear())} / year at this pace`, status));
  }
  $("#tiles").innerHTML = tiles.join("");
  $("#tiles").classList.toggle("five", tiles.length === 5);
  const fix = $("#fix-uncat");
  if (fix) fix.onclick = (e) => { e.preventDefault(); setFilter("__uncat"); $("#tx-table").scrollIntoView({ behavior: "smooth" }); };
  const seeFixed = $("#see-fixed");
  if (seeFixed) seeFixed.onclick = (e) => { e.preventDefault(); $("#fixed-details").open = true; $("#flow").scrollIntoView({ behavior: "smooth" }); };
  renderFlow();
}

// Same math as the plan: income − fixed − card spend − tax set-aside − one-offs.
function savedThisMonth() {
  const s = state.summary, c = state.config;
  return regularLeftOver() - s.oneoff_total;
}

// What's left after the items that repeat every month.
function regularLeftOver() {
  const s = state.summary, c = state.config;
  return c.monthly_net_income - c.monthly_fixed_costs - s.variable_total - c.monthly_tax_setaside;
}

// One-offs don't repeat, so multiplying them by 12 would mislead (one movers
// bill would count as twelve). Use what was actually paid this year so far.
function oneoffsYearToDate() {
  const month = state.summary.month, year = month.slice(0, 4);
  return state.months
    .filter((m) => m.month.startsWith(year) && m.month <= month)
    .reduce((sum, m) => sum + m.oneoff_total, 0);
}

// Yearly pace: regular items × 12, minus the one-offs actually paid this year.
function savedPerYear() {
  return regularLeftOver() * 12 - oneoffsYearToDate();
}

// "Where the money goes": the same flow as the tiles, one line per step, with
// fixed costs itemized (rent, school, ...) and a per-year column.
function renderFlow() {
  const c = state.config, s = state.summary;
  const card = $("#flow");
  if (c.monthly_net_income == null || c.monthly_fixed_costs == null) { card.hidden = true; return; }
  card.hidden = false;
  // Deductions are shown as positive amounts; the "−" is in the label.
  const row = (label, month, year, cls = "") =>
    `<div class="flow-row ${cls}"><span>${label}</span><span class="num">${money(month)}</span><span class="num yr">${money(year)}</span></div>`;
  const wasOpen = $("#fixed-details")?.open;
  const fixedItems = c.fixed_costs.map((i) =>
    `<div class="flow-row item"><span>${esc(i.name)}</span><span class="num">${money(i.amount)}</span><span class="num yr">${money(i.amount * 12)}</span></div>`).join("");
  const fixedRow = row("− Fixed costs", c.monthly_fixed_costs, c.monthly_fixed_costs * 12);
  $("#flow-body").innerHTML = `
    <div class="flow-row head"><span></span><span class="num">${monthName(s.month)}</span><span class="num yr">Per year</span></div>
    ${row("Income (net)", c.monthly_net_income, c.monthly_net_income * 12, "strong")}
    ${c.fixed_costs.length
      ? `<details id="fixed-details" ${wasOpen ? "open" : ""}><summary>${fixedRow}</summary>${fixedItems}</details>`
      : fixedRow}
    ${row("− Variable spend (card)", s.variable_total, s.variable_total * 12)}
    ${row("− Tax set-aside", c.monthly_tax_setaside, c.monthly_tax_setaside * 12)}
    ${row(`− One-offs <span class="hint">year = paid so far in ${s.month.slice(0, 4)}</span>`, s.oneoff_total, oneoffsYearToDate())}
    ${row("= Saved", savedThisMonth(), savedPerYear(), "total")}
    ${c.yearly_savings_goal ? row("Goal", c.yearly_savings_goal / 12, c.yearly_savings_goal, "goal") : ""}`;
}

// ---------- trend chart (hand-built SVG) ----------
// Single series (spend per month) + a dashed target line. The selected month
// is full strength with a value label; others are dimmed. Hover any column for
// details, click to open that month.
function renderTrend() {
  const el = $("#trend-chart");
  const data = state.months;
  const W = Math.max(el.clientWidth, 300), H = 220;
  const m = { top: 18, right: 8, bottom: 24, left: 44 };
  const iw = W - m.left - m.right, ih = H - m.top - m.bottom;
  const target = data[0]?.variable_target ?? 0;

  // y-scale: 0 .. a "nice" max above both the data and the target
  const rawMax = Math.max(target, ...data.map((d) => d.variable_total)) * 1.08;
  const step = niceStep(rawMax / 4);
  const yMax = Math.ceil(rawMax / step) * step;
  const y = (v) => m.top + ih - (v / yMax) * ih;

  const band = iw / data.length;
  const barW = Math.min(44, band * 0.6);
  const x = (i) => m.left + band * i + (band - barW) / 2;

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Monthly variable spend versus target">
    <defs><pattern id="partial-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <line x1="0" y1="0" x2="0" y2="6" class="hatch-line"/></pattern></defs>`;
  for (let v = 0; v <= yMax; v += step) {
    svg += `<line class="grid-line" x1="${m.left}" x2="${W - m.right}" y1="${y(v)}" y2="${y(v)}"/>`;
    svg += `<text class="axis-label" x="${m.left - 6}" y="${y(v) + 4}" text-anchor="end">${v >= 1000 ? `$${v / 1000}k` : `$${v}`}</text>`;
  }
  data.forEach((d, i) => {
    const sel = d.month === state.month;
    const h = Math.max(0, y(0) - y(d.variable_total));
    svg += `<path class="bar ${sel ? "" : "dim"}" d="${topRoundedBar(x(i), y(0), barW, h, 4)}"/>`;
    const partial = partialNote(d.month);
    if (partial) svg += `<path class="bar-partial" d="${topRoundedBar(x(i), y(0), barW, h, 4)}"/>`;
    svg += `<text class="axis-label" x="${x(i) + barW / 2}" y="${H - 6}" text-anchor="middle">${monthName(d.month)}${partial ? "*" : ""}</text>`;
    if (sel) svg += `<text class="value-label" x="${x(i) + barW / 2}" y="${y(d.variable_total) - 6}" text-anchor="middle">${money(d.variable_total)}</text>`;
    // invisible full-height hit area: easier to hover/tap than the bar itself
    svg += `<rect class="hit" data-i="${i}" x="${m.left + band * i}" y="${m.top}" width="${band}" height="${ih}"/>`;
  });
  if (target) {
    svg += `<line class="target-line" x1="${m.left}" x2="${W - m.right}" y1="${y(target)}" y2="${y(target)}"/>`;
    // Label sits at the left end, where the (partial) first month is short.
    svg += `<text class="target-label" x="${m.left + 4}" y="${y(target) - 6}">Target ${money(target)}</text>`;
  }
  el.innerHTML = svg + "</svg>";

  el.querySelectorAll(".hit").forEach((r) => {
    const d = data[Number(r.dataset.i)];
    const pct = target ? Math.round(((d.variable_total - target) / target) * 100) : 0;
    r.addEventListener("mousemove", (e) => showTip(e,
      `<b>${monthName(d.month, "long")}</b><br>Variable ${money(d.variable_total)} (${pct >= 0 ? "+" : ""}${pct}% vs target)` +
      (d.oneoff_total ? `<br>One-offs ${money(d.oneoff_total)} (excluded)` : "") +
      (partialNote(d.month) ? `<br><i>* ${partialNote(d.month)}</i>` : "")));
    r.addEventListener("mouseleave", hideTip);
    r.addEventListener("click", () => { hideTip(); selectMonth(d.month); });
  });
  $("#trend-note").textContent = `Excludes fixed costs and one-offs · target includes ${money(state.config.monthly_tax_setaside)} tax set-aside`;
}

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((k) => k * p).find((s) => s >= raw);
}

// A bar with 4px rounded top corners, flat on the baseline.
function topRoundedBar(x, base, w, h, r) {
  if (h <= 0) return "";
  r = Math.min(r, h, w / 2);
  return `M${x},${base} V${base - h + r} Q${x},${base - h} ${x + r},${base - h} H${x + w - r} Q${x + w},${base - h} ${x + w},${base - h + r} V${base} Z`;
}

// ---------- categories ----------
function renderCategories() {
  const s = state.summary;
  const rows = s.categories.filter((c) => c.category !== TAXES && (c.target || c.actual));
  if (s.uncategorized_total) {
    rows.push({ category: "Uncategorized", target: 0, actual: s.uncategorized_total, variance_pct: null, key: "__uncat" });
  }
  const max = Math.max(1, ...rows.map((r) => Math.max(r.actual, r.target)));
  $("#categories").innerHTML = rows.map((r) => {
    const key = r.key || r.category;
    const pct = (v) => `${Math.min(100, (Math.max(0, v) / max) * 100)}%`;
    return `<li class="cat-row ${state.filter === key ? "active" : ""}" data-key="${esc(key)}" tabindex="0">
        <span class="name">${esc(r.category)}</span>
        <span class="nums"><b>${money(r.actual)}</b>${r.target ? ` / ${money(r.target)}` : ""} ${badge(r.variance_pct)}</span>
        <span class="meter" aria-hidden="true">
          <span class="fill" style="width:${pct(r.actual)}"></span>
          ${r.target ? `<span class="tick" style="left:calc(${pct(r.target)} - 1px)"></span>` : ""}
        </span>
      </li>`;
  }).join("");
  $("#categories").querySelectorAll(".cat-row").forEach((li) => {
    const go = () => { setFilter(state.filter === li.dataset.key ? "" : li.dataset.key); };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
}

// ---------- one-offs ----------
function renderOneoffs() {
  const list = state.summary.oneoffs;
  $("#oneoffs").innerHTML = list.length
    ? list.map((o) => `<li><span class="desc">${esc(o.description)}<span class="date">${shortDate(o.date)} · ${esc(o.category)}</span></span><b>${usd2.format(o.amount)}</b></li>`).join("")
    : `<li class="muted">None this month</li>`;
}

// ---------- transactions ----------
function setFilter(key) {
  state.filter = key;
  $("#cat-filter").value = key;
  renderCategories();
  renderTransactions();
}

function renderFilterOptions() {
  const cats = state.config.categories;
  $("#cat-filter").innerHTML =
    `<option value="">All categories</option><option value="__uncat">Uncategorized</option><option value="__oneoff">One-offs</option>` +
    cats.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  $("#cat-filter").value = state.filter;
}

function visibleTxs() {
  const q = state.search.trim().toLowerCase();
  return state.txs.filter((t) => {
    if (state.filter === "__uncat" && t.category) return false;
    if (state.filter === "__oneoff" && !t.is_oneoff) return false;
    // A category filter shows what counts toward that category's bar.
    if (state.filter && !state.filter.startsWith("__") && (t.category !== state.filter || (t.is_oneoff && state.filter !== "One-off"))) return false;
    return !q || t.description.toLowerCase().includes(q);
  });
}

function renderTransactions() {
  const rows = visibleTxs().slice().sort((a, b) => b.date.localeCompare(a.date) || b.amount - a.amount);
  const options = (current) =>
    `<option value="" ${!current ? "selected" : ""}>— Uncategorized —</option>` +
    state.config.categories.map((c) => `<option value="${esc(c)}" ${c === current ? "selected" : ""}>${esc(c)}</option>`).join("");

  $("#tx-table tbody").innerHTML = rows.map((t) => {
    const flags = [t.is_oneoff && "one-off", t.is_fixed && "fixed"].filter(Boolean).join(" · ");
    return `<tr data-id="${t.id}" class="${t.amount < 0 ? "refund" : ""}">
        <td class="date">${shortDate(t.date)}</td>
        <td>${esc(t.description)}${flags ? `<span class="flag">${flags}</span>` : ""}</td>
        <td class="num">${t.amount < 0 ? "−" : ""}${usd2.format(Math.abs(t.amount))}</td>
        <td><select class="${t.category ? "" : "uncat"}" aria-label="Category for ${esc(t.description)}">${options(t.category)}</select></td>
      </tr>`;
  }).join("");

  const total = rows.reduce((sum, t) => sum + t.amount, 0);
  $("#tx-count").textContent = `${rows.length} transaction${rows.length === 1 ? "" : "s"} · ${usd2.format(total)}`;

  $("#tx-table tbody").querySelectorAll("select").forEach((sel) => {
    sel.addEventListener("change", () => openRuleForm(sel));
  });
}

// Changing a category doesn't edit one row: it creates a merchant rule, so the
// same merchant is fixed in every month (past and future). The pattern is
// pre-filled with the cleaned merchant name and can be shortened or widened.
function openRuleForm(select) {
  const tr = select.closest("tr");
  const tx = state.txs.find((t) => String(t.id) === tr.dataset.id);
  document.querySelectorAll(".rule-row").forEach((r) => r.remove());
  if (!select.value) { select.value = tx.category || ""; return; }  // can't "un-categorize" via a rule

  const row = document.createElement("tr");
  row.className = "rule-row";
  row.innerHTML = `<td colspan="4"><div class="rule-form">
      <span>Every transaction containing</span>
      <input value="${esc(tx.merchant_normalized || tx.description)}" aria-label="Pattern">
      <span>→ <b>${esc(select.value)}</b></span>
      <button class="primary">Save rule</button>
      <button class="cancel">Cancel</button>
      <span class="msg muted"></span>
    </div></td>`;
  tr.after(row);
  const input = row.querySelector("input");
  input.focus();
  input.select();

  row.querySelector(".cancel").onclick = () => { select.value = tx.category || ""; row.remove(); };
  row.querySelector(".primary").onclick = async () => {
    const msg = row.querySelector(".msg");
    try {
      const res = await api("/merchant-map", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pattern: input.value, category: select.value }),
      });
      msg.textContent = `Saved · ${res.transactions_updated} transactions updated`;
      await refreshAll();
    } catch (err) {
      msg.textContent = err.message;
    }
  };
}

// ---------- import ----------
async function importFiles(files) {
  if (!files.length) return;
  const out = $("#import-result");
  out.innerHTML = `<p class="muted">Importing ${files.length} file${files.length === 1 ? "" : "s"}…</p>`;
  const form = new FormData();
  [...files].forEach((f) => form.append("files", f));
  try {
    const r = await api("/import", { method: "POST", body: form });
    const flagged = r.flagged.slice().sort((a, b) => b.amount - a.amount);
    out.innerHTML = `
      <p><b>${r.imported}</b> imported · ${r.duplicates} duplicates skipped · ${r.uncategorized} uncategorized · ${r.oneoffs} one-offs</p>
      ${flagged.length ? `<p class="muted">Worth a look: one-offs and single charges over $200</p>
      <ul class="plain-list">${flagged.map((f) => `<li><span class="desc">${esc(f.description)}<span class="date">${shortDate(f.date)} · ${esc(f.category || "Uncategorized")} · ${esc(f.reason)}</span></span><b>${usd2.format(f.amount)}</b></li>`).join("")}</ul>
      ${state.config.assistant_enabled ? `<button class="primary" id="ask-flagged" type="button">Ask the assistant about these</button>` : ""}` : ""}`;
    const askFlagged = $("#ask-flagged");
    if (askFlagged) askFlagged.onclick = () => {
      const list = flagged.map((f) => `${f.date} ${f.description} $${f.amount}`).join("; ");
      $("#ask").scrollIntoView({ behavior: "smooth" });
      askAssistant(`I just imported new charges. These were flagged (one-offs or over $200): ${list}. Anything I should look at or recategorize?`);
    };
    await (state.months.length ? refreshAll() : init());
  } catch (err) {
    out.innerHTML = `<p class="badge critical">✕ ${esc(err.message)}</p>`;
  }
}

// ---------- assistant chat ----------
const CHIPS = [
  "Why is food so high this month?",
  "Break down my Amazon charges",
  "Health this month vs last month",
  "Where can we cut to hit our savings goal?",
];

// Tiny, safe formatter for the assistant's replies: escape everything first,
// then allow **bold** and "- " bullet lists. No raw HTML ever gets through.
function formatReply(text) {
  const blocks = [];
  let list = null;
  for (const raw of esc(text).split("\n")) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    if (/^\s*[-•]\s+/.test(line)) {
      if (!list) { list = []; blocks.push(list); }
      list.push(line.replace(/^\s*[-•]\s+/, ""));
    } else {
      list = null;
      if (line.trim()) blocks.push(line);
    }
  }
  return blocks.map((b) => Array.isArray(b) ? `<ul>${b.map((i) => `<li>${i}</li>`).join("")}</ul>` : `<p>${b}</p>`).join("");
}

function addMessage(role, text, extra = "") {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = role === "assistant" ? formatReply(text) + extra : esc(text);
  $("#chat-log").append(div);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  $("#chat-clear").hidden = false;
  return div;
}

async function loadChat() {
  $("#chat-chips").innerHTML = CHIPS.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("#chat-chips").querySelectorAll("button").forEach((b) => b.onclick = () => askAssistant(b.textContent));
  try {
    const history = await api("/chat");
    $("#chat-log").innerHTML = "";
    history.forEach((m) => addMessage(m.role, m.content));
    $("#chat-clear").hidden = !history.length;
  } catch { /* chat history is optional; the page works without it */ }
}

// The assistant sees the selected month as context, so "this month" means
// the month you're looking at.
async function askAssistant(question) {
  question = question.trim();
  if (!question) return;
  const monthNote = state.month ? ` (I'm looking at ${monthName(state.month, "long")}.)` : "";
  addMessage("user", question);
  const pending = addMessage("assistant thinking", "Looking at your transactions…");
  $("#chat-input").value = "";
  $("#chat-form button").disabled = true;
  try {
    const r = await api("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question + monthNote }),
    });
    pending.remove();
    addMessage("assistant", r.reply);
    // A rule saved from chat changes categories: refresh the numbers.
    if (r.tools_used.includes("set_merchant_category")) await refreshAll();
  } catch (err) {
    pending.className = "msg error";
    pending.textContent = err.message;
  } finally {
    $("#chat-form button").disabled = false;
  }
}

$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); askAssistant($("#chat-input").value); });
$("#chat-clear").addEventListener("click", async () => {
  await api("/chat", { method: "DELETE" });
  $("#chat-log").innerHTML = "";
  $("#chat-clear").hidden = true;
});

// ---------- wire up ----------
$("#month-select").addEventListener("change", (e) => selectMonth(e.target.value));
$("#cat-filter").addEventListener("change", (e) => setFilter(e.target.value));
$("#search").addEventListener("input", (e) => { state.search = e.target.value; renderTransactions(); });

const drop = $("#dropzone");
$("#file-input").addEventListener("change", (e) => importFiles(e.target.files));
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); importFiles(e.dataTransfer.files); });

// Redraw the chart when its width changes (phone rotation, window resize).
new ResizeObserver(() => { if (state.months.length) renderTrend(); }).observe($("#trend-chart"));

init().catch((err) => { $("#tiles").innerHTML = `<div class="tile"><div class="label">Couldn't load</div><div class="sub">${esc(err.message)}</div></div>`; });
