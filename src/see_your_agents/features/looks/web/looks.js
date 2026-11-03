// Per-agent looks in the office (shape, hue, desk, things on the desk) and palette tweaks.
// Server routes: /api/looks* (looks.py). Demo page: window.SYA_API.looks (demo/demo-api.js).
// Slots: `drawer` (the Look section), `prefs` (the palette in the look & feel panel).
// Strings: looks/i18n/<lang>.json through t().
//
// Globals for the other scripts:
//   lookFor(card)              the resolved look {shape, hue, desk, items, label, source}
//   agentAvatarSvg(card, px)   inline <svg> of the body and face in the agent's look and status
//   agentDeskSvg(card)         the office desk with the agent and its things (used by office.js)
//   agentLabel(card)           desk caption html (the custom label, else ticket + title)
//   officeToolbarHtml(), afterOfficeRender()   hooks called by office.js
// SYA.emit("looks") fires whenever the looks document changed.
(() => {
  const BLANK = () => ({ shape: null, hue: null, desk: null, items: [], label: "" });
  const MAX_ITEMS = 4, MAX_LABEL = 40;
  const doc = { sessions: {}, projects: {} };

  /* ---------- store: the server, or the demo's localStorage ---------- */
  const remote = {
    get: () => getJson("/api/looks"),
    post: (body) => postJson("/api/looks", body),
  };
  const backend = () => window.SYA_API?.looks || remote;
  const saving = new Map();
  const announce = () => SYA.emit("looks");

  async function loadLooks() {
    if (saving.size) return;
    try {
      const fresh = await backend().get();
      if (saving.size || !fresh || typeof fresh !== "object") return;
      doc.sessions = fresh.sessions || {}; doc.projects = fresh.projects || {};
      announce();
      if (typeof state !== "undefined" && state.board && state.view === "office" && !editor) render();
    } catch (e) { /* offline: the skin defaults stay */ }
  }

  let saveNote = () => {};
  function persist(target, look) {
    const kind = target.session_id ? "sessions" : "projects", key = target.session_id ?? target.project;
    if (look) doc[kind][key] = look; else delete doc[kind][key];
    announce();
    const id = kind + "\0" + key;
    clearTimeout(saving.get(id));
    saveNote(t("looks.save.saving"));
    saving.set(id, setTimeout(async () => {
      try {
        const r = await backend().post({ ...target, look });
        saving.delete(id);
        saveNote(r.ok ? t("looks.save.saved") : t("looks.save.failed", { reason: r.data?.error || r.status }));
      } catch (e) { saving.delete(id); saveNote(t("looks.save.offline")); }
    }, 250));
  }

  /* ---------- resolving a look ---------- */
  // Sessions of another program than the default one (the registry's first) wear its colour until someone picks one.
  const PROGRAM_HUE = { codex: 200 };
  function programHue(card) {
    const ids = Object.keys(state.board?.providers || {});
    if (!card.provider || !ids.length || card.provider === ids[0]) return null;
    return PROGRAM_HUE[card.provider] ?? hash(card.provider) % 360;
  }
  function lookFor(card) {
    const mine = doc.sessions[card.session_id], shared = card.project ? doc.projects[card.project] : null;
    const own = mine || shared || null;
    const l = { ...BLANK(), ...own };
    return {
      shape: SHAPES[l.shape] ? l.shape : shapeFor(card),
      hue: Number.isInteger(l.hue) ? l.hue : own ? null : programHue(card),
      desk: DESKS[l.desk] ? l.desk : prefs.desk,
      items: (l.items || []).filter((i) => ITEMS[i]).slice(0, MAX_ITEMS),
      label: String(l.label || "").slice(0, MAX_LABEL),
      source: mine ? "session" : shared ? "project" : "default",
    };
  }

  // Body colour for a hue, tuned per skin so it stays calm next to the status ring.
  const BAUHAUS = [[4, "#e3302b", "#ff5c50"], [46, "#f5c211", "#ffd43b"], [130, "#111111", "#f4f4f0"], [222, "#1f4fd6", "#5d88ff"]];
  const hueGap = (a, b) => Math.min(Math.abs(a - b), 360 - Math.abs(a - b));
  function agentFill(hue, skin = prefs.skin) {
    if (skin === "konstruktor") {
      const [, l, d] = BAUHAUS.reduce((best, x) => (hueGap(hue, x[0]) < hueGap(hue, best[0]) ? x : best));
      return `light-dark(${l}, ${d})`;
    }
    if (skin === "dispatch") return `light-dark(hsl(${hue} 30% 54%), hsl(${hue} 26% 50%))`;
    return `light-dark(hsl(${hue} 56% 68%), hsl(${hue} 46% 55%))`;
  }

  function bodyMarkup(l, column) {
    const d = SHAPES[l.shape].d, faces = prefs.faces ? face(l.shape, column) : "";
    if (l.hue == null) return `<path class="body" d="${d}"/>${faces}`;
    return `<g class="custom" style="--agent:${agentFill(l.hue)}"><path class="ring" d="${d}"/><path class="body custom" d="${d}"/>${faces}</g>`;
  }

  function agentAvatarSvg(card, px = 32) {
    return `<svg class="avatar d-${esc(card.column)}" width="${px}" height="${px}" viewBox="-14 -14 128 128" aria-hidden="true">${bodyMarkup(lookFor(card), card.column)}</svg>`;
  }

  /* ---------- things on the desk: drawn in a 14×16 box, standing on y = 16 ---------- */
  const ITEMS = {
    mug: { name: t("looks.item.mug"), svg: `<path class="ia" d="M2 6h8.4v7.6A2.4 2.4 0 0 1 8 16H4.4A2.4 2.4 0 0 1 2 13.6z"/>
      <path class="la" d="M10.4 8h.7a2.1 2.1 0 0 1 0 4.2h-.7"/><rect class="icof" x="2.7" y="6.5" width="7" height="1.2" rx=".6"/>
      <rect class="iw" opacity=".35" x="3.2" y="9" width="1.1" height="5" rx=".55"/><path class="ls" d="M4.8 4.4c-1-1 1-1.6 0-3.2M7.8 4.4c-1-1 1-1.6 0-3.2"/>` },
    plant: { name: t("looks.item.plant"), svg: `<path class="ig" d="M7 10.6C6.6 7 4.6 5.4 1.4 5.4c.2 3.2 2.3 5.3 5.6 5.2z"/>
      <path class="ig2" d="M7 10.6C7.2 6.4 9.1 3.2 12.6 2.4c.4 3.8-1.7 7.2-5.6 8.2z"/>
      <path class="lg2" d="M7 10.6C6.6 8.8 5.5 7.6 3.5 6.9M7 10.6c.3-2 1.2-4 3.2-5.6"/>
      <path class="ipot" d="M3 10.4h8l-1 5.6H4z"/><rect class="ipot2" x="2.6" y="10" width="8.8" height="1.8" rx=".7"/>` },
    cactus: { name: t("looks.item.cactus"), svg: `<path class="lg" d="M4.7 9.6H3.4a1.2 1.2 0 0 1-1.2-1.2V5.4M9.3 8.2h1.3a1.2 1.2 0 0 0 1.2-1.2V4.4"/>
      <rect class="ig" x="4.8" y="2" width="4.4" height="11" rx="2.2"/><path class="lg2" d="M7 3.6v8.4M5.6 6v4.6M8.4 6v4.6"/>
      <circle class="ip" cx="7" cy="2.1" r="1.2"/><path class="ipot" d="M3.7 12.2h6.6l-.9 3.8H4.6z"/><rect class="ipot2" x="3.3" y="11.8" width="7.4" height="1.5" rx=".6"/>` },
    lamp: { name: t("looks.item.lamp"), svg: `<path class="iy" opacity=".3" d="M3.4 7.8L.8 15.6h12.4l-2.6-7.8z"/>
      <rect class="ik" x="4.2" y="14.5" width="5.6" height="1.5" rx=".75"/><rect class="ik" x="6.4" y="8" width="1.2" height="6.8"/>
      <path class="ia" d="M3.4 8.4L4.9 2.2h4.2l1.5 6.2z"/><path class="ia2" d="M3.4 8.4h7.2l-.2-1H3.6z"/>` },
    books: { name: t("looks.item.books"), svg: `<rect class="ia" x="1" y="12.6" width="12" height="3.4" rx=".6"/><rect class="ib" x="2.4" y="9.2" width="9.6" height="3.4" rx=".6"/>
      <rect class="ic" x="1.6" y="5.8" width="10.4" height="3.4" rx=".6"/>
      <path class="lw" d="M3 14.3h3M4 10.9h2.4M3.4 7.5h2.8"/><path class="ia2" d="M9.6 5.8v2.8l.7-.6.7.6V5.8z"/>` },
    duck: { name: t("looks.item.duck"), svg: `<path class="iy" d="M.8 8.6l3 1.2-.4 2z"/><ellipse class="iy" cx="6.6" cy="12" rx="5.4" ry="3.9"/>
      <circle class="iy" cx="9.6" cy="5.9" r="3.3"/><ellipse class="iy2" cx="5.8" cy="12.2" rx="2.7" ry="1.7"/>
      <path class="io" d="M12.3 5.3l1.9.8-1.9 1z"/><circle class="ik" cx="10.5" cy="5.1" r=".7"/>` },
    cat: { name: t("looks.item.cat"), svg: `<path class="lo" d="M10.5 15.2c2.7.2 3.5-2.6 2.5-4.8"/>
      <path class="io" d="M3 16c-.4-3.6 1.1-6.2 4-6.2s4.4 2.6 4 6.2z"/><circle class="io" cx="7" cy="6.2" r="3.7"/>
      <path class="io" d="M3.6 4.8L3.8 1l2.9 2.1zM10.4 4.8L10.2 1 7.3 3.1z"/><path class="ip" d="M4.5 3.6l.1-1.2 1 .8zM9.5 3.6l-.1-1.2-1 .8z"/>
      <path class="lo2" d="M7 2.9v1.5M5.5 3.3l.4 1.1M8.5 3.3l-.4 1.1"/>
      <circle class="ik" cx="5.5" cy="6.2" r=".6"/><circle class="ik" cx="8.5" cy="6.2" r=".6"/><path class="ip" d="M6.4 7.2h1.2L7 7.9z"/>` },
    headphones: { name: t("looks.item.headphones"), svg: `<path class="lk" d="M2.6 10.6V8a4.4 4.4 0 0 1 8.8 0v2.6"/>
      <rect class="ia" x="1" y="9.8" width="3.6" height="6.2" rx="1.6"/><rect class="ia" x="9.4" y="9.8" width="3.6" height="6.2" rx="1.6"/>
      <rect class="ia2" x="1" y="12.4" width="1.4" height="2.8" rx=".7"/><rect class="ia2" x="11.6" y="12.4" width="1.4" height="2.8" rx=".7"/>` },
    laptop: { name: t("looks.item.laptop"), svg: `<rect class="ilap" x=".8" y="3.4" width="12.4" height="9.8" rx="1.1"/>
      <circle class="ia" cx="4.2" cy="7" r="1.8"/><rect class="ib" x="6.6" y="5.2" width="3.2" height="3.2" rx=".5" transform="rotate(-12 8.2 6.8)"/>
      <path class="ic" d="M8.2 12.2l1.6-2.8 1.6 2.8z"/><circle class="ip" cx="11" cy="5.8" r="1"/><rect class="ig" x="3" y="10.2" width="3.4" height="1.4" rx=".7"/>
      <rect class="ilap2" x="0" y="13.2" width="14" height="2.8" rx="1.2"/>` },
    globe: { name: t("looks.item.globe"), svg: `<rect class="ik" x="6.4" y="11" width="1.2" height="3.2"/><rect class="ik" x="3.6" y="14" width="6.8" height="2" rx="1"/>
      <circle class="ib" cx="7" cy="6.2" r="5.4"/><path class="ig" d="M3.7 4.2c1.4-1.1 2.9-.7 3.3.3s-.9 1.7-.9 2.7-1.7 1-2.3-.3S2.8 5 3.7 4.2zM8.4 6.8c1.3-.5 2.6.3 2.4 1.8s-1.5 1.9-2.3 1.1S7.6 7.1 8.4 6.8z"/>
      <path class="lw2" d="M7 .9a3 5.3 0 0 1 0 10.6"/><ellipse class="iw" opacity=".4" cx="4.6" cy="3.3" rx=".9" ry="1.6" transform="rotate(40 4.6 3.3)"/>` },
    trophy: { name: t("looks.item.trophy"), svg: `<path class="ly" d="M3.2 2.6H1.6v1.5a2.5 2.5 0 0 0 2.5 2.5M10.8 2.6h1.6v1.5a2.5 2.5 0 0 1-2.5 2.5"/>
      <path class="iy" d="M3.2 1h7.6v4.8a3.8 3.8 0 0 1-7.6 0z"/><rect class="iy2" x="6.1" y="9.4" width="1.8" height="3.2"/>
      <rect class="iy2" x="3.6" y="12.4" width="6.8" height="3.6" rx=".9"/><rect class="iw" opacity=".75" x="5.4" y="13.6" width="3.2" height="1.2" rx=".4"/>
      <path class="iw" opacity=".9" d="M7 2.7l.55 1.15 1.25.15-.9.85.25 1.25L7 5.5l-1.15.6.25-1.25-.9-.85 1.25-.15z"/>` },
    photo: { name: t("looks.item.photo"), svg: `<rect class="iwood2" x="5.2" y="12.6" width="3.6" height="3.4" rx=".6"/><rect class="iwood" x="1" y="2" width="12" height="11.2" rx=".9"/>
      <rect class="isky" x="2.7" y="3.7" width="8.6" height="7.8"/><circle class="iy" cx="9.2" cy="5.8" r="1.2"/>
      <path class="ig" d="M2.7 11.5l3-4 2.2 2.6 1.4-1.5 2 2.9z"/>` },
  };
  // Slots on the desktop (x of the item centre, y of the baseline); two each side of the agent.
  const SLOTS = {
    desk: { y: 45, x: [13, 28, 92, 106] }, round: { y: 53, x: [16, 31, 89, 104] }, console: { y: 45, x: [20, 33, 87, 100] },
    pouf: { y: 66, x: [33, 47, 73, 87] }, none: { y: 52, x: [14, 29, 91, 106] },
  };
  function itemsSvg(ids, deskId) {
    const { y, x } = SLOTS[deskId] || SLOTS.desk;
    return ids.map((id, i) => `<g class="it" transform="translate(${x[i] - 7} ${y - 16})"><ellipse class="ishadow" cx="7" cy="16" rx="6.2" ry="1"/>${ITEMS[id].svg}</g>`).join("");
  }

  function agentDeskSvg(card) {
    const l = lookFor(card), desk = DESKS[l.desk];
    return `<svg viewBox="0 0 120 96" aria-hidden="true">${desk.under || ""}<g transform="translate(40 4) scale(.4)">${bodyMarkup(l, card.column)}</g>${desk.over}${itemsSvg(l.items, l.desk)}</svg>`;
  }
  function agentLabel(card) {
    const custom = lookFor(card).label;
    if (custom) return esc(custom);
    return `${esc(card.jira[0] && !card.title.includes(card.jira[0].key) ? card.jira[0].key + " " : "")}${esc(card.title)}`;
  }

  /* ---------- edit mode and the editor popover ---------- */
  let editMode = false, editor = null;
  const officeEl = () => document.getElementById("office");
  const deskEl = (id) => officeEl().querySelector(`.desk[data-id="${CSS.escape(id)}"]`);
  const cardOf = (id) => state.board?.cards.find((c) => c.session_id === id);

  function officeToolbarHtml() {
    return `<div class="look-bar"><button class="ghost look-toggle" aria-pressed="${editMode}">${PENCIL}${t("looks.bar.customize")}</button><span class="look-hint">${editMode
      ? t("looks.bar.editing") : t("looks.bar.hint")}</span></div>`;
  }
  const PENCIL = `<svg class="look-ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M11.2 1.8l3 3L5.4 13.6l-3.8.8.8-3.8z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>`;

  // A pencil per desk: a sibling of the desk button (a button inside a button is invalid), laid over its top-right corner.
  function placePencils() {
    officeEl().querySelectorAll(".look-pencil").forEach((p) => {
      const d = p.previousElementSibling;
      p.style.left = `${d.offsetLeft + d.offsetWidth - 26}px`;
      p.style.top = `${d.offsetTop + 2}px`;
    });
  }
  function addPencils() {
    officeEl().querySelectorAll(".desk[data-id]").forEach((d) => {
      const b = document.createElement("button");
      b.className = "look-pencil";
      b.type = "button";
      b.dataset.lookPencil = d.dataset.id;
      b.title = t("looks.pencil");
      b.setAttribute("aria-label", t("looks.pencil_for", { name: d.querySelector(".label")?.textContent.trim() || "" }));
      b.innerHTML = PENCIL;
      d.after(b);
    });
    placePencils();
  }
  function afterOfficeRender() {
    officeEl().classList.toggle("editing", editMode);
    addPencils();
    if (!editor) return;
    const el = deskEl(editor.id);
    if (!el) { closeEditor(); return; }
    el.classList.add("look-open");
    placeEditor();
  }

  function refreshDesk(card) {
    const el = deskEl(card.session_id);
    if (!el) return;
    el.querySelector("svg").outerHTML = agentDeskSvg(card);
    el.querySelector(".label").innerHTML = agentLabel(card);
  }

  const targetOf = (card) => ({ session_id: card.session_id });
  // Editing a project-wide look makes the agent its own copy; everything the user did not touch stays.
  function change(card, patch) {
    const l = lookFor(card);
    const base = l.source === "default" ? { ...BLANK(), hue: l.hue } : { ...BLANK(), ...(doc.sessions[card.session_id] || doc.projects[card.project]) };
    persist(targetOf(card), { ...base, ...patch });
    refreshDesk(card);
    syncEditor();
  }

  const STATUS_COLOR = { needs_you: "var(--needs)", working: "var(--work)", your_move: "var(--move)" };
  const HUES = [4, 28, 46, 98, 160, 200, 222, 270, 320];
  const NARROW = "(max-width: 620px)";

  function openEditor(id) {
    closeEditor();
    const card = cardOf(id);
    if (!card) return;
    hidePeekNow();
    const el = document.createElement("div");
    el.className = "le";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-label", t("looks.editor.title"));
    el.innerHTML = `<header><div><b class="le-title"></b><small class="le-where"></small></div><button class="ghost le-x" data-act="close" aria-label="${t("app.close")}">×</button></header>
      <p class="le-note"></p>
      <section><h5>${t("looks.editor.shape")}</h5><div class="picks le-shapes">${Object.entries(SHAPES).map(([sid, s]) =>
        `<button class="pick" data-shape="${sid}" title="${esc(s.name)}" aria-label="${esc(s.name)}"><svg viewBox="0 0 100 100"><path d="${s.d}"/></svg></button>`).join("")}</div></section>
      <section><h5>${t("looks.editor.color")}</h5><input class="le-hue" type="range" min="0" max="359" step="1" aria-label="${t("looks.editor.hue")}">
        <div class="le-sw">${HUES.map((h) => `<button class="sw" data-hue="${h}" aria-label="${t("looks.editor.hue_n", { n: h })}"></button>`).join("")}
        <button class="ghost le-status-hue" data-hue="">${t("looks.editor.by_status")}</button></div></section>
      <section><h5>${t("looks.editor.desk")}</h5><div class="picks desk-picks">${Object.entries(DESKS).map(([did, d]) =>
        `<button class="pick" data-desk="${did}">${deskSvg("circle", "your_move", d, false)}<span>${esc(d.name)}</span></button>`).join("")}</div></section>
      <section><h5>${t("looks.editor.items")} <span class="le-count"></span></h5><div class="picks le-items">${Object.entries(ITEMS).map(([iid, it]) =>
        `<button class="pick" data-item="${iid}" title="${esc(it.name)}" aria-label="${esc(it.name)}"><svg viewBox="-1 0 16 16" class="it">${it.svg}</svg></button>`).join("")}</div></section>
      <section><h5>${t("looks.editor.label")}</h5><input class="le-label" type="text" maxlength="${MAX_LABEL}" placeholder="${t("looks.editor.label_placeholder")}" aria-label="${t("looks.editor.label_aria")}"></section>
      <footer><button class="ghost" data-act="project">${t("looks.editor.to_project")}</button><button class="ghost" data-act="reset">${t("looks.editor.reset")}</button><span class="le-save" role="status"></span></footer>`;
    document.body.append(el);
    editor = { id, el };
    saveNote = (text) => { const s = el.querySelector(".le-save"); if (s) s.textContent = text; };
    el.querySelector(".le-title").textContent = card.title;
    el.querySelector(".le-where").textContent = card.project || t("looks.editor.no_project");
    el.querySelector(".le-hue").oninput = (e) => { const c = cardOf(id); if (c) change(c, { hue: +e.target.value }); };
    el.querySelector(".le-label").oninput = (e) => { const c = cardOf(id); if (c) change(c, { label: e.target.value.slice(0, MAX_LABEL) }); };
    el.addEventListener("click", onEditorClick);
    deskEl(id)?.classList.add("look-open");
    syncEditor();
    placeEditor(true);
    el.querySelector(".pick[aria-pressed=true]")?.focus({ preventScroll: true });
  }

  function onEditorClick(e) {
    const b = e.target.closest("button");
    const card = editor && cardOf(editor.id);
    if (!b || !card) return;
    const l = lookFor(card);
    if (b.dataset.act === "close") closeEditor();
    else if (b.dataset.shape) change(card, { shape: b.dataset.shape });
    else if (b.dataset.desk) change(card, { desk: b.dataset.desk });
    else if (b.dataset.hue !== undefined) change(card, { hue: b.dataset.hue === "" ? null : +b.dataset.hue });
    else if (b.dataset.item) {
      const on = l.items.includes(b.dataset.item);
      if (!on && l.items.length >= MAX_ITEMS) return;
      change(card, { items: on ? l.items.filter((i) => i !== b.dataset.item) : [...l.items, b.dataset.item] });
    } else if (b.dataset.act === "project" && card.project) {
      persist({ project: card.project }, { shape: l.shape, hue: l.hue, desk: l.desk, items: l.items, label: "" });
      if (!l.label) persist(targetOf(card), null);
      refreshDesk(card);
      syncEditor();
    } else if (b.dataset.act === "reset") {
      persist(l.source === "project" ? { project: card.project } : targetOf(card), null);
      refreshDesk(card);
      syncEditor();
    }
  }

  function syncEditor() {
    if (!editor) return;
    const card = cardOf(editor.id), el = editor.el;
    if (!card) { closeEditor(); return; }
    const l = lookFor(card);
    const press = (node, on) => node.setAttribute("aria-pressed", String(on));
    el.querySelectorAll("[data-shape]").forEach((b) => press(b, b.dataset.shape === l.shape));
    el.querySelectorAll("[data-desk]").forEach((b) => press(b, b.dataset.desk === l.desk));
    el.querySelectorAll("[data-item]").forEach((b) => {
      const on = l.items.includes(b.dataset.item);
      press(b, on); b.disabled = !on && l.items.length >= MAX_ITEMS;
    });
    el.querySelectorAll(".le-sw .sw").forEach((b) => { b.style.setProperty("--agent", agentFill(+b.dataset.hue)); press(b, l.hue === +b.dataset.hue); });
    press(el.querySelector(".le-status-hue"), l.hue == null);
    el.querySelector(".le-shapes").style.setProperty("--pv", l.hue == null ? STATUS_COLOR[card.column] || "var(--closed)" : agentFill(l.hue));
    const slider = el.querySelector(".le-hue");
    if (document.activeElement !== slider) slider.value = l.hue ?? 0;
    slider.classList.toggle("off", l.hue == null);
    const label = el.querySelector(".le-label");
    if (document.activeElement !== label) label.value = l.label;
    el.querySelector(".le-count").textContent = `${l.items.length}/${MAX_ITEMS}`;
    el.querySelector(".le-note").textContent = {
      default: l.hue == null ? t("looks.note.default") : t("looks.note.default_program"),
      project: t("looks.note.project", { project: card.project }),
      session: t("looks.note.session"),
    }[l.source];
    const reset = el.querySelector('[data-act="reset"]');
    reset.disabled = l.source === "default";
    reset.textContent = l.source === "project" ? t("looks.editor.reset_project") : t("looks.editor.reset");
    el.querySelector('[data-act="project"]').disabled = !card.project;
  }

  function placeEditor(first = false) {
    if (!editor) return;
    const { el, id } = editor, desk = deskEl(id);
    if (!desk) return;
    const m = 12, w = el.offsetWidth, h = el.offsetHeight, r = desk.getBoundingClientRect();
    el.classList.remove("sheet");
    el.style.maxHeight = "";
    if (matchMedia(NARROW).matches) {
      // bottom sheet: keep the desk visible between the sticky bar and the sheet
      el.classList.add("sheet");
      el.style.left = el.style.top = "";
      const top = document.querySelector(".bar").getBoundingClientRect().bottom + 8, deskBottom = top + r.height;
      if (first && Math.abs(r.top - top) > 4) scrollBy(0, r.top - top);
      el.style.maxHeight = `${Math.max(220, innerHeight - deskBottom - 24)}px`;
      return;
    }
    let left = r.right + m;
    if (left + w > innerWidth - m) left = r.left - w - m;
    if (left < m) left = Math.min(Math.max(m, r.left + r.width / 2 - w / 2), innerWidth - w - m);
    el.style.left = `${left}px`;
    el.style.top = `${Math.min(Math.max(m, r.top - 20), Math.max(m, innerHeight - h - m))}px`;
  }

  function closeEditor() {
    if (!editor) return;
    document.querySelectorAll("#office .look-open").forEach((d) => d.classList.remove("look-open"));
    editor.el.remove();
    editor = null;
    saveNote = () => {};
  }

  function setEditMode(on) {
    editMode = on;
    closeEditor();
    hidePeekNow();
    render();
  }

  // Drawer section Look: avatar, where the look comes from, a button that opens the editor in the office.
  function lookSectionHtml(card) {
    if (card.column === "archive") return "";
    const src = lookFor(card).source;
    const text = src === "session" ? t("looks.section.own")
      : src === "project" ? esc(t("looks.section.project", { project: card.project })) : t("looks.section.default");
    return `<section class="look-section"><h4>${t("looks.section.title")}</h4><div class="look-row">${agentAvatarSvg(card, 48)}<span>${text}</span>
      <button class="ghost" data-look-edit="${esc(card.session_id)}">${t("looks.section.change")}</button></div></section>`;
  }
  function editFromDrawer(id) {
    closeDrawer();
    setView("office");
    requestAnimationFrame(() => {
      const el = deskEl(id);
      if (!el) return;
      el.scrollIntoView({ block: "center", inline: "nearest" });
      requestAnimationFrame(() => openEditor(id));
    });
  }

  document.addEventListener("click", (e) => {
    if (!e.target.closest) return;
    const edit = e.target.closest("[data-look-edit]");
    if (edit) { e.stopPropagation(); e.preventDefault(); editFromDrawer(edit.dataset.lookEdit); return; }
    const pencil = e.target.closest("#office .look-pencil");
    if (pencil) {
      e.stopPropagation(); e.preventDefault();  // not the drawer
      const id = pencil.dataset.lookPencil;
      if (editor?.id === id) closeEditor(); else openEditor(id);
      return;
    }
    if (e.target.closest("#office .look-toggle")) { setEditMode(!editMode); return; }
    const desk = editMode && e.target.closest("#office .desk");
    if (!desk) return;
    e.stopPropagation(); e.preventDefault();  // no drawer while customising
    if (editor?.id === desk.dataset.id) closeEditor(); else openEditor(desk.dataset.id);
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && editor) { e.stopPropagation(); closeEditor(); }
  }, true);
  document.addEventListener("pointerdown", (e) => {
    if (editor && !e.target.closest(".le, #office .desk, #office .look-pencil")) closeEditor();
  });
  // no hover peek on top of the editor
  for (const type of ["pointerover", "focusin"]) {
    document.addEventListener(type, (e) => { if ((editMode || editor) && e.target.closest?.("#office")) e.stopPropagation(); }, true);
  }
  addEventListener("resize", () => { placePencils(); placeEditor(); });
  addEventListener("scroll", () => placeEditor(), { passive: true });
  addEventListener("focus", loadLooks);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadLooks(); });
  new MutationObserver(() => { if (officeEl().classList.contains("hidden")) closeEditor(); })
    .observe(officeEl(), { attributes: true, attributeFilter: ["class"] });

  /* ---------- palette: presets and colour inputs per skin ---------- */
  // Each preset: [needs you, working, your turn, accent] as the light-theme colours. The first is the skin's own.
  const PALETTES = {
    buro: [
      { name: t("looks.palette.buro"), c: ["#d93a3f", "#178a7e", "#b7791f", "#3d4fc4"] },
      { name: t("looks.palette.forest"), c: ["#c8452c", "#2f7d4f", "#b8860b", "#2f6f5e"] },
      { name: t("looks.palette.ocean"), c: ["#d6456b", "#1f6fb2", "#7a5bc4", "#1f6fb2"] },
      { name: t("looks.palette.sunset"), c: ["#c2362f", "#7b4fa3", "#d9822b", "#7b4fa3"] },
    ],
    konstruktor: [
      { name: t("looks.palette.bauhaus"), c: ["#e3302b", "#1f4fd6", "#f5c211", "#1f4fd6"] },
      { name: t("looks.palette.mondrian"), c: ["#dd0100", "#225095", "#fac901", "#225095"] },
      { name: t("looks.palette.constructivism"), c: ["#d62718", "#1a1a1a", "#f2a900", "#1a1a1a"] },
      { name: t("looks.palette.pop"), c: ["#ff3d7f", "#00a6a6", "#ffb400", "#5b3df5"] },
    ],
    dispatch: [
      { name: t("looks.palette.dispatch"), c: ["#d1362e", "#2f63c8", "#c98a12", "#1d6a4a"] },
      { name: t("looks.palette.amber"), c: ["#e0502b", "#3f8f5b", "#d8a31a", "#b5760f"] },
      { name: t("looks.palette.signals"), c: ["#c8102e", "#0072ce", "#e8a900", "#0072ce"] },
      { name: t("looks.palette.fog"), c: ["#b5473f", "#4d7c8a", "#9a8a3c", "#4d7c8a"] },
    ],
  };
  const PAL_FIELDS = [
    ["needs", t("looks.palette.needs")], ["work", t("looks.palette.work")], ["move", t("looks.palette.move")],
    ["accent", t("looks.palette.accent")],
  ];
  // Page and card surfaces per skin and theme: the colours text has to stand out on. Keep in step with index.html tokens.
  const SURFACES = {
    buro: { light: ["#edf0f3", "#ffffff"], dark: ["#151b22", "#1e2630"] },
    konstruktor: { light: ["#ffffff", "#ffffff"], dark: ["#141518", "#1c1e22"] },
    dispatch: { light: ["#cfd6d1", "#f7f8f3"], dark: ["#151a18", "#222926"] },
  };
  const MIN_TEXT_CONTRAST = 4.5, MIN_UI_CONTRAST = 3;

  const rgbOf = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const hexOf = (rgb) => "#" + rgb.map((v) => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");
  function toHsl(hex) {
    const [r, g, b] = rgbOf(hex).map((v) => v / 255), mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, d = mx - mn;
    if (!d) return [0, 0, l];
    const s = d / (1 - Math.abs(2 * l - 1));
    const h = mx === r ? ((g - b) / d + 6) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [h * 60, s, l];
  }
  function fromHsl(h, s, l) {
    const k = (n) => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    return hexOf([0, 8, 4].map((n) => 255 * (l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1)))));
  }
  const luminance = (hex) => {
    const [r, g, b] = rgbOf(hex).map((v) => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
    return .2126 * r + .7152 * g + .0722 * b;
  };
  function contrast(a, b) {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (hi + .05) / (lo + .05);
  }
  const mix = (a, b, share) => hexOf(rgbOf(a).map((v, i) => v * share + rgbOf(b)[i] * (1 - share)));
  // Step the lightness (dir -1 darker, +1 lighter) until the colour reads on every background.
  function readable(hex, backgrounds, min, dir) {
    const [h, s, l0] = toHsl(hex);
    for (let l = l0; l >= 0 && l <= 1; l += dir * .01) {
      const c = fromHsl(h, s, l);
      if (backgrounds.every((bg) => contrast(c, bg) >= min)) return c;
    }
    return dir < 0 ? "#000000" : "#ffffff";
  }

  // One status colour as the set of tokens the page uses: solid, text ink, tint, in a light and a dark variant.
  function statusTokens(hex, skin) {
    const [pl, sl] = SURFACES[skin].light, [pd, sd] = SURFACES[skin].dark;
    const [h, s, l] = toHsl(hex);
    const dark = readable(fromHsl(h, Math.min(s, .9), Math.max(l, .6)), [sd], MIN_UI_CONTRAST, 1);
    const tintL = mix(hex, sl, .12), tintD = mix(dark, pd, .2);
    return {
      solid: [hex, dark],
      ink: [readable(hex, [pl, sl, tintL], MIN_TEXT_CONTRAST, -1), readable(dark, [pd, sd, tintD], MIN_TEXT_CONTRAST, 1)],
      tint: [tintL, tintD],
      on: [hex, dark].map((c) => (contrast("#ffffff", c) >= contrast("#000000", c) ? "#ffffff" : "#000000")),
    };
  }
  const pair = ([l, d]) => `light-dark(${l}, ${d})`;
  function paletteVars(skin, pal) {
    const vars = {};
    for (const key of ["needs", "work", "move"]) {
      const tok = statusTokens(pal[key], skin);
      vars[`--${key}`] = pair(tok.solid); vars[`--${key}-ink`] = pair(tok.ink); vars[`--${key}-tint`] = pair(tok.tint);
      if (key === "needs") vars["--on-needs"] = pair(tok.on);
    }
    vars["--link"] = pair(statusTokens(pal.accent, skin).ink);
    return vars;
  }
  const PALETTE_VARS = ["--needs", "--needs-ink", "--needs-tint", "--on-needs", "--work", "--work-ink", "--work-tint",
    "--move", "--move-ink", "--move-tint", "--link"];

  const hexOk = (v) => typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v);
  const presetPal = (skin, i) => Object.fromEntries(PAL_FIELDS.map(([k], j) => [k, PALETTES[skin][i].c[j]]));
  const savedPal = (skin) => {
    const p = prefs.palettes?.[skin];
    return p && PAL_FIELDS.every(([k]) => hexOk(p[k])) ? p : null;
  };
  const currentPal = (skin) => savedPal(skin) || presetPal(skin, 0);

  // Inline custom properties on :root beat the skin's own tokens; the page's early script replays prefs.paletteVars.
  function applyPalette() {
    const style = document.documentElement.style, skin = prefs.skin, pal = savedPal(skin);
    for (const k of PALETTE_VARS) style.removeProperty(k);
    const vars = pal ? paletteVars(skin, pal) : null;
    for (const [k, v] of Object.entries(vars || {})) style.setProperty(k, v);
    prefs.paletteVars = vars; prefs.paletteSkin = skin;
  }
  const storePal = (skin, pal) => {
    prefs.palettes = { ...prefs.palettes };
    if (pal) prefs.palettes[skin] = pal; else delete prefs.palettes[skin];
    applyPalette(); savePrefs();
  };

  function renderPalette() {
    const box = document.getElementById("palette");
    if (!box) return;
    const skin = prefs.skin, pal = currentPal(skin), saved = savedPal(skin);
    const active = saved ? PALETTES[skin].findIndex((p, i) => PAL_FIELDS.every(([k]) => presetPal(skin, i)[k] === saved[k].toLowerCase())) : 0;
    box.innerHTML = `<legend>${t("looks.palette.title")}</legend>
      <div class="presets">${PALETTES[skin].map((p, i) => `<button class="preset" data-preset="${i}" aria-pressed="${i === active}">
        <span class="sw">${p.c.map((c) => `<i style="background:${c}"></i>`).join("")}</span><b>${esc(p.name)}</b></button>`).join("")}</div>
      <div class="pal-inputs">${PAL_FIELDS.map(([k, name]) => `<label>${name}<input type="color" data-pal="${k}" value="${pal[k]}"></label>`).join("")}</div>
      <p class="note">${t("looks.palette.note")}</p>
      <button class="ghost" data-pal-reset ${saved ? "" : "disabled"}>${t("looks.palette.reset")}</button>`;
  }
  const palBox = () => document.getElementById("palette");
  palBox()?.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const skin = prefs.skin;
    if (b.dataset.preset !== undefined) storePal(skin, +b.dataset.preset ? presetPal(skin, +b.dataset.preset) : null);
    else if ("palReset" in b.dataset) storePal(skin, null);
    else return;
    renderPalette();
  });
  palBox()?.addEventListener("input", (e) => {
    const k = e.target.dataset.pal;
    if (k) storePal(prefs.skin, { ...currentPal(prefs.skin), [k]: e.target.value.toLowerCase() });
  });
  palBox()?.addEventListener("change", renderPalette);

  Object.assign(window, { lookFor, agentAvatarSvg, agentDeskSvg, agentLabel, officeToolbarHtml, afterOfficeRender });
  SYA.feature({ id: "looks", drawer: lookSectionHtml, prefs: { render: renderPalette, apply: applyPalette } });
  window.lookPalette = { vars: paletteVars, contrast, PALETTES, SURFACES };  // exposed for checks

  const before = JSON.stringify(prefs.paletteVars ?? null);
  applyPalette();
  if (JSON.stringify(prefs.paletteVars) !== before) savePrefs();
  loadLooks();
})();
