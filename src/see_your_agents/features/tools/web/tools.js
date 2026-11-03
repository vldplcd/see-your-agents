// Tools: catalog of skills, subagents, MCP servers, plugins and hooks; the view #tools/<section>.
// Server route: /api/tools (toolbox.py): built-in items' descriptions come worded in the page's language. Demo
// page: window.SYA_API.tools (demo/demo-api.js). Strings: tools/i18n/<lang>.json through t().
(() => {
  const SECTIONS = [
    { key: "skills", title: t("tools.section.skills"), count: (n) => t("tools.count.skills", { n }), hint: t("tools.hint.skills") },
    { key: "agents", title: t("tools.section.agents"), count: (n) => t("tools.count.agents", { n }), hint: t("tools.hint.agents") },
    { key: "mcp", title: t("tools.section.mcp"), count: (n) => t("tools.count.mcp", { n }), hint: t("tools.hint.mcp") },
    { key: "plugins", title: t("tools.section.plugins"), count: (n) => t("tools.count.plugins", { n }), hint: t("tools.hint.plugins") },
    { key: "commands", title: t("tools.section.commands"), count: (n) => t("tools.count.commands", { n }), hint: t("tools.hint.commands") },
    { key: "hooks", title: t("tools.section.hooks"), count: (n) => t("tools.count.hooks", { n }), hint: t("tools.hint.hooks") },
  ];
  const SOURCES = [
    { key: "own", title: t("tools.filter.own"), match: ["user", "local"] },
    { key: "project", title: t("tools.filter.project"), match: ["project"] },
    { key: "plugin", title: t("tools.filter.plugin"), match: ["plugin"] },
    { key: "builtin", title: t("tools.filter.builtin"), match: ["builtin", "connector"] },
  ];
  const SOURCE_LABEL = {
    user: t("tools.source.user"), local: t("tools.source.local"), project: t("tools.source.project"),
    plugin: t("tools.source.plugin"), builtin: t("tools.source.builtin"), connector: t("tools.source.connector"),
  };
  const FRESH_MS = 30000;

  let catalog = null;
  let fetchedAt = 0;
  let loading = false;
  let failed = false;
  const LAYOUT_KEY = "sya.tools.layout";
  const readLayout = () => {
    try { return localStorage.getItem(LAYOUT_KEY) === "cards" ? "cards" : "list"; } catch (err) { return "list"; }
  };
  const saveLayout = (v) => { try { localStorage.setItem(LAYOUT_KEY, v); } catch (err) { /* storage unavailable */ } };
  const ui = { query: "", sources: new Set(), sort: "usage", layout: readLayout(), expanded: new Set(), neverOpen: false };

  const root = () => document.getElementById("tools");
  const calls = (n) => t("tools.calls", { n });
  const lastUsed = (ts) => t("tools.last_used", { when: agoBack(ts) });

  async function load() {
    if (loading || (catalog && Date.now() - fetchedAt < FRESH_MS)) return;
    loading = true;
    try {
      const next = await (window.SYA_API?.tools ? window.SYA_API.tools() : getJson("/api/tools"));
      if (!next) throw new Error("no catalog");
      const changed = JSON.stringify(next) !== JSON.stringify(catalog);
      catalog = next; failed = false;
      fetchedAt = Date.now();
      if (changed && isActive()) draw();
    } catch (err) {
      failed = !catalog;
      if (isActive()) draw();
    } finally {
      loading = false;
    }
  }
  const isActive = () => state.view === "tools";

  function sectionKey() {
    return SECTIONS.some((s) => s.key === state.viewArg) ? state.viewArg : "skills";
  }

  function matches(item, q) {
    if (!q) return true;
    const hay = [item.name, item.description, item.plugin, item.project, item.host, item.command, item.event, item.matcher]
      .filter(Boolean).join(" ").toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  }
  function inSources(item) {
    if (!ui.sources.size) return true;
    return SOURCES.some((s) => ui.sources.has(s.key) && s.match.includes(item.source));
  }
  function ordered(items) {
    const byName = (a, b) => a.name.localeCompare(b.name, SYA.lang);
    const used = (it) => it.usage?.count || 0;
    return [...items].sort(ui.sort === "name" ? byName : (a, b) => used(b) - used(a) || byName(a, b));
  }

  /* ---------- markup ---------- */
  const badge = (item) => {
    const owner = item.plugin || item.project;
    const extra = item.also_in ? ` +${item.also_in}` : "";
    return `<span class="tl-badge tl-src-${esc(item.source)}">${SOURCE_LABEL[item.source] || esc(item.source)}</span>`
      + (owner ? `<span class="tl-owner">${esc(owner)}${extra}</span>` : "");
  };

  function usageHtml(item) {
    const u = item.usage;
    if (!u) return "";
    if (!u.count) return `<div class="tl-use tl-never">${t("tools.never_called")}</div>`;
    const sessions = (u.sessions || []).map((s) =>
      `<a href="#chat/${encodeURIComponent(s.session_id)}" class="tl-sess" data-sid="${esc(s.session_id)}" title="${esc(s.title)}">${esc(s.title)}<i>${s.count}</i></a>`).join("");
    return `<div class="tl-use"><b>${calls(u.count)}</b><span> · ${esc(lastUsed(u.last))}</span></div>
      ${sessions ? `<div class="tl-sessions">${sessions}</div>` : ""}`;
  }

  function metaHtml(item) {
    const kv = (k, v) => v ? `<div class="tl-kv"><span>${k}</span><code>${esc(v)}</code></div>` : "";
    const list = (k, arr) => arr && arr.length ? `<div class="tl-kv"><span>${k}</span><span class="tl-tags">${arr.map((x) => `<code>${esc(x)}</code>`).join("")}</span></div>` : "";
    if (item.kind === "mcp") {
      const run = item.url || [item.command, ...(item.args || [])].filter(Boolean).join(" ");
      const tools = (item.tools || []).map((tool) => `${tool.name} ×${tool.count}`);
      return kv(t("tools.meta.transport"), item.transport) + kv(item.url ? t("tools.meta.address") : t("tools.meta.command"), run)
        + list(t("tools.meta.env"), item.env_names) + list(t("tools.meta.headers"), item.header_names) + list(t("tools.meta.tools"), tools);
    }
    if (item.kind === "agent") {
      return kv(t("tools.meta.model"), item.model) + list(t("tools.meta.tools"), item.tools);
    }
    if (item.kind === "plugin") {
      const c = item.counts || {};
      const inside = [
        c.skills && t("tools.inside.skills", { n: c.skills }), c.agents && t("tools.inside.agents", { n: c.agents }),
        c.commands && t("tools.inside.commands", { n: c.commands }), c.mcp && t("tools.inside.mcp", { n: c.mcp }),
      ].filter(Boolean).join(", ");
      return kv(t("tools.meta.marketplace"), item.marketplace) + kv(t("tools.meta.version"), item.version)
        + kv(t("tools.meta.inside"), inside || t("tools.meta.empty"));
    }
    if (item.kind === "command") return kv(t("tools.meta.arguments"), item.argument_hint);
    return "";
  }

  function hookHtml(item) {
    return `<article class="tl-item tl-hook">
      <div class="tl-head">${badge(item)}</div>
      <div class="tl-flow"><b>${esc(item.event)}</b><span>→</span><em>${esc(item.matcher || t("tools.any"))}</em><span>→</span><code>${esc(item.command || "—")}</code></div>
      ${copyHtml(item)}</article>`;
  }

  const copyHtml = (item) => item.path
    ? `<button class="tl-copy ghost" data-path="${esc(item.path)}" title="${esc(item.path)}">${t("tools.copy_path")}</button>` : "";

  function itemHtml(item) {
    if (item.kind === "hook") return hookHtml(item);
    const long = (item.description || "").length > 150;
    const open = ui.expanded.has(item.id);
    const off = item.enabled === false ? `<span class="tl-off">${t("tools.off")}</span>` : "";
    return `<article class="tl-item${item.enabled === false ? " tl-disabled" : ""}" data-tool="${esc(item.id)}">
      <div class="tl-head"><h3 class="tl-name">${esc(item.name)}</h3>${badge(item)}${off}</div>
      ${item.description ? `<p class="tl-desc${open ? "" : " tl-clamp"}">${esc(item.description)}</p>` : ""}
      ${long ? `<button class="tl-more" data-tool="${esc(item.id)}">${open ? t("tools.collapse") : t("tools.show_all")}</button>` : ""}
      ${usageHtml(item)}
      <div class="tl-meta">${metaHtml(item)}</div>
      ${copyHtml(item)}</article>`;
  }

  function toggleExpanded(id) {
    ui.expanded.has(id) ? ui.expanded.delete(id) : ui.expanded.add(id);
  }

  /* ---------- list rows ---------- */
  function rowHtml(item, max) {
    const open = ui.expanded.has(item.id);
    const off = item.enabled === false;
    const u = item.usage;
    const cls = `tl-row${open ? " open" : ""}${off ? " tl-disabled" : ""}${u ? "" : " tl-nouse"}${u && !u.count ? " tl-zero" : ""}`;
    const offTag = off ? `<span class="tl-off">${t("tools.off")}</span>` : "";
    const main = item.kind === "hook"
      ? `<span class="tl-r-name tl-r-flow"><b>${esc(item.event)}</b><span>→</span><em>${esc(item.matcher || t("tools.any"))}</em><span>→</span><code>${esc(item.command || "—")}</code></span>`
      : `<span class="tl-r-name"><span class="tl-r-title">${esc(item.name)}</span>${offTag}</span>`;
    const usage = u
      ? `<span class="tl-r-bar" aria-hidden="true"><i style="width:${u.count ? Math.max(2, Math.round((u.count / max) * 100)) : 0}%"></i></span>
         <span class="tl-r-cnt">${u.count || "—"}</span>
         <span class="tl-r-last">${u.count && u.last ? esc(agoBack(u.last)) : ""}</span>`
      : "";
    const body = item.kind === "hook"
      ? `<div class="tl-flow"><b>${esc(item.event)}</b><span>→</span><em>${esc(item.matcher || t("tools.any"))}</em><span>→</span><code>${esc(item.command || "—")}</code></div>`
      : (item.description ? `<p class="tl-desc">${esc(item.description)}</p>` : "")
        + (u ? usageHtml(item) : "") + `<div class="tl-meta">${metaHtml(item)}</div>`;
    return `<div class="${cls}" data-tool="${esc(item.id)}">
      <button class="tl-rowbtn" aria-expanded="${open}">${main}<span class="tl-r-src">${badge(item)}</span>${usage}</button>
      <div class="tl-r-body">${body}${copyHtml(item)}</div></div>`;
  }

  const sectionSummary = (sec, items) => {
    const n = items.length;
    if (!items.some((it) => it.usage)) return sec.count(n);
    const used = items.filter((it) => it.usage?.count).length;
    return t("tools.summary", { count: sec.count(n), used, never: n - used });
  };

  function listHtml(sec, items, all) {
    const hasUsage = items.some((it) => it.usage);
    const used = hasUsage ? items.filter((it) => it.usage?.count).length : 0;
    const total = items.length;
    const filtered = total !== all.length ? ` · ${t("tools.shown", { n: total, total: all.length })}` : "";
    const summary = `<div class="tl-summary"><span>${sectionSummary(sec, items)}${filtered}</span>${hasUsage
      ? `<span class="tl-sumbar" aria-hidden="true"><i style="width:${(used / total) * 100}%"></i></span>` : ""}</div>`;
    const max = Math.max(1, ...all.map((it) => it.usage?.count || 0));
    const rows = (arr) => arr.map((it) => rowHtml(it, max)).join("");
    if (!hasUsage || ui.sort !== "usage") return summary + `<div class="tl-rows">${rows(items)}</div>`;
    const hot = items.filter((it) => !it.usage || it.usage.count);
    const never = items.filter((it) => it.usage && !it.usage.count);
    return summary + `<div class="tl-rows">${rows(hot)}</div>`
      + (never.length
        ? `<div class="tl-never-group${ui.neverOpen ? " open" : ""}">
            <button class="tl-never-toggle" aria-expanded="${ui.neverOpen}">${t("tools.never_group", { n: never.length })}</button>
            <div class="tl-rows">${rows(never)}</div></div>` : "");
  }

  function shell() {
    const el = root();
    if (el.dataset.ready) return;
    el.dataset.ready = "1";
    el.innerHTML = `<div class="tl-controls">
        <input id="tl-q" class="search tl-search" type="search" placeholder="${t("tools.search")}" aria-label="${t("tools.search_label")}">
        <div class="seg tl-sources" role="group" aria-label="${t("tools.sources")}"></div>
        <div class="seg tl-sorts" role="group" aria-label="${t("tools.sort")}">
          <button data-sort="usage">${t("tools.sort.usage")}</button><button data-sort="name">${t("tools.sort.name")}</button>
        </div>
        <div class="seg tl-layouts" role="group" aria-label="${t("tools.layout")}">
          <button data-layout="list">${t("tools.layout.list")}</button><button data-layout="cards">${t("tools.layout.cards")}</button>
        </div>
      </div>
      <nav class="tl-tabs" aria-label="${t("tools.sections")}"></nav>
      <p class="tl-hint"></p>
      <div class="tl-list"></div>`;
    el.querySelector("#tl-q").oninput = (e) => { ui.query = e.target.value.trim().toLowerCase(); drawList(); };
    el.querySelector(".tl-sorts").onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      ui.sort = b.dataset.sort; drawControls(); drawList();
    };
    el.querySelector(".tl-layouts").onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      ui.layout = b.dataset.layout; saveLayout(ui.layout); drawControls(); drawList();
    };
    el.querySelector(".tl-sources").onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      ui.sources.has(b.dataset.src) ? ui.sources.delete(b.dataset.src) : ui.sources.add(b.dataset.src);
      drawControls(); drawList();
    };
    el.querySelector(".tl-tabs").onclick = (e) => {
      const b = e.target.closest("button"); if (b) setView("tools", b.dataset.key);
    };
    el.querySelector(".tl-list").onclick = (e) => {
      const copy = e.target.closest(".tl-copy");
      if (copy) {
        navigator.clipboard.writeText(copy.dataset.path)
          .then(() => { copy.textContent = t("tools.copied"); setTimeout(() => { copy.textContent = t("tools.copy_path"); }, 1500); })
          .catch(() => { copy.textContent = copy.dataset.path; });
        return;
      }
      const rowBtn = e.target.closest(".tl-rowbtn");
      if (rowBtn) {
        const row = rowBtn.closest(".tl-row");
        toggleExpanded(row.dataset.tool);
        const open = ui.expanded.has(row.dataset.tool);
        row.classList.toggle("open", open);
        rowBtn.setAttribute("aria-expanded", String(open));
        return;
      }
      const never = e.target.closest(".tl-never-toggle");
      if (never) {
        ui.neverOpen = !ui.neverOpen;
        never.closest(".tl-never-group").classList.toggle("open", ui.neverOpen);
        never.setAttribute("aria-expanded", String(ui.neverOpen));
        return;
      }
      const more = e.target.closest(".tl-more");
      if (more) {
        toggleExpanded(more.dataset.tool);
        drawList(); return;
      }
      const sess = e.target.closest(".tl-sess");
      if (sess) { e.preventDefault(); openDrawer(sess.dataset.sid); }
    };
  }

  function drawControls() {
    const el = root();
    el.querySelector(".tl-sources").innerHTML = SOURCES.map((s) =>
      `<button data-src="${s.key}" aria-pressed="${ui.sources.has(s.key)}">${s.title}</button>`).join("");
    el.querySelectorAll(".tl-layouts button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.layout === ui.layout)));
    el.querySelectorAll(".tl-sorts button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.sort === ui.sort)));
    const current = sectionKey();
    el.querySelector(".tl-tabs").innerHTML = SECTIONS.map((s) =>
      `<button data-key="${s.key}" aria-pressed="${s.key === current}">${s.title}<i>${catalog ? (catalog[s.key] || []).length : "…"}</i></button>`).join("");
    el.querySelector(".tl-hint").textContent = SECTIONS.find((s) => s.key === current).hint;
  }

  function drawList() {
    const list = root().querySelector(".tl-list");
    if (!catalog) { list.innerHTML = `<p class="empty">${failed ? t("tools.load_failed") : t("tools.loading")}</p>`; return; }
    const all = catalog[sectionKey()] || [];
    const items = ordered(all.filter((it) => inSources(it) && matches(it, ui.query)));
    const unused = items.filter((it) => it.usage && !it.usage.count).length;
    if (items.length && ui.layout === "list") {
      list.innerHTML = listHtml(SECTIONS.find((s) => s.key === sectionKey()), items, all);
      return;
    }
    list.innerHTML = items.length
      ? `<p class="tl-count">${unused && ui.sort === "usage" ? t("tools.count_unused", { n: items.length, total: all.length, unused })
        : t("tools.count_of", { n: items.length, total: all.length })}</p>`
        + `<div class="tl-grid">${items.map(itemHtml).join("")}</div>`
      : `<p class="empty">${all.length ? t("tools.no_match") : t("tools.empty")}</p>`;
  }

  function draw() { shell(); drawControls(); drawList(); }

  // render() runs every few seconds along with the board: the list is redrawn only when the section
  // changes or fresh data arrives, so typing and expanded descriptions survive.
  SYA.feature({
    id: "tools",
    view: {
      title: t("tools.title"),
      render() {
        shell();
        drawControls();
        const key = sectionKey();
        if (root().dataset.section !== key) { root().dataset.section = key; drawList(); }
        load();
      },
    },
  });
})();
