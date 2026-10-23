"use strict";
/* stickersbot admin frontend. Vanilla JS (no framework, no build step).
 * Talks to the aiohttp API in bot/panel.py; the chart is hand-rolled SVG. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // textContent -> no HTML injection
  return n;
};
const svgEl = (tag, attrs) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
const fmtPct = (x) => Math.round((x || 0) * 100) + "%";
const fmtTime = (ts) => {
  const d = new Date(ts * 1000), p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}`;
};
const SERIES_COLORS = ["#5794f2", "#ff9830", "#73bf69", "#f2495c", "#fade2a"];
const GRID = "#23252c", AXIS = "#8e929b";

/* ------------------------------------------------------------------ api */
async function api(path, opts = {}) {
  if (opts.body != null && typeof opts.body === "object") {
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (!r.ok) throw new Error((body && body.error) || `http ${r.status}`);
  return body;
}

/* ------------------------------------------------------------------ toast */
function toast(msg, kind = "ok") {
  const t = el("div", `toast ${kind}`, msg);
  $("toast").append(t);
  setTimeout(() => t.remove(), 3000);
}

/* ------------------------------------------------------- chart (Grafana-style) */
function niceCeil(v) {
  if (v <= 1) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function renderChart(mount, dates, series) {
  mount.innerHTML = "";
  const W = Math.max(mount.clientWidth - 24, 540), H = 250;
  const padL = 46, padR = 16, padT = 14, padB = 28;
  const n = dates.length;
  const maxV = niceCeil(Math.max(1, ...series.flatMap((s) => s.data)));
  const px = (i) => padL + (n < 2 ? (W - padL - padR) / 2 : (W - padL - padR) * i / (n - 1));
  const py = (v) => padT + (H - padT - padB) * (1 - v / maxV);
  const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` });

  // horizontal grid + y-axis labels (5 ticks: 0 .. maxV)
  for (let s = 0; s <= 4; s++) {
    const v = maxV * s / 4, y = py(v);
    svg.append(svgEl("line", { x1: padL, x2: W - padR, y1: y, y2: y,
                               stroke: GRID, "stroke-width": 1 }));
    const lbl = svgEl("text", { x: padL - 8, y: y + 4, "text-anchor": "end",
                                "font-size": 11, fill: AXIS });
    lbl.textContent = v;
    svg.append(lbl);
  }

  // x labels: at most ~7 date marks (MM-DD, from 'YYYY-MM-DD')
  const each = n ? Math.ceil(n / 7) : 1;
  for (let i = 0; i < n; i += each) {
    const lbl = svgEl("text", { x: px(i), y: H - 8, "text-anchor": "middle",
                                "font-size": 11, fill: AXIS });
    lbl.textContent = dates[i].slice(5);
    svg.append(lbl);
  }

  // areas (12% fill) and lines
  for (let si = 0; si < series.length; si++) {
    const s = series[si], color = s.color || SERIES_COLORS[si % SERIES_COLORS.length];
    if (!n) continue;
    let d = "";
    s.data.forEach((v, i) => {
      d += `${i ? "L" : "M"}${px(i).toFixed(1)} ${py(v).toFixed(1)} `;
    });
    svg.append(svgEl("path", {
      d: `${d}L ${px(n - 1).toFixed(1)} ${py(0).toFixed(1)} ` +
         `L ${px(0).toFixed(1)} ${py(0).toFixed(1)} Z`,
      fill: color, "fill-opacity": 0.12, stroke: "none" }));
    svg.append(svgEl("path", { d, fill: "none", stroke: color,
                               "stroke-width": 1.6, "stroke-linejoin": "round" }));
    if (n === 1) svg.append(svgEl("circle", {
      cx: px(0), cy: py(s.data[0] || 0), r: 3, fill: color }));
  }

  // hover crosshair + dots + tooltip (created lazily, moved on pointermove)
  const rule = svgEl("line", { y1: padT, y2: H - padB, stroke: "#555b66",
                               "stroke-width": 1, "stroke-dasharray": "3 3",
                               visibility: "hidden" });
  svg.append(rule);
  const dots = series.map((s, i) => {
    const c = svgEl("circle", { r: 3.2, visibility: "hidden",
                                fill: s.color || SERIES_COLORS[i % SERIES_COLORS.length] });
    svg.append(c);
    return c;
  });

  svg.addEventListener("pointermove", (ev) => {
    if (!n) return;
    const rect = svg.getBoundingClientRect();
    let best = 0, bd = Infinity;
    for (let i = 0; i < n; i++) {
      const dx = Math.abs(px(i) - (ev.clientX - rect.left));
      if (dx < bd) { bd = dx; best = i; }
    }
    rule.setAttribute("x1", px(best));
    rule.setAttribute("x2", px(best));
    rule.setAttribute("visibility", "visible");
    series.forEach((s, i) => {
      dots[i].setAttribute("cx", px(best));
      dots[i].setAttribute("cy", py(s.data[best]));
      dots[i].setAttribute("visibility", "visible");
    });
    let tip = mount.querySelector(".tip");
    if (!tip) {
      tip = el("div", "tip");
      tip.append(el("div", "tip-date"));
      series.forEach(() => tip.append(el("div", "row")));
      mount.append(tip);
    }
    tip.firstChild.textContent = dates[best];
    [...tip.querySelectorAll(".row")].forEach((row, i) => {
      row.textContent = `${series[i].name}: ${series[i].data[best]}`;
      row.style.color = series[i].color || SERIES_COLORS[i % SERIES_COLORS.length];
    });
    const tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(px(best) - tw / 2 + 8, 4), W - tw - 4) + "px";
    tip.style.top = "10px";
  });
  svg.addEventListener("pointerleave", () => {
    rule.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    const tip = mount.querySelector(".tip");
    if (tip) tip.remove();
  });
  mount.append(svg);
}

/* ------------------------------------------------------------------ tabs */
const TABS = ["dashboard", "stickers", "boosts", "suggestions", "packs"];
const state = { tab: "dashboard" };
let lastStats = null;

function route() {
  const tab = (location.hash || "#dashboard").slice(1);
  const active = TABS.includes(tab) ? tab : "dashboard";
  document.querySelectorAll("section.view").forEach((v) =>
    v.classList.toggle("on", v.id === `view-${active}`));
  document.querySelectorAll("header nav a").forEach((a) =>
    a.classList.toggle("on", a.dataset.tab === active));
  state.tab = active;
  if (state.tab === "dashboard") loadDashboard();
  if (state.tab === "boosts") loadBoosts();
  if (state.tab === "suggestions") loadSuggestions();
  if (state.tab === "packs") loadPackRequests();
}

/* ------------------------------------------------------------ dashboard */
async function loadDashboard() {
  try {
    const [s, hist] = await Promise.all([
      api("/api/stats"),
      api("/api/queries?page=1"),          // recent queries, page 1
    ]);
    lastStats = s;
    const cards = $("cards");
    cards.replaceChildren();
    const items = [
      [String(s.dau), "DAU (searched today)"],
      [String(s.mau), "MAU (last 30d)"],
      [String(s.new_users), "new users (30d)"],
      [fmtPct(s.new_ratio), "new-user ratio"],
      [String(s.total_queries), "total queries"],
      [`${s.total_chosen} (${fmtPct(s.ctr)})`, "chosen results (CTR)"],
    ];
    for (const [v, l] of items) {
      const c = el("div", "card");
      c.append(el("div", "v", v), el("div", "l", l));
      cards.append(c);
    }

    const series = [
      { name: "queries", data: s.queries_series, color: SERIES_COLORS[0] },
      { name: "chosen", data: s.chosen_series, color: SERIES_COLORS[1] },
    ];
    const legend = $("legend");
    legend.replaceChildren();
    for (const sr of series) {
      const item = el("span");
      const dot = el("span", "dot");
      dot.style.background = sr.color;
      item.append(dot, document.createTextNode(sr.name));
      legend.append(item);
    }
    const box = $("chart");
    if (s.categories.length) renderChart(box, s.categories, series);
    else {
      box.innerHTML = "";
      box.append(el("div", "empty",
        "no queries yet - the chart will appear as the bot gets used"));
    }

    renderQueries(hist);
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ------------------------------------ recent queries table (paginated) */
const qPager = { page: 1, pages: 1 };

async function loadQueries(page) {
  try {
    const hist = await api(`/api/queries?page=${page}`);
    qPager.page = hist.page;
    qPager.pages = hist.pages;
    renderQueries(hist);
  } catch (e) {
    toast(e.message, "err");
  }
}

function renderQueries(hist) {
  const tb = $("recent");
  tb.replaceChildren();
  $("qPage").textContent = `${hist.page} / ${hist.pages}`;
  $("qPrev").disabled = hist.page <= 1;
  $("qNext").disabled = hist.page >= hist.pages;
  const rows = hist.rows || [];
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "empty", "no queries yet");
    td.colSpan = 3;
    tr.append(td);
    tb.append(tr);
    return;
  }
  for (const [ts, uid, q] of rows) {
    const tr = document.createElement("tr");
    for (const val of [fmtTime(ts), String(uid), q || ""]) {
      const td = document.createElement("td");
      td.textContent = val;
      tr.append(td);
    }
    tb.append(tr);
  }
}

/* ------------------------------------------------------------- stickers */
async function runSearch() {
  const q = $("q").value.trim();
  if (q.length < 2) { toast("type at least 2 characters", "err"); return; }
  const box = $("results");
  box.replaceChildren(el("div", "empty", "searching..."));
  try {
    const rows = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (!rows.length) { box.replaceChildren(el("div", "empty", "no results")); return; }
    const list = el("div");
    for (const it of rows) {
      const item = el("div", "item");
      const meta = el("div", "meta",
        `#${it.sid} · ${it.set_name || it.set_title || "?"} · ${it.emoji || ""}`);
      if (it.nsfw) meta.append(el("span", "badge", "NSFW"));
      const act = el("div", "actions");
      const btn = el("button", "primary", "edit");
      btn.addEventListener("click", () => openEditor(it));
      act.append(btn);
      item.append(meta, el("div", "desc", it.description || "(no description)"), act);
      list.append(item);
    }
    box.replaceChildren(list);
  } catch (e) {
    box.replaceChildren();
    toast(e.message, "err");
  }
}

/* edit dialog */
let editing = null;                     // the sticker row shown in the dialog

function openEditor(it) {
  editing = it;
  $("dlgMeta").textContent = `sticker #${it.sid} · ${it.set_name || it.set_title || "?"}`;
  $("dlgText").value = it.description || "";
  $("overlay").hidden = false;
  $("dlgText").focus();
}

function closeEditor() {
  $("overlay").hidden = true;
  editing = null;
}

async function saveEditor() {
  if (!editing) return closeEditor();
  try {
    await api(`/api/sticker/${editing.sid}`,
              { method: "POST", body: { description: $("dlgText").value } });
    closeEditor();
    toast("saved, embedding regenerated");
    runSearch();                        // refresh descriptions on screen
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ------------------------------------------------------------------ boosts */
const PACK_OK = /^[A-Za-z0-9_]{3,64}$/;

async function loadBoosts() {
  const tb = $("boostRows");
  try {
    const rows = await api("/api/boosts");
    tb.replaceChildren();
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", "empty", "no boosted packs yet - add one above");
      td.colSpan = 7;
      tr.append(td);
      tb.append(tr);
      return;
    }
    for (const b of rows) {
      const tr = document.createElement("tr");
      const cells = [
        [b.pack, "mono"],
        [(b.weight || 1).toFixed(2) + "x", ""],
        [b.note || "", "muted"],
        [b.in_index ? "yes" : "no", b.in_index ? "" : "muted"],
        [String(b.chosen || 0), ""],
        [fmtTime(b.ts), "muted"],
      ];
      for (const [val, cls] of cells) {
        const td = document.createElement("td");
        td.textContent = val;
        if (cls) td.className = cls;
        tr.append(td);
      }
      const tdAct = document.createElement("td");
      const rm = el("button", "danger", "remove");
      rm.addEventListener("click", async () => {
        rm.disabled = true;
        try {
          await api(`/api/boosts/${encodeURIComponent(b.pack)}`, { method: "DELETE" });
          toast("boost removed");
          loadBoosts();
        } catch (e) {
          rm.disabled = false;
          toast(e.message, "err");
        }
      });
      tdAct.append(rm);
      tr.append(tdAct);
      tb.append(tr);
    }
  } catch (e) {
    toast(e.message, "err");
  }
}

async function addBoost() {
  const pack = $("bPack").value.trim();
  const weight = parseFloat($("bWeight").value || "2");
  const note = $("bNote").value.trim();
  if (!PACK_OK.test(pack)) {
    toast("pack: a set short name like AndreyPack (A-Z, digits, _)", "err");
    return;
  }
  if (!(weight > 1 && weight <= 50)) {
    toast("weight must be in the range (1, 50]", "err");
    return;
  }
  try {
    await api("/api/boosts",
              { method: "POST", body: { pack, weight, note } });
    toast(`${pack} boosted x${weight}`);
    $("bPack").value = "";
    $("bNote").value = "";
    $("bWeight").value = "2";
    loadBoosts();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ---------------------------------------------------------- suggestions */
async function loadSuggestions() {
  const box = $("suggList");
  try {
    const rows = await api("/api/suggestions");
    if (!rows.length) {
      box.replaceChildren(el("div", "empty", "no pending suggestions"));
      return;
    }
    const list = el("div");
    for (const s of rows) {
      const item = el("div", "item");
      item.append(el("div", "meta",
        `#${s.id} · user ${s.user_id} · sticker #${s.sid} · ${fmtTime(s.ts)}`));
      item.append(el("div", "muted", "proposed:"));
      item.append(el("div", "proposed", s.text));
      item.append(el("div", "cur",
        s.current != null ? "current: " + s.current : "sticker no longer indexed"));
      const act = el("div", "actions");
      const ap = el("button", "approve", "approve");
      const rj = el("button", "danger", "reject");
      ap.addEventListener("click", () => { ap.disabled = true; decide(s.id, true, rj); });
      rj.addEventListener("click", () => { rj.disabled = true; decide(s.id, false, rj); });
      act.append(ap, rj);
      item.append(act);
      list.append(item);
    }
    box.replaceChildren(list);
  } catch (e) {
    toast(e.message, "err");
  }
}

async function decide(id, approve, _btn) {
  try {
    const r = await api(`/api/suggestion/${id}`, { method: "POST", body: { approve } });
    if (r.ok === false) toast(r.note || "rejected", "err");
    else toast(approve ? "applied (description + embedding rewritten)" : "rejected");
    loadSuggestions();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ------------------------------------------------------------ pack requests */
async function loadPackRequests() {
  const tb = $("packRows");
  try {
    const rows = await api("/api/pack_requests");
    tb.replaceChildren();
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", "empty", "no pack requests yet");
      td.colSpan = 8;
      tr.append(td);
      tb.append(tr);
      return;
    }
    const order = { pending: 0, approved: 1, rejected: 2 };
    rows.sort((a, b) => (order[a.status] - order[b.status]) || (b.ts - a.ts));
    for (const r of rows) {
      const tr = document.createElement("tr");
      const tdName = document.createElement("td");
      const a = el("a", "mono", r.pack);
      a.href = `https://t.me/addstickers/${encodeURIComponent(r.pack)}`;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      tdName.append(a);
      tr.append(tdName);
      const cells = [
        [r.title || "", ""],
        [String(r.count || 0), ""],
        [String(r.users || 0), ""],
        [r.status, r.status === "pending" ? "" : "muted"],
        [r.in_index ? "yes" : "no", r.in_index ? "" : "muted"],
        [fmtTime(r.ts), "muted"],
      ];
      for (const [val, cls] of cells) {
        const td = document.createElement("td");
        td.textContent = val;
        if (cls) td.className = cls;
        tr.append(td);
      }
      const tdAct = document.createElement("td");
      const ap = el("button", "approve", "approve");
      const rj = el("button", "danger", "reject");
      ap.disabled = r.status === "approved";
      rj.disabled = r.status === "rejected";
      ap.addEventListener("click", () => decidePack(r.pack, true));
      rj.addEventListener("click", () => decidePack(r.pack, false));
      tdAct.append(ap, document.createTextNode(" "), rj);
      tr.append(tdAct);
      tb.append(tr);
    }
  } catch (e) {
    toast(e.message, "err");
  }
}

async function decidePack(pack, approve) {
  try {
    await api(`/api/pack_request/${encodeURIComponent(pack)}`,
              { method: "POST", body: { approve } });
    toast(approve ? `${pack} approved` : `${pack} rejected`);
    loadPackRequests();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ----------------------------------------------------------------- auth */
function showLogin() {
  $("login").hidden = false;
  $("app").hidden = true;
  $("pw").focus();
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  route();
}

async function doLogin() {
  $("loginErr").textContent = "";
  $("loginBtn").disabled = true;
  try {
    await api("/api/login", { method: "POST", body: { password: $("pw").value } });
    $("pw").value = "";
    showApp();
  } catch (e) {
    $("loginErr").textContent = e.message || "wrong password";
  } finally {
    $("loginBtn").disabled = false;
  }
}

async function doLogout() {
  try { await api("/api/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  location.hash = "#dashboard";
  showLogin();
}

/* ----------------------------------------------------------------- boot */
window.addEventListener("hashchange", route);
$("loginBtn").addEventListener("click", doLogin);
$("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
$("logoutBtn").addEventListener("click", doLogout);
$("searchBtn").addEventListener("click", runSearch);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
$("refreshBtn").addEventListener("click", loadDashboard);
$("packsRefresh").addEventListener("click", loadPackRequests);
$("bAddBtn").addEventListener("click", addBoost);
["bPack", "bNote"].forEach((id) => $(id).addEventListener(
  "keydown", (e) => { if (e.key === "Enter") addBoost(); }));
$("qPrev").addEventListener("click", () => loadQueries(qPager.page - 1));
$("qNext").addEventListener("click", () => loadQueries(qPager.page + 1));
$("dlgSave").addEventListener("click", saveEditor);
$("dlgCancel").addEventListener("click", closeEditor);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("overlay").hidden) closeEditor();
});
window.addEventListener("resize", () => {       // keep the chart responsive
  if (lastStats && state.tab === "dashboard" && lastStats.categories.length)
    renderChart($("chart"), lastStats.categories, [
      { name: "queries", data: lastStats.queries_series, color: SERIES_COLORS[0] },
      { name: "chosen", data: lastStats.chosen_series, color: SERIES_COLORS[1] },
    ]);
});

api("/api/me").then((me) => (me.authed ? showApp() : showLogin()))
  .catch(() => showLogin());
