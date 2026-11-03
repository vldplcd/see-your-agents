// Hello: the example feature. A right dock with who said hello, a line on every board card, a drawer section.
// Server routes: GET and POST /api/hello (hello.py). Strings: i18n/<lang>.json through t(), every UI string.
// Globals: t (i18n.js); esc, getJson, postJson, render, SYA (app.js).
(() => {
  let names = [];

  const list = (root) => {
    root.querySelector(".hello-names").innerHTML =
      names.map((n) => `<li>${esc(n)}</li>`).join("") || `<li class="empty">${t("hello.nobody")}</li>`;
  };
  async function load() {
    names = (await getJson("/api/hello").catch(() => null))?.names || names;  // null: the next open retries
    render();  // the shell's: redraws the list and the badge
  }

  // Called on every render while the dock is open: build once, then only update, so typing survives.
  function draw(root) {
    if (!root.firstElementChild) {
      root.innerHTML = `<div class="dock-title"><h2>${t("hello.title")}</h2>
          <button class="ghost dock-x" type="button" aria-label="${t("hello.hide")}" title="${t("hello.hide")}">×</button></div>
        <form class="hello-form"><input name="name" maxlength="40" placeholder="${t("hello.name_placeholder")}" aria-label="${t("hello.name")}">
          <button class="ghost">${t("hello.say")}</button></form>
        <ul class="hello-names"></ul>`;
      root.querySelector("form").onsubmit = async (e) => {
        e.preventDefault();
        const input = e.target.elements.name;
        const r = await postJson("/api/hello", { name: input.value });  // an error comes worded in the page's language
        if (r.ok) { names = r.data.names; input.value = ""; render(); }
      };
      load();
    }
    list(root);
  }

  SYA.feature({
    id: "hello",
    right: {
      button: t("hello.title"), title: t("hello.dock_title"), label: t("hello.title"),
      icon: `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M12 5v14"/></svg>`,
      render: draw,
      badge: () => names.length || "",
    },
    card: (c) => `<p class="hello-line">${t("hello.card", { n: c.hello_words })}</p>`,
    drawer: (d) => `<section><h4>${t("hello.title")}</h4><p>${t("hello.drawer", { n: d.hello_words })}</p></section>`,
  });
})();
