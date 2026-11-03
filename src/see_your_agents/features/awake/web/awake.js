// Keep the Mac awake: a switch in Appearance (the prefs slot). Server: GET / POST /api/awake. Where the server
// cannot (not macOS, no caffeinate) the block is not shown. Strings: awake/i18n/<lang>.json through t(). Globals
// from app.js: $, t, getJson, postJson, SYA.
(() => {
  if (window.SYA_API) return;  // the static demo has no server to keep awake
  let info = null;

  function draw() {
    let box = document.getElementById("awake-prefs");
    if (!info?.available) { box?.remove(); return; }
    if (!box) {
      $("#prefs").insertAdjacentHTML("beforeend", `<fieldset id="awake-prefs"><legend>${t("awake.title")}</legend>
        <label class="check"><input type="checkbox" class="awake-on"> ${t("awake.switch")}</label>
        <p class="note awake-note"></p></fieldset>`);
      box = document.getElementById("awake-prefs");
      box.querySelector(".awake-on").onchange = (e) => save(e.target.checked);
    }
    box.querySelector(".awake-on").checked = info.enabled;
    box.querySelector(".awake-note").textContent = !info.enabled
      ? t("awake.off") : info.active ? t("awake.active") : t("awake.idle");
  }

  async function load() {
    try { info = await getJson("/api/awake"); } catch (e) { info = null; }
    draw();
  }

  async function save(on) {
    const r = await postJson("/api/awake", { enabled: on }).catch(() => null);
    if (r?.ok) info = r.data;
    draw();  // a refusal puts the switch back
  }

  SYA.feature({ id: "awake", prefs: { render: () => { draw(); load(); } } });
})();
