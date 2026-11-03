// "Reply to this": select text inside one agent message of the chat panel (a session's thread or a source's
// feed, such as a group) and a button over the selection puts it into that conversation's composer as a Markdown
// quote; R (the same key in the Russian layout) does the same from the keyboard. Works on the DOM chat.js and
// sources draw: .msg.k-* messages and the .composer textarea of the same pane. Globals: t (i18n.js).
(() => {
  const AGENT = ".msg.k-agent, .msg.k-peer_in, .msg.k-plan, .msg.k-recap";  // what agents wrote, not you
  const KEYS = ["r", "R", "к", "К"];  // R, and the key in its place on the Russian layout
  const ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 8L5 12.5 10 17"/><path d="M5.5 12.5H14a5 5 0 015 5V19"/></svg>`;
  let btn = null, picked = null, held = false, timer = 0;

  const elementOf = (n) => (n?.nodeType === 1 ? n : n?.parentElement);
  // The selection when it lies in one agent message of the chat panel and that pane has a composer to write in.
  function selected() {
    const sel = getSelection();
    if (!sel?.rangeCount || sel.isCollapsed) return null;
    const msg = elementOf(sel.anchorNode)?.closest(AGENT);
    if (!msg || msg !== elementOf(sel.focusNode)?.closest(AGENT) || !msg.closest("#chat")) return null;
    const ta = msg.closest(".chat-thread, .chat-source")?.querySelector(".composer textarea");
    const text = sel.toString().replace(/\r/g, "").replace(/\n{3,}/g, "\n\n").trim();
    if (!ta || ta.disabled || !text) return null;
    return { text, ta, rect: sel.getRangeAt(0).getBoundingClientRect(), view: msg.closest(".chat-scroll") || msg };
  }

  function quote({ text, ta }) {
    const block = text.split("\n").map((l) => `> ${l}`.trimEnd()).join("\n");  // an empty line stays ">"
    const before = ta.value.replace(/\s+$/, "");
    ta.value = `${before ? `${before}\n\n` : ""}${block}\n\n`;
    ta.dispatchEvent(new Event("input", { bubbles: true }));  // the composer's own draft, height and hint
    getSelection().removeAllRanges();
    hide();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    ta.scrollTop = ta.scrollHeight;
  }

  function hide() {
    picked = null;
    if (btn) btn.hidden = true;
  }
  function place() {
    picked = held ? null : selected();
    if (!picked) return hide();
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button"; btn.className = "chat-quote"; btn.title = t("chat.quote.title");
      btn.innerHTML = `${ICON}<span>${t("chat.quote.button")}</span>`;
      btn.addEventListener("pointerdown", (e) => e.preventDefault());  // keep the selection and the focus where they are
      btn.addEventListener("click", () => { if (picked) quote(picked); });
      document.body.append(btn);
    }
    // Over the selection, or under it at the top of the feed; scrolled out of the feed, the button waits unseen.
    const r = picked.rect, v = picked.view.getBoundingClientRect();
    btn.hidden = r.bottom < v.top || r.top > v.bottom;
    if (btn.hidden) return;
    const w = btn.offsetWidth, h = btn.offsetHeight;
    const top = r.top - h - 8 >= Math.max(v.top, 0) ? r.top - h - 8 : Math.min(r.bottom + 8, v.bottom - h - 4);
    btn.style.top = `${top}px`;
    btn.style.left = `${Math.max(8, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 8))}px`;
  }
  const soon = () => { clearTimeout(timer); timer = setTimeout(place, 120); };

  // Shown once the mouse lets go (not under a dragging pointer), moved with scrolling, gone with the selection.
  document.addEventListener("selectionchange", soon);
  document.addEventListener("pointerdown", (e) => { if (e.target !== btn && !btn?.contains(e.target)) held = true; });
  document.addEventListener("pointerup", () => { held = false; soon(); });
  document.addEventListener("scroll", () => { if (picked) place(); }, { capture: true, passive: true });
  addEventListener("resize", () => { if (picked) place(); });
  document.addEventListener("keydown", (e) => {
    if (!KEYS.includes(e.key) || e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.defaultPrevented) return;
    if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
    const sel = selected();
    if (!sel) return;
    e.preventDefault();
    quote(sel);
  });
})();
