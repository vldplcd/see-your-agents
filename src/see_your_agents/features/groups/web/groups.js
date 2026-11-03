// Groups: group chats in the chat panel, as a SYA.chat source. Server: /api/groups (api.py); its handles and
// errors come worded in the page's language. Strings: groups/i18n/<lang>.json through t().
// Globals: $, esc, t, state, SYA, getJson, postJson, clock; SYA.chat (chat.js: addSource, refresh, open, md);
// figures.js: shapeFor, SHAPES, face; looks.js (optional): agentAvatarSvg.
// The list has a pinned "New group" row (key "new"); every group is a row; opening a group draws the thread.
(() => {
  if (!SYA.chat) return;  // the page needs the chat panel
  const SEEN_KEY = "sya.groups.seen", LIST_MS = 5000, FEED_MS = 3500, NEW = "new", MAX_MEMBERS = 12;
  const enc = encodeURIComponent;
  const get = (path) => getJson(path).catch(() => null);

  /* ---------- the list: polled on the board tick, at most every few seconds ---------- */
  const groups = { list: [], at: 0, busy: false, sig: "", seen: null };
  const seenMap = () => {
    if (!groups.seen) {
      try { groups.seen = JSON.parse(localStorage.getItem(SEEN_KEY)) || {}; } catch (e) { groups.seen = {}; }
      if (typeof groups.seen !== "object") groups.seen = {};
    }
    return groups.seen;
  };
  function markSeen(id, ts) {
    const seen = seenMap();
    if (!ts || (seen[id] && Date.parse(seen[id]) >= Date.parse(ts))) return;
    seen[id] = ts;
    try { localStorage.setItem(SEEN_KEY, JSON.stringify(seen)); } catch (e) { /* not persisted */ }
  }
  const unreadOf = (g) => {
    const from = Date.parse(seenMap()[g.id] || g.created_at) || 0;
    return (g.activity || []).filter((ts) => Date.parse(ts) > from).length;
  };
  async function loadList(force = false) {
    if (groups.busy || (!force && Date.now() - groups.at < LIST_MS)) return;
    groups.busy = true;
    const data = await get("/api/groups");
    groups.busy = false; groups.at = Date.now();
    if (!data) return;
    const sig = JSON.stringify(data.groups);
    groups.list = data.groups;
    if (sig !== groups.sig) { groups.sig = sig; SYA.chat.refresh(); }
  }

  /* ---------- avatars ---------- */
  const cardOf = (sid) => state.board?.cards.find((c) => c.session_id === sid);
  function avatarOf(sid, px) {
    const c = cardOf(sid);
    if (!c) return `<svg class="av-svg" viewBox="0 0 100 100" width="${px}" height="${px}" aria-hidden="true"><circle class="body" cx="50" cy="50" r="40"/></svg>`;
    if (typeof agentAvatarSvg === "function") return agentAvatarSvg(c, px);
    const shape = shapeFor(c);
    return `<svg class="av-svg" viewBox="0 0 100 100" width="${px}" height="${px}" aria-hidden="true"><path class="body" d="${SHAPES[shape].d}"/>${face(shape, c.column)}</svg>`;
  }
  const stackHtml = (members) => `<span class="groups-stack n${Math.min(members.length, 3)}">${
    members.slice(0, 3).map((sid) => `<span class="av av-${cardOf(sid)?.column || "recent"}">${avatarOf(sid, 24)}</span>`).join("")}</span>`;
  const PLUS = `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>`;

  /* ---------- items for the chat list and rail ---------- */
  function items() {
    loadList();
    const rows = groups.list.map((g) => ({
      key: g.id, title: g.title, short: g.title.slice(0, 8), avatar: stackHtml(g.members),
      unread: openId === g.id ? 0 : unreadOf(g), alert: g.members.some((sid) => cardOf(sid)?.column === "needs_you"),
    }));
    return [{ key: NEW, title: t("groups.new"), short: t("groups.new_short"), avatar: `<span class="groups-plus">${PLUS}</span>`, unread: 0, alert: false }, ...rows];
  }

  /* ---------- helpers ---------- */
  const DELIVERED = {
    peer: t("groups.mark.by_courier"), courier: t("groups.mark.by_courier"), resume: t("groups.mark.by_resume"),
    queue: t("groups.mark.by_queue"),
  };
  const COL = { needs_you: "needs", working: "work", your_move: "move" };
  let openId = "", timer = 0, token = 0;
  const drafts = new Map();

  /* ---------- the thread ---------- */
  function openThread(id, root) {
    clearTimeout(timer);
    const my = ++token;
    openId = id;
    const th = { id, data: null, mode: "", notice: "", sig: new Map(), stick: true, hint: "", ac: null, busy: false, pick: new Set(), find: "" };
    root.innerHTML = `<div class="groups-thread">
      <div class="groups-bar"></div>
      <div class="chat-scroll groups-feed" tabindex="-1"><div class="msgs"></div></div>
      <form class="composer groups-composer" autocomplete="off">
        <div class="groups-ac hidden" role="listbox"></div>
        <div class="comp-row">
          <textarea rows="1" aria-label="${t("groups.composer.label")}" placeholder="${t("groups.composer.placeholder")}"></textarea>
          <button type="submit" class="comp-send" aria-label="${t("groups.send")}" title="${t("groups.send_title")}"><svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l16-8-6 17-3-7z"/></svg></button>
        </div>
        <div class="comp-hint"><span class="hint-text" aria-live="polite"></span></div>
      </form></div>`;
    const el = { bar: root.querySelector(".groups-bar"), feed: root.querySelector(".groups-feed"), box: root.querySelector(".msgs"),
      form: root.querySelector("form"), ta: root.querySelector("textarea"), hint: root.querySelector(".hint-text"), ac: root.querySelector(".groups-ac") };
    el.ta.value = drafts.get(id) || "";
    const alive = () => token === my && openId === id;

    const handleOf = (sid) => th.data?.members.find((m) => m.session_id === sid)?.handle || "";
    const hasSup = () => !!th.data?.members.some((m) => m.is_supervisor);

    /* header: member chips and the «…» menu */
    function chip(m) {
      const col = COL[m.column] || "closed";
      return `<span class="groups-chip" title="${esc(m.title)}${m.name ? ` · ${esc(m.name)}` : ""}">
        <span class="av av-${esc(m.column)}">${avatarOf(m.session_id, 20)}</span><b>@${esc(m.handle)}</b><i class="groups-dot d-${col}"></i></span>`;
    }
    function candidates() {
      const inGroup = new Set(th.data.members.map((m) => m.session_id)), needle = th.find.trim().toLowerCase();
      const cards = (state.board?.cards || []).filter((c) => !inGroup.has(c.session_id));
      return pickable(cards, needle);
    }
    function panelHtml() {
      const cancel = `<button class="act" data-g="close" type="button">${t("groups.cancel")}</button>`;
      if (th.mode === "menu") return `<div class="groups-panel"><button class="act" data-g="rename" type="button">${t("groups.rename")}</button>
        <button class="act" data-g="remove" type="button">${t("groups.remove_member")}</button><button class="act danger" data-g="delete" type="button">${t("groups.delete")}</button></div>`;
      if (th.mode === "rename") return `<form class="groups-panel groups-rename"><input type="text" maxlength="60" value="${esc(th.data.group.title)}" aria-label="${t("groups.title_label")}">
        <button class="act" type="submit">${t("groups.save")}</button>${cancel}</form>`;
      if (th.mode === "remove") return `<div class="groups-panel"><span class="groups-lead">${t("groups.remove_lead")}</span>${th.data.members.map((m) =>
        `<button class="act" data-rm="${esc(m.session_id)}" type="button">@${esc(m.handle)} ×</button>`).join("")}${cancel}</div>`;
      if (th.mode === "delete") return `<div class="confirm" role="alertdialog" aria-label="${t("groups.delete")}"><p>${t("groups.delete_confirm")}</p>
        <button class="act danger" data-g="delete-yes" type="button">${t("groups.delete_yes")}</button>${cancel}</div>`;
      if (th.mode === "add") return `<div class="groups-panel groups-add"><input type="search" placeholder="${t("groups.find")}" value="${esc(th.find)}" aria-label="${t("groups.find_label")}">
        <div class="groups-pick">${pickRows(candidates(), null) || `<p class="empty">${t("groups.nobody_to_add")}</p>`}</div>
        <button class="act" data-g="close" type="button">${t("app.close")}</button></div>`;
      return "";
    }
    function renderBar() {
      if (!th.data) { el.bar.innerHTML = ""; return; }
      const sig = JSON.stringify([th.data.members, th.mode, th.find, th.notice, th.data.group.title, th.mode === "add" ? state.board?.cards.length : 0]);
      if (sig === th.barSig) return;
      th.barSig = sig;
      const keep = el.bar.querySelector("input"), focus = keep && document.activeElement === keep;
      el.bar.innerHTML = `<div class="groups-chips">${th.data.members.map(chip).join("")}
        <button class="act" data-g="add" type="button">${t("groups.add_member")}</button>
        <button class="act groups-more" data-g="menu" type="button" aria-label="${t("groups.actions")}" title="${t("groups.actions")}">…</button></div>
        ${panelHtml()}${th.notice ? `<p class="comp-hint bad" role="alert">${esc(th.notice)}</p>` : ""}`;
      const input = el.bar.querySelector("input");
      if (input && focus) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
      else if (input && !keep) input.focus();
    }

    /* the feed */
    function marks(p) {
      return (p.deliveries || []).map((d) => {
        const who = handleOf(d.session_id) || d.name;
        if (d.ok === null) return `<span class="g-mark pend" title="${t("groups.mark.sending")}">@${esc(who)} <i>…</i></span>`;
        if (d.ok) return `<span class="g-mark ok" title="${DELIVERED[d.route] || t("groups.mark.delivered")}">@${esc(who)} <i>✓</i></span>`;
        return `<span class="g-mark bad" title="${esc(d.error)}">@${esc(who)} <i>!</i> <em>${esc(d.error)}</em></span>`;
      }).join("");
    }
    function itemHtml(it) {
      const time = `<time>${esc(clock(it.ts))}</time>`;
      if (it.kind === "post") return { cls: "k-you", html: `<div class="bub"><div class="plain">${esc(it.text)}</div><div class="g-marks">${marks(it)}</div>${time}</div>` };
      if (it.kind === "reply") {
        const m = th.data.members.find((x) => x.session_id === it.session_id);
        return { cls: "k-agent g-reply", html: `<span class="av av-${esc(m?.column || "recent")}">${avatarOf(it.session_id, 30)}</span>
          <div class="bub md"><b class="g-who">@${esc(it.handle)}</b>${SYA.chat.md(it.text)}${it.in_progress
            ? `<div class="g-typing"><span class="dots"><i></i><i></i><i></i></span>${t("groups.typing")}</div>` : ""}${time}</div>` };
      }
      if (it.kind === "peer") return { cls: "k-peer_in", html: `<div class="peer-in"><div class="peer-from"><b>@${esc(it.from)}</b> → <b>@${esc(it.to)}</b></div>
        <div class="plain">${esc(it.text)}</div>${time}</div>` };
      return { cls: "k-agent g-sup", html: `<div class="bub md"><span class="sup-tag">${t("groups.supervisor_tag")}</span>${SYA.chat.md(it.text)}${time}</div>` };
    }
    function renderFeed() {
      const feed = th.data.feed, seen = new Set();
      const near = el.feed.scrollHeight - el.feed.scrollTop - el.feed.clientHeight < 90;
      let prev = null;
      for (const it of feed) {
        seen.add(it.key);
        const { cls, html } = itemHtml(it), sig = cls + html, old = th.sig.get(it.key);
        let node = old?.node;
        if (!old || old.sig !== sig) {
          const fresh = document.createElement("div");
          fresh.className = `msg ${cls}`; fresh.innerHTML = html;
          if (old) old.node.replaceWith(fresh); else el.box.insertBefore(fresh, prev ? prev.nextSibling : el.box.firstChild);
          node = fresh; th.sig.set(it.key, { sig, node });
        } else if (prev ? old.node.previousSibling !== prev : old.node !== el.box.firstChild) {
          el.box.insertBefore(old.node, prev ? prev.nextSibling : el.box.firstChild);  // replies can land before a later post
        }
        prev = node;
      }
      for (const [key, v] of th.sig) if (!seen.has(key)) { v.node.remove(); th.sig.delete(key); }
      if (!feed.length) {
        const code = (word) => `<code>@${word}</code>`;
        el.box.innerHTML = `<p class="empty groups-empty">${t("groups.feed.empty", { one: code(t("groups.handle.placeholder")), all: code(t("groups.handle.all")) })}</p>`;
      } else el.box.querySelector(".groups-empty")?.remove();
      if (th.stick || near) { el.feed.scrollTop = el.feed.scrollHeight; th.stick = false; }
    }
    const hintText = () => hasSup() ? t("groups.hint.to_supervisor") : t("groups.hint.no_supervisor");
    function paint() {
      if (!alive() || !th.data) return;
      renderBar(); renderFeed();
      el.hint.parentElement.classList.toggle("bad", !!th.sendError);
      el.hint.textContent = th.sendError || hintText();
      const last = th.data.feed.filter((i) => i.kind !== "post").at(-1)?.ts;
      markSeen(id, last);
    }
    let loadSeq = 0;  // an action reloads at once; an older load still in flight must not start a second loop
    async function load() {
      const mine = ++loadSeq;
      clearTimeout(timer);
      const data = await get(`/api/groups/${enc(id)}`);
      if (!alive() || mine !== loadSeq) return;
      if (!data) { th.data = th.data || null; if (!th.data) root.querySelector(".groups-bar").innerHTML = `<p class="empty">${t("groups.thread.not_found")}</p>`; }
      else { th.data = data; paint(); }
      timer = setTimeout(load, FEED_MS);
    }

    /* actions */
    async function act(body, then) {
      const r = await postJson("/api/groups", { ...body, id });
      if (!alive()) return;
      if (!r.ok) { th.notice = r.data?.error || t("groups.failed"); th.barSig = ""; renderBar(); return; }
      th.notice = ""; th.mode = ""; th.barSig = "";
      await loadList(true);
      then?.(r.data);
      load();
    }
    el.bar.addEventListener("click", (e) => {
      const rm = e.target.closest("[data-rm]"), add = e.target.closest("[data-add]"), b = e.target.closest("[data-g]");
      if (rm) return act({ action: "remove_member", session_id: rm.dataset.rm });
      if (add) return act({ action: "add_member", session_id: add.dataset.add });
      if (!b) return;
      const g = b.dataset.g;
      if (g === "delete-yes") return act({ action: "delete" }, () => SYA.chat.open(""));
      th.mode = g === "close" || th.mode === g ? "" : g; th.notice = ""; th.find = ""; th.barSig = ""; renderBar();
    });
    el.bar.addEventListener("input", (e) => { if (e.target.matches("input[type=search]")) { th.find = e.target.value; th.barSig = ""; renderBar(); } });
    el.bar.addEventListener("submit", (e) => {
      e.preventDefault();
      act({ action: "rename", title: e.target.querySelector("input").value });
    });

    /* composer: @-autocomplete, Enter sends */
    const words = () => [{ h: t("groups.handle.all"), note: t("groups.to_everyone") }, ...(th.data?.members || []).map((m) => ({ h: m.handle, note: m.name || m.title }))];
    function closeAc() { th.ac = null; el.ac.classList.add("hidden"); }
    function updateAc() {
      const caret = el.ta.selectionStart, m = /(^|\s)@([a-zа-яё0-9-]*)$/i.exec(el.ta.value.slice(0, caret));
      if (!m || !th.data) return closeAc();
      const opts = words().filter((w) => w.h.startsWith(m[2].toLowerCase()));
      if (!opts.length) return closeAc();
      th.ac = { opts, at: caret - m[2].length - 1, caret, i: Math.min(th.ac?.i || 0, opts.length - 1) };
      paintAc();
    }
    function paintAc() {
      el.ac.classList.remove("hidden");
      el.ac.innerHTML = th.ac.opts.map((w, i) => `<button type="button" role="option" class="${i === th.ac.i ? "on" : ""}" data-h="${esc(w.h)}"><b>@${esc(w.h)}</b> <span>${esc(w.note)}</span></button>`).join("");
    }
    function pickAc(h) {
      const { at, caret } = th.ac, v = el.ta.value;
      el.ta.value = `${v.slice(0, at)}@${h} ${v.slice(caret)}`;
      const pos = at + h.length + 2;
      el.ta.setSelectionRange(pos, pos); closeAc(); el.ta.focus();
    }
    el.ac.addEventListener("mousedown", (e) => { const b = e.target.closest("[data-h]"); if (b) { e.preventDefault(); pickAc(b.dataset.h); } });
    el.ta.addEventListener("input", () => {
      drafts.set(id, el.ta.value); th.sendError = "";
      el.ta.style.height = "auto"; el.ta.style.height = `${Math.min(el.ta.scrollHeight, 180)}px`;
      updateAc();
      el.hint.parentElement.classList.remove("bad"); el.hint.textContent = hintText();
    });
    el.ta.addEventListener("keydown", (e) => {
      if (th.ac) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); th.ac.i = (th.ac.i + (e.key === "ArrowDown" ? 1 : -1) + th.ac.opts.length) % th.ac.opts.length; return paintAc(); }
        if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); return pickAc(th.ac.opts[th.ac.i].h); }
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); return closeAc(); }
      }
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); el.form.requestSubmit(); }
    });
    el.ta.addEventListener("click", updateAc);
    el.ta.addEventListener("blur", () => setTimeout(closeAc, 120));
    el.form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = el.ta.value.trim();
      if (!text || th.busy) return;
      th.busy = true; closeAc();
      const r = await postJson(`/api/groups/${enc(id)}/post`, { text });
      th.busy = false;
      if (!alive()) return;
      if (!r.ok) { th.sendError = r.data?.error || t("groups.not_sent"); el.hint.parentElement.classList.add("bad"); el.hint.textContent = th.sendError; return; }
      th.sendError = ""; el.ta.value = ""; drafts.delete(id); el.ta.style.height = "auto"; th.stick = true;
      load();
    });

    load();
    el.ta.focus({ preventScroll: true });
  }

  /* ---------- the pick list shared by the create form and "Add member" ---------- */
  const ORDER = ["needs_you", "working", "your_move", "recent", "archive"];
  function pickable(cards, needle) {
    const hit = (c) => !needle || [c.title, c.project, c.live?.name].join(" ").toLowerCase().includes(needle);
    const rank = (c) => (c.live ? 0 : 1) * 10 + ORDER.indexOf(c.column);
    return cards.filter((c) => hit(c) && c.session_id !== state.board?.supervisor_id && (needle || c.column !== "archive"))
      .sort((a, b) => rank(a) - rank(b)).slice(0, 60);
  }
  function pickRows(cards, selected) {
    return cards.map((c) => {
      const tag = c.live ? esc(c.live.name || t("groups.pick.running")) : t("groups.pick.closed");
      const body = `<span class="av av-${esc(c.column)}">${avatarOf(c.session_id, 28)}</span>
        <span class="g-pick-main"><b>${esc(c.title)}</b><span>${esc(c.project)} · ${tag}</span></span>`;
      return selected
        ? `<label class="g-pick"><input type="checkbox" value="${esc(c.session_id)}"${selected.has(c.session_id) ? " checked" : ""}>${body}</label>`
        : `<button class="g-pick" type="button" data-add="${esc(c.session_id)}">${body}</button>`;
    }).join("");
  }

  /* ---------- the create form ---------- */
  function openCreate(root) {
    openId = NEW;
    const sup = state.board?.supervisor_id, supCard = sup ? cardOf(sup) : null;
    const picked = new Set();
    let find = "", error = "", busy = false;
    root.innerHTML = `<form class="groups-create">
      <label class="groups-field"><span>${t("groups.create.title")}</span><input class="g-title" type="text" maxlength="60" placeholder="${t("groups.create.title_placeholder")}" required></label>
      ${supCard ? `<div class="g-pick fixed"><input type="checkbox" checked disabled><span class="av av-${esc(supCard.column)}">${avatarOf(sup, 28)}</span>
        <span class="g-pick-main"><b>${esc(supCard.title)}</b><span><span class="sup-tag">${t("groups.supervisor_tag")}</span> ${t("groups.create.already_in")}</span></span></div>` : ""}
      <label class="groups-field"><span>${t("groups.create.members")} <em class="g-count"></em></span><input class="g-find" type="search" placeholder="${t("groups.find")}"></label>
      <div class="groups-pick"></div>
      <p class="comp-hint bad g-error hidden" role="alert"></p>
      <div class="groups-actions"><button class="act" type="submit">${t("groups.create.submit")}</button></div></form>`;
    const form = root.firstElementChild, list = form.querySelector(".groups-pick"), err = form.querySelector(".g-error");
    const count = () => { form.querySelector(".g-count").textContent = picked.size ? t("groups.create.picked", { n: picked.size }) : ""; };
    const paint = () => {
      list.innerHTML = pickRows(pickable(state.board?.cards || [], find.trim().toLowerCase()), picked) || `<p class="empty">${t("groups.create.no_match")}</p>`;
      count();
    };
    paint();
    form.querySelector(".g-find").addEventListener("input", (e) => { find = e.target.value; paint(); });
    list.addEventListener("change", (e) => {
      const box = e.target;
      if (!box.checked) picked.delete(box.value);
      else if (picked.size + (supCard ? 1 : 0) >= MAX_MEMBERS) { box.checked = false; err.textContent = t("groups.create.too_many", { n: MAX_MEMBERS }); err.classList.remove("hidden"); }
      else { picked.add(box.value); err.classList.add("hidden"); }
      count();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      busy = true;
      const r = await postJson("/api/groups", { action: "create", title: form.querySelector(".g-title").value, members: [...picked] });
      busy = false;
      if (openId !== NEW) return;
      if (!r.ok) { err.textContent = r.data?.error || t("groups.failed"); err.classList.remove("hidden"); return; }
      await loadList(true);
      SYA.chat.open(`groups:${r.data.group.id}`);
    });
    form.querySelector(".g-title").focus({ preventScroll: true });
  }

  SYA.chat.addSource({
    id: "groups",
    items,
    open(key, root) { if (key === NEW) openCreate(root); else openThread(key, root); },
    close() { clearTimeout(timer); token++; openId = ""; },
  });
  SYA.feature({ id: "groups" });
})();
