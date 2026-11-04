// Chat: a messenger over sessions in the left dock, next to every view: the list of agents, or one thread
// with its composer. Server routes: /api/chat/* (thread.py, delivery.py). Demo page: window.SYA_API.chat.
// Globals from app.js: $, esc, state, SYA, WIDE, ago, clock, dayLabel, liveLine, peerName, openDrawer, closeDrawer,
// refresh, getJson, postJson, LANG_HEADER, pairHeader, visibleCards, can, programTag; from i18n.js: t; from figures.js:
// shapeFor, SHAPES, face; from looks.js (optional): agentAvatarSvg.
// Exposes openChat(sessionId) ("" opens the list); #chat/<id> in the URL and any [data-chat] element call it.
// Other features add conversations to the list and the rail with SYA.chat.addSource({id, items, open, close}), and
// blocks above a session's composer with SYA.chat.addPanel({id, render, hint}) (below).
(() => {
  const enc = encodeURIComponent;
  const POLL_MS = 2500, PAGE = 60, PENDING_TTL_MS = 60000;
  const SEEN_KEY = "sya.chat.seen";
  const ORDER = ["needs_you", "working", "your_move", "recent"];
  const ROUTE_WORD = { vscode: "VS Code", tmux: "tmux", resume: "claude --resume", takeover: t("chat.route.takeover"), peer: t("chat.route.peer") };
  const RAIL_MAX = 24;
  // The courier's sender (core/providers/claude/routes.py SENDER): what the chat sent shows as the user's own.
  // Transcripts from before it was English name it «Дашборд».
  const FROM_DASHBOARD = new Set(["Dashboard", "Дашборд"]);
  const fromHere = (m) => m.kind === "peer_in" && FROM_DASHBOARD.has(m.from);
  const PEER_NOTE = t("chat.peer_note");
  // A stopped supervisor is resumed with its own tools and rules (server: Feature.resume_args), not as a plain session.
  const SUP_RESUME_NOTE = t("chat.sup.resume_note");
  const NO_FILES = t("chat.no_files");  // no Cap.FILES
  const supStopped = (c) => !c.live && c.session_id === state.board?.supervisor_id;
  // The supervisor goes by its role, whatever its session is titled ("Supervisor", «Супервизор» before that).
  const chatTitle = (c) => (c.session_id === state.board?.supervisor_id ? t("chat.supervisor") : c.title);

  /* ---------- data source: the server, or the demo page's fake ---------- */
  const asResult = async (r) => ({ ok: r.ok, status: r.status, data: await r.json().catch(() => null) });
  const serverChat = {
    thread: (id, q) => getJson(`/api/chat/${enc(id)}?${new URLSearchParams(q)}`),
    imageSrc: (id, url) => `/api/chat/${enc(id)}/${url}`,
    send: (id, text, files, extra = {}) => postJson(`/api/chat/${enc(id)}/send`, { text, files, ...extra }),
    upload: (id, file) => fetch(`/api/chat/${enc(id)}/upload`, {
      method: "POST", body: file,
      headers: { "X-SYA": "1", ...LANG_HEADER, ...pairHeader(), "X-Filename": enc(file.name), "Content-Type": file.type || "application/octet-stream" },
    }).then(asResult),
    act: (id, name, body = {}) => postJson(`/api/chat/${enc(id)}/${name}`, body),
  };
  const chatApi = window.SYA_API?.chat || serverChat;

  /* ---------- state ---------- */
  const chat = {
    sid: "", token: 0, ids: [], els: new Map(), before: null, after: 0, hasMore: false, loaded: false, loading: false,
    pending: [], files: [], drafts: new Map(), filter: "", showArchive: false, confirmStop: false, confirmTakeover: false, note: "", attach: null,
    sendError: "", stuck: true, pollTimer: 0, built: false, seen: null, src: null, altTakeover: false,
  };
  // Sent bubbles live per session (chat.pending is the open one's), so one on its way survives a switch.
  const pendingBy = new Map();
  const pendingOf = (sid) => pendingBy.get(sid) || pendingBy.set(sid, []).get(sid);
  // A session's messages go one at a time and in order: sid -> those not delivered yet, the first on its way.
  const outbox = new Map();
  const sources = new Map();  // id -> {id, items(board), open(key, root), close?()}
  const panels = new Map();  // id -> {id, render(card, root), hint?(card)}: blocks above a session's composer
  const card = (sid = chat.sid) => state.board?.cards.find((c) => c.session_id === sid);

  /* ---------- the panel: the shell's left dock; which thread is open is kept in its state (dock.state.sid) ---------- */
  const dock = SYA.feature({
    id: "chat",
    left: {
      button: t("chat.button"), title: t("chat.dock_title"), label: t("chat.label"),
      icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/></svg>`,
      open: () => matchMedia(WIDE).matches,
      render,
      hide: () => { clearTimeout(chat.pollTimer); if (chat.src) { closeSource(); chat.sid = ""; } },  // reopened: the source opens again
      badge: () => totalUnread() || "",
    },
    route: (sid) => { openChat(sid); queueMicrotask(() => syncHash(chat.sid)); },  // setView() clears the hash right after a route
  }).left;

  /* ---------- unread bookkeeping: sya.chat.seen = {session id: n_replies when last seen} (localStorage may be unavailable) ---------- */
  function loadSeen() {
    if (chat.seen) return;
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(SEEN_KEY)); } catch (e) { /* unavailable */ }
    if (saved && typeof saved === "object") { chat.seen = saved; return; }
    // First visit: nothing is "new" yet. An empty board (the index is still filling) is not the first visit.
    const cards = state.board?.cards || [];
    if (!cards.length) return;
    chat.seen = Object.fromEntries(cards.map((c) => [c.session_id, c.n_replies || 0]));
    saveSeen();
  }
  function saveSeen() { try { localStorage.setItem(SEEN_KEY, JSON.stringify(chat.seen)); } catch (e) { /* not persisted */ } }
  function markSeen(c) {
    if (!chat.seen || (c.n_replies || 0) === chat.seen[c.session_id]) return;
    chat.seen[c.session_id] = c.n_replies || 0; saveSeen();
  }
  // A session met for the first time (started since, or new to this browser): its history so far is not news.
  function adoptNew(cards) {
    if (!chat.seen) return;
    const fresh = cards.filter((c) => !(c.session_id in chat.seen));
    for (const c of fresh) chat.seen[c.session_id] = c.n_replies || 0;
    if (fresh.length) saveSeen();
  }
  const unread = (c) => (chat.seen && c.session_id !== chat.sid ? Math.max(0, (c.n_replies || 0) - (chat.seen[c.session_id] ?? (c.n_replies || 0))) : 0);
  // The archive is folded away in the list, so its replies are not news on the dock button.
  const totalUnread = () => (loadSeen(), adoptNew(state.board?.cards || []), state.board?.cards || [])
    .reduce((n, c) => n + (c.column === "archive" ? 0 : unread(c)), 0)
    + sourceItems().reduce((n, it) => n + (it.key === chat.sid ? 0 : it.unread || 0), 0);
  const badgeText = (n) => (n > 99 ? "99+" : String(n));

  /* ---------- light markdown: everything is escaped first, tags are only ever produced here ---------- */
  const URL_OR_LINK = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<]+[^\s<.,;:!?)\]])/g;
  function inline(raw) {
    return raw.split(/(`[^`\n]+`)/).map((part, i) => {
      if (i % 2) return `<code>${esc(part.slice(1, -1))}</code>`;
      return esc(part)
        .replace(URL_OR_LINK, (_, text, href, bare) => `<a href="${href || bare}" target="_blank" rel="noopener noreferrer">${text || bare}</a>`)
        .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
        .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s.,;:!?)])/g, "$1<i>$2</i>");
    }).join("");
  }
  const LIST_ITEM = /^\s*([-*+]|\d+[.)])\s+/;
  const startsBlock = (l) => /^\s*```/.test(l) || /^#{1,6}\s/.test(l) || LIST_ITEM.test(l) || /^\s*>/.test(l) || /^\s*\|/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  function md(src) {
    const lines = String(src).replace(/\r/g, "").split("\n"), out = [];
    for (let i = 0; i < lines.length;) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        const code = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
        i++;
        out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      } else if (!line.trim()) i++;
      else if (/^\s*([-*_])\s*\1\s*\1[-*_\s]*$/.test(line)) { out.push("<hr>"); i++; }
      else if (/^#{1,6}\s/.test(line)) { out.push(`<p class="md-h">${inline(line.replace(/^#+\s+/, ""))}</p>`); i++; }
      else if (LIST_ITEM.test(line)) {
        const ordered = /^\s*\d/.test(line), items = [];
        for (; i < lines.length && (LIST_ITEM.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length)); i++) {
          if (LIST_ITEM.test(lines[i])) items.push(lines[i].replace(LIST_ITEM, "")); else items[items.length - 1] += " " + lines[i].trim();
        }
        out.push(`<${ordered ? "ol" : "ul"}>${items.map((item) => `<li>${inline(item)}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      } else if (/^\s*\|/.test(line) && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1] || "") && (lines[i + 1] || "").includes("-")) {
        const head = cells(line), rows = [];
        for (i += 2; i < lines.length && /^\s*\|/.test(lines[i]); i++) rows.push(cells(lines[i]));
        out.push(`<div class="tbl"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
          rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      } else if (/^\s*>/.test(line)) {
        const quote = [];
        for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(lines[i].replace(/^\s*>\s?/, ""));
        out.push(`<blockquote>${quote.map(inline).join("<br>")}</blockquote>`);
      } else {
        const para = [];
        for (; i < lines.length && lines[i].trim() && (!para.length || !startsBlock(lines[i])); i++) para.push(lines[i]);
        out.push(`<p>${para.map(inline).join("<br>")}</p>`);
      }
    }
    return out.join("");
  }

  /* ---------- small helpers ---------- */
  const icon = (d) => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`;
  const ICONS = {
    back: icon("M15 5l-7 7 7 7"),
    clip: icon("M20 11.5l-8 8a5 5 0 01-7-7l8.5-8.5a3.3 3.3 0 014.7 4.7L9.5 17.3a1.7 1.7 0 01-2.4-2.4l7.4-7.4"),
    send: icon("M4 12l16-8-6 17-3-7z"),
    down: icon("M6 9l6 6 6-6"),
    x: icon("M6 6l12 12M18 6L6 18"),
    hide: icon("M15 5l-7 7 7 7M20 5v14"),
    arrIn: icon("M17 7L7 17M7 9v8h8"),   // down-left: received
    arrOut: icon("M7 17L17 7M9 7h8v8"),  // up-right: sent
    star: icon("M12 4l2.4 5 5.4.7-4 3.8 1 5.4-4.8-2.7-4.8 2.7 1-5.4-4-3.8 5.4-.7z"),
  };
  const hideBtn = `<button class="ghost dock-x" type="button" aria-label="${t("chat.hide")}" title="${t("chat.hide")}">${ICONS.hide}</button>`;
  const backBtn = `<button class="ghost chat-back" type="button" aria-label="${t("chat.back")}">${ICONS.back}</button>`;
  const isBusy = (c) => !!c && (c.running_reply || ["busy", "shell"].includes(c.live?.status));
  const wired = (c) => c && c.delivery !== undefined;  // A2's fields have landed

  function avatar(c, px) {
    if (typeof agentAvatarSvg === "function") return agentAvatarSvg(c, px);
    const shape = shapeFor(c);
    return `<svg class="av-svg" viewBox="0 0 100 100" width="${px}" height="${px}" aria-hidden="true"><path class="body" d="${SHAPES[shape].d}"/>${face(shape, c.column)}</svg>`;
  }

  function preview(c) {
    const promptAt = Date.parse(c.last_prompt_at) || 0, recapAt = Date.parse(c.recap_at) || 0;
    const mine = () => t("chat.you_said", { text: c.last_prompt });
    if (c.last_prompt && promptAt > recapAt) return mine();
    return c.recap || c.last_assistant_text || (c.last_prompt && mine()) || "";
  }

  /* ---------- skeleton ---------- */
  function build() {
    $("#chat").innerHTML = `<div class="chat-shell" data-open="0">
      <nav class="chat-rail" aria-label="${t("chat.rail")}"></nav>
      <div class="chat-list" aria-label="${t("chat.agents")}">
        <div class="chat-list-head"><div class="dock-title"><h2>${t("chat.label")}</h2>${hideBtn}</div>
          <input class="chat-filter" type="search" placeholder="${t("chat.filter")}" aria-label="${t("chat.filter_label")}"></div>
        <div class="chat-rows"></div>
      </div>
      <section class="chat-pane">
        <div class="chat-blank"><p>${t("chat.blank")}</p><p class="sub">${t("chat.blank_sub")}</p></div>
        <div class="chat-source hidden">
          <header class="chat-head src-head"></header>
          <div class="src-body"></div>
        </div>
        <div class="chat-thread hidden">
          <header class="chat-head"></header>
          <div class="chat-scroll" tabindex="-1">
            <div class="chat-older"></div>
            <div class="msgs"></div><div class="pend"></div>
            <div class="typing hidden" aria-live="polite"><span class="dots"><i></i><i></i><i></i></span>${t("chat.typing")}</div>
            <button class="chat-jump hidden" type="button" aria-label="${t("chat.jump")}">${ICONS.down}</button>
          </div>
          <div class="chat-panels"></div>
          <form class="composer" autocomplete="off">
            <div class="att-list"></div>
            <div class="comp-row">
              <button type="button" class="comp-attach ghost" aria-label="${t("chat.attach")}" title="${t("chat.attach")}">${ICONS.clip}</button>
              <textarea rows="1" placeholder="${t("chat.placeholder")}" aria-label="${t("chat.message_label")}"></textarea>
              <button type="submit" class="comp-send" aria-label="${t("chat.send_button")}" title="${t("chat.send_title")}">${ICONS.send}</button>
            </div>
            <div class="comp-hint"><span class="hint-text" aria-live="polite"></span><button type="button" class="comp-alt link hidden"></button></div>
            <input type="file" class="comp-file" multiple hidden>
          </form>
        </div>
        <div class="chat-drop hidden">${t("chat.drop")}</div>
      </section>
    </div>`;
    wire();
    chat.built = true;
  }
  const q = (sel) => $("#chat").querySelector(sel);

  /* ---------- sources: conversations other features put into the list and the rail ---------- */
  const sourceOf = (sid) => (sid.includes(":") ? sources.get(sid.slice(0, sid.indexOf(":"))) : null);
  const sourceKey = (sid) => sid.slice(sid.indexOf(":") + 1);
  function sourceItems() {
    const out = [];
    for (const src of sources.values()) {
      let items = [];
      try { items = src.items(state.board) || []; } catch (e) { items = []; }  // one broken source must not break the chat
      for (const it of items) out.push({ ...it, key: `${src.id}:${it.key}`, unread: it.unread | 0, alert: !!it.alert });
    }
    return out;
  }

  /* ---------- list ---------- */
  function listCards() {
    const needle = chat.filter.trim().toLowerCase();
    const all = visibleCards().filter((c) => !needle || [chatTitle(c), c.title, c.project, c.branch, c.worktree, preview(c), ...c.jira.map((j) => j.key)]
      .join(" ").toLowerCase().includes(needle));
    const supId = state.board?.supervisor_id;
    const rest = all.filter((c) => c.session_id !== supId);
    const live = ORDER.flatMap((col) => rest.filter((c) => c.column === col));
    const archive = rest.filter((c) => c.column === "archive");
    // The supervisor is pinned first whatever the project filter says; only the search box can drop it.
    const found = supId ? state.board.cards.find((c) => c.session_id === supId) : null;
    const sup = found && (!needle || all.includes(found)) ? found : null;
    const pinned = sourceItems().filter((it) => !needle || `${it.title} ${it.short || ""}`.toLowerCase().includes(needle));
    return { sup, noSup: !supId && !needle, pinned, live, archive, searching: !!needle };
  }
  const SUP_CMD = "uv run sya supervisor";
  const supHintHtml = () => `<div class="chat-row sup-hint"><span class="av av-sup">${ICONS.star}</span><span class="row-main">
      <span class="row-top"><b class="row-title">${t("chat.sup.not_running")}</b></span>
      <span class="row-sub">${t("chat.sup.hint")}</span>
      <span class="row-cmd"><code>${SUP_CMD}</code></span></span></div>`;
  const countBadge = (n, cls = "unread") => (n ? `<em class="${cls}" title="${t("chat.unread", { n })}">${badgeText(n)}</em>` : "");
  const alertDot = `<i class="alert-dot" title="${t("chat.needs_you")}"></i>`;
  function rowHtml(c) {
    const text = preview(c), n = unread(c);
    const sup = c.session_id === state.board?.supervisor_id;
    return `<button class="chat-row c-${c.column}${sup ? " is-sup" : ""}${c.session_id === chat.sid ? " on" : ""}${c.done ? " is-done" : ""}" data-sid="${esc(c.session_id)}">
      <span class="av av-${c.column}">${avatar(c, 40)}${c.column === "needs_you" ? alertDot : ""}${sup ? `<i class="sup-mark" title="${t("chat.supervisor")}">${ICONS.star}</i>` : ""}</span>
      <span class="row-main">
        <span class="row-top"><b class="row-title">${esc(chatTitle(c))}</b><time>${esc(ago(c.last_activity))}</time></span>
        <span class="row-sub">${esc([c.project, c.worktree || c.branch].filter(Boolean).join(" / "))}</span>
        <span class="row-prev">${esc(text)}${countBadge(n)}</span>
      </span></button>`;
  }
  // A source item: the avatar is the source's own HTML, the rest is plain text.
  function sourceRowHtml(it) {
    return `<button class="chat-row c-source${it.key === chat.sid ? " on" : ""}" data-sid="${esc(it.key)}">
      <span class="av">${it.avatar || ""}${it.alert ? alertDot : ""}</span>
      <span class="row-main"><span class="row-top"><b class="row-title">${esc(it.title)}</b>${countBadge(it.unread)}</span></span></button>`;
  }
  function renderList() {
    const { sup, noSup, pinned, live, archive, searching } = listCards();
    const showArchive = chat.showArchive || searching;
    const toggle = archive.length && !searching
      ? `<button class="ghost chat-arch" type="button">${chat.showArchive ? t("chat.archive.hide") : t("chat.archive.show", { n: archive.length })}</button>` : "";
    q(".chat-rows").innerHTML = (sup ? rowHtml(sup) : noSup ? supHintHtml() : "") + pinned.map(sourceRowHtml).join("")
      + (live.map(rowHtml).join("") || (archive.length && !searching || pinned.length ? "" : `<p class="empty">${t("chat.nobody")}</p>`))
      + (showArchive ? archive.map(rowHtml).join("") : "") + toggle;
  }

  /* ---------- rail: the same conversations as a narrow strip next to an open thread ---------- */
  const shortName = (title) => { const s = String(title || "").trim(); return s.length > 8 ? `${s.slice(0, 7)}…` : s; };
  function railItems() {
    const cards = visibleCards(), supId = state.board?.supervisor_id;
    const sup = supId ? state.board?.cards.find((c) => c.session_id === supId) : null;
    const item = (c) => ({ key: c.session_id, title: chatTitle(c), short: shortName(chatTitle(c)), column: c.column, unread: unread(c), alert: c.column === "needs_you", card: c });
    const list = [
      ...(sup ? [item(sup)] : []),
      ...sourceItems().map((it) => ({ ...it, short: shortName(it.short || it.title), column: "source" })),
      ...ORDER.flatMap((col) => cards.filter((c) => c.column === col && c.session_id !== supId).map(item)),
    ];
    const items = list.slice(0, RAIL_MAX), open = list.findIndex((it) => it.key === chat.sid);
    if (open >= RAIL_MAX) items[RAIL_MAX - 1] = list[open];  // the open chat never falls off the strip
    else if (open < 0 && chat.sid) {  // e.g. an archived session that is open
      const c = card();
      if (c) items.push(item(c));
    }
    return items;
  }
  function railItemHtml(it) {
    const av = it.card ? avatar(it.card, 34) : it.avatar || "";
    return `<button class="rail-item c-${it.column}${it.key === chat.sid ? " on" : ""}" data-sid="${esc(it.key)}" title="${esc(it.title)}" aria-label="${esc(it.title)}"${it.key === chat.sid ? ` aria-current="true"` : ""}>
      <span class="av av-${it.column}">${av}${it.alert ? alertDot : ""}${countBadge(it.unread, "rail-badge")}</span>
      <span class="rail-name">${esc(it.short)}</span></button>`;
  }
  function renderRail() {
    const rail = q(".chat-rail"), top = rail.scrollTop;
    rail.innerHTML = railItems().map(railItemHtml).join("");
    rail.scrollTop = top;
    const on = rail.querySelector(".on");  // keep the open chat in view without scrolling anything but the rail
    if (on && (on.offsetTop < rail.scrollTop || on.offsetTop + on.offsetHeight > rail.scrollTop + rail.clientHeight)) {
      rail.scrollTop = on.offsetTop - (rail.clientHeight - on.offsetHeight) / 2;
    }
  }
  function stepRail(dir) {
    const items = railItems(), i = items.findIndex((it) => it.key === chat.sid), next = items[i + dir];
    if (i >= 0 && next) openChat(next.key);
  }

  /* ---------- header ---------- */
  function renderHead() {
    const c = card(), head = q(".chat-thread .chat-head");
    if (!c) { head.innerHTML = `${backBtn}<h2>${t("chat.head.not_found")}</h2>${hideBtn}`; return; }
    const l = c.live || {}, route = c.delivery?.route;
    const since = l.status_since_ms ? ` · ${ago(new Date(l.status_since_ms).toISOString())}` : "";
    let actions = "";
    if (chat.confirmStop) {
      actions = `<div class="confirm" role="alertdialog" aria-label="${t("chat.stop.label")}">
        <p>${t("chat.stop.note")} <code>claude --resume</code>.</p>
        <button class="act danger" data-act="stop-yes" type="button">${t("chat.stop.yes")}</button><button class="act" data-act="stop-no" type="button">${t("chat.stop.no")}</button></div>`;
    } else {
      if (route === "vscode") actions += `<button class="act" data-act="focus" type="button">${t("chat.act.focus")}</button>`;
      if (l.job_id) actions += `<button class="act" data-act="attach" type="button" title="${esc(t("chat.act.attach_title", { command: `claude attach ${l.job_id}` }))}">${t("chat.act.attach")}</button>`;
      if (wired(c)) actions += `<button class="act" data-act="done" type="button" title="${c.done ? t("chat.act.undone_title") : t("chat.act.done_title")}">${c.done ? t("chat.act.undone") : t("chat.act.done")}</button>`;
      if (wired(c) && c.live && can(c, "stop")) actions += `<button class="act" data-act="stop" type="button">${t("chat.act.stop")}</button>`;
      // launch.js answers any [data-fork]: a new session from a copy of this one, after a confirm popover
      if (c.can_fork) actions += `<button class="act" data-fork="${esc(c.session_id)}" type="button" title="${t("chat.act.fork_title")}">${t("chat.act.fork")}</button>`;
      actions += `<button class="act" data-act="details" type="button">${t("chat.act.details")}</button>`;
    }
    head.innerHTML = `${backBtn}
      <span class="av av-${c.column}">${avatar(c, 36)}</span>
      <div class="head-main"><h2 title="${esc(c.title)}">${esc(chatTitle(c))}</h2>
        <div class="status-line s-${c.column}">${programTag(c)}<span class="dot"></span>${esc(supStopped(c) ? t("chat.sup.stopped_line") : liveLine(c))}${esc(since)}${c.done ? ` · <span class="done-tag">${t("chat.done_tag")}</span>` : ""}</div>
      </div>${hideBtn}<div class="head-actions${chat.confirmStop ? " asking" : ""}">${actions}</div>
      ${chat.note ? `<p class="head-note" role="alert">${esc(chat.note)}</p>` : ""}
      ${l.needs ? `<p class="head-note calm">${t("chat.needs")} ${esc(l.needs)}</p>` : ""}
      ${chat.attach ? `<p class="head-note calm">${chat.attach.copied ? t("chat.attach_cmd.copied") : t("chat.attach_cmd.open")} <code>${esc(chat.attach.cmd)}</code></p>` : ""}`;
  }

  /* ---------- messages ---------- */
  function toolsHtml(m) {
    const n = m.calls.length, errors = m.calls.filter((c) => c.error).length;
    const names = [...new Set(m.calls.map((c) => c.name.replace(/^mcp__([^_]+(?:_[^_]+)*?)__.*/, "$1")))].slice(0, 4).join(", ");
    const title = n === 1 ? m.calls[0].summary : t("chat.tools.title", { n, names });
    return `<details class="tools"><summary><span class="chev"></span><span class="t-sum">${esc(title)}</span>${errors ? `<em class="t-err">${t("chat.tools.errors", { n: errors })}</em>` : ""}</summary>
      <ul>${m.calls.map((c) => `<li${c.error ? ` class="err"` : ""}>${esc(c.summary)}${c.error ? ` — ${t("chat.tools.error")}` : ""}</li>`).join("")}</ul></details>`;
  }
  function questionHtml(m) {
    const items = m.items.map((it) => `<div class="q-item">${it.header ? `<span class="q-head">${esc(it.header)}</span>` : ""}<p class="q-text">${esc(it.question)}</p>
      <div class="q-opts">${it.options.map((o) => `<button type="button" class="q-opt" data-opt="${esc(o.label)}" title="${esc(o.description)}"${m.answer != null ? " disabled" : ""}>${esc(o.label)}</button>`).join("")}</div></div>`).join("");
    return `<div class="q-card"><span class="q-tag">${t("chat.question.tag")}</span>${items}<p class="q-answer ${m.answer != null ? "got" : ""}">${m.answer != null ? esc(t("chat.question.answered", { answer: m.answer })) : t("chat.question.none")}</p></div>`;
  }
  // Sent to another session: the summary on one line, the full text expands. ok === false: not delivered; null: not confirmed yet.
  function peerOutHtml(m) {
    const mod = m.ok === false ? " failed" : m.ok == null ? " unsure" : "";
    const tag = m.ok === false ? `<em class="po-tag">${t("chat.peer_out.failed")}</em>` : m.ok == null ? `<em class="po-tag">${t("chat.peer_out.unsure")}</em>` : "";
    return `<details class="peer-out${mod}"><summary><span class="chev"></span><span class="po-to">${ICONS.arrOut}<b>@${esc(peerName(m.to))}</b></span>
      <span class="po-sum">${esc(m.summary || m.text)}</span>${tag}</summary>
      <div class="md">${md(m.text)}</div><time>${esc(clock(m.at))}</time></details>`;
  }
  function bodyHtml(m, sid) {
    switch (m.kind) {
      case "you": {
        const imgs = (m.images || []).map((im) => { const src = esc(chatApi.imageSrc(sid, im.url));
          return `<a href="${src}" target="_blank" rel="noopener"><img class="pic" loading="lazy" alt="${t("chat.image")}" src="${src}"></a>`; }).join("");
        return `<div class="bub">${imgs ? `<div class="pics">${imgs}</div>` : ""}${m.text ? `<div class="plain">${esc(m.text)}</div>` : ""}<time>${esc(clock(m.at))}</time></div>`;
      }
      case "agent": return `<div class="bub md">${md(m.text)}<time>${esc(clock(m.at))}</time></div>`;
      case "tools": return toolsHtml(m);
      case "question": return questionHtml(m);
      case "plan": return `<details class="plan"><summary><span class="chev"></span>${t("chat.plan")}${m.path ? ` · ${esc(m.path.split("/").slice(-2).join("/"))}` : ""}</summary><div class="md">${md(m.text)}</div></details>`;
      case "recap": return `<div class="recap-bub"><span class="r-tag">${t("chat.recap")}</span><p>${esc(m.text)}</p><time>${esc(clock(m.at))}</time></div>`;
      case "peer_in":
        if (fromHere(m)) return `<div class="bub"><div class="plain">${esc(m.text)}</div><time>${esc(clock(m.at))}</time></div>`;  // sent from here
        return `<div class="peer-in"><div class="peer-from">${ICONS.arrIn}<span>${t("chat.peer_in.from")} <b>@${esc(peerName(m.from))}</b></span></div><div class="md">${md(m.text)}</div><time>${esc(clock(m.at))}</time></div>`;
      case "peer_out": return peerOutHtml(m);
      case "compact": return `<div class="compact"><span>${t("chat.compact")}</span></div>`;  // m.text is the provider's, for agents
      default: return "";
    }
  }
  function makeEl(m, old) {
    const el = document.createElement("div");
    el.className = `msg k-${fromHere(m) ? "you" : m.kind}`;
    el.dataset.mid = m.id; el.dataset.at = m.at;
    el.innerHTML = bodyHtml(m, chat.sid);
    const wasOpen = old?.querySelector("details")?.open;
    if (wasOpen) el.querySelector("details").open = true;
    return el;
  }
  function upsert(m) {
    const prev = chat.els.get(m.id), sig = JSON.stringify(m);
    if (prev && prev.sig === sig) return false;
    const el = makeEl(m, prev?.el), box = q(".msgs");
    if (prev) prev.el.replaceWith(el);
    else {
      const nextId = chat.ids.find((id) => id > m.id);
      const at = nextId === undefined ? null : chat.els.get(nextId).el;
      box.insertBefore(el, at);
      chat.ids.push(m.id); chat.ids.sort((a, b) => a - b);
    }
    chat.els.set(m.id, { sig, el });
    return true;
  }
  function layoutDays() {
    const box = q(".msgs");
    box.querySelectorAll(".day").forEach((d) => d.remove());
    let last = "";
    for (const el of [...box.children]) {
      const day = el.dataset.at ? new Date(el.dataset.at).toDateString() : last;
      if (day !== last) {
        last = day;
        const d = document.createElement("div"); d.className = "day"; d.innerHTML = `<span>${esc(dayLabel(el.dataset.at))}</span>`;
        box.insertBefore(d, el);
      }
    }
  }
  // The session's own copy of a sent message; the courier's copy can land before the send request returns.
  const isEcho = (m, p) => p.text && m.text.includes(p.text) && m.at + 10000 >= p.t;  // 10 s clock skew
  function renderPending() {
    const now = Date.now();
    // on its way: stays; delivered: waits for the session's own copy, at most PENDING_TTL_MS
    chat.pending = chat.pending.filter((p) => !p.done || now - p.done < PENDING_TTL_MS);
    pendingBy.set(chat.sid, chat.pending);
    q(".pend").innerHTML = chat.pending.map((p) => `<div class="msg k-you pending${p.done ? "" : " on-way"}"><div class="bub">
      ${p.files.length ? `<div class="sent-files">${p.files.map((f) => `<span class="file-chip">${esc(f)}</span>`).join("")}</div>` : ""}
      ${p.text ? `<div class="plain">${esc(p.text)}</div>` : ""}<time>${p.done ? t("chat.sent") : t("chat.sending")}</time></div></div>`).join("");
  }
  function renderTyping() { q(".typing").classList.toggle("hidden", !isBusy(card())); }

  const scroller = () => q(".chat-scroll");
  const atBottom = () => { const s = scroller(); return s.scrollHeight - s.scrollTop - s.clientHeight < 90; };
  const toBottom = () => { const s = scroller(); s.scrollTop = s.scrollHeight; };

  // Merge a page of messages into the DOM; keep the reading position the way a messenger does.
  function apply(msgs, { prepend = false } = {}) {
    const s = scroller(), stick = atBottom(), h0 = s.scrollHeight, top0 = s.scrollTop;
    let changed = false, appended = false;
    for (const m of msgs) {
      const isNew = !chat.els.has(m.id);
      if (upsert(m)) changed = true;
      if (isNew && !prepend) appended = true;
      if (m.kind === "you" || fromHere(m)) {
        const echo = { text: m.text, at: Date.parse(m.at) };
        const i = chat.pending.findIndex((p) => isEcho(echo, p));
        if (i >= 0) chat.pending.splice(i, 1);
      }
    }
    renderPending();  // also drops a bubble whose time ran out
    if (!changed) return;
    layoutDays();
    if (prepend) s.scrollTop = top0 + (s.scrollHeight - h0);
    else if (stick) { toBottom(); chat.stuck = true; }
    else if (appended) q(".chat-jump").classList.remove("hidden");
  }

  /* ---------- loading and polling ---------- */
  function resetThread() {
    chat.ids = []; chat.els.clear(); chat.before = null; chat.after = 0; chat.hasMore = false; chat.loaded = false;
    chat.loading = false; chat.pending = pendingOf(chat.sid); chat.files = []; chat.confirmStop = false; chat.confirmTakeover = false; chat.altTakeover = false; chat.note = ""; chat.attach = null;
    q(".msgs").innerHTML = ""; q(".pend").innerHTML = ""; q(".chat-older").textContent = "";
    q(".chat-jump").classList.add("hidden");
    chat.stuck = true;
  }
  async function loadFirst() {
    const token = chat.token, page = await chatApi.thread(chat.sid, { limit: PAGE }).catch(() => null);
    if (token !== chat.token) return;
    const older = q(".chat-older");
    if (!page) { older.textContent = t("chat.not_indexed"); return; }
    chat.loaded = true; chat.before = page.before; chat.after = page.after; chat.hasMore = page.has_more;
    older.textContent = "";
    apply(page.messages);
    toBottom();
    fillIfShort();
  }
  async function loadOlder() {
    if (chat.loading || !chat.hasMore || !chat.loaded) return;
    chat.loading = true;
    const token = chat.token;
    q(".chat-older").textContent = t("chat.loading_older");
    const page = await chatApi.thread(chat.sid, { limit: PAGE, before: chat.before }).catch(() => null);
    if (token !== chat.token) return;
    chat.loading = false;
    q(".chat-older").textContent = "";
    if (!page) return;
    chat.before = page.before; chat.hasMore = page.has_more;
    apply(page.messages, { prepend: true });
    fillIfShort();
  }
  function fillIfShort() {  // a short first page can't scroll, so it could never ask for more
    const s = scroller();
    if (chat.hasMore && s.scrollHeight <= s.clientHeight + 40) loadOlder();
  }
  async function poll() {
    clearTimeout(chat.pollTimer);
    if (!dock.open || !chat.sid || chat.src) return;
    if (!document.hidden && chat.built) {
      const token = chat.token;
      if (!chat.loaded) await loadFirst();
      else {
        const page = await chatApi.thread(chat.sid, { after: chat.after }).catch(() => null);
        if (token === chat.token && page) { chat.after = page.after; apply(page.messages); }
      }
      if (token === chat.token) { renderPending(); renderTyping(); }
    }
    chat.pollTimer = setTimeout(poll, POLL_MS);
  }
  const pollSoon = (ms = 600) => { clearTimeout(chat.pollTimer); chat.pollTimer = setTimeout(poll, ms); };

  /* ---------- composer ---------- */
  function renderAttachments() {
    q(".att-list").innerHTML = chat.files.map((f, i) => `<span class="att ${f.status}" title="${esc(f.error || f.name)}"><span class="att-name">${esc(f.name)}</span>${
      f.status === "uploading" ? `<em>${t("chat.file.uploading")}</em>` : f.status === "error" ? `<em>${esc(f.error || t("chat.file.failed"))}</em>` : ""
    }<button type="button" data-rm="${i}" aria-label="${esc(t("chat.file.remove", { name: f.name }))}">${ICONS.x}</button></span>`).join("");
    syncComposer();
  }
  function syncComposer() {
    const c = card(), ta = q(".composer textarea"), send = q(".comp-send"), hint = q(".comp-hint"), alt = q(".comp-alt");
    if (!c) return;
    const d = c.delivery, blocked = d && d.can_send === false, replying = !!c.running_reply;
    const uploading = chat.files.some((f) => f.status === "uploading");
    const noFiles = !can(c, "files"), textOnly = peerOnly(c) || noFiles;  // the courier carries text, not files
    ta.disabled = !!blocked && !replying;  // the next message can be written while a reply is on its way
    ta.placeholder = blocked ? d.reason || t("chat.blocked") : t("chat.placeholder");
    const attach = q(".comp-attach");
    attach.disabled = !!blocked || textOnly;
    attach.title = noFiles ? NO_FILES : textOnly ? t("chat.courier.no_files") : t("chat.attach");
    send.disabled = !!blocked || replying || uploading;
    const note = blocked ? panelHint(c) : "";  // e.g. «answer above» instead of «answer in the terminal»
    if (note) ta.placeholder = note;
    let text = note || (d ? d.reason || "" : t("chat.hint"));
    if (!blocked && d?.route) text = `${d.reason || ROUTE_WORD[d.route] || ""}`;
    if (!blocked && d?.route === "resume" && supStopped(c)) text = SUP_RESUME_NOTE;
    if (!blocked && peerOnly(c)) text = PEER_NOTE;
    if (replying) text = t("chat.replying");
    if (outbox.has(chat.sid)) text = t("chat.sending");
    // Takeover closes the session in its terminal, so the first press only asks; the second one sends.
    if (chat.confirmTakeover) text = t("chat.confirm_takeover");
    q(".hint-text").textContent = chat.sendError || text;
    hint.classList.toggle("bad", !!chat.sendError || chat.confirmTakeover);
    send.classList.toggle("confirm", chat.confirmTakeover);
    const offerAlt = !blocked && !replying && d?.route === "peer" && d.alt === "takeover";
    alt.classList.toggle("hidden", !offerAlt);
    alt.textContent = chat.altTakeover ? t("chat.alt.cancel") : t("chat.alt.take");
  }
  /* ---------- panels: blocks other features put above a session's composer (SYA.chat.addPanel) ---------- */
  function renderPanels(c) {
    const box = q(".chat-panels");
    for (const p of panels.values()) {
      let root = box.querySelector(`:scope > [data-panel="${p.id}"]`);
      if (!root) { root = document.createElement("div"); root.dataset.panel = p.id; box.append(root); }
      try { p.render(c || null, root); } catch (e) { root.textContent = ""; }  // a broken panel must not break the chat
    }
  }
  function panelHint(c) {
    for (const p of panels.values()) {
      try { const h = p.hint?.(c); if (h) return String(h); } catch (e) { /* the panel's own problem */ }
    }
    return "";
  }
  // Peer route and no takeover chosen: only text can go.
  const peerOnly = (c) => c?.delivery?.route === "peer" && !chat.altTakeover;
  function autosize() {
    const ta = q(".composer textarea");
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 180)}px`;
  }
  async function addFiles(list) {
    const sid = chat.sid;
    if (!can(card(), "files")) { chat.sendError = NO_FILES; return syncComposer(); }
    if (peerOnly(card())) { chat.sendError = t("chat.courier.take_over"); return syncComposer(); }
    for (const file of list) {
      const item = { name: file.name || t("chat.file.default"), status: "uploading", path: "", error: "" };
      chat.files.push(item);
      renderAttachments();
      try {
        const r = await chatApi.upload(sid, file);
        if (r.ok && r.data?.path) Object.assign(item, { status: "ready", path: r.data.path, name: r.data.name || item.name });
        else Object.assign(item, { status: "error", error: r.data?.error || t("chat.file.error", { status: r.status }) });
      } catch (e) { Object.assign(item, { status: "error", error: t("chat.file.offline") }); }
      if (sid === chat.sid) renderAttachments();
    }
  }
  // The message shows in the feed at once and the input is free for the next one; the request runs behind it.
  function submit() {
    const c = card(), ta = q(".composer textarea");
    const text = ta.value.trim(), ready = chat.files.filter((f) => f.status === "ready");
    if (!c || (!text && !ready.length) || chat.files.some((f) => f.status === "uploading")) return;
    if (c.delivery?.can_send === false || c.running_reply) return;
    if (peerOnly(c) && ready.length) { chat.sendError = t("chat.courier.text_only"); return syncComposer(); }
    const takeover = c.delivery?.route === "takeover" || chat.altTakeover;
    if (takeover && !chat.confirmTakeover) { chat.confirmTakeover = true; return syncComposer(); }
    chat.sendError = ""; chat.confirmTakeover = false; chat.altTakeover = false;
    const p = { text, files: ready.map((f) => f.name), uploads: ready, takeover, t: Date.now(), done: 0 };
    chat.pending.push(p);
    ta.value = ""; chat.drafts.delete(chat.sid); chat.files = chat.files.filter((f) => f.status === "error");
    autosize(); renderAttachments(); renderPending(); toBottom();
    const queue = outbox.get(chat.sid);
    if (queue) queue.push(p);
    else drain(chat.sid, [p]);
  }
  async function drain(sid, queue) {
    outbox.set(sid, queue);
    if (sid === chat.sid) syncComposer();
    while (queue.length) {
      const p = queue[0];
      let r;
      try { r = await chatApi.send(sid, p.text, p.uploads.map((f) => f.path), p.takeover ? { takeover: true } : {}); }
      catch (e) { r = { ok: false, status: 0, data: { error: t("chat.send.offline") } }; }
      if (!r.ok || r.data?.ok === false) { giveBack(sid, queue.splice(0), r); break; }
      queue.shift();
      p.done = Date.now();
      if (sid === chat.sid) { renderPending(); pollSoon(); }
      refresh();
    }
    outbox.delete(sid);
    if (sid === chat.sid) syncComposer();
  }
  // A message that did not go, and the ones queued behind it, go back to the input (before what was typed since),
  // so none is lost and none arrives out of order.
  function giveBack(sid, items, r) {
    const list = pendingOf(sid);
    for (const p of items) { const i = list.indexOf(p); if (i >= 0) list.splice(i, 1); }
    const joined = (...parts) => parts.filter(Boolean).join("\n\n");
    const text = joined(...items.map((p) => p.text));
    const error = r.data?.error || (r.status === 409 ? t("chat.send.busy")
      : r.status ? t("chat.send.failed", { status: r.status }) : t("chat.send.failed_network"));
    if (sid !== chat.sid) { chat.drafts.set(sid, joined(text, chat.drafts.get(sid))); return; }
    const ta = q(".composer textarea");
    ta.value = joined(text, ta.value);
    chat.files.unshift(...items.flatMap((p) => p.uploads));
    chat.sendError = error;
    autosize(); renderAttachments(); renderPending();
  }

  /* ---------- header actions ---------- */
  async function act(name) {
    const c = card();
    if (!c) return;
    if (name === "details") return openDrawer(c.session_id);
    if (name === "stop") { chat.confirmStop = true; return renderHead(); }
    if (name === "stop-no") { chat.confirmStop = false; return renderHead(); }
    if (name === "attach") {  // a background session's terminal is `claude attach <job id>`
      const job = c.live?.job_id, sid = chat.sid;
      if (!job) return;
      const cmd = `claude attach ${job}`;
      let copied = true;
      try { await navigator.clipboard.writeText(cmd); } catch (e) { copied = false; }
      if (chat.sid !== sid) return;  // another chat opened meanwhile
      chat.attach = { cmd, copied };
      return renderHead();
    }
    chat.note = "";
    let r;
    try {
      if (name === "done") r = await chatApi.act(c.session_id, "done", { done: !c.done });
      else if (name === "stop-yes") { chat.confirmStop = false; r = await chatApi.act(c.session_id, "stop"); }
      else if (name === "focus") r = await chatApi.act(c.session_id, "focus");
    } catch (e) { r = { ok: false, data: { error: t("chat.offline") } }; }
    if (!r?.ok || r.data?.ok === false) chat.note = r?.data?.error || t("chat.failed");
    renderHead();
    if (r?.ok) refresh();
  }

  /* ---------- events ---------- */
  function wire() {
    const root = $("#chat");
    root.querySelector(".chat-filter").oninput = (e) => { chat.filter = e.target.value; renderList(); };
    root.querySelector(".chat-rows").addEventListener("click", (e) => {
      if (e.target.closest(".chat-arch")) { chat.showArchive = !chat.showArchive; return renderList(); }
      const row = e.target.closest("[data-sid]");
      if (row) openChat(row.dataset.sid);
    });
    root.querySelector(".chat-rail").addEventListener("click", (e) => {
      const item = e.target.closest("[data-sid]");
      if (item) openChat(item.dataset.sid);
    });
    root.querySelector(".src-head").addEventListener("click", (e) => { if (e.target.closest(".chat-back")) openChat(""); });
    root.querySelector(".chat-thread .chat-head").addEventListener("click", (e) => {
      if (e.target.closest(".chat-back")) return openChat("");
      const b = e.target.closest("[data-act]");
      if (b) act(b.dataset.act);
    });
    const s = scroller();
    // Only the reader moving away from the bottom un-sticks the view: layout shifts and scroll anchoring also fire scroll events.
    let touched = 0;
    for (const ev of ["wheel", "touchstart", "touchmove", "pointerdown", "keydown"]) s.addEventListener(ev, () => { touched = Date.now(); }, { passive: true });
    s.addEventListener("scroll", () => {
      if (atBottom()) chat.stuck = true;
      else if (Date.now() - touched < 2000) chat.stuck = false;
      if (s.scrollTop < 140) loadOlder();
      if (chat.stuck) q(".chat-jump").classList.add("hidden");
    }, { passive: true });
    // Images and web fonts change heights after the scroll position was set: stay at the bottom if we were there.
    const follow = new ResizeObserver(() => { if (chat.stuck) toBottom(); });
    for (const part of [".msgs", ".pend", ".typing"]) follow.observe(q(part));
    q(".chat-jump").onclick = () => { toBottom(); q(".chat-jump").classList.add("hidden"); };
    s.addEventListener("click", (e) => {
      const opt = e.target.closest(".q-opt");
      if (!opt) return;
      const ta = q(".composer textarea");
      ta.value = opt.dataset.opt; ta.focus(); autosize(); chat.drafts.set(chat.sid, ta.value);
    });

    const form = q(".composer"), ta = form.querySelector("textarea"), input = form.querySelector(".comp-file");
    form.onsubmit = (e) => { e.preventDefault(); submit(); };
    ta.addEventListener("input", () => { autosize(); chat.drafts.set(chat.sid, ta.value); if (chat.sendError) { chat.sendError = ""; syncComposer(); } });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    });
    ta.addEventListener("paste", (e) => {
      const files = [...(e.clipboardData?.files || [])];
      if (files.length) { e.preventDefault(); addFiles(files); }
    });
    form.querySelector(".comp-attach").onclick = () => input.click();
    form.querySelector(".comp-alt").onclick = () => {
      chat.altTakeover = !chat.altTakeover; chat.confirmTakeover = chat.altTakeover; chat.sendError = "";
      syncComposer();
    };
    input.onchange = () => { addFiles([...input.files]); input.value = ""; };
    form.querySelector(".att-list").addEventListener("click", (e) => {
      const b = e.target.closest("[data-rm]");
      if (b) { chat.files.splice(Number(b.dataset.rm), 1); renderAttachments(); }
    });

    const pane = root.querySelector(".chat-pane"), drop = root.querySelector(".chat-drop");
    let depth = 0;
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    pane.addEventListener("dragenter", (e) => { if (chat.sid && hasFiles(e)) { depth++; drop.classList.remove("hidden"); } });
    pane.addEventListener("dragover", (e) => { if (chat.sid && hasFiles(e)) e.preventDefault(); });
    pane.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; drop.classList.add("hidden"); } });
    pane.addEventListener("drop", (e) => {
      depth = 0; drop.classList.add("hidden");
      if (!chat.sid || !hasFiles(e)) return;
      e.preventDefault(); addFiles([...e.dataTransfer.files]);
    });
  }

  /* ---------- the panel ---------- */
  // Leaving a source conversation (switching, going back, closing the dock) tells the source.
  function closeSource() {
    const src = chat.src;
    chat.src = null;
    if (src) { try { src.close?.(); } catch (e) { /* the source's own problem */ } }
  }
  function openThread(sid) {
    clearTimeout(chat.pollTimer);
    if (chat.sid && !chat.src) chat.drafts.set(chat.sid, q(".composer textarea").value);
    closeSource();
    chat.token++; chat.sid = sid;
    const src = sid ? sourceOf(sid) : null;
    const thread = q(".chat-thread"), ta = q(".composer textarea"), box = q(".chat-source");
    thread.classList.toggle("hidden", !sid || !!src);
    box.classList.toggle("hidden", !src);
    q(".chat-blank").classList.toggle("hidden", !!sid);
    resetThread();
    q(".chat-panels").innerHTML = "";  // panels draw afresh for the new session
    chat.sendError = "";
    if (src) {
      chat.src = src;
      const body = q(".src-body");
      body.innerHTML = "";
      try { src.open(sourceKey(sid), body); } catch (e) { body.textContent = t("chat.source_failed"); }
      return;
    }
    ta.value = sid ? chat.drafts.get(sid) || "" : "";
    renderAttachments(); autosize();
    if (sid) {
      const token = chat.token;
      loadFirst().then(() => { if (token === chat.token) chat.pollTimer = setTimeout(poll, POLL_MS); });
    }
  }

  function renderSourceHead() {
    const it = sourceItems().find((x) => x.key === chat.sid);
    q(".src-head").innerHTML = `${backBtn}
      <span class="av">${it?.avatar || ""}</span><div class="head-main"><h2 title="${esc(it?.title || "")}">${esc(it?.title || t("chat.chat"))}</h2></div>${hideBtn}`;
  }

  function render() {
    if (!dock.open || !state.board) return;
    if (!chat.built || !$("#chat").firstElementChild) build();
    loadSeen();
    let sid = dock.state.sid || "";
    if (sid.includes(":") && !sourceOf(sid)) sid = dock.state.sid = "";  // a source that is not registered (any more)
    if (sid !== chat.sid) openThread(sid);
    q(".chat-shell").dataset.open = sid ? "1" : "0";
    const c = card();
    if (c && sid) markSeen(c);
    renderList();
    renderRail();
    if (chat.src) renderSourceHead();
    else if (sid) { renderHead(); renderPanels(c); syncComposer(); renderTyping(); }
  }

  // The address keeps the open chat (#chat/<id>, #chat/<source>:<key>) unless another view already owns the hash.
  function syncHash(sid) {
    const h = location.hash;
    if (h && !h.startsWith("#chat")) return;
    const next = sid ? `#chat/${encodeURIComponent(sid).replace(/%3A/gi, ":")}` : location.pathname + location.search;
    if (sid ? h !== next : h) history.replaceState(null, "", next);
  }

  // A thread, or the list when sid is "". Opens the panel; on a narrow screen it would sit on top of the drawer.
  function openChat(sid) {
    dock.state.sid = sid;
    if (!matchMedia(WIDE).matches && state.openId) closeDrawer();
    dock.setOpen(true);
    syncHash(sid);
    if (sid && !sourceOf(sid)) q(".composer textarea")?.focus({ preventScroll: true });
  }

  /* ---------- the page-level API for other features: SYA.chat ---------- */
  // addSource({id, items(board) -> [{key, title, short, avatar (html), unread, alert}], open(key, root), close?()}):
  // items are pinned after the supervisor in the list and the rail; open() draws the conversation into `root`.
  SYA.chat = {
    addSource(src) {
      if (!src || !/^[a-z][a-z0-9-]*$/.test(src.id || "") || typeof src.items !== "function" || typeof src.open !== "function") {
        throw new Error("SYA.chat.addSource: needs id (a-z0-9-), items(board) and open(key, root)");
      }
      if (sources.has(src.id)) throw new Error(`SYA.chat.addSource: there is a source "${src.id}" already`);
      sources.set(src.id, src);
      SYA.chat.refresh();
    },
    // addPanel({id, render(card, root), hint?(card)}): a block above the composer of the open session, drawn on
    // every tick into its own `root` (card is null when the session is gone); hint() replaces the composer's
    // note while the session cannot take text.
    addPanel(panel) {
      if (!panel || !/^[a-z][a-z0-9-]*$/.test(panel.id || "") || typeof panel.render !== "function") {
        throw new Error("SYA.chat.addPanel: needs id (a-z0-9-) and render(card, root)");
      }
      if (panels.has(panel.id)) throw new Error(`SYA.chat.addPanel: there is a panel "${panel.id}" already`);
      panels.set(panel.id, panel);
      SYA.chat.refresh();
    },
    refresh() { renderDocks(); },  // the list, the rail and the dock button's badge
    open: openChat,
    sourceItems,  // every source's items as the list shows them, key "<source>:<key>" for open()
    md,  // the light markdown of the bubbles: md(text) -> html, everything escaped
  };

  // Looks edited in the office change the avatars here too.
  SYA.on("looks", () => { if (chat.built && dock.open) { renderList(); renderRail(); if (chat.sid && !chat.src) renderHead(); } });
  window.openChat = openChat;
  // "Chat" on a board card, "Open chat" in the drawer: anything with data-chat="<session id>".
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-chat]");
    if (b) { e.preventDefault(); openChat(b.dataset.chat); }
  });
  // Alt+Up / Alt+Down: the previous / next chat on the rail.
  document.addEventListener("keydown", (e) => {
    if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.isComposing || !dock.open || !chat.sid) return;
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    stepRail(e.key === "ArrowUp" ? -1 : 1);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && dock.open) pollSoon(100); });
})();
