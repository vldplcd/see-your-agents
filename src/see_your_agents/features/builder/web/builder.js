// Builder: a banner under the header with the dashboard's own builds — what the builder is doing, a preview,
// the diff, Rework, Accept, Roll back, Reject. Data: state.board.builder (Feature.board_extra); a build's
// `error` comes worded in the page's language.
// #builder/<id>/diff opens a build's diff. Globals from app.js: $, esc, t, state, postJson, agoBack, render, refresh, SYA.
(() => {
  const HIDDEN_KEY = "sya.builder.hidden";
  const LABEL = {
    building: t("builder.status.building"), checking: t("builder.status.checking"), ready: t("builder.status.ready"),
    failed: t("builder.status.failed"), accepted: t("builder.status.accepted"), rolled_back: t("builder.status.rolled_back"),
  };
  const RESTARTING = { accept: t("builder.restarting.accepted"), rollback: t("builder.restarting.rolled_back") };
  const RESTART_YOURSELF = { accept: t("builder.restart_yourself.accepted"), rollback: t("builder.restart_yourself.rolled_back") };
  const busy = {}, notes = {}, drafts = {}, forms = {};  // per build id: action in flight, last message, Rework text and form
  let shownSig = "", restarting = "";

  function hiddenIds() {
    try { return JSON.parse(localStorage.getItem(HIDDEN_KEY)) || []; } catch (e) { return []; }
  }
  function hide(id) {
    try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...hiddenIds(), id].slice(-50))); } catch (e) { /* until reload */ }
  }
  const builds = () => (state.board?.builder?.builds || []).filter((b) => !hiddenIds().includes(b.id));
  const preview = () => state.board?.builder?.preview || null;

  function statusLine(b) {
    const chatLink = (label) => `<a href="#chat/${esc(b.session_id)}">${label}</a>`;
    if (b.status === "building" && b.waiting) return `${t("builder.waiting")} · ${chatLink(t("builder.open_chat"))}`;
    if (b.status === "building" && b.question) {
      return `${esc(t("builder.asks", { question: b.question }))} · ${chatLink(t("builder.answer_in_chat"))}`;
    }
    if (b.status === "failed") return esc(t("builder.failed", { error: b.error || t("builder.no_reason") }));
    if (b.status === "accepted") return esc(t("builder.accepted_into", { ago: agoBack(b.accepted_at), branch: b.base_branch }));
    if (b.status === "rolled_back") return b.error ? esc(t("builder.rolled_back_because", { error: b.error })) : LABEL.rolled_back;
    const chat = b.session_id && b.status === "building" ? ` · ${chatLink(t("builder.chat"))}` : "";
    return esc(LABEL[b.status] || b.status) + chat;
  }

  function actions(b) {
    const off = busy[b.id] ? "disabled" : "";
    const btn = (act, label, cls = "") => `<button class="ghost ${cls}" type="button" data-act="${act}" ${off}>${label}</button>`;
    const pv = preview()?.id === b.id;
    const out = [];
    if (["ready", "failed"].includes(b.status)) out.push(pv ? btn("stop-preview", t("builder.act.stop_preview")) : btn("preview", t("builder.act.preview")));
    if (b.status !== "rolled_back") out.push(btn("diff", t("builder.act.diff")));
    if (["building", "ready", "failed"].includes(b.status)) out.push(btn("comment", t("builder.act.comment")));
    if (b.status === "ready") out.push(btn("accept", t("builder.act.accept"), "builder-go"));
    if (b.status === "accepted") out.push(btn("rollback", t("builder.act.rollback")));
    if (["building", "checking", "ready", "failed"].includes(b.status)) out.push(btn("reject", t("builder.act.reject")));
    if (b.status === "rolled_back") out.push(btn("hide", t("builder.act.hide")));
    return out.join("");
  }

  function rowHtml(b) {
    const pv = preview()?.id === b.id ? ` · ${t("builder.preview_link")} <a href="${esc(preview().url)}" target="_blank" rel="noopener">${esc(preview().url)}</a>` : "";
    const note = notes[b.id] ? `<p class="builder-note">${esc(notes[b.id])}</p>` : "";
    const tail = b.checks?.tail && !b.checks.ok && ["failed", "rolled_back"].includes(b.status)  // rolled back: uv sync
      ? `<details class="builder-tail"><summary>${b.status === "failed" ? t("builder.tail.checks") : t("builder.tail.sync")}</summary><pre>${esc(b.checks.tail)}</pre></details>` : "";
    const form = forms[b.id] ? `<form class="builder-form"><textarea name="text" rows="2" maxlength="8000"
        placeholder="${t("builder.comment.placeholder")}">${esc(drafts[b.id] || "")}</textarea>
        <button class="ghost builder-go" type="submit" ${busy[b.id] ? "disabled" : ""}>${busy[b.id] === "comment" ? t("builder.comment.sending") : t("builder.comment.send")}</button></form>` : "";
    const asks = b.waiting || (b.status === "building" && b.question);
    return `<li class="builder-row s-${esc(b.status)}${asks ? " waiting" : ""}" data-id="${esc(b.id)}">
      <div class="builder-text"><b class="builder-title">${esc(b.title)}</b>
        <span class="builder-status">${statusLine(b)}${pv}</span>${note}${tail}</div>
      <div class="builder-acts">${actions(b)}</div>${form}</li>`;
  }

  function draw() {
    const el = $("#builder-banner"), list = builds();
    diffTitle();
    const sig = JSON.stringify([list, preview(), busy, notes, forms, restarting, Math.floor(Date.now() / 60000)]);
    if (sig === shownSig) return;
    shownSig = sig;
    const show = list.length || restarting;
    el.classList.toggle("hidden", !show);
    if (!show) { el.innerHTML = ""; return; }
    const focused = document.activeElement?.closest?.("#builder-banner textarea") ? document.activeElement.closest("[data-id]")?.dataset.id : null;
    el.innerHTML = `<div class="builder-head"><b>${t("builder.title")}</b>${restarting ? `<span class="builder-restart">${esc(restarting)}</span>` : ""}</div>
      <ul class="builder-list">${list.map(rowHtml).join("")}</ul>`;
    if (focused) el.querySelector(`[data-id="${CSS.escape(focused)}"] textarea`)?.focus();
  }

  async function act(id, action, body = {}) {
    busy[id] = action; delete notes[id]; draw();
    const r = await postJson(`/api/builder/${encodeURIComponent(id)}/${action}`, body).catch(() => null);
    delete busy[id];
    if (!r?.ok) notes[id] = r?.data?.error || t("builder.no_answer");
    return r;
  }

  async function onClick(e) {
    const b = e.target.closest("[data-act]");
    const row = e.target.closest("[data-id]");
    if (!b || !row) return;
    const id = row.dataset.id, build = builds().find((x) => x.id === id);
    if (!build) return;
    switch (b.dataset.act) {
      case "preview": {
        const tab = window.open("about:blank", "_blank");  // opened inside the click, filled when the preview answers
        const r = await act(id, "preview");
        if (r?.ok && tab) tab.location = r.data.preview.url;
        else tab?.close();
        break;
      }
      case "stop-preview":
        busy[id] = "stop"; draw();
        await postJson("/api/builder/stop-preview", {}).catch(() => null);
        delete busy[id];
        break;
      case "diff": openDiff(id); return;
      case "comment": forms[id] = !forms[id]; break;
      case "hide": hide(id); break;
      case "accept":
        if (!confirm(t("builder.confirm.accept", { title: build.title, branch: build.base_branch }))) return;
        afterRestart(await act(id, "accept"), "accept");
        break;
      case "rollback":
        if (!confirm(t("builder.confirm.rollback", { title: build.title }))) return;
        afterRestart(await act(id, "rollback"), "rollback");
        break;
      case "reject":
        if (!confirm(t("builder.confirm.reject", { title: build.title, branch: build.branch }))) return;
        await act(id, "reject");
        break;
    }
    draw(); refresh();
  }

  function afterRestart(r, action) {
    if (!r?.ok) return;
    restarting = r.data.restart ? RESTARTING[action] : RESTART_YOURSELF[action];
  }

  async function onSubmit(e) {
    const form = e.target.closest(".builder-form");
    if (!form) return;
    e.preventDefault();
    const id = form.closest("[data-id]").dataset.id;
    const r = await act(id, "comment", { text: form.elements.text.value });
    if (r?.ok) { delete drafts[id]; delete forms[id]; notes[id] = t("builder.sent"); }
    draw(); refresh();
  }

  /* ---------- the diff ---------- */
  function diffModal() {
    let m = $("#builder-diff");
    if (m) return m;
    document.body.insertAdjacentHTML("beforeend", `<div id="builder-diff" class="builder-modal hidden" role="dialog" aria-modal="true" aria-labelledby="builder-diff-title">
      <header><b id="builder-diff-title"></b><button class="ghost" type="button" data-close>${t("app.close")}</button></header>
      <div class="builder-diff-body"></div></div>`);
    m = $("#builder-diff");
    m.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) m.classList.add("hidden"); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") m.classList.add("hidden"); });
    return m;
  }
  const lineClass = (l) => l.startsWith("diff --git") ? "file" : l.startsWith("@@") ? "hunk"
    : /^(\+\+\+|---|index |new file|deleted file|similarity|rename )/.test(l) ? "meta" : l[0] === "+" ? "add" : l[0] === "-" ? "del" : "";
  function diffTitle() {  // a diff opened by link before the board arrived shows the id until then
    const m = $("#builder-diff");
    if (!m || m.classList.contains("hidden")) return;
    const b = (state.board?.builder?.builds || []).find((x) => x.id === m.dataset.id);
    $("#builder-diff-title").textContent = t("builder.diff.title", { title: b ? b.title : m.dataset.id });
  }
  async function openDiff(id) {
    const m = diffModal(), body = m.querySelector(".builder-diff-body");
    m.dataset.id = id;
    m.classList.remove("hidden");
    diffTitle();
    body.innerHTML = `<p class="builder-note">${t("builder.diff.loading")}</p>`;
    // not getJson: a refusal's own words ({ok: false, error}) are what to show, in the page's language
    const d = await fetch(`/api/builder/${encodeURIComponent(id)}/diff`, { headers: { "X-SYA-Lang": SYA.lang } })
      .then((r) => r.json()).catch(() => null);
    if (!d?.ok) { body.innerHTML = `<p class="builder-note">${esc(d?.error || t("builder.diff.unavailable"))}</p>`; return; }
    const patch = d.patch.split("\n").map((l) => { const c = lineClass(l); return c ? `<span class="builder-${c}">${esc(l)}</span>` : esc(l); }).join("\n");
    body.innerHTML = `<pre class="builder-stat">${esc(d.stat || t("builder.diff.empty"))}</pre>
      ${d.truncated ? `<p class="builder-note">${t("builder.diff.truncated")}</p>` : ""}<pre class="builder-patch">${patch}</pre>`;
  }

  SYA.feature({
    id: "builder",
    top: `<section id="builder-banner" class="builder hidden" aria-label="${t("builder.title")}"></section>`,
    render: draw,
    route: (arg) => { const [id, what] = arg.split("/"); if (id && what === "diff") openDiff(id); },
  });
  const root = $("#builder-banner");
  root.addEventListener("click", onClick);
  root.addEventListener("submit", onSubmit);
  root.addEventListener("input", (e) => { const row = e.target.closest("[data-id]"); if (row && e.target.name === "text") drafts[row.dataset.id] = e.target.value; });
})();
