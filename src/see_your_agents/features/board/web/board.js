// Board: sessions in columns by what they need from you; the home view. Other features add lines to a card
// with their `card` slot. Strings: board/i18n/<lang>.json through t(). Globals from app.js: $, esc, t, state,
// prefs, savePrefs, ago, waited, spend, contextFill, contextText, askText, chips, progressBar, peerName,
// visibleCards, SYA.
(() => {
  const ARCHIVE_PAGE = 25;
  let archiveShown = ARCHIVE_PAGE;

  // The page names the columns by id: /api/board's own titles are core's, in one language.
  const COLUMN = {
    needs_you: t("board.column.needs_you"), working: t("board.column.working"), your_move: t("board.column.your_move"),
    recent: t("board.column.recent"), archive: t("board.column.archive"),
  };
  const EMPTY = {
    needs_you: t("board.empty.needs_you"), working: t("board.empty.working"), your_move: t("board.empty.your_move"),
    recent: t("board.empty.recent"), archive: t("board.empty.archive"),
  };
  const LANE_HINT = {
    recent: (days) => t("board.hint.recent", { n: days }),
    archive: () => t("board.hint.archive"),
  };

  function peerLine(c) {
    const p = c.peer;
    if (!p) return "";
    const out = p.dir === "out";
    const title = out ? t("board.peer.sent", { text: p.text }) : t("board.peer.received", { text: p.text });
    return `<p class="peer" title="${esc(title)}"><span class="arrow">${out ? "→" : "←"}</span>
      <span class="p-text"><b>${esc(peerName(p.who))}:</b> ${esc(p.text)}</span><time>${esc(ago(p.at))}</time></p>`;
  }

  function cardHtml(c) {
    const where = [c.project, c.worktree || c.branch].filter(Boolean).join(" / ");
    const l = c.live || {};
    let ask = "";
    if (c.column === "needs_you") ask = `<p class="ask">${esc(askText(c))}</p>`;
    else if (c.column === "working" && c.current_task) ask = `<p class="ask doing">${esc(c.current_task)}</p>`;
    let text = c.recap || c.last_assistant_text || c.last_prompt;
    if (text && text.startsWith(c.title.replace(/…$/, ""))) text = "";  // stub cards are titled by their recap
    let age = c.column === "needs_you" && l.status_since_ms
      ? `<span class="waited">${esc(t("board.card.waiting", { time: waited(l.status_since_ms) }))}</span>` : esc(ago(c.last_activity));
    if (c.column === "recent") age += ` <small>· ${esc(t("board.card.archive_in", { when: archiveIn(c) }))}</small>`;
    const cost = spend(c), ctx = contextFill(c);
    // "context 63%" next to the cost, marked from 80 %: the session is close to compacting
    const fill = ctx == null ? "" : ` · <span class="ctx${ctx >= 80 ? " ctx-high" : ""}" title="${esc(t("board.card.context_title", { text: contextText(c) }))}">${t("board.card.context", { pct: ctx })}</span>`;
    // The card is a button, so its own actions are spans; the keyboard path to them is the drawer and the chat.
    const toArchive = c.column === "recent" ? `<span class="card-act" data-archive="${esc(c.session_id)}" title="${t("board.card.archive_now")}">${t("board.card.archive")}</span>` : "";
    return `<button class="card c-${c.column}" data-id="${esc(c.session_id)}">
      <h3>${esc(c.title)}</h3>
      <div class="where" title="${esc(c.cwd)}">${esc(where)}</div>
      ${chips(c)}${ask}
      ${c.column === "archive" ? "" : `<p class="recap">${esc(text)}</p>`}
      ${peerLine(c)}${SYA.collect("card", c)}
      ${progressBar(c)}
      <div class="foot"><span class="foot-l">${age}${cost ? ` · ${esc(cost)}` : ""}${fill}</span>
        <span class="foot-r">${toArchive}<a class="chat-link" href="#chat/${encodeURIComponent(c.session_id)}" data-chat="${esc(c.session_id)}">${t("board.card.chat")}</a></span></div>
    </button>`;
  }

  // Closed sessions leave «recent» for the archive this long after their last activity.
  function archiveIn(c) {
    const left = Date.parse(c.last_activity) + (state.board.recent_days || 3) * 864e5 - Date.now();
    const h = Math.max(1, Math.round(left / 36e5));
    return h < 24 ? t("board.in_hours", { n: h }) : t("board.in_days", { n: Math.round(h / 24) });
  }

  function renderBoard() {
    const cards = visibleCards(), days = state.board.recent_days || 3;
    $("#board").classList.toggle("arch-folded", !prefs.archiveOpen);
    $("#board").innerHTML = state.board.columns.map((col) => {
      let list = cards.filter((c) => c.column === col.id);
      const total = list.length, title = esc(COLUMN[col.id] || col.title);
      if (col.id === "archive" && !prefs.archiveOpen) {
        return `<section class="lane lane-archive folded"><button class="arch-fold" data-arch-toggle aria-expanded="false" title="${t("board.archive.show")}">
          <b>${total}</b>${title}<small>${t("board.archive.folded")}</small></button></section>`;
      }
      let more = "";
      if (col.id === "archive" && total > archiveShown) {
        list = list.slice(0, archiveShown);
        more = `<button class="ghost more" id="more">${t("board.archive.more", { n: total - archiveShown })}</button>`;
      }
      const fold = col.id === "archive" ? `<button class="ghost lane-fold" data-arch-toggle aria-expanded="true">${t("board.archive.fold")}</button>` : "";
      return `<section class="lane lane-${col.id === "needs_you" ? "needs" : col.id}">
        <h2><span class="n">${total}</span>${title}${fold}</h2>
        ${LANE_HINT[col.id] ? `<p class="lane-hint">${esc(LANE_HINT[col.id](days))}</p>` : ""}
        <div class="cards">${list.map(cardHtml).join("") || `<p class="empty">${EMPTY[col.id] || ""}</p>`}</div>${more}
      </section>`;
    }).join("");
    const m = $("#more");
    if (m) m.onclick = () => { archiveShown += ARCHIVE_PAGE; renderBoard(); };
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-arch-toggle]")) { prefs.archiveOpen = !prefs.archiveOpen; savePrefs(); renderBoard(); }
  });
  SYA.feature({ id: "board", view: { title: t("board.title"), render: renderBoard } });
})();
