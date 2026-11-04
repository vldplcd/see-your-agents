// Office: the sessions on a floor plan, a room per project and a desk per session, with a hover card. Desks,
// captions and the toolbar come from looks.js (agentDeskSvg, agentLabel, officeToolbarHtml, afterOfficeRender).
// Strings: office/i18n/<lang>.json through t(). Globals from app.js: $, esc, t, state, prefs, hash, liveLine, askText,
// chips, progressBar, taskItems, waited, agoBack, can, spend, visibleCards, SYA; from figures.js: DESKS. Exposes
// hidePeekNow for looks.js. A room's empty desk opens New session in its folder through SYA.launch (optional).
(() => {
  let peekId = null;

  function renderOffice() {
    const cards = visibleCards().filter((c) => c.column !== "archive");
    const rooms = new Map();
    for (const c of cards) (rooms.get(c.project) || rooms.set(c.project, []).get(c.project)).push(c);
    const order = { needs_you: 0, working: 1, your_move: 2, recent: 3 };
    const sorted = [...rooms.entries()].sort((a, b) =>
      Math.min(...a[1].map((c) => order[c.column])) - Math.min(...b[1].map((c) => order[c.column])) || b[1].length - a[1].length);
    const bubble = { needs_you: "!", working: "", your_move: "…", recent: "" };
    $("#office").innerHTML = officeToolbarHtml() + (sorted.map(([name, list]) => {
      const plus = newDesk(list);
      return `
      <section class="room" style="--desks:${Math.min(list.length + (plus ? 1 : 0), 8)}"><span class="room-name">${esc(name || t("office.no_project"))}</span><span class="room-door"></span>
        <div class="desks">${list.sort((a, b) => order[a.column] - order[b.column]).map((c) => `
          <button class="desk d-${c.column}" data-id="${esc(c.session_id)}" aria-label="${esc(c.title)}: ${esc(liveLine(c))}" style="--blink:-${hash(c.session_id) % 7}s">
            ${bubble[c.column] ? `<span class="bubble">${bubble[c.column]}</span>` : ""}
            ${agentDeskSvg(c)}
            <div class="label">${agentLabel(c)}</div>
          </button>`).join("")}${plus}
        </div></section>`;
    }).join("") || `<p class="empty">${t("office.empty")}</p>`);
    afterOfficeRender();
    if (peekId) drawPeek();
  }

  /* ---------- an empty desk: a new session in the room's folder ---------- */
  // The room's most recent session outside a worktree says where the project is; the dialog lets you change it.
  function roomFolder(list) {
    const recent = [...list].sort((a, b) => (b.last_activity || "").localeCompare(a.last_activity || ""));
    return (recent.find((c) => c.cwd && !c.worktree) || recent.find((c) => c.cwd))?.cwd || "";
  }
  function newDesk(list) {
    const cwd = roomFolder(list);
    if (!SYA.launch || !cwd) return "";  // the demo has no launch feature
    const desk = DESKS[prefs.desk] || Object.values(DESKS)[0];
    return `<button class="desk-new" type="button" data-launch-cwd="${esc(cwd)}" title="${esc(t("office.new_session_in", { folder: cwd }))}">
      <svg viewBox="0 0 120 96" aria-hidden="true">${desk.under || ""}<g class="plus"><circle cx="60" cy="25" r="14"/><path d="M60 18v14M53 25h14"/></g>${desk.over}</svg>
      <div class="label">${t("office.new_session")}</div></button>`;
  }

  /* ---------- hover card ---------- */
  function peekHtml(c) {
    const l = c.live || {};
    const where = [c.project, c.worktree || c.branch].filter(Boolean).join(" / ");
    const since = l.status_since_ms ? waited(l.status_since_ms) : agoBack(c.last_activity);
    let now = "";
    if (c.tasks && c.tasks.length && can(c, "tasks")) {
      const n = 6, at = Math.max(0, c.tasks.findIndex((task) => task.status === "in_progress"));
      const start = Math.max(0, Math.min(at - 2, c.tasks.length - n)), after = c.tasks.length - start - n;
      now = `<h4>${t("office.plan")}</h4>${progressBar(c)}<ul class="tasks">${start ? `<li class="gap">${t("office.tasks_above", { n: start })}</li>` : ""}
        ${taskItems(c.tasks.slice(start, start + n))}${after > 0 ? `<li class="gap">${t("office.tasks_below", { n: after })}</li>` : ""}</ul>`;
    }
    const said = c.recap
      ? `<h4>${esc(c.recap_at ? t("office.recap_at", { when: agoBack(c.recap_at) }) : t("office.recap"))}</h4><p class="clamp">${esc(c.recap)}</p>`
      : c.last_assistant_text ? `<h4>${t("office.agent_wrote")}</h4><p class="clamp">${esc(c.last_assistant_text)}</p>` : "";
    const asked = c.last_prompt
      ? `<h4>${esc(c.last_prompt_at ? t("office.you_wrote_at", { when: agoBack(c.last_prompt_at) }) : t("office.you_wrote"))}</h4><p class="clamp two">${esc(c.last_prompt)}</p>` : "";
    const meta = [c.started_at && t("office.started", { when: agoBack(c.started_at) }), c.n_prompts && t("office.prompts", { n: c.n_prompts }),
      c.n_agents && t("office.subagents", { n: c.n_agents }), c.n_plans && t("office.plans", { n: c.n_plans }),
      spend(c)].filter(Boolean).join(", ");
    return `<h3>${esc(c.title)}</h3><div class="where">${esc(where)}</div>
      <div class="status-line s-${c.column}"><span class="dot"></span>${esc(liveLine(c))}${since ? `, ${esc(since)}` : ""}</div>
      ${c.column === "needs_you" && (l.needs || c.pending_tool || c.open_questions) ? `<div class="callout"><p>${esc(askText(c))}</p></div>` : ""}
      ${c.column === "working" && c.current_task && !now ? `<p class="ask doing">${esc(c.current_task)}</p>` : ""}
      ${now}${said}${asked}${chips(c)}
      ${meta ? `<p class="meta">${esc(meta)}.</p>` : ""}`;
  }
  function drawPeek() {
    const p = $("#peek"), id = peekId;
    const el = id && state.view === "office" && document.querySelector(`#office .desk[data-id="${CSS.escape(id)}"]`);
    const c = el && state.board.cards.find((x) => x.session_id === id);
    if (!c) { hidePeekNow(); return; }
    p.innerHTML = peekHtml(c);
    p.classList.remove("hidden");
    const r = el.getBoundingClientRect(), m = 12, w = p.offsetWidth, h = p.offsetHeight;
    let left = r.right + m;
    if (left + w > innerWidth - m) left = r.left - w - m;
    if (left < m) left = Math.min(Math.max(m, r.left), innerWidth - w - m);
    p.style.left = `${left}px`;
    p.style.top = `${Math.min(Math.max(m, r.top - 10), innerHeight - h - m)}px`;
  }
  let peekTimer;
  function showPeek(id) {
    clearTimeout(peekTimer);
    if (id === peekId) return;
    peekTimer = setTimeout(() => { peekId = id; drawPeek(); }, peekId ? 0 : 160);
  }
  function hidePeekNow() { clearTimeout(peekTimer); peekId = null; $("#peek").classList.add("hidden"); }
  function hidePeek() { clearTimeout(peekTimer); peekTimer = setTimeout(hidePeekNow, 80); }

  SYA.feature({ id: "office", view: { title: t("office.title"), render: renderOffice } });
  $("#prefs").insertAdjacentHTML("beforebegin", `<div id="peek" class="peek hidden" role="tooltip"></div>`);
  const office = $("#office");
  if (matchMedia("(hover: hover)").matches) {
    office.addEventListener("pointerover", (e) => { const d = e.target.closest(".desk"); if (d) showPeek(d.dataset.id); });
    office.addEventListener("pointerout", (e) => {
      const d = e.target.closest(".desk");
      const to = e.relatedTarget instanceof Element ? e.relatedTarget.closest(".desk") : null;
      if (d && (!to || to.dataset.id !== d.dataset.id)) hidePeek();
    });
  }
  office.addEventListener("focusin", (e) => { const d = e.target.closest(".desk"); if (d) showPeek(d.dataset.id); });
  office.addEventListener("click", (e) => {
    const b = e.target.closest(".desk-new");
    if (b) SYA.launch?.open({ cwd: b.dataset.launchCwd });
  });
  office.addEventListener("focusout", hidePeek);
  addEventListener("scroll", () => { if (peekId) hidePeekNow(); }, { passive: true });
  SYA.on("view", hidePeekNow);
  SYA.on("drawer", (id) => { if (id) hidePeekNow(); });
  window.hidePeekNow = hidePeekNow;
})();
