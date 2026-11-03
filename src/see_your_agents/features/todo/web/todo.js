// To-do: the todo list in the right dock. Server routes: /api/todo and /api/todo/morning (todo.py); the
// header badge reads board.todo. Strings: todo/i18n/<lang>.json through t(); the server's errors come worded
// in the page's language. Globals from app.js: $, esc, t, state, getJson, postJson, refresh, render, SYA; from
// launch (optional): SYA.launch.open, "Start" on an item.
(() => {
  const API = "/api/todo";
  const SENT_SHOWN_MS = 20000;
  let data = null;          // last GET /api/todo
  let sig = "";
  let loading = false;
  let editing = false;      // an item's text is being edited: don't repaint the list under the cursor
  let doneOpen = false;
  let newUrgent = false;
  let note = "";            // the morning button's local message (before the server has a verdict)
  let watch = 0;

  const items = (pred) => (data?.items || []).filter(pred);

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const d = await getJson(API);
      if (d) take(d);
    } catch (e) { /* the next tick retries */ }
    loading = false;
  }
  function take(d) {
    const s = JSON.stringify(d);
    data = d;
    if (s === sig) return;
    sig = s;
    paint();
    refresh();  // the board's counts follow the list
  }
  async function act(body) {
    const r = await postJson(API, body);
    if (r.ok) take(r.data); else flash(r.data?.error || t("todo.failed"));
    return r.ok;
  }

  let flashTimer = 0;
  function flash(text) {
    const el = $("#todo-msg");
    if (!el) return;
    el.textContent = text;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
  }

  function chipsOf(it) {
    const jira = it.jira.map((j) => j.url
      ? `<a class="chip chip-jira" href="${esc(j.url)}" target="_blank" rel="noopener noreferrer">${esc(j.key)}</a>`
      : `<span class="chip chip-jira">${esc(j.key)}</span>`);
    const sid = it.session_id
      ? `<button class="chip todo-session" type="button" data-chat="${esc(it.session_id)}" title="${t("todo.session_chat")}">${esc(it.session_title || t("todo.session"))}</button>`
      : "";
    const by = it.source === "supervisor" ? `<span class="todo-by" title="${t("todo.by_supervisor_title")}">${t("todo.by_supervisor")}</span>` : "";
    return jira.join("") + sid + by;
  }
  const ARROW = {
    up: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V6M6 11l6-6 6 6"/></svg>`,
    down: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v13M6 13l6 6 6-6"/></svg>`,
  };
  const PLAY = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l10-6.5z"/></svg>`;
  function itemHtml(it) {
    const meta = chipsOf(it), move = it.urgent ? t("todo.make_normal") : t("todo.make_urgent");
    return `<li class="todo-item${it.done ? " done" : ""}" data-id="${esc(it.id)}">
      <input class="todo-check" type="checkbox" ${it.done ? "checked" : ""} aria-label="${t("todo.done_check")}">
      <div class="todo-main"><span class="todo-text" tabindex="0" title="${t("todo.click_to_edit")}">${esc(it.text)}</span>
        ${meta ? `<div class="todo-meta">${meta}</div>` : ""}</div>
      <div class="todo-acts">
        ${it.done || !SYA.launch ? "" : `<button class="todo-act todo-launch" type="button" aria-label="${t("todo.launch")}" title="${t("todo.launch_title")}">${PLAY}</button>`}
        ${it.done ? "" : `<button class="todo-act todo-move" type="button" aria-label="${move}" title="${move}">${it.urgent ? ARROW.down : ARROW.up}</button>`}
        <button class="todo-act todo-del" type="button" aria-label="${t("todo.delete")}" title="${t("todo.delete")}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
      </div></li>`;
  }
  function section(title, list, cls) {
    if (!list.length) return "";
    return `<section class="todo-sec ${cls}"><h3>${title} <span>${list.length}</span></h3><ul>${list.map(itemHtml).join("")}</ul></section>`;
  }
  function paint() {
    const box = $("#todo-list");
    if (!box || editing || !data) return;
    const open = items((i) => !i.done);
    const done = items((i) => i.done).sort((a, b) => (b.done_at || "").localeCompare(a.done_at || ""));
    const hint = !data.items.length
      ? `<p class="todo-empty">${t("todo.empty")}</p>`
      : !open.length ? `<p class="todo-empty">${t("todo.none_open")}</p>` : "";
    box.innerHTML = hint
      + section(t("todo.urgent"), open.filter((i) => i.urgent), "urgent")
      + section(t("todo.rest"), open.filter((i) => !i.urgent), "rest")
      + (done.length ? `<details class="todo-done"${doneOpen ? " open" : ""}>
          <summary><span>${t("todo.done")} <span class="todo-n">${done.length}</span></span>
            <button class="ghost todo-clear" type="button">${t("todo.clear")}</button></summary>
          <ul>${done.map(itemHtml).join("")}</ul></details>` : "");
    paintMorning();
  }

  // The morning button: the server answers 202 at once; the verdict arrives through GET /api/todo.
  function morningLine() {
    const m = data?.morning;
    if (note) return note;
    if (m?.state === "sending") return t("todo.morning.sending");
    if (m?.state === "error") return m.error || t("todo.morning.not_sent");
    if (m?.state === "sent" && Date.now() - Date.parse(m.at) < SENT_SHOWN_MS) return t("todo.morning.sent");
    return "";
  }
  function paintMorning() {
    const el = $("#todo-morning-note"), btn = $("#todo-morning");
    if (!el) return;
    const line = morningLine();
    el.textContent = line;
    el.classList.toggle("bad", data?.morning?.state === "error" || !!note);
    btn.disabled = data?.morning?.state === "sending";
  }
  async function morning() {
    const sid = state.board?.supervisor_id;
    note = "";
    const r = sid ? await postJson(`${API}/morning`, { session_id: sid }) : null;
    if (!r || !r.ok) {
      note = r?.data?.error || t("todo.not_running");
      setTimeout(() => { note = ""; paintMorning(); }, 6000);
    }
    paintMorning();
    if (r?.ok) {  // follow the courier until it has a verdict
      clearInterval(watch);
      let left = 40;
      watch = setInterval(async () => {
        await load();
        paintMorning();
        if (data?.morning?.state !== "sending" || --left <= 0) {
          clearInterval(watch);
          setTimeout(paintMorning, SENT_SHOWN_MS + 200);
        }
      }, 1500);
    }
  }

  // "Start": the "New session" dialog filled from the item; the session it starts gets attached to the item.
  // The folder: where the latest session with the item's Jira key worked, above its worktree (…/.claude/worktrees/x).
  const aboveHidden = (path) => {
    const parts = path.split("/"), i = parts.findIndex((p) => p.startsWith("."));
    return i < 0 ? path : parts.slice(0, i).join("/");
  };
  const slug = (text) => text.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(Boolean).slice(0, 4).join("-").slice(0, 40).replace(/-+$/, "");
  function launchFor(it) {
    const key = it.jira[0]?.key || "", sup = state.board?.supervisor_id;
    const seen = key && state.board?.cards.find((c) => c.session_id !== sup && c.cwd && c.jira.some((j) => j.key === key));
    const rest = it.text.split(/\s+/).filter((w) => w.replace(/[.,;:!?]+$/, "") !== key).join(" ");  // the key moves first
    SYA.launch.open({
      cwd: seen ? aboveHidden(seen.cwd) : "",
      prompt: !key || it.text.startsWith(key) ? it.text : `${key}: ${rest}`,
      name: key ? key.toLowerCase() : slug(it.text),
      worktree: !!key,
      note: t("todo.launch_note"),
      started: (sid) => act({ action: "update", id: it.id, session_id: sid }),
    });
  }

  function edit(span, it) {
    editing = true;
    const input = document.createElement("textarea");
    input.className = "todo-edit"; input.value = it.text; input.rows = 2; input.maxLength = 500;
    input.setAttribute("aria-label", t("todo.item_text"));
    span.replaceWith(input);
    input.focus();
    let over = false;
    const finish = async (save) => {
      if (over) return;
      over = true; editing = false;
      const text = input.value.trim();
      if (save && text && text !== it.text) await act({ action: "update", id: it.id, text });
      else if (save && !text) flash(t("todo.text_empty"));
      paint();
    };
    input.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") finish(false);
    };
    input.onblur = () => finish(true);
  }

  function build(root) {
    root.innerHTML = `<div class="dock-title"><h2>${t("todo.title")}</h2>
        <button class="ghost dock-x" type="button" aria-label="${t("todo.hide")}" title="${t("todo.hide")}"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
      <form class="todo-form" autocomplete="off">
        <input name="text" maxlength="501" placeholder="${t("todo.new_placeholder")}" aria-label="${t("todo.new")}">
        <button class="ghost todo-urg" type="button" aria-pressed="false" title="${t("todo.urgent_title")}">${t("todo.urgent")}</button>
        <button class="ghost todo-add" type="submit">${t("todo.add")}</button></form>
      <div class="todo-morning"><button class="ghost" id="todo-morning" type="button" title="${t("todo.morning.title")}">${t("todo.morning.button")}</button>
        <span id="todo-morning-note" role="status"></span></div>
      <p id="todo-msg" class="todo-msg" role="status"></p>
      <div id="todo-list" class="todo-list"></div>`;
    const form = root.querySelector(".todo-form"), urg = root.querySelector(".todo-urg");
    urg.onclick = () => { newUrgent = !newUrgent; urg.setAttribute("aria-pressed", String(newUrgent)); };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const input = form.elements.text;
      const text = input.value.trim();
      if (!text) return;
      if (await act({ action: "add", text, urgent: newUrgent })) {
        input.value = ""; newUrgent = false; urg.setAttribute("aria-pressed", "false");
      }
    };
    root.querySelector("#todo-morning").onclick = morning;
    const list = root.querySelector("#todo-list");
    const itemOf = (el) => data?.items.find((i) => i.id === el.closest(".todo-item")?.dataset.id);
    list.addEventListener("click", (e) => {
      const el = e.target;  // not `t`: that name is the strings' function
      if (el.closest(".todo-clear")) { e.preventDefault(); act({ action: "clear_done" }); return; }
      const it = itemOf(el);
      if (!it) return;
      if (el.closest(".todo-del")) act({ action: "delete", id: it.id });
      else if (el.closest(".todo-launch")) launchFor(it);
      else if (el.closest(".todo-move")) act({ action: "update", id: it.id, urgent: !it.urgent });
      else if (el.closest(".todo-text") && !it.done) edit(el.closest(".todo-text"), it);
    });
    list.addEventListener("change", (e) => {
      const it = e.target.closest(".todo-check") && itemOf(e.target);
      if (it) act({ action: "update", id: it.id, done: e.target.checked });
    });
    list.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.matches(".todo-text")) { const it = itemOf(e.target); if (it && !it.done) edit(e.target, it); }
    });
    list.addEventListener("toggle", (e) => { if (e.target.matches(".todo-done")) doneOpen = e.target.open; }, true);
  }

  function draw(root) {  // every tick while the dock is open: build once, then refetch and repaint on change
    if (!root.firstElementChild) build(root);
    load();
    paint();
  }

  function onBoard(board) {
    const btn = $("#dock-right-btn");
    if (btn && board.todo) btn.classList.toggle("todo-calm", !board.todo.urgent);
  }

  SYA.feature({
    id: "todo",
    right: {
      button: t("todo.title"), title: t("todo.dock_title"), label: t("todo.title"),
      icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7l2 2 3-3.5M4 16.5l2 2 3-3.5M12 7.5h8M12 17h8"/></svg>`,
      render: draw,
      badge: () => state.board?.todo?.urgent || state.board?.todo?.open || "",
    },
    onBoard,
  });
})();
