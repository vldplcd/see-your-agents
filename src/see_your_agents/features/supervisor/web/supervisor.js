// Supervisor: its digest above the board and the office, hidden until a newer one arrives. The board payload
// carries supervisor_id and digest (Feature.board_extra). Strings: supervisor/i18n/<lang>.json through t().
// Globals from app.js: $, esc, t, state, agoBack, SYA.
(() => {
  const DIGEST_KEY = "sya.digest.dismissed";
  function digestDismissed(at) {
    try { return localStorage.getItem(DIGEST_KEY) === at; } catch (e) { return false; }
  }
  function renderDigest() {
    const el = $("#digest"), d = state.board.digest;
    const show = !!d && !!d.text && ["board", "office"].includes(state.view) && !digestDismissed(d.at);
    el.classList.toggle("hidden", !show);
    if (!show) { el.innerHTML = ""; return; }
    const sid = state.board.supervisor_id;
    el.innerHTML = `<div class="digest-head"><b>${t("supervisor.digest.title")}</b><time>${esc(agoBack(d.at))}</time></div>
      <p class="digest-text">${esc(d.text)}</p>
      <div class="digest-acts">${sid ? `<button class="ghost digest-open" type="button" data-chat="${esc(sid)}">${t("supervisor.digest.open")}</button>` : ""}
        <button class="ghost digest-x" type="button" aria-label="${t("supervisor.digest.hide")}" title="${t("supervisor.digest.hide_title")}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>`;
    el.querySelector(".digest-x").onclick = () => {
      try { localStorage.setItem(DIGEST_KEY, d.at); } catch (e) { /* hidden until reload */ }
      el.classList.add("hidden"); el.innerHTML = "";
    };
  }
  SYA.feature({
    id: "supervisor",
    top: `<section id="digest" class="digest hidden" aria-label="${t("supervisor.digest.title")}"></section>`,
    render: renderDigest,
  });
})();
