// The page shell: state, polling /api/board, the render loop, the drawer, search, the look & feel panel, the
// docks and the feature registry (SYA.feature). Everything a feature script needs from the page is a global
// declared here or in figures.js; docs/ARCHITECTURE.md lists them.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// The page's language: web/i18n.js loaded its strings before any script, and t(key, params) reads them.
const I18N = window.SYA_I18N || { lang: "en", langs: { en: "English" }, strings: {} };
document.documentElement.lang = I18N.lang;
// "today" -> "Today": a phrase that starts a line.
const capital = (s) => (s ? s[0].toLocaleUpperCase(I18N.lang) + s.slice(1) : s);

const STATUS = {
  needs_you: t("app.status.needs_you"), working: t("app.status.working"), your_move: t("app.status.your_move"),
  recent: t("app.status.recent"), archive: t("app.status.archive"),
};
const LIVE_STATUS = {
  busy: t("app.live.busy"), shell: t("app.live.shell"), idle: t("app.live.idle"), waiting: t("app.live.waiting"),
  blocked: t("app.live.blocked"),
};
// What a waiting session waits for, as a whole phrase: "waiting for permission".
const WAITING = {
  "permission prompt": t("app.waiting.permission"), "input needed": t("app.waiting.input"),
  "dialog open": t("app.waiting.dialog"),
};
const EVENT = {
  prompt: t("app.event.prompt"), recap: t("app.event.recap"), question: t("app.event.question"), pr: t("app.event.pr"),
  plan: t("app.event.plan"), agent: t("app.event.agent"), compact: t("app.event.compact"),
  msg_in: t("app.event.msg_in"), msg_out: t("app.event.msg_out"),
};

// Every request names the page's language, so what the server words (errors, notes) comes back in it.
const LANG_HEADER = { "X-SYA-Lang": I18N.lang };
// A feature's GET: JSON in the page's language, or null when the server says no.
const getJson = (path) => fetch(path, { headers: LANG_HEADER }).then((r) => (r.ok ? r.json() : null));
const api = window.SYA_API || {
  board: () => fetch("/api/board", { headers: LANG_HEADER }).then((r) => r.json()),
  session: (id) => getJson(`/api/session/${encodeURIComponent(id)}`),
  search: (q) => fetch(`/api/search?q=${encodeURIComponent(q)}`, { headers: LANG_HEADER }).then((r) => r.json()),
  planFrame: (id, i) => ({ src: `/api/plan?session=${encodeURIComponent(id)}&i=${i}` }),
};
// The pairing secret of this tab (app/pairing.py): sessionStorage is per tab and per origin, port included, so
// no other tab or page gets it; it travels only as a header to this server, and only this server process knows it.
const PAIR_KEY = "sya.pair";
const pairHeader = () => {
  try { const s = sessionStorage.getItem(PAIR_KEY); return s ? { "X-SYA-Pair": s } : {}; } catch (e) { return {}; }
};
const dropPairSecret = () => { try { sessionStorage.removeItem(PAIR_KEY); } catch (e) { /* nothing kept */ } };
// The dashboard has no service worker. One here was left by whatever answered on this port while the dashboard
// was down: unregister it (it may already have served this load; it will not serve the next).
navigator.serviceWorker?.getRegistrations?.().then((rs) => rs.forEach((r) => r.unregister())).catch(() => {});
// Actions that change something go through POST with the header the server requires. The server refuses one
// from an unpaired browser (403 {unpaired}); surface the pairing prompt so the first action asks to pair.
const postJson = (path, body) => fetch(path, {
  method: "POST", headers: { "Content-Type": "application/json", "X-SYA": "1", ...LANG_HEADER, ...pairHeader() },
  body: JSON.stringify(body),
}).then(async (r) => {
  const res = { ok: r.ok, status: r.status, data: await r.json().catch(() => null) };
  if (r.status === 403 && res.data?.unpaired) SYA.pairing?.require();
  return res;
});

// The view on screen and what it shows; the board is the last /api/board payload.
let state = {
  board: null, sig: "", view: "", viewArg: "", project: "", openId: null, prevNeeds: null, version: "", instance: "",
  ownInstances: new Set(),
};

const PREFS_KEY = "sya.prefs";
let prefs = (() => {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch (e) { /* storage unavailable */ }
  const skin = SKINS[saved.skin] ? saved.skin : "buro";
  const p = { skin, theme: "", faces: true, ...SKINS[skin].office, ...saved };
  p.shapes = (Array.isArray(p.shapes) ? p.shapes : []).filter((s) => SHAPES[s]);
  if (!p.shapes.length) p.shapes = ["circle"];
  if (!DESKS[p.desk]) p.desk = "desk";
  if (!DEALS[p.deal]) p.deal = "session";
  if (!(p.theme in THEMES)) p.theme = "";
  return p;
})();
function savePrefs() { try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* not persisted */ } }

/* ---------- the feature registry: SYA.feature(def), see docs/ARCHITECTURE.md ("Page (JS)") ---------- */
// Feature scripts load after this one, in registry order, and register while they load; the first render waits
// for DOMContentLoaded, when every feature is in. def: { id, view, left, right, header, top, render, card,
// chip, drawer, prefs, route, onBoard }. SYA.on / SYA.emit: events between features ("view", "drawer", "looks").
const WIDE = "(min-width: 1000px)";  // docks push the page aside; narrower, they cover it
const SYA = (() => {
  const features = [], docks = {}, handlers = {};
  const html = (x) => (typeof x === "function" ? x() : x);
  function feature(def) {
    if (!def?.id || features.some((f) => f.id === def.id)) throw new Error(`SYA.feature: missing or repeated id ${def?.id}`);
    features.push(def);
    if (def.view) addView(def);
    if (def.header) $("#prefs-btn").insertAdjacentHTML("beforebegin", html(def.header));
    if (def.top) $(".stage > main").insertAdjacentHTML("beforebegin", html(def.top));
    const made = {};
    for (const side of ["left", "right"]) if (def[side]) made[side] = docks[side] = addDock(side, def);
    return made;
  }
  return {
    features, docks, feature, t, lang: I18N.lang, langs: I18N.langs,
    on: (name, fn) => { (handlers[name] ||= []).push(fn); },
    emit: (name, data) => { for (const fn of handlers[name] || []) fn(data); },
    // A slot that collects html from every feature, in registry order: "card" (a board card), "drawer" (a session).
    collect: (slot, item) => features.map((f) => f[slot]?.(item) || "").join(""),
    views: () => features.filter((f) => f.view).map((f) => f.id),
  };
})();

// Browser pairing (app/pairing.py): the server opened http://127.0.0.1:<port>/#pair=<code> in a new tab. The
// code leaves the address at once; only after the person confirms is it traded for the secret, kept in this
// tab's sessionStorage: the tab that confirmed is the paired one, until the dashboard restarts. SYA.pairing tells
// features whether this tab is paired (answering permission prompts needs it) and, on another host name
// (localhost), where to go instead: pairing works on 127.0.0.1 only.
// Event "pair" (paired: true | false | null when unknown).
SYA.pairing = {
  paired: null,
  local: location.hostname === "127.0.0.1",
  get home() { return `http://127.0.0.1:${location.port}/${location.hash}`; },  // the same page on 127.0.0.1
  async check() {
    try {
      const headers = { ...LANG_HEADER, ...pairHeader() };
      this.paired = this.local && (await (await fetch("/api/pair", { headers })).json()).paired === true;
    } catch (e) { this.paired = null; }
    SYA.emit("pair", this.paired);
    return this.paired;
  },
  start: () => postJson("/api/pair/start", {}),  // the server opens a new tab with a fresh code
  // An action was refused because this tab is not paired: a dismissable banner with "Pair this browser"
  // (or, off 127.0.0.1, a link to the address where pairing works). One at a time.
  require() {
    this.check();
    if (document.getElementById("pair-required")) return;
    const bar = document.createElement("div");
    bar.id = "pair-required";
    bar.className = "pair-ask";
    bar.setAttribute("role", "alertdialog");
    bar.innerHTML = this.local
      ? `<p>${t("app.pair.required")}</p>
        <button type="button" class="pair-do">${t("app.pair.do")}</button>
        <button type="button" class="ghost pair-dismiss">${t("app.close")}</button>
        <p class="pair-msg" aria-live="polite"></p>`
      : `<p>${t("app.pair.required_elsewhere")}</p>
        <a href="${this.home}">${t("app.pair.open_home")}</a>
        <button type="button" class="ghost pair-dismiss">${t("app.close")}</button>`;
    document.body.append(bar);
    bar.querySelector(".pair-dismiss").onclick = () => bar.remove();
    bar.querySelector(".pair-do")?.addEventListener("click", async () => {
      const r = await this.start().catch(() => null);
      bar.querySelector(".pair-msg").textContent = r?.ok
        ? t("app.pair.opened_retry")
        : r?.data?.error || t("app.pair.open_failed");
    });
  },
  async forget() {
    const r = await postJson("/api/pair/forget", {});
    if (r.ok) dropPairSecret();
    await this.check();
    return r;
  },
};
(() => {
  const m = location.hash.match(/^#pair=([A-Za-z0-9_-]{20,100})$/);
  if (m) history.replaceState(null, "", location.pathname + location.search);
  if (!m || !SYA.pairing.local) { SYA.pairing.check(); return; }
  // A tab with a code can come from anyone who made the server open it: the person says it was them.
  const ask = document.createElement("div");
  ask.className = "pair-ask";
  ask.setAttribute("role", "alertdialog");
  ask.innerHTML = `<p>${t("app.pair.ask")}</p><p class="pair-note">${t("app.pair.ask_note")}</p>
    <button type="button" class="pair-yes">${t("app.pair.yes")}</button>
    <button type="button" class="ghost pair-no">${t("app.pair.no")}</button>
    <p class="pair-msg" aria-live="polite"></p>`;
  document.body.append(ask);
  ask.querySelector(".pair-no").onclick = () => { ask.remove(); SYA.pairing.check(); };
  ask.querySelector(".pair-yes").onclick = async () => {
    const r = await postJson("/api/pair", { code: m[1] }).catch(() => null);
    let kept = false;
    try { if (r?.ok && r.data?.secret) { sessionStorage.setItem(PAIR_KEY, r.data.secret); kept = true; } } catch (e) { /* storage off */ }
    if (kept) ask.remove();
    else ask.querySelector(".pair-msg").textContent = r?.ok ? t("app.pair.not_kept") : r?.data?.error || t("app.pair.no_answer");
    SYA.pairing.check();
  };
})();

// A view is a tab in the bar and a <main id="<id>" class="<id>"> in .stage; the first one is the home view.
function addView(def) {
  if (!state.view) state.view = def.id;
  const main = document.createElement("main");
  main.id = def.id; main.className = def.id + (state.view === def.id ? "" : " hidden");
  $("#results").before(main);
  const tab = document.createElement("button");
  tab.dataset.view = def.id; tab.setAttribute("aria-pressed", String(state.view === def.id)); tab.textContent = def.view.title;
  tab.onclick = () => setView(def.id);
  $(".views").append(tab);
}

// A dock is a panel on the left or the right of every view: open or hidden and how wide, per viewer
// (localStorage, where a feature may keep its own keys in dock.state). The left one keeps the chat's old key.
function addDock(side, def) {
  const p = def[side], left = side === "left", key = left ? "sya.dock" : `sya.dock.${side}`;
  if (SYA.docks[side]) throw new Error(`SYA.feature ${def.id}: the ${side} dock belongs to ${SYA.docks[side].id}`);
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key)); } catch (e) { /* storage unavailable */ }
  const aside = document.createElement("aside");
  aside.id = left ? "dock" : `dock-${side}`; aside.className = left ? "dock" : `dock dock-${side}`;
  aside.setAttribute("aria-label", p.label); aside.hidden = true;
  aside.innerHTML = `<div id="${def.id}" class="${def.id}"></div><div class="dock-grip" title="${t("app.dock.resize")}"></div>`;
  (Object.values(SYA.docks).at(-1)?.el || $(".stage")).after(aside);
  const button = document.createElement("button");
  button.className = left ? "ghost icon dock-btn" : `ghost icon dock-btn dock-btn-${side}`;
  button.id = left ? "dock-btn" : `dock-${side}-btn`;
  button.setAttribute("aria-controls", aside.id); button.setAttribute("aria-expanded", "false"); button.title = p.title;
  button.innerHTML = `${p.icon || ""}<span>${esc(p.button)}</span>`;
  if (left) $(".bar").prepend(button); else $("#prefs-btn").before(button);
  const dock = {
    id: def.id, side, panel: p, el: aside, root: aside.firstElementChild, button,
    state: saved && typeof saved === "object" ? saved : { open: typeof p.open === "function" ? !!p.open() : !!p.open },
    get open() { return !!this.state.open; },
    save() { try { localStorage.setItem(key, JSON.stringify(dock.state)); } catch (e) { /* not persisted */ } },
    setOpen(open) {
      dock.state.open = open;
      dock.save();
      aside.hidden = !open;
      document.body.classList.toggle(left ? "dock-open" : `dock-${side}-open`, open);
      button.setAttribute("aria-expanded", String(open));
      if (!open) p.hide?.();
      else if (state.board) p.render(dock.root);
    },
  };
  // Width: drag the panel's inner edge; double-click resets it.
  const cssVar = left ? "--dock-w" : `--dock-${side}-w`;
  const setWidth = (px) => {
    dock.state.w = px ? Math.round(Math.min(Math.max(px, 280), innerWidth * .5)) : 0;
    document.documentElement.style[dock.state.w ? "setProperty" : "removeProperty"](cssVar, `${dock.state.w}px`);
  };
  const grip = aside.querySelector(".dock-grip");
  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId); grip.classList.add("drag");
    const move = (ev) => setWidth(left ? ev.clientX : innerWidth - ev.clientX);
    const up = () => { grip.removeEventListener("pointermove", move); grip.classList.remove("drag"); dock.save(); };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up, { once: true });
  });
  grip.addEventListener("dblclick", () => { setWidth(0); dock.save(); });
  aside.addEventListener("click", (e) => { if (e.target.closest(".dock-x")) dock.setOpen(false); });  // the panel's own hide button
  button.onclick = () => dock.setOpen(!dock.open);
  if (dock.state.w) setWidth(dock.state.w);
  dock.setOpen(dock.open);
  return dock;
}
function renderDocks() {
  for (const dock of Object.values(SYA.docks)) {
    const p = dock.panel;
    if (p.badge) {
      const n = p.badge(), b = dock.button.querySelector(".dock-badge");
      if (!n) b?.remove();
      else if (b) b.textContent = n;
      else dock.button.insertAdjacentHTML("beforeend", `<b class="dock-badge">${esc(n)}</b>`);
    }
    if (dock.open) p.render(dock.root);
  }
}

/* ---------- formatting, in the page's language ---------- */
// The time since ts: "5 min", "3 hr", "2 days"; "" for no time, null under a minute. Catalog strings with
// plural forms rather than Intl.RelativeTimeFormat, whose short Russian forms carry a period.
function elapsed(ts) {
  if (!ts) return "";
  const m = Math.max(0, Math.round((Date.now() - Date.parse(ts)) / 60000));
  if (m < 1) return null;
  if (m < 60) return t("app.time.minutes", { n: m });
  if (m < 48 * 60) return t("app.time.hours", { n: Math.round(m / 60) });
  return t("app.time.days", { n: Math.round(m / 1440) });
}
const ago = (ts) => elapsed(ts) ?? t("app.time.now");  // "5 min", "just now"
const agoBack = (ts) => { const s = elapsed(ts); return s ? t("app.time.ago", { span: s }) : s ?? t("app.time.now"); };  // "5 min ago"
const waited = (ms) => elapsed(new Date(ms).toISOString()) ?? t("app.time.under_minute");  // "5 min", "less than a minute"
// A 24-hour clock keeps two-digit hours (09:05); a 12-hour one reads 9:05 AM.
const TIME = { hour: new Intl.DateTimeFormat(I18N.lang, { hour: "numeric" }).resolvedOptions().hour12 ? "numeric" : "2-digit", minute: "2-digit" };
const when = (ts) => ts ? new Date(ts).toLocaleString(I18N.lang, { day: "numeric", month: "short", ...TIME }) : "";  // "3 Nov, 14:05"
const clock = (ts) => ts ? new Date(ts).toLocaleTimeString(I18N.lang, TIME) : "";  // "14:05"
// "Today", "Yesterday", else the date ("3 November", with the year when it is not this one).
function dayLabel(ts) {
  const d = new Date(ts), now = new Date(), same = (a, b) => a.toDateString() === b.toDateString();
  const days = same(d, now) ? 0 : same(d, new Date(now - 864e5)) ? -1 : null;
  if (days !== null) return capital(new Intl.RelativeTimeFormat(I18N.lang, { numeric: "auto" }).format(days, "day"));
  return d.toLocaleDateString(I18N.lang, { day: "numeric", month: "long", ...(d.getFullYear() !== now.getFullYear() && { year: "numeric" }) });
}
const money = (x) => x ? `$${x.toFixed(x < 10 ? 2 : 0)}` : "";
// How full the session's context window is, in percent; null when its provider cannot tell.
const contextFill = (c) => (c.context_window ? Math.min(100, Math.round(100 * c.context_tokens / c.context_window)) : null);
const kTokens = (n) => (n >= 1e6 ? t("app.tokens.millions", { n: +(n / 1e6).toFixed(1) }) : t("app.tokens.thousands", { n: Math.round(n / 1000) }));
const contextText = (c) => t("app.context", { fill: contextFill(c), used: kTokens(c.context_tokens), total: kTokens(c.context_window) });
// Russian plural forms of n, for the strings still written in the code; a new string is a catalog plural.
function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  return `${n} ${a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many}`;
}
const tokens = (n) => (!n ? "" : n >= 1e4 ? t("app.tokens.many", { amount: kTokens(n) }) : t("app.tokens.count", { n }));

/* ---------- programs: what the session's provider can do (/api/board `providers`, ids in registry order) ---------- */
// Without a cap the page hides the control or the field. A board without the field (the demo) can do everything.
function can(c, cap) {
  const p = state.board?.providers?.[c?.provider];
  return !p || p.caps.includes(cap);
}
// A session of any program but the default (the registry's first) carries its name: card, drawer, chat header.
function programTag(c) {
  const all = state.board?.providers || {}, p = all[c?.provider];
  return p && c.provider !== Object.keys(all)[0]
    ? `<span class="chip chip-program" title="${esc(t("app.program_session", { program: p.title }))}">${esc(p.title)}</span>` : "";
}
// What the session cost: dollars where the program counts them, tokens elsewhere.
const spend = (c) => (can(c, "cost") ? money(c.cost_usd) : tokens(c.tokens));

function liveLine(c) {
  const l = c.live;
  if (!l) return STATUS[c.column];
  if (l.status === "waiting") return waitingFor(l.waiting_for) || t("app.waiting.reply");
  return LIVE_STATUS[l.status] || l.status;
}
// "waiting for permission"; a reason the page does not know is named as it came.
const waitingFor = (why) => (why ? WAITING[why] || t("app.waiting.other", { what: why }) : "");
function askText(c) {
  const l = c.live || {};
  if (l.needs) return l.needs;
  if (l.waiting_for === "permission prompt" && c.pending_tool) return t("app.ask.permission_for", { tool: c.pending_tool });
  if (l.waiting_for) return capital(waitingFor(l.waiting_for));
  return c.open_questions ? t("app.ask.question") : t("app.ask.decision");
}

function chips(c) {
  const jira = c.jira.slice(0, 3).map((j) => j.url
    ? `<a class="chip chip-jira" href="${esc(j.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(j.key)}</a>`
    : `<span class="chip chip-jira">${esc(j.key)}</span>`).join("");
  const prs = c.prs.filter((p) => isWebUrl(p.url)).slice(-2).map((p) => prChip(p, c)).join("");
  const tag = programTag(c);
  return tag || jira || prs ? `<div class="chips">${tag}${jira}${prs}</div>` : "";
}
// A link from a transcript goes on the page only as a web address: never javascript: or data:.
const isWebUrl = (u) => typeof u === "string" && /^https?:\/\//i.test(u);
// A PR/MR chip. The `chip` slot of the features adds to it: {cls, html, title} (classes, html after the
// number, tooltip lines), or nothing.
function prChip(p, c) {
  if (!isWebUrl(p.url)) return "";
  const add = SYA.features.map((f) => f.chip?.(p, c)).filter(Boolean);
  const cls = add.map((a) => a.cls).filter(Boolean).join(" "), title = add.map((a) => a.title).filter(Boolean).join("\n");
  return `<a class="chip${cls ? ` ${esc(cls)}` : ""}" href="${esc(p.url)}"${title ? ` title="${esc(title)}"` : ""} target="_blank" rel="noopener" onclick="event.stopPropagation()">${p.url.includes("merge_requests") ? "!" : "#"}${esc(p.number)}${add.map((a) => a.html || "").join("")}</a>`;
}
const progressBar = (c) => c.tasks_total && can(c, "tasks")
  ? `<div class="progress"><div class="track"><div class="fill" style="width:${100 * c.tasks_done / c.tasks_total}%"></div></div>${c.tasks_done}/${c.tasks_total}</div>` : "";
const taskItems = (tasks) => tasks.map((task) => `<li class="${esc(task.status)}"${task.status === "in_progress"
  ? ` data-now="${t("app.tasks.now")}"` : ""}>${esc(task.status === "in_progress" && task.active_form ? task.active_form : task.subject)}</li>`).join("");

// A session id in a peer message shows as that session's title, the supervisor by its role (by id, or by any name it
// had: `supervisor_names`); any other label stays as it came.
function peerName(who) {
  const c = state.board?.cards.find((x) => x.session_id === who);
  if (!c) return state.board?.supervisor_names?.includes(who) ? t("app.supervisor") : who;
  return c.session_id === state.board.supervisor_id ? t("app.supervisor") : c.live?.name || c.title;
}

function visibleCards() {
  const cards = state.board ? state.board.cards : [];
  return state.project ? cards.filter((c) => c.project === state.project) : cards;
}

/* ---------- tally, favicon, notifications ---------- */
function setFavicon(needs) {
  const badge = needs ? `<circle cx="23" cy="9" r="9" fill="#e3302b"/><text x="23" y="13" font-size="12" font-weight="700" font-family="sans-serif" text-anchor="middle" fill="#fff">${needs > 9 ? "9+" : needs}</text>` : "";
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="1" y="17" width="13" height="13" fill="#d93a3f"/>
    <circle cx="23" cy="23.5" r="7" fill="#178a7e"/>${needs ? "" : `<path d="M8 2l7 13H1z" fill="#d79a2b"/>`}${badge}</svg>`;
  $("#favicon").href = "data:image/svg+xml," + encodeURIComponent(svg);
}
function renderTally() {
  const cards = state.board.cards;
  const n = (col) => cards.filter((c) => c.column === col).length;
  const needs = n("needs_you");
  $("#n-needs").textContent = needs; $("#n-work").textContent = n("working"); $("#n-move").textContent = n("your_move");
  document.title = needs ? `(${needs}) See your agents` : "See your agents";
  setFavicon(needs);
  const projects = [...new Set(cards.map((c) => c.project).filter(Boolean))].sort();
  const sel = $("#project");
  if (sel.options.length - 1 !== projects.length) {
    sel.innerHTML = `<option value="">${t("app.bar.all_projects")}</option>` + projects.map((p) => `<option>${esc(p)}</option>`).join("");
    sel.value = state.project;
  }
}

function notifyNewNeeds() {
  const now = new Set(state.board.cards.filter((c) => c.column === "needs_you").map((c) => c.session_id));
  if (state.prevNeeds && "Notification" in window && Notification.permission === "granted") {
    for (const c of state.board.cards) {
      if (now.has(c.session_id) && !state.prevNeeds.has(c.session_id)) {
        const n = new Notification(t("app.notify.needs_you", { title: c.title }), { body: liveLine(c), tag: c.session_id });
        n.onclick = () => { window.focus(); openDrawer(c.session_id); };
      }
    }
  }
  state.prevNeeds = now;
}

function render() {
  if (!state.board) return;
  renderTally();
  for (const f of SYA.features) f.render?.();
  SYA.features.find((f) => f.id === state.view)?.view.render($("#" + state.view), state.viewArg);
  renderDocks();
}

async function refresh() {
  try {
    const board = await api.board();
    // Another server process answers now: a restart forgot this tab's pairing. New code too (an accepted
    // build, a rollback): ask for a reload rather than doing it, so a restart never silently loads whatever
    // took the port meanwhile.
    if (state.instance && board.instance !== state.instance) { dropPairSecret(); SYA.pairing.check(); }
    state.instance = board.instance || "";
    // ...and the page keeps showing what it had: it does not render a board its own code was not made for
    if (board.version && (state.version ||= board.version) !== board.version) { showUpdateBanner(); return; }
    if (board.instance) state.ownInstances.add(board.instance);  // processes that served this page's code
    // re-render only when something changed (or the minute ticked), so animations and hover don't restart
    const sig = JSON.stringify({ ...board, generated_at: 0 }) + Math.floor(Date.now() / 60000);
    if (sig === state.sig) return;
    state.board = board; state.sig = sig;
    notifyNewNeeds();
    for (const f of SYA.features) f.onBoard?.(board);
    if (!$("#q").value) render();
  } catch (e) { /* server restarting; next tick retries */ }
}

// "Reload" reloads only into a process that looks like the new dashboard: a fresh /api/board names an
// instance this page never had, and still the same one 2 s later. A squatter can fake that; this only keeps the
// button from loading straight into whatever briefly answers on the port.
function showUpdateBanner() {
  if ($("#update-banner")) return;
  const b = document.createElement("div");
  b.id = "update-banner";
  b.className = "update-banner";
  b.setAttribute("role", "status");
  b.innerHTML = `<span>${t("app.update.note")}</span><button type="button">${t("app.update.reload")}</button>`;
  const button = b.querySelector("button"), note = b.querySelector("span");
  const instanceNow = async () => { try { return (await api.board()).instance || ""; } catch (e) { return ""; } };
  button.onclick = async () => {
    button.disabled = true;
    const first = await instanceNow();
    const fresh = first && !state.ownInstances.has(first);
    const steady = fresh && (await new Promise((r) => setTimeout(r, 2000)), await instanceNow()) === first;
    if (steady) { location.reload(); return; }
    note.textContent = t("app.update.no_answer");
    button.disabled = false;
  };
  document.body.append(b);
}

/* ---------- look & feel panel ---------- */
function pressed(on) { return `aria-pressed="${on}"`; }
function renderPrefs() {
  $("#skins").innerHTML = Object.entries(SKINS).map(([id, s]) => `<button class="skin" data-skin="${id}" ${pressed(prefs.skin === id)}>
      <span class="sw">${s.sw.map((c) => `<i style="background:${c}"></i>`).join("")}</span>
      <b style="font-family:'${s.font}', system-ui">${esc(s.name)}</b><small>${esc(s.note)}</small></button>`).join("");
  $("#themes").innerHTML = Object.entries(THEMES).map(([id, name]) => `<button data-theme-pick="${id}" ${pressed(prefs.theme === id)}>${name}</button>`).join("");
  const byStatus = prefs.deal === "status";
  $("#shapes").innerHTML = Object.entries(SHAPES).map(([id, s]) => `<button class="pick" data-shape="${id}" title="${esc(s.name)}" aria-label="${esc(s.name)}"
      ${pressed(prefs.shapes.includes(id))} ${byStatus ? "disabled" : ""}><svg viewBox="0 0 100 100"><path class="body" d="${s.d}"/></svg></button>`).join("");
  $("#deal").innerHTML = Object.entries(DEALS).map(([id, name]) => `<button data-deal="${id}" ${pressed(prefs.deal === id)}>${name}</button>`).join("");
  $("#deal-note").textContent = [DEAL_NOTE[prefs.deal], byStatus ? "" : t("app.prefs.pick_shapes"), t("app.prefs.one_agent")]
    .filter(Boolean).join(" ");
  $("#desk-picks").innerHTML = Object.entries(DESKS).map(([id, d]) => `<button class="pick" data-desk="${id}" ${pressed(prefs.desk === id)}>
      ${deskSvg("circle", "your_move", d, false)}<span>${esc(d.name)}</span></button>`).join("");
  $("#faces").checked = prefs.faces;
  $("#lang").innerHTML = Object.entries(SYA.langs).map(([id, name]) => `<option value="${esc(id)}" lang="${esc(id)}"
      ${id === SYA.lang ? "selected" : ""}>${esc(name)}</option>`).join("");
  for (const f of SYA.features) f.prefs?.render?.();
}
// Another language: kept in prefs and the page reloads, so every string comes from the new catalog. Only into
// the process this page talks to now, as the update banner does: never into whatever took the port.
$("#lang").onchange = async (e) => {
  const now = await api.board().then((b) => b.instance || "", () => null);
  if (now !== state.instance) { e.target.value = SYA.lang; $("#lang-note").textContent = t("app.update.no_answer"); return; }
  setPrefs({ lang: e.target.value });
  location.reload();
};
function setPrefs(change) {
  Object.assign(prefs, change);
  for (const f of SYA.features) f.prefs?.apply?.();
  savePrefs();
  document.documentElement.dataset.skin = prefs.skin;
  renderPrefs();
  render();
}
function placePrefs() {
  const r = $("#prefs-btn").getBoundingClientRect(), p = $("#prefs");
  p.style.top = `${r.bottom + 8}px`;
  p.style.left = `${Math.max(12, Math.min(r.right - p.offsetWidth, innerWidth - p.offsetWidth - 12))}px`;
}
function togglePrefs(open) {
  $("#prefs").classList.toggle("hidden", !open);
  $("#prefs-btn").setAttribute("aria-expanded", String(open));
  if (open) { renderPrefs(); placePrefs(); }
}
$("#prefs-btn").onclick = () => togglePrefs($("#prefs").classList.contains("hidden"));
$("#prefs").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.skin) setPrefs({ skin: b.dataset.skin, ...SKINS[b.dataset.skin].office });
  else if (b.dataset.themePick !== undefined) {
    const root = document.documentElement;
    if (b.dataset.themePick) root.dataset.theme = b.dataset.themePick; else delete root.dataset.theme;
    setPrefs({ theme: b.dataset.themePick });
  } else if (b.dataset.shape) {
    const on = prefs.shapes.includes(b.dataset.shape);
    if (on && prefs.shapes.length === 1) return;  // keep at least one shape
    setPrefs({ shapes: on ? prefs.shapes.filter((s) => s !== b.dataset.shape) : [...prefs.shapes, b.dataset.shape] });
  } else if (b.dataset.deal) setPrefs({ deal: b.dataset.deal });
  else if (b.dataset.desk) setPrefs({ desk: b.dataset.desk });
};
$("#faces").onchange = (e) => setPrefs({ faces: e.target.checked });
document.addEventListener("pointerdown", (e) => {
  if (!$("#prefs").classList.contains("hidden") && !e.target.closest("#prefs, #prefs-btn")) togglePrefs(false);
});
addEventListener("resize", () => { if (!$("#prefs").classList.contains("hidden")) placePrefs(); });

/* ---------- drawer ---------- */
function section(title, body) { return body ? `<section><h4>${title}</h4>${body}</section>` : ""; }

function drawerHtml(d) {
  const l = d.live || {};
  const now = [];
  if (l.needs) now.push(`<p><b>${t("app.drawer.needs")}</b> ${esc(l.needs)}</p>`);
  if (l.suggested_reply) now.push(`<p><b>${t("app.drawer.suggested")}</b> ${esc(l.suggested_reply)}</p>`);
  const open = d.questions.filter((q) => q.answer === null);
  if (open.length && d.column === "needs_you") now.push(...open.slice(-1).flatMap((q) => q.items.map((i) =>
    `<p><b>${esc(i.header || t("app.drawer.question"))}:</b> ${esc(i.question)}<br><span style="color:var(--ink-2)">${esc(i.options.join(" · "))}</span></p>`)));
  const tasks = d.tasks.length && can(d, "tasks") ? `<ul class="tasks">${taskItems(d.tasks)}</ul>` : "";
  const plans = d.plans.slice().reverse().map((p) => `<button class="ghost plan-btn" data-plan="${p.index}" ${p.path && !p.exists ? "disabled" : ""}>
      ${esc(p.path ? p.path.split("/").slice(-2).join("/") : t("app.drawer.plan_mode"))} <span style="color:var(--ink-3)">· ${esc(when(p.ts))}${p.path && !p.exists ? ` · ${t("app.drawer.plan_gone")}` : ""}</span></button>`).join("");
  const recaps = d.events.filter((e) => e.kind === "recap");
  const events = d.events.slice(-120).reverse();
  // The index keeps markers, not words: a compaction by its kind (older rows carry Russian text: the kind wins),
  // a subagent without a task name as "".
  const eventText = (e) => (e.kind === "compact" ? t("app.event.compacted") : e.text || (e.kind === "agent" ? t("app.event.subagent") : ""));
  const timeline = events.map((e) => `<li class="${esc(e.kind)}">
      <span class="k"><b>${esc(EVENT[e.kind] || e.kind)}</b>${esc(when(e.ts))}</span><span class="txt">${esc(eventText(e))}</span></li>`).join("");
  const agents = d.agents.slice(-15).reverse().map((a) => `<li>${esc(a.description || t("app.event.subagent"))} <span style="color:var(--ink-3)">· ${esc(a.type || "agent")}${a.background ? ` · ${t("app.drawer.background")}` : ""} · ${esc(when(a.ts))}</span></li>`).join("");
  const none = "—";
  return `
    <button class="ghost close" id="d-close">${t("app.close")}</button>
    <h2 id="d-title">${esc(d.title)}</h2>
    <div class="status-line s-${d.column}"><span class="dot"></span>${esc(liveLine(d))}${l.status_since_ms ? ` · ${esc(ago(new Date(l.status_since_ms).toISOString()))}` : ""}</div>
    ${chips(d)}
    ${now.length ? `<div class="callout">${now.join("")}</div>` : ""}
    <div class="d-actions"><button class="ghost d-chat" data-chat="${esc(d.session_id)}">${t("app.drawer.open_chat")}</button>
      ${d.done ? `<button class="ghost" data-unarchive="${esc(d.session_id)}">${t("app.drawer.unarchive")}</button>`
        : d.column !== "archive" ? `<button class="ghost" data-archive="${esc(d.session_id)}">${t("app.drawer.archive")}</button>` : ""}
      ${d.live && can(d, "stop") ? `<button class="ghost" data-stop="${esc(d.session_id)}">${t("app.drawer.stop")}</button>` : ""}
      ${d.can_fork ? `<button class="ghost" data-fork="${esc(d.session_id)}" title="${t("app.drawer.fork_title")}">${t("app.drawer.fork")}</button>` : ""}</div>
    ${d.resume_command ? `<div class="cmd"><code>${esc(d.resume_command)}</code><button class="ghost" id="copy">${t("app.drawer.copy")}</button></div>` : ""}
    ${section(t("app.drawer.now"), (tasks || "") + (recaps.length ? `<p style="color:var(--ink-2)">${esc(recaps[recaps.length - 1].text)}</p>` : (tasks ? "" : `<p style="color:var(--ink-2)">${esc(d.last_assistant_text || t("app.drawer.no_recaps"))}</p>`)))}
    ${section(t("app.drawer.plans"), plans)}
    ${section(t("app.drawer.agents"), agents ? `<ul style="margin:0;padding-left:18px;font-size:13.5px">${agents}</ul>` : "")}
    ${SYA.collect("drawer", d)}
    ${section(t("app.drawer.details"), `<dl class="kv">
      <dt>${t("app.drawer.folder")}</dt><dd>${esc(d.cwd)}</dd><dt>${t("app.drawer.branch")}</dt><dd>${esc(d.branch || none)}</dd>
      <dt>${t("app.drawer.started")}</dt><dd>${esc(when(d.started_at))}</dd><dt>${t("app.drawer.last_activity")}</dt><dd>${esc(when(d.last_activity))}</dd>
      <dt>${t("app.drawer.prompts")}</dt><dd>${d.n_prompts}</dd>${can(d, "cost")
        ? `<dt>${t("app.drawer.cost")}</dt><dd>${esc(money(d.cost_usd) || none)}</dd>`
        : `<dt>${t("app.drawer.tokens")}</dt><dd>${esc(tokens(d.tokens) || none)}</dd>`}
      ${contextFill(d) != null ? `<dt>${t("app.drawer.context")}</dt><dd>${esc(contextText(d))}</dd>` : ""}
      ${can(d, "cost") ? `<dt>${t("app.drawer.lines")}</dt><dd>+${d.lines_added} / −${d.lines_removed}</dd>` : ""}
      <dt>${t("app.drawer.models")}</dt><dd>${esc(d.models.join(", ") || none)}</dd>
      <dt>${t("app.drawer.tools")}</dt><dd>${esc(d.top_tools.map(([n, k]) => `${n} ${k}`).join(", "))}</dd>
      <dt>${t("app.drawer.session")}</dt><dd>${esc(d.session_id)}${d.transcript_missing ? ` ${t("app.drawer.transcript_gone")}` : ""}</dd>
      ${d.continued_in ? `<dt>${t("app.drawer.continued_in")}</dt><dd><a href="#" data-goto="${esc(d.continued_in)}">${esc(d.continued_in.slice(0, 8))}</a></dd>` : ""}
    </dl>`)}
    ${timeline ? `<section><details class="d-more"><summary>${t("app.drawer.timeline")}<small>${t("app.drawer.events", { n: events.length })}</small></summary>
      <ul class="timeline">${timeline}</ul></details></section>` : ""}`;
}

async function openDrawer(id) {
  SYA.emit("drawer", id);
  state.openId = id;
  const d = await api.session(id);
  const drawer = $("#drawer");
  if (!d) {
    const c = state.board.cards.find((x) => x.session_id === id);
    drawer.innerHTML = `<button class="ghost close" id="d-close">${t("app.close")}</button><h2 id="d-title">${esc(c ? c.title : id)}</h2>
      <p>${t("app.drawer.not_indexed")}</p>${c && c.recap ? `<p>${esc(c.recap)}</p>` : ""}`;
  } else {
    drawer.innerHTML = drawerHtml(d);
    const copy = $("#copy");
    if (copy) copy.onclick = () => navigator.clipboard.writeText(d.resume_command)
      .then(() => { copy.textContent = t("app.drawer.copied"); })
      .catch(() => {  // clipboard refused: select the command so the user can copy it
        const range = document.createRange();
        range.selectNodeContents(drawer.querySelector(".cmd code"));
        getSelection().removeAllRanges(); getSelection().addRange(range);
        copy.textContent = t("app.drawer.selected");
      });
    drawer.querySelectorAll("[data-plan]").forEach((b) => b.onclick = () => openPlan(id, b.dataset.plan, b.textContent.trim()));
    drawer.querySelectorAll("[data-goto]").forEach((a) => a.onclick = (e) => { e.preventDefault(); openDrawer(a.dataset.goto); });
  }
  $("#d-close").onclick = closeDrawer;
  drawer.classList.remove("hidden"); $("#scrim").classList.remove("hidden");
  $("#d-close").focus();
}
function closeDrawer() {
  state.openId = null; $("#drawer").classList.add("hidden"); $("#scrim").classList.add("hidden");
  SYA.emit("drawer", null);
}
function openPlan(id, i, title) {
  $("#m-title").textContent = title;
  const frame = $("#m-frame"), source = api.planFrame(id, i);
  if (source.srcdoc != null) frame.srcdoc = source.srcdoc; else { frame.removeAttribute("srcdoc"); frame.src = source.src; }
  $("#modal").classList.remove("hidden");
}
$("#m-close").onclick = () => { $("#modal").classList.add("hidden"); $("#m-frame").removeAttribute("srcdoc"); $("#m-frame").src = "about:blank"; };
$("#scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !e.target.closest("input, select, textarea")) { e.preventDefault(); $("#q").focus(); return; }
  if (e.key !== "Escape") return;
  if (!$("#prefs").classList.contains("hidden")) togglePrefs(false);
  else if (!$("#modal").classList.contains("hidden")) $("#m-close").click();
  else if (state.openId) closeDrawer();
  else if ($("#q").value) { $("#q").value = ""; showSearch(""); }
});
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-archive], [data-unarchive], [data-stop], [data-stop-yes], [data-stop-no]");
  if (b?.dataset.archive) return setDone(b.dataset.archive, true);
  if (b?.dataset.unarchive) return setDone(b.dataset.unarchive, false);
  if (b?.dataset.stop) return askStop(b.closest(".d-actions"), b.dataset.stop);
  if (b?.dataset.stopYes) return stopSession(b.closest(".d-actions"), b.dataset.stopYes);
  if (b?.dataset.stopNo) return openDrawer(b.dataset.stopNo);
  const target = e.target.closest(".card[data-id], .desk[data-id], .hit[data-id]");
  if (target && !e.target.closest("a")) openDrawer(target.dataset.id);
});
// A session's done mark and its stop: the chat's endpoints (features/chat/delivery.py), or the demo page's fake.
const sessionAct = (id, name, body = {}) => (window.SYA_API?.chat
  ? window.SYA_API.chat.act(id, name, body)
  : postJson(`/api/chat/${encodeURIComponent(id)}/${name}`, body)).catch(() => null);
async function setDone(id, done) {
  const r = await sessionAct(id, "done", { done });
  if (r?.ok && state.openId === id) { if (done) closeDrawer(); else openDrawer(id); }
  refresh();
}
// "Finish" asks first, in the panel's own row: the agent's process stops and the session goes to the archive.
function askStop(row, id) {
  row.innerHTML = `<span class="d-ask">${t("app.drawer.stop_ask")}</span>
    <button class="danger" data-stop-yes="${esc(id)}">${t("app.drawer.stop_yes")}</button>
    <button class="ghost" data-stop-no="${esc(id)}">${t("app.drawer.stop_no")}</button>`;
  row.querySelector("[data-stop-no]").focus();
}
async function stopSession(row, id) {
  row.querySelectorAll("button").forEach((x) => { x.disabled = true; });
  const r = await sessionAct(id, "stop");
  if (r?.ok) { if (state.openId === id) closeDrawer(); return refresh(); }
  row.querySelector(".d-ask").textContent = r?.data?.error || t("app.drawer.no_answer");
  row.querySelectorAll("button").forEach((x) => { x.disabled = false; });
}

/* ---------- views, filter, search ---------- */
// The view and what it opens live in the hash (#chat/<session id>, #tools), so a reload or a link keeps them.
function setView(view, arg = "") {
  state.view = view; state.viewArg = arg;
  history.replaceState(null, "", view === SYA.views()[0] ? location.pathname + location.search : `#${view}${arg ? "/" + encodeURIComponent(arg) : ""}`);
  document.querySelectorAll(".views button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.view === view)));
  for (const v of SYA.views()) $("#" + v).classList.toggle("hidden", v !== view);
  $("#q").value = ""; $("#results").classList.add("hidden");
  SYA.emit("view", view);
  render();
}
// Feature scripts load after this one; by DOMContentLoaded every view has registered.
function viewFromHash() {
  const [view, ...rest] = location.hash.slice(1).split("/");
  const arg = decodeURIComponent(rest.join("/"));
  const routed = SYA.features.find((f) => f.id === view && f.route);
  if (routed) { routed.route(arg); setView(state.view, state.viewArg); return; }  // #chat/<id>: a panel opens, the view stays
  if (SYA.views().includes(view) && (view !== state.view || arg !== state.viewArg)) setView(view, arg);
}
window.addEventListener("hashchange", viewFromHash);  // links like <a href="#chat/ID">
$("#project").onchange = (e) => { state.project = e.target.value; render(); };

let searchTimer;
$("#q").oninput = (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => showSearch(e.target.value.trim()), 250); };
async function showSearch(q) {
  const res = $("#results");
  if (!q) { res.classList.add("hidden"); $("#" + state.view).classList.remove("hidden"); render(); return; }
  const hits = await api.search(q);
  const byId = new Map((state.board?.cards || []).map((c) => [c.session_id, c]));
  const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  const mark = (t) => esc(t).replace(new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
  const snippet = (t) => { const i = t.search(re); return i < 0 ? t.slice(0, 220) : (i > 80 ? "…" : "") + t.slice(Math.max(0, i - 80), i + 160); };
  res.innerHTML = Object.entries(hits).map(([sid, list]) => {
    const c = byId.get(sid);
    return `<div class="hit" data-id="${esc(sid)}"><h3>${esc(c ? c.title : sid.slice(0, 8))}</h3>
      <div class="where">${esc(c ? [c.project, STATUS[c.column], ago(c.last_activity)].join(" · ") : "")}</div>
      ${list.slice(0, 4).map((h) => `<p><b>${esc(EVENT[h.kind] || t("app.search.title"))}:</b> ${mark(snippet(h.text))}</p>`).join("")}</div>`;
  }).join("") || `<p class="empty">${esc(t("app.search.nothing", { q }))}</p>`;
  res.classList.remove("hidden"); for (const v of SYA.views()) $("#" + v).classList.add("hidden");
}

/* ---------- notifications ---------- */
const nb = $("#notify");
function showNotifyState() {
  const p = Notification.permission;
  const label = p === "granted" ? t("app.notify.on") : p === "denied" ? t("app.notify.denied") : t("app.notify.off");
  nb.title = label; nb.setAttribute("aria-label", label);
  nb.setAttribute("aria-pressed", String(Notification.permission === "granted"));
}
if (!("Notification" in window)) nb.classList.add("hidden");
else showNotifyState();
nb.onclick = async () => {
  if (Notification.permission !== "granted") await Notification.requestPermission();
  showNotifyState();
};

document.documentElement.dataset.skin = prefs.skin;
window.addEventListener("DOMContentLoaded", () => {
  viewFromHash();
  refresh();
  setInterval(refresh, 3000);
});
