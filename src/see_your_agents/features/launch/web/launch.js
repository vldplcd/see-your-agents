// New session: the «+» in the header opens a dialog that starts a background session of an agent program (Claude
// Code: claude --bg; Codex: codex exec) in a project folder, waits for its card on the board and opens its chat. The
// program choice shows when more than one can start here; the options below it are that program's own.
// Fork (any [data-fork] button: a chat's header, the drawer) does the same from a copy of a
// session's history, after a small confirm popover.
// Server: GET /api/launch (folders, options), GET /api/launch/dirs (suggestions), POST /api/launch, POST
// /api/launch/fork. Strings: launch/i18n/<lang>.json through t(). Globals from app.js: $, esc, t, capital,
// agoBack, getJson, postJson, refresh, openDrawer, state, SYA.
// Exposes SYA.launch.open(prefill) for other features (todo's Start), see open() below.
(() => {
  if (window.SYA_API) return;  // the static demo has no server to start sessions on
  const LAST_KEY = "sya.launch.last";  // {program, by: {program id: the options last picked}}
  const WAIT_MS = 90_000, INDEX_WAIT_MS = 15_000;
  const DEFAULT = t("launch.default");
  // The option keys of the programs' options(); a key not here shows as it is.
  const LABELS = {
    model: t("launch.opt.model"), effort: t("launch.opt.effort"), permission_mode: t("launch.opt.permission_mode"),
    sandbox: t("launch.opt.sandbox"),
  };
  let info = null, prog = null, dialog = null, forkBox = null, waiting = null, suggestTimer = 0;
  let started = null;  // the prefill's callback, until this dialog closes or its start succeeds

  const remembered = () => { try { return JSON.parse(localStorage.getItem(LAST_KEY)) || {}; } catch (e) { return {}; } };
  const remember = (picked) => { try { localStorage.setItem(LAST_KEY, JSON.stringify(picked)); } catch (e) { /* not kept */ } };
  const lastFor = (id) => remembered().by?.[id] || {};  // what was last picked for a program
  const tilde = (p) => (info?.home && (p === info.home || p.startsWith(info.home + "/")) ? "~" + p.slice(info.home.length) : p);
  const q = (s) => dialog.querySelector(s);

  function build() {
    document.body.insertAdjacentHTML("beforeend", `<dialog id="launch" class="launch" aria-labelledby="launch-title">
      <form class="launch-form" novalidate>
        <div class="launch-head"><h2 id="launch-title">${t("launch.title")}</h2>
          <button class="ghost launch-x" type="button" aria-label="${t("app.close")}" title="${t("app.close")}">×</button></div>
        <div class="launch-program hidden"><span>${t("launch.program")}</span><div class="seg" role="group" aria-label="${t("launch.program")}"></div></div>
        <section class="launch-recent" aria-label="${t("launch.recent")}"><h3>${t("launch.recent")}</h3><ul class="launch-folders"><li class="launch-empty">${t("launch.loading")}</li></ul></section>
        <label class="launch-field"><span>${t("launch.field.folder")}</span>
          <input name="cwd" list="launch-dirs" autocomplete="off" spellcheck="false" placeholder="${t("launch.field.folder_placeholder")}"></label>
        <datalist id="launch-dirs"></datalist>
        <label class="launch-field"><span>${t("launch.field.task")}</span>
          <textarea name="prompt" rows="4" maxlength="20000" placeholder="${t("launch.field.task_placeholder")}"></textarea></label>
        <div class="launch-opts"></div>
        <p class="launch-hint"></p>
        <div class="launch-row">
          <label class="launch-field launch-name"><span>${t("launch.field.name")}</span><input name="name" maxlength="80" autocomplete="off" placeholder="${t("launch.field.name_placeholder")}"></label>
          <label class="check launch-wt"><input type="checkbox" name="worktree"> ${t("launch.worktree")}</label>
        </div>
        <p class="launch-error" role="alert"></p>
        <div class="launch-foot"><span class="launch-status" aria-live="polite"></span>
          <button class="launch-go" type="submit">${t("launch.start")}</button></div>
      </form></dialog>`);
    dialog = $("#launch");
    const form = q("form");
    form.addEventListener("submit", submit);
    q(".launch-x").onclick = () => dialog.close();
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });  // the backdrop
    dialog.addEventListener("keydown", (e) => { if (e.key === "Escape") e.stopPropagation(); });  // not the drawer too
    dialog.addEventListener("close", () => {  // closed unstarted: nothing to link, and the note about it goes
      if (!waiting) { started = null; q(".launch-status").textContent = ""; }
    });
    q(".launch-folders").addEventListener("click", (e) => {
      const row = e.target.closest("[data-path]");
      if (!row) return;
      q("[name=cwd]").value = tilde(row.dataset.path);
      markPicked(); error("");
      q("[name=prompt]").focus();
    });
    form.elements.cwd.addEventListener("input", () => { markPicked(); clearTimeout(suggestTimer); suggestTimer = setTimeout(suggest, 150); });
    form.elements.cwd.addEventListener("focus", suggest);
    form.addEventListener("input", (e) => { if (e.target.name !== "cwd") error(""); });
    form.addEventListener("change", (e) => { if (e.target.name === "model") fillSelect("effort", q("[name=effort]")?.value); });
    q(".launch-program .seg").addEventListener("click", (e) => {
      const b = e.target.closest("[data-program]");
      if (b && !waiting) pick(info.programs.find((p) => p.id === b.dataset.program));
    });
  }

  function folderRow(f) {
    const meta = [tilde(f.path), t("launch.folder.sessions", { n: f.sessions }), agoBack(f.last_activity)].filter(Boolean);
    return `<li><button type="button" class="launch-folder" data-path="${esc(f.path)}" aria-pressed="false">
      <b>${esc(f.name)}</b><small>${esc(meta.join(" · "))}</small>${prog.trusted.includes(f.path) ? "" : `<em title="${esc(prog.untrusted_note || t("launch.folder.untrusted_note"))}">${t("launch.folder.untrusted")}</em>`}</button></li>`;
  }
  function renderFolders() {
    q(".launch-folders").innerHTML = info.folders.map(folderRow).join("")
      || `<li class="launch-empty">${t("launch.folders.none")}</li>`;
    markPicked();
  }

  // The program's options as selects, each set to `picked[key]` where the program still offers it.
  const picks = () => Object.fromEntries([...dialog.querySelectorAll(".launch-opts select")].map((s) => [s.name, s.value]));
  function pick(p, picked = lastFor(p.id)) {
    prog = p;
    for (const b of dialog.querySelectorAll("[data-program]")) b.setAttribute("aria-pressed", String(b.dataset.program === p.id));
    q(".launch-opts").innerHTML = Object.keys(p.choices).map((key) =>
      `<label class="launch-field"><span>${esc(LABELS[key] || key)}</span><select name="${esc(key)}"></select></label>`).join("");
    for (const key of Object.keys(p.choices)) fillSelect(key, picked[key]);
    q(".launch-hint").textContent = p.note || "";
    q(".launch-name").classList.toggle("hidden", !p.caps.includes("name"));
    renderFolders();
  }
  // Effort follows the chosen model where the program says which levels each model has. "" is the program's own
  // default: the user's model, named in brackets; for another key the value it stands for, offered once by its name.
  function fillSelect(key, want) {
    const select = q(`.launch-opts [name=${key}]`);
    if (!select) return;
    const name = (v) => prog.labels[v] || v;
    const model = q(".launch-opts [name=model]")?.value || prog.default_model;
    const fixed = key === "model" ? "" : prog.defaults[key] || "";
    const values = ((key === "effort" && prog.efforts[model]) || prog.choices[key]).filter((v) => v !== fixed);
    const first = fixed ? name(fixed) : key === "model" && prog.default_model ? `${DEFAULT} (${name(prog.default_model)})` : DEFAULT;
    select.innerHTML = `<option value="">${esc(first)}</option>`
      + values.map((v) => `<option value="${esc(v)}">${esc(name(v))}</option>`).join("");
    select.value = values.includes(want) ? want : "";
  }

  async function load() {
    const got = await getJson("/api/launch").catch(() => null);  // the options in the page's language
    if (!got) {
      q(".launch-folders").innerHTML = `<li class="launch-empty">${t("launch.folders.no_answer")}</li>`;
      return;
    }
    info = got;
    const progs = info.programs;
    if (!progs.length) {
      q(".launch-folders").innerHTML = `<li class="launch-empty">${t("launch.programs.none")}</li>`;
      return;
    }
    q(".launch-program").classList.toggle("hidden", progs.length < 2);
    q(".launch-program .seg").innerHTML = progs.map((p) => `<button type="button" data-program="${esc(p.id)}">${esc(p.title)}</button>`).join("");
    // A later open keeps the program and what was picked in it; the first one takes the last remembered.
    const keep = prog && progs.find((p) => p.id === prog.id);
    pick(keep || progs.find((p) => p.id === remembered().program) || progs[0], keep ? picks() : undefined);
  }

  function markPicked() {
    const typed = q("[name=cwd]").value.trim();
    for (const b of dialog.querySelectorAll(".launch-folder")) b.setAttribute("aria-pressed", String(tilde(b.dataset.path) === typed || b.dataset.path === typed));
  }

  async function suggest() {
    const typed = q("[name=cwd]").value;
    let dirs = [];
    try { dirs = (await getJson(`/api/launch/dirs?path=${encodeURIComponent(typed)}`))?.dirs || []; } catch (e) { /* no suggestions */ }
    if (q("[name=cwd]").value !== typed) return;  // typed on meanwhile
    // The datalist matches what is typed: offer "~/…" when the path was typed that way.
    q("#launch-dirs").innerHTML = dirs.map((d) => `<option value="${esc(typed.startsWith("/") ? d : tilde(d))}/"></option>`).join("");
  }

  // In the dialog (or the fork popover) while it is open; a launch that fails after it was closed says so in a toast.
  function error(text, field, root = dialog) {
    if (root?.open || !text) {
      if (root) root.querySelector(".launch-error").textContent = text;
      if (field) field.focus();
      return;
    }
    const toast = document.createElement("div");
    toast.className = "launch-toast";
    toast.setAttribute("role", "alert");
    toast.textContent = text;
    toast.onclick = () => toast.remove();
    document.body.append(toast);
    setTimeout(() => toast.remove(), 12_000);
  }
  function busy(on, status = "", root = dialog) {
    if (!root) return;
    root.querySelector(".launch-go").disabled = on;
    root.querySelector(".launch-status").textContent = status;
  }
  function failure(r) {
    return capital(r?.data?.error || (r ? t("launch.failed", { status: r.status }) : t("launch.no_answer")));
  }

  async function submit(e) {
    e.preventDefault();
    if (waiting) return;
    const f = e.target.elements;
    if (!prog) return error(info ? t("launch.programs.none_short") : t("launch.programs.loading"));
    const options = picks();
    const body = {
      provider: prog.id, cwd: f.cwd.value.trim().replace(/(.)\/+$/, "$1"), prompt: f.prompt.value,
      name: prog.caps.includes("name") ? f.name.value.trim() : "", worktree: f.worktree.checked, ...options,
    };
    if (!body.cwd) return error(t("launch.need_folder"), f.cwd);
    if (!body.prompt.trim()) return error(t("launch.need_task"), f.prompt);
    remember({ program: prog.id, by: { ...remembered().by, [prog.id]: options } });
    error("");
    busy(true, t("launch.starting"));
    const r = await postJson("/api/launch", body).catch(() => null);
    if (!r?.ok || !r.data?.ok) {
      busy(false);
      return error(failure(r));
    }
    const onStart = started;
    started = null;
    wait(r.data, dialog, { onStart });
  }

  // `root`: the dialog or the fork popover that started it; `idle`: a fork without a first message; `onStart`:
  // the prefill's callback, told the new session's id once.
  function wait(launched, root, { idle = false, onStart = null } = {}) {
    waiting = {
      job: launched.job_id, sid: launched.session_id, name: launched.name, until: Date.now() + WAIT_MS, root, idle,
      started: onStart,
    };
    if (waiting.sid) tell(waiting, waiting.sid);
    waiting.timer = setInterval(() => check(state.board), 1000);  // the card may be on a board polled meanwhile
    busy(true, launched.name ? t("launch.started_named", { name: launched.name }) : t("launch.started"), root);
    refresh();
  }

  // The board shows the new session once `claude agents` lists it, and its transcript a few seconds later (the
  // index refreshes every 5 s): then its chat opens on the first exchange and the dialog closes. A fork without a
  // first message writes no transcript until it gets one: its live card is all there is to wait for.
  function check(board) {
    if (!waiting) return;
    const w = waiting, card = board?.cards?.find((c) => (w.job && c.live?.job_id === w.job) || (w.sid && c.session_id === w.sid));
    if (card) { w.seen ||= Date.now(); tell(w, card.session_id); }
    if (!card && Date.now() > w.until) {
      stopWaiting();
      return error(w.name ? t("launch.not_on_board_named", { name: w.name }) : t("launch.not_on_board"), null, w.root);
    }
    if (!card || (!w.idle && !card.started_at && Date.now() - w.seen < INDEX_WAIT_MS)) return;  // no transcript yet
    stopWaiting();
    w.root?.close();
    if (w.root === dialog) reset();
    if (SYA.chat) SYA.chat.open(card.session_id); else openDrawer(card.session_id);
  }
  function tell(w, sid) {  // once: the caller's callback must not break the wait
    const fn = w.started;
    w.started = null;
    try { fn?.(sid); } catch (e) { /* the caller's own problem */ }
  }
  function stopWaiting() {
    clearInterval(waiting.timer);
    const root = waiting.root;
    waiting = null;
    busy(false, "", root);
  }
  function reset() {
    const f = q("form").elements;
    f.prompt.value = ""; f.name.value = ""; f.worktree.checked = false;
    q(".launch-status").textContent = "";
    error("");
  }

  // prefill (from another feature): {cwd, prompt, name, worktree, note, started(sessionId)}. Each field replaces
  // what the dialog holds, a missing one empties it; `note` stands by Start. `started` is called once with
  // the new session's id after a start from this dialog; closing the dialog unstarted drops it. A launch that is
  // still waiting for its card keeps the dialog as it is.
  function open(prefill) {
    if (!dialog) build();
    if (!dialog.open) dialog.showModal();
    if (!waiting) q(".launch-error").textContent = "";  // a refusal from a previous try is stale
    load();
    const f = q("form").elements;
    if (prefill && !waiting) {
      f.cwd.value = prefill.cwd || ""; f.prompt.value = prefill.prompt || ""; f.name.value = prefill.name || "";
      f.worktree.checked = !!prefill.worktree;
      q(".launch-status").textContent = prefill.note || "";
      started = typeof prefill.started === "function" ? prefill.started : null;
      markPicked();
    }
    (f.cwd.value ? f.prompt : f.cwd).focus();
  }

  /* ---------- Fork: a confirm popover by the button that asked for it ---------- */
  function buildFork() {
    document.body.insertAdjacentHTML("beforeend", `<dialog class="launch launch-fork" aria-labelledby="launch-fork-title">
      <form class="launch-form" novalidate>
        <div class="launch-head"><h2 id="launch-fork-title">${t("launch.fork.title")}</h2>
          <button class="ghost launch-x" type="button" aria-label="${t("app.close")}" title="${t("app.close")}">×</button></div>
        <p class="launch-fork-note"></p>
        <label class="launch-field"><span>${t("launch.fork.first_message")}</span>
          <textarea name="prompt" rows="3" maxlength="20000" placeholder="${t("launch.fork.optional")}"></textarea></label>
        <p class="launch-hint">${t("launch.fork.hint")}</p>
        <p class="launch-error" role="alert"></p>
        <div class="launch-foot"><span class="launch-status" aria-live="polite"></span>
          <button class="launch-go" type="submit">${t("launch.fork.go")}</button></div>
      </form></dialog>`);
    forkBox = document.body.lastElementChild;
    forkBox.querySelector("form").addEventListener("submit", submitFork);
    forkBox.querySelector(".launch-x").onclick = () => forkBox.close();
    forkBox.addEventListener("click", (e) => { if (e.target === forkBox) forkBox.close(); });  // outside the box
    forkBox.addEventListener("keydown", (e) => {
      if (e.key === "Escape") e.stopPropagation();  // not the drawer under it too
      else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); forkBox.querySelector("form").requestSubmit(); }
    });
    forkBox.addEventListener("input", () => { if (!waiting) error("", null, forkBox); });
  }

  function openFork(button) {
    if (!forkBox) buildFork();
    const sid = button.dataset.fork, card = state.board?.cards.find((c) => c.session_id === sid);
    if (forkBox.dataset.sid !== sid) forkBox.querySelector("[name=prompt]").value = "";  // a message for another session
    forkBox.dataset.sid = sid;
    forkBox.querySelector(".launch-fork-note").textContent = t("launch.fork.note", { title: card?.title || sid.slice(0, 8) });
    if (!waiting) { error("", null, forkBox); busy(false, "", forkBox); }
    if (!forkBox.open) forkBox.showModal();
    // under the button, inside the window; above it when there is no room below
    const r = button.getBoundingClientRect(), w = forkBox.offsetWidth, h = forkBox.offsetHeight;
    forkBox.style.left = `${Math.max(12, Math.min(r.right - w, innerWidth - w - 12))}px`;
    forkBox.style.top = `${r.bottom + 8 + h <= innerHeight - 12 ? r.bottom + 8 : Math.max(12, r.top - h - 8)}px`;
    forkBox.querySelector("[name=prompt]").focus();
  }

  async function submitFork(e) {
    e.preventDefault();
    if (waiting) return error(t("launch.fork.wait"), null, forkBox);
    const prompt = e.target.elements.prompt.value;
    error("", null, forkBox);
    busy(true, t("launch.fork.forking"), forkBox);
    const r = await postJson("/api/launch/fork", { session_id: forkBox.dataset.sid, prompt }).catch(() => null);
    if (!r?.ok || !r.data?.ok) {
      busy(false, "", forkBox);
      return error(failure(r), null, forkBox);
    }
    e.target.elements.prompt.value = "";
    wait(r.data, forkBox, { idle: !prompt.trim() });
  }

  // Any [data-fork="<session id>"]: Fork in a chat's header (chat.js) and in the drawer (app.js).
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-fork]");
    if (b) { e.preventDefault(); openFork(b); }
  });

  SYA.feature({
    id: "launch",
    header: `<button class="ghost icon launch-btn" id="launch-btn" type="button" aria-haspopup="dialog" aria-label="${t("launch.title")}" title="${t("launch.title")}">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>`,
    onBoard: check,
  });
  $("#launch-btn").onclick = () => open();
  SYA.launch = { open };
})();
