// Keys: ⌘K / Ctrl+K opens a quick switcher over the board already on the page (sessions, the supervisor and the
// chat's sources such as groups), Enter opens the chat; ? lists every shortcut of the page. Both are <dialog>s.
// Single keys stay quiet while you type in a field; ⌘K works everywhere. Strings: keys/i18n/<lang>.json through
// t(). Globals from app.js: esc, t, state, SYA, ago, openDrawer; from chat (optional): SYA.chat.open,
// SYA.chat.sourceItems.
(() => {
  const MAC = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || "");
  const MOD = MAC ? "⌘" : "Ctrl", ALT = MAC ? "⌥" : "Alt", MOD_K = MAC ? "⌘K" : "Ctrl+K";
  const ORDER = ["needs_you", "working", "your_move", "recent", "archive"];
  const MAX = 50;
  // [title, feature id that brings them ("" — the page itself), [[keys, what]]]; "+" joins keys, " / " separates
  // alternatives; the words are keys.* strings. Kept by hand: grep keydown in web/ and features/ when a shortcut
  // changes.
  const SHORTCUTS = [
    [t("keys.section.everywhere"), "", [
      [`${MOD}+K`, t("keys.do.switch")],
      ["?", t("keys.do.help")],
      ["/", t("keys.do.search")],
      ["Esc", t("keys.do.close")],
    ]],
    [t("keys.section.switch"), "", [["↑ / ↓", t("keys.do.select")], ["Enter", t("keys.do.open_chat")]]],
    [t("keys.section.chat"), "chat", [
      [`${ALT}+↑ / ${ALT}+↓`, t("keys.do.prev_next_chat")],
      ["Enter", t("keys.do.send")],
      ["Shift+Enter", t("keys.do.new_line")],
      ["R", t("keys.do.quote")],
      ["@", t("keys.do.mention")],
    ]],
    [t("keys.section.todo"), "todo", [
      ["Enter", t("keys.do.edit_todo")],
      ["!", t("keys.do.urgent")],
    ]],
  ];
  const SEARCH = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg>`;
  const GROUP = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="9" r="3"/><circle cx="16.5" cy="10" r="2.3"/><path d="M3.5 19a5.5 5.5 0 0111 0M14.5 15.3a4.3 4.3 0 016 3.7"/></svg>`;
  let sw = null, help = null, rows = [], at = 0;

  /* ---------- what to switch to: the board on the page and the chat's sources ---------- */
  const refOf = (p) => `${String(p.url).includes("merge_requests") ? "!" : "#"}${p.number}`;
  function sessionEntry(c, sup = false) {
    const tags = [...(c.jira || []).map((j) => j.key), ...(c.prs || []).map(refOf)];
    return {
      key: c.session_id, kind: "session", title: c.title, column: c.column, sup, when: ago(c.last_activity),
      sub: [c.project, c.worktree || c.branch, ...tags].filter(Boolean).join(" · "), tags: tags.map((tag) => tag.toLowerCase()),
      hay: [c.title, c.live?.name, c.project, c.branch, c.worktree, ...tags, sup ? `${t("keys.supervisor")} supervisor` : ""].join(" ").toLowerCase(),
    };
  }
  function entries() {
    const cards = state.board?.cards || [], supId = state.board?.supervisor_id;
    const sup = supId && cards.find((c) => c.session_id === supId);
    let sources = [];
    try { sources = SYA.chat?.sourceItems?.() || []; } catch (e) { /* the chat's problem, not the switcher's */ }
    return [
      ...(sup ? [sessionEntry(sup, true)] : []),
      ...sources.map((it) => ({ key: it.key, kind: "source", title: it.title, sub: "", when: "", tags: [], hay: String(it.title).toLowerCase() })),
      ...ORDER.flatMap((col) => cards.filter((c) => c.column === col && c.session_id !== supId).map((c) => sessionEntry(c))),
    ];
  }
  // Every word must match; an exact Jira key or MR ref comes first, then titles that start with the first word.
  function found(query) {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean), all = entries();
    if (!words.length) return all.filter((e) => e.column !== "archive").slice(0, MAX);
    const rank = (e) => (e.tags.includes(words[0]) ? 0 : e.title.toLowerCase().startsWith(words[0]) ? 1 : 2);
    return all.filter((e) => words.every((w) => e.hay.includes(w))).sort((a, b) => rank(a) - rank(b)).slice(0, MAX);  // stable
  }

  /* ---------- the switcher ---------- */
  function rowHtml(e, i) {
    const mark = e.kind === "source" ? `<span class="keys-mark keys-icon">${GROUP}</span>`
      : `<span class="keys-mark s-${esc(e.column)}"><span class="dot"></span></span>`;
    return `<li class="keys-opt" role="option" id="keys-opt-${i}" data-i="${i}" aria-selected="false">${mark}
      <b>${esc(e.title)}${e.sup ? ` <span class="keys-tag">${t("keys.supervisor")}</span>` : ""}</b><time>${esc(e.when)}</time>
      ${e.sub ? `<small>${esc(e.sub)}</small>` : ""}</li>`;
  }
  function markActive() {
    const input = sw.querySelector("input");
    sw.querySelectorAll(".keys-opt").forEach((li) => li.setAttribute("aria-selected", String(+li.dataset.i === at)));
    if (rows.length) input.setAttribute("aria-activedescendant", `keys-opt-${at}`); else input.removeAttribute("aria-activedescendant");
    sw.querySelector(`#keys-opt-${at}`)?.scrollIntoView({ block: "nearest" });
  }
  function paint() {
    sw.querySelector(".keys-list").innerHTML = rows.map(rowHtml).join("")
      || `<li class="keys-empty">${t("keys.nothing")}</li>`;
    markActive();
  }
  function go(e) {
    if (!e) return;
    sw.close();
    if (SYA.chat) SYA.chat.open(e.key); else if (e.kind === "session") openDrawer(e.key);
  }
  function wire(dialog) {
    dialog.addEventListener("keydown", (e) => { if (e.key === "Escape") e.stopPropagation(); });  // not the drawer or the search too
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });  // the backdrop
  }
  function buildSwitch() {
    document.body.insertAdjacentHTML("beforeend", `<dialog class="keys-dialog keys-switch" aria-label="${t("keys.switch")}">
      <div class="keys-search">${SEARCH}<input type="text" role="combobox" aria-expanded="true" aria-controls="keys-list"
        aria-autocomplete="list" aria-label="${t("keys.find")}" placeholder="${t("keys.find_placeholder")}"
        autocomplete="off" spellcheck="false"></div>
      <ul class="keys-list" id="keys-list" role="listbox" aria-label="${t("keys.where")}"></ul>
      <p class="keys-foot"><span><kbd>↑</kbd><kbd>↓</kbd> ${t("keys.foot.select")}</span><span><kbd>Enter</kbd> ${t("keys.foot.open_chat")}</span><span><kbd>Esc</kbd> ${t("keys.foot.close")}</span></p>
    </dialog>`);
    sw = document.body.lastElementChild;
    const input = sw.querySelector("input"), list = sw.querySelector(".keys-list");
    input.addEventListener("input", () => { rows = found(input.value); at = 0; paint(); });
    input.addEventListener("keydown", (e) => {
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && rows.length) {
        e.preventDefault();
        at = (at + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length;
        markActive();
      } else if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); go(rows[at]); }
    });
    list.addEventListener("click", (e) => { const li = e.target.closest("[data-i]"); if (li) go(rows[+li.dataset.i]); });
    list.addEventListener("pointermove", (e) => {
      const li = e.target.closest("[data-i]");
      if (li && +li.dataset.i !== at) { at = +li.dataset.i; markActive(); }
    });
    wire(sw);
  }
  function openSwitch() {
    if (!sw) buildSwitch();
    const input = sw.querySelector("input");
    input.value = ""; rows = found(""); at = 0;
    paint();
    sw.showModal();
    input.focus();
  }

  /* ---------- the list of shortcuts ---------- */
  const caps = (keys) => keys.split(" / ").map((alt) => alt.split("+").map((k) => `<kbd>${esc(k)}</kbd>`).join(""))
    .join(`<span class="keys-or">/</span>`);
  function buildHelp() {
    const has = (id) => !id || SYA.features.some((f) => f.id === id);
    const sections = SHORTCUTS.filter(([, id]) => has(id)).map(([title, , list]) => `<section><h3>${esc(title)}</h3><dl>${
      list.map(([keys, what]) => `<dt>${caps(keys)}</dt><dd>${esc(what)}</dd>`).join("")}</dl></section>`).join("");
    document.body.insertAdjacentHTML("beforeend", `<dialog class="keys-dialog keys-help" aria-labelledby="keys-help-title"><div class="keys-body">
      <div class="keys-head"><h2 id="keys-help-title">${t("keys.help.title")}</h2>
        <button class="ghost keys-x" type="button" aria-label="${t("app.close")}" title="${t("app.close")}">×</button></div>
      ${sections}<p class="keys-note">${esc(t("keys.help.note", { key: MOD_K }))}</p>
    </div></dialog>`);
    help = document.body.lastElementChild;
    help.querySelector(".keys-x").onclick = () => help.close();
    wire(help);
  }
  function openHelp() {
    if (!help) buildHelp();
    help.showModal();
  }

  const typing = (el) => !!el?.closest?.("input, textarea, select, [contenteditable]");
  const otherDialog = (...mine) => [...document.querySelectorAll("dialog[open]")].some((d) => !mine.includes(d));
  document.addEventListener("keydown", (e) => {
    if (e.isComposing || e.defaultPrevented || e.altKey) return;
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && ["k", "K", "л", "Л"].includes(e.key)) {  // л: K on a Russian layout
      if (otherDialog(sw, help)) return;  // New session and the like keep the keyboard
      e.preventDefault();
      if (help?.open) help.close();
      if (sw?.open) sw.close(); else openSwitch();
    } else if (e.key === "?" && !e.metaKey && !e.ctrlKey && !typing(e.target) && !otherDialog(help)) {
      e.preventDefault();
      if (help?.open) help.close(); else openHelp();
    }
  });

  SYA.feature({ id: "keys" });
})();
