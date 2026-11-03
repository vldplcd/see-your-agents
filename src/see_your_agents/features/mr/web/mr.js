// MR: the GitLab state of a session's MRs (card field `mr`): marks on its MR chips, an "MR merged" badge on a live
// card whose MRs are all merged, a drawer section. The server words its errors (`error`, `off`) in the page's
// language. Strings: mr/i18n/<lang>.json through t(). Globals from app.js: esc, t, capital, ago, SYA.
(() => {
  const STATE = {
    opened: t("mr.state.opened"), merged: t("mr.state.merged"), closed: t("mr.state.closed"), locked: t("mr.state.locked"),
  };
  const PIPE = {
    success: t("mr.pipe.success"), failed: t("mr.pipe.failed"), running: t("mr.pipe.running"),
    pending: t("mr.pipe.pending"), created: t("mr.pipe.created"), waiting_for_resource: t("mr.pipe.waiting_for_resource"),
    preparing: t("mr.pipe.preparing"), scheduled: t("mr.pipe.scheduled"), manual: t("mr.pipe.manual"),
    canceled: t("mr.pipe.canceled"), canceling: t("mr.pipe.canceling"), skipped: t("mr.pipe.skipped"),
  };
  const PIPE_KIND = {
    success: "ok", failed: "bad", running: "run", pending: "wait", created: "wait", waiting_for_resource: "wait",
    preparing: "wait", scheduled: "wait", manual: "wait", canceled: "off", canceling: "off", skipped: "off",
  };
  const svg = (cls, body) => `<svg class="mr-i ${cls}" viewBox="0 0 16 16" aria-hidden="true">${body}</svg>`;
  const ICON = {
    merged: svg("mr-c-ok", '<circle cx="4.5" cy="3.5" r="1.8"/><circle cx="4.5" cy="12.5" r="1.8"/><circle cx="11.5" cy="8" r="1.8"/><path d="M4.5 5.3v5.4M4.5 5.6c0 2.2 1.6 2.4 5.2 2.4"/>'),
    closed: svg("mr-c-off", '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>'),
    draft: svg("mr-c-off", '<path d="M3 13l.8-3.2 7-7 2.4 2.4-7 7z"/>'),
    ok: svg("mr-c-ok", '<circle cx="8" cy="8" r="6"/><path d="M5.4 8.2l1.8 1.8 3.5-3.7"/>'),
    bad: svg("mr-c-bad", '<circle cx="8" cy="8" r="6"/><path d="M6 6l4 4M10 6l-4 4"/>'),
    run: svg("mr-c-wait", '<circle cx="8" cy="8" r="6"/><path class="mr-fill" d="M8 2a6 6 0 0 1 0 12z"/>'),
    wait: svg("mr-c-wait", '<circle cx="8" cy="8" r="6"/><path d="M8 5v3.3l2.2 1.4"/>'),
    off: svg("mr-c-off", '<circle cx="8" cy="8" r="6"/><path d="M4 12l8-8"/>'),
    thumb: (cls) => svg(cls, '<path d="M5 7.2V13H2.6V7.2zM5 7.2l2.6-4.6c1 0 1.7.8 1.5 1.8l-.4 2.1h3.6c.8 0 1.4.8 1.2 1.6l-1.1 4.1c-.2.5-.6.8-1.1.8H5"/>'),
    bubble: svg("mr-c-wait", '<path d="M2.5 3.5h11v7.2H7.6L4.6 13v-2.3H2.5z"/>'),
  };

  const itemOf = (c, url) => c.mr?.items?.find((i) => i.url === url);
  // The state goes into class names: only the ones GitLab has, anything else is «unknown».
  const kind = (it) => (!Object.hasOwn(STATE, it.state) ? "unknown" : it.state === "opened" && it.draft ? "draft" : it.state);
  const webUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : "");
  const stateText = (it) => (it.state === "opened" && it.draft ? t("mr.state.draft") : STATE[it.state] || it.state);
  const open = (it) => it.state === "opened";

  function approvals(it) {
    if (it.approved) return t("mr.approved");
    if (it.approvals_left > 0) {
      const given = Math.max(0, it.approvals_required - it.approvals_left);
      return t("mr.approvals", { given, required: it.approvals_required });
    }
    return it.approved === null ? t("mr.approvals_unknown") : "";
  }
  function threads(it) {
    if (it.unresolved === null) return t("mr.threads_unknown");
    return it.unresolved ? t("mr.threads", { n: it.unresolved }) : "";
  }
  const unknown = (it) => (it.error ? t("mr.no_data", { error: it.error }) : t("mr.no_status"));
  const checked = (ts) => { const span = ago(ts); return span === t("app.time.now") ? t("mr.checked_now") : t("mr.checked", { span }); };

  function marks(it) {
    if (!it.state) return `<span class="mr-marks"><span class="mr-q">${it.error ? "?" : "…"}</span></span>`;
    const m = [];
    if (it.state === "merged") m.push(ICON.merged);
    else if (!open(it)) m.push(ICON.closed);
    else {
      if (it.draft) m.push(ICON.draft);
      if (PIPE_KIND[it.pipeline]) m.push(ICON[PIPE_KIND[it.pipeline]]);
      if (it.approved) m.push(ICON.thumb("mr-c-ok"));
      else if (it.approvals_left > 0) {
        m.push(`<span class="mr-n mr-c-wait">${ICON.thumb("mr-c-wait")}${Math.max(0, it.approvals_required - it.approvals_left)}/${it.approvals_required}</span>`);
      }
      if (it.unresolved > 0) m.push(`<span class="mr-n mr-c-wait">${ICON.bubble}${it.unresolved}</span>`);
    }
    if (it.error) m.push(`<span class="mr-q">?</span>`);
    return `<span class="mr-marks">${m.join("")}</span>`;
  }

  function tip(it) {
    const lines = [`!${it.iid}${it.title ? ` ${it.title}` : ""}`];
    if (!it.state) return [...lines, unknown(it)].join("\n");
    lines.push(t("mr.tip.state", { state: stateText(it) }));
    if (open(it)) {
      lines.push(t("mr.tip.pipeline", { pipeline: PIPE[it.pipeline] || it.pipeline || t("mr.tip.no_pipeline") }));
      for (const line of [approvals(it), threads(it)]) if (line) lines.push(capital(line));
    }
    if (it.error) lines.push(t("mr.tip.stale", { error: it.error }));
    return lines.join("\n");
  }

  function chip(p, c) {
    const it = itemOf(c, p.url);
    if (!it || c.mr.off || it.off) return null;
    return { cls: `mr-chip mr-${kind(it)}`, html: marks(it), title: tip(it) };
  }

  function card(c) {
    if (!c.live || !c.mr?.all_merged) return "";
    return `<p class="mr-badge-line"><span class="mr-badge">${ICON.merged}${c.mr.items.length > 1 ? t("mr.badge_many") : t("mr.badge_one")}</span></p>`;
  }

  function row(it) {
    const facts = [];
    if (it.off) facts.push(`<span class="mr-err">${esc(it.off)}</span>`);
    else if (!it.state) facts.push(`<span class="${it.error ? "mr-err" : ""}">${esc(unknown(it))}</span>`);
    else {
      if (open(it)) {
        const pk = PIPE_KIND[it.pipeline];
        facts.push(`<span>${pk ? ICON[pk] : ""}${esc(t("mr.pipeline", { pipeline: PIPE[it.pipeline] || it.pipeline || t("mr.pipeline_never") }))}</span>`);
        const a = approvals(it);
        if (a) facts.push(`<span>${ICON.thumb(it.approved ? "mr-c-ok" : "mr-c-wait")}${esc(a)}</span>`);
        const th = threads(it);
        facts.push(`<span>${th ? `${ICON.bubble}${esc(th)}` : t("mr.threads_none")}</span>`);
      }
      if (it.error) facts.push(`<span class="mr-err">${esc(t("mr.stale", { error: it.error }))}</span>`);
      if (it.checked_at) facts.push(`<span class="mr-when">${esc(checked(it.checked_at))}</span>`);
    }
    return `<li class="mr-row">
      <div class="mr-head">${webUrl(it.url) ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">!${esc(it.iid)}</a>` : `!${esc(it.iid)}`}
        ${it.state ? `<span class="mr-state mr-s-${kind(it)}">${esc(stateText(it))}</span>` : ""}
        <span class="mr-title">${esc(it.title)}</span></div>
      ${facts.length ? `<div class="mr-facts">${facts.join("")}</div>` : ""}</li>`;
  }

  function drawer(d) {
    if (!d.mr) return "";
    const head = `<h4>${t("mr.drawer.title")}</h4>`;
    if (d.mr.off) return `<section class="mr-sec">${head}<p class="mr-note">${esc(t("mr.drawer.off", { reason: d.mr.off }))}</p></section>`;
    return `<section class="mr-sec">${head}<ul class="mr-list">${d.mr.items.map(row).join("")}</ul>
      <p class="mr-note">${t("mr.drawer.note")}</p></section>`;
  }

  SYA.feature({ id: "mr", chip, card, drawer });
})();
