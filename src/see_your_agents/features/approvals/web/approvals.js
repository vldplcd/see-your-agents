// Approvals: a permission prompt, a question or a plan the hook handed to the dashboard, answered from the
// chat panel (SYA.chat.addPanel); the card and the drawer say what waits and lead there. The terminal's dialog
// stays open meanwhile: the first answer wins. Data: card.approvals = [{id, tool, summary, text, plan, questions,
// fields, warn, complete, session}] from /api/board; answers go to POST /api/approvals/<id>/decide. `warn` holds codes and
// an empty `summary` means no title: both are worded here.
// Allow is offered only when the whole input is on screen (`complete`): an allow approves all of it. Answering
// needs a paired tab (SYA.pairing, app/pairing.py): otherwise the buttons are off and "Pair this browser" opens a
// new tab; the tab that confirms is the paired one, until the dashboard restarts. The same choice, and "Unpair all
// tabs", sit in Appearance.
// Globals from app.js: esc, t, postJson, refresh, render, SYA; SYA.chat from chat.js.
(() => {
  const ASK = "AskUserQuestion", PLAN = "ExitPlanMode";
  const current = new Map();  // id -> the request on screen in the chat panel
  const answered = new Map();  // session id -> {text, at}: the page's last answer, shown while the agent moves on
  const ANSWERED_MS = 15000;
  const ARM_MS = 700;  // the set of requests changed: a click meant for the old one must not land on a new one
  const HINT = t("approvals.hint");
  const WHAT = { [ASK]: t("approvals.what.question"), [PLAN]: t("approvals.what.plan") };
  const WAITS = { [ASK]: t("approvals.waits.question"), [PLAN]: t("approvals.waits.plan") };
  const WARN = { no_sandbox: t("approvals.warn.no_sandbox") };
  const lastAnswer = (sid) => { const a = answered.get(sid); return a && Date.now() - a.at < ANSWERED_MS ? a.text : ""; };
  const what = (a) => WHAT[a.tool] || t("approvals.what.permission");
  const summary = (a) => a.summary || (a.tool === PLAN ? t("approvals.untitled") : t("approvals.no_text"));
  const paired = () => SYA.pairing?.paired === true;
  const UNPAIRED = t("approvals.unpaired");
  // Pairing works on http://127.0.0.1 only (localhost may be another process's ::1): elsewhere, a way there.
  const pairAction = (attr) => (SYA.pairing?.local === false
    ? `<a class="approvals-pair" href="${esc(SYA.pairing.home)}">${t("approvals.pair_elsewhere")}</a>`
    : `<button type="button" class="approvals-pair" ${attr}>${t("app.pair.do")}</button>`);
  // A catalog sentence around a command: t(key, {command: CODE}) escaped, the command put in <code> in its place.
  const CODE = "\u0001";
  const withCode = (text, command) => esc(text).replace(CODE, `<code>${esc(command)}</code>`);

  // Pairing: the server opens a new tab with a one-time code; that tab, once confirmed, is the one that answers.
  async function startPairing(msg) {
    const r = await SYA.pairing.start().catch(() => null);
    msg.textContent = r?.ok ? t("approvals.pair_opened") : r?.data?.error || t("app.pair.no_answer");
  }
  function renderPairPrefs() {
    const prefsBox = document.getElementById("prefs");
    if (!prefsBox || !SYA.pairing) return;
    let box = document.getElementById("approvals-pair");
    if (!box) {
      box = document.createElement("fieldset");
      box.id = "approvals-pair";
      prefsBox.append(box);
      box.addEventListener("click", async (e) => {
        const b = e.target.closest("[data-pair]");
        if (!b) return;
        e.stopPropagation();
        const msg = box.querySelector(".approvals-pair-msg");
        if (b.dataset.pair === "start") return startPairing(msg);
        const r = await SYA.pairing.forget().catch(() => null);
        msg.textContent = r?.ok ? t("approvals.unpaired_all") : r?.data?.error || t("approvals.failed");
      });
    }
    const on = paired();
    box.innerHTML = `<legend>${t("approvals.prefs.legend")}</legend>
      <p class="note">${on ? t("approvals.prefs.paired") : UNPAIRED}</p>
      ${on ? `<button class="ghost" type="button" data-pair="forget">${t("approvals.prefs.forget")}</button>` : pairAction('data-pair="start"')}
      <p class="note approvals-pair-msg" aria-live="polite"></p>`;
  }

  const openLink = (sid) =>
    `<a class="approvals-open" href="#chat/${encodeURIComponent(sid)}" data-chat="${esc(sid)}">${t("approvals.answer")}</a>`;
  const warnHtml = (a) => (a.warn || []).map((w) => `<p class="approvals-warn">${esc(WARN[w] || w)}</p>`).join("");

  // A background job already names what it needs on the card (live.needs): then only the way to answer.
  function cardLine(c) {
    const a = c.approvals?.[0];
    if (!a) return "";
    const more = c.approvals.length > 1 ? ` <small>${esc(t("approvals.more", { n: c.approvals.length - 1 }))}</small>` : "";
    const head = c.live?.needs ? `<b>${WAITS[a.tool] || t("approvals.waits.permission")}</b>`
      : `<b>${what(a)}:</b> <span class="approvals-sum">${esc(summary(a))}</span>`;
    return `<p class="approvals-line">${head}${more} ${openLink(c.session_id)}</p>${warnHtml(a)}`;
  }

  function drawerSection(d) {
    if (!d.approvals?.length) return "";
    return `<section class="approvals-drawer"><h4>${t("approvals.drawer.title")}</h4>${d.approvals.map((a) =>
      `<p><b>${what(a)}:</b> ${esc(summary(a))}</p>${warnHtml(a)}`).join("")}<p>${openLink(d.session_id)} ${t("approvals.drawer.where")}</p></section>`;
  }

  function questionsHtml(a) {
    return a.questions.map((q, i) => {
      const type = q.multi ? "checkbox" : "radio";
      const options = q.options.map((o) => `<label class="approvals-opt"><input type="${type}" name="q${i}" value="${esc(o.label)}">
        <span><b>${esc(o.label)}</b>${o.description ? ` <small>${esc(o.description)}</small>` : ""}</span></label>`).join("");
      return `<fieldset class="approvals-q"><legend>${q.header ? `<span class="approvals-tag">${esc(q.header)}</span> ` : ""}${esc(q.question)}</legend>
        ${options}<label class="approvals-opt"><input type="${type}" name="q${i}" value="" data-other>
        <input type="text" class="approvals-other" name="o${i}" maxlength="500" placeholder="${t("approvals.own_answer")}"></label></fieldset>`;
    }).join("");
  }

  // Every other input field, name and value: what an allow also approves. Under a session id, the session it
  // names (a.session: the dashboard's own title and project for that id).
  const sessionHtml = (s) => `<p class="approvals-session"><b>${esc(s.title)}</b>${s.project ? ` <span>${esc(s.project)}</span>` : ""}</p>`;
  const fieldsHtml = (a) => (a.fields?.length ? `<dl class="approvals-fields">${a.fields.map(([k, v]) =>
    `<dt>${esc(k)}</dt><dd><pre>${esc(v)}</pre>${k === "session_id" && a.session ? sessionHtml(a.session) : ""}</dd>`).join("")}</dl>` : "");

  function terminalNote(c) {
    const note = c.live?.job_id
      ? withCode(t("approvals.partial_attach", { command: CODE }), `claude attach ${c.live.job_id}`)
      : t("approvals.partial");
    return `<p class="approvals-partial">${note}</p>`;
  }

  function boxHtml(a, c) {
    const body = a.tool === ASK ? questionsHtml(a)
      : a.plan ? `<div class="approvals-plan md">${SYA.chat?.md ? SYA.chat.md(a.plan) : esc(a.plan)}</div>`
      : a.text ? `<pre class="approvals-text">${esc(a.text)}</pre>` : "";
    const allow = a.tool === ASK ? t("approvals.allow.answer") : a.tool === PLAN ? t("approvals.allow.plan") : t("approvals.allow.allow");
    const why = a.tool === PLAN ? t("approvals.why.plan") : t("approvals.why.other");
    return `<form class="approvals-box" data-ap-id="${esc(a.id)}" data-sid="${esc(c.session_id)}" autocomplete="off">
      <div class="approvals-head"><b>${what(a)}</b><span>${t("approvals.first_wins")}</span></div>
      ${warnHtml(a)}${a.tool === ASK ? "" : `<p class="approvals-sum">${esc(summary(a))}</p>`}
      ${body}${fieldsHtml(a)}${a.complete ? "" : terminalNote(c)}
      ${paired() ? "" : `<p class="approvals-partial">${UNPAIRED}</p><p>${pairAction('data-ap="pair"')}</p>`}
      <div class="approvals-actions">
        ${a.complete ? `<button type="submit" class="approvals-allow">${allow}</button>` : ""}
        <button type="button" class="approvals-deny" data-ap="deny">${t("approvals.deny")}</button>
        <button type="button" class="approvals-link" data-ap="why">${t("approvals.deny_why")}</button>
      </div>
      <div class="approvals-why" hidden><textarea maxlength="2000" rows="2" placeholder="${why}"></textarea>
        <button type="button" class="approvals-deny" data-ap="deny-why">${t("approvals.deny")}</button></div>
      <p class="approvals-msg" aria-live="polite"></p>
    </form>`;
  }

  // Each answered question: the chosen labels (several: comma-separated, as Claude Code joins them) or own text.
  function answers(form, a) {
    const out = {};
    for (const [i, q] of a.questions.entries()) {
      const picked = [...form.querySelectorAll(`input[name="q${i}"]:checked`)].map((el) =>
        el.dataset.other !== undefined ? form.querySelector(`[name="o${i}"]`).value.trim() : el.value).filter(Boolean);
      if (!picked.length) return null;
      out[q.question] = picked.join(", ");
    }
    return out;
  }

  // The server answers only when the hook took the decision; a hook already gone gives 409 with the reason.
  async function decide(form, body) {
    const id = form.dataset.apId, msg = form.querySelector(".approvals-msg");
    form.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    msg.classList.remove("bad");
    msg.textContent = t("approvals.sending");
    const r = await postJson(`/api/approvals/${encodeURIComponent(id)}/decide`, body).catch(() => null);
    if (r?.ok) {  // the board drops the request on its next poll; until the agent moves on, say what left
      const text = body.behavior === "allow" ? t("approvals.sent") : t("approvals.denied");
      answered.set(form.dataset.sid, { text, at: Date.now() });
      msg.textContent = text;
      form.querySelector(".approvals-actions").hidden = true;
      form.querySelector(".approvals-why").hidden = true;
      refresh();
      return;
    }
    msg.textContent = r?.data?.error || t("approvals.not_sent");
    msg.classList.add("bad");
    if (r?.data?.unpaired) { SYA.pairing.check(); return; }  // the panel redraws with "Pair this browser"
    form.querySelectorAll("button").forEach((b) => { b.disabled = r?.status === 409; });  // 409: no longer open
    if (r?.status === 409) refresh();
  }

  function wire(root) {
    const armed = () => Date.now() >= (root.armAt || 0);
    root.addEventListener("submit", (e) => {
      e.preventDefault();
      const form = e.target.closest(".approvals-box"), a = current.get(form?.dataset.apId);
      if (!a || !a.complete || !armed()) return;
      if (a.tool !== ASK) return decide(form, { behavior: "allow" });
      const picked = answers(form, a);
      if (!picked) { form.querySelector(".approvals-msg").textContent = t("approvals.answer_each"); return; }
      decide(form, { behavior: "allow", answers: picked });
    });
    root.addEventListener("click", (e) => {
      const b = e.target.closest("[data-ap]"), form = b?.closest(".approvals-box");
      if (!form || !armed()) return;
      if (b.dataset.ap === "pair") startPairing(form.querySelector(".approvals-msg"));
      else if (b.dataset.ap === "deny") decide(form, { behavior: "deny" });
      else if (b.dataset.ap === "why") {
        const why = form.querySelector(".approvals-why");
        why.hidden = !why.hidden;
        if (!why.hidden) why.querySelector("textarea").focus();
      } else if (b.dataset.ap === "deny-why") {
        const text = form.querySelector(".approvals-why textarea").value.trim();
        if (!text) { form.querySelector(".approvals-why textarea").focus(); return; }
        decide(form, { behavior: "deny", message: text });
      }
    });
    root.addEventListener("focusin", (e) => {  // typing an own answer picks it
      const other = e.target.closest(".approvals-other");
      if (other) other.closest("label").querySelector("[data-other]").checked = true;
    });
  }

  function renderPanel(c, root) {
    const list = c?.approvals || [], done = list.length ? "" : lastAnswer(c?.session_id);
    const key = list.map((a) => a.id).join(",") + done + (paired() ? ":paired" : "");
    if (!root.dataset.wired) { wire(root); root.dataset.wired = "1"; }
    if (root.dataset.key === key) return;  // nothing new: keep what the user has typed or picked
    root.dataset.key = key;
    current.clear();
    for (const a of list) current.set(a.id, a);
    root.innerHTML = list.map((a) => boxHtml(a, c)).join("") || (done ? `<p class="approvals-done">${esc(done)}</p>` : "");
    if (!list.length) return;
    root.armAt = Date.now() + ARM_MS;
    const buttons = [...root.querySelectorAll(".approvals-actions button, .approvals-why button")];
    buttons.forEach((b) => { b.disabled = true; });
    if (paired()) setTimeout(() => { if (root.dataset.key === key) buttons.forEach((b) => { b.disabled = false; }); }, ARM_MS);
  }

  SYA.feature({ id: "approvals", card: cardLine, drawer: drawerSection, prefs: { render: renderPairPrefs } });
  SYA.on("pair", () => { renderPairPrefs(); render(); });
  const hint = (c) => (c?.approvals?.length ? HINT : lastAnswer(c?.session_id));
  SYA.chat?.addPanel({ id: "approvals", render: renderPanel, hint });
})();
