// Agent figures, shared by the office, the chat's avatars, the looks editor and the look & feel panel:
// shapes, desks, skins and faces. Globals: SHAPES, DESKS, SKINS, THEMES, DEALS, DEAL_NOTE, KANDINSKY, hash,
// shapeFor, face, deskSvg. shapeFor and deskSvg read the viewer's prefs (app.js) when they are called. Names
// come from the shell's catalog: t() (web/i18n.js) works here at load time.

/* ---------- agent shapes and desks (shape paths live in a 100×100 box) ---------- */
const pathOf = (pts) => "M" + pts.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join("L") + "Z";
const around = (n, radius, rot = -90, cx = 50, cy = 50) => Array.from({ length: n }, (_, i) => {
  const a = (rot + 360 * i / n) * Math.PI / 180, r = typeof radius === "function" ? radius(i) : radius;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
});
const SHAPES = {
  circle: { name: t("app.shape.circle"), d: "M50 6A44 44 0 1 1 49.9 6Z" },
  square: { name: t("app.shape.square"), d: "M8 8H92V92H8Z" },
  squircle: { name: t("app.shape.squircle"), d: "M50 6C88 6 94 12 94 50S88 94 50 94 6 88 6 50 12 6 50 6Z" },
  triangle: { name: t("app.shape.triangle"), d: pathOf(around(3, 54, -90, 50, 60)), eyes: { y: 66, dx: 10, r: 6 } },
  semicircle: { name: t("app.shape.semicircle"), d: "M4 80A46 46 0 0 1 96 80Z", eyes: { y: 60 } },
  diamond: { name: t("app.shape.diamond"), d: "M50 2L98 50L50 98L2 50Z" },
  pentagon: { name: t("app.shape.pentagon"), d: pathOf(around(5, 50, -90, 50, 54)), eyes: { y: 52 } },
  hexagon: { name: t("app.shape.hexagon"), d: pathOf(around(6, 48, 0)) },
  octagon: { name: t("app.shape.octagon"), d: pathOf(around(8, 48, 22.5)) },
  star: { name: t("app.shape.star"), d: pathOf(around(10, (i) => (i % 2 ? 22 : 50), -90, 50, 54)), eyes: { y: 54, dx: 9, r: 5.5 } },
  sparkle: { name: t("app.shape.sparkle"), d: "M50 2C54 36 64 46 98 50C64 54 54 64 50 98C46 64 36 54 2 50C36 46 46 36 50 2Z", eyes: { y: 50, dx: 8, r: 5 } },
  flower: { name: t("app.shape.flower"), d: pathOf(around(120, (i) => 41 + 7 * Math.cos(6 * 2 * Math.PI * i / 120), 0)) },
  gear: { name: t("app.shape.gear"), d: pathOf(around(40, (i) => (i % 4 < 2 ? 48 : 38), -90 + 4.5)) },
  plus: { name: t("app.shape.plus"), d: "M34 4H66V34H96V66H66V96H34V66H4V34H34Z" },
  blob: { name: t("app.shape.blob"), d: "M52 6C76 4 96 22 92 50C88 76 72 96 46 92C20 88 4 72 8 46C12 22 28 8 52 6Z" },
};
const KANDINSKY = { needs_you: "square", working: "circle", your_move: "triangle" };
const DEALS = { session: t("app.deal.session"), project: t("app.deal.project"), status: t("app.deal.status") };
const DEAL_NOTE = {
  session: t("app.deal.session_note"),
  project: t("app.deal.project_note"),
  status: t("app.deal.status_note"),
};
const leds = [22, 36, 50, 64, 78, 92].map((x) => `<rect class="led" x="${x}" y="60" width="7" height="5" rx="1"/>`).join("");
const DESKS = {
  desk: { name: t("app.desk.desk"), over: `<rect class="top" x="6" y="44" width="108" height="9" rx="2"/><rect class="leg" x="13" y="53" width="6" height="34"/>
    <rect class="leg" x="101" y="53" width="6" height="34"/><rect class="screen" x="86" y="28" width="22" height="15" rx="2"/><rect class="leg" x="95" y="43" width="4" height="2"/>` },
  round: { name: t("app.desk.round"), over: `<rect class="leg" x="57" y="54" width="6" height="30"/><ellipse class="leg" cx="60" cy="86" rx="18" ry="4"/>
    <ellipse class="top" cx="60" cy="50" rx="54" ry="10"/><path class="screen" d="M84 47h22l-4-13h-14z"/>` },
  console: { name: t("app.desk.console"), over: `<path class="top" d="M2 56L14 44H106L118 56V88H2Z"/><g>${leds}</g><rect class="screen" x="30" y="72" width="60" height="9" rx="1"/>` },
  pouf: { name: t("app.desk.pouf"), over: `<ellipse class="top" cx="60" cy="60" rx="42" ry="17"/><path class="screen" d="M76 46h24l-4-13h-16z"/>` },
  none: { name: t("app.desk.none"), under: `<ellipse class="shadow" cx="60" cy="48" rx="24" ry="4"/>`, over: "" },
};
const SKINS = {
  buro: { name: t("app.skin.buro"), note: t("app.skin.buro_note"), font: "Unbounded", sw: ["#d93a3f", "#178a7e", "#b7791f"],
    office: { shapes: ["circle", "squircle", "hexagon", "flower", "blob"], deal: "session", desk: "desk" } },
  konstruktor: { name: t("app.skin.konstruktor"), note: t("app.skin.konstruktor_note"), font: "Jost", sw: ["#e3302b", "#1f4fd6", "#f5c211"],
    office: { shapes: ["circle", "square", "triangle", "semicircle"], deal: "status", desk: "desk" } },
  dispatch: { name: t("app.skin.dispatch"), note: t("app.skin.dispatch_note"), font: "Sofia Sans Extra Condensed", sw: ["#d1362e", "#2f63c8", "#c98a12"],
    office: { shapes: ["diamond", "triangle", "square", "plus", "star"], deal: "project", desk: "console" } },
};
const THEMES = { "": t("app.theme.system"), light: t("app.theme.light"), dark: t("app.theme.dark") };

const hash = (s) => [...String(s)].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 7);
function shapeFor(c) {
  if (prefs.deal === "status") return KANDINSKY[c.column] || "circle";
  return prefs.shapes[hash(prefs.deal === "project" ? c.project : c.session_id) % prefs.shapes.length];
}
function face(shape, column) {
  const { y = 46, dx = 12, r = 7 } = SHAPES[shape].eyes || {};
  if (column === "recent") return `<path class="lid" d="M${50 - dx - r} ${y}h${2 * r}M${50 + dx - r} ${y}h${2 * r}"/>`;
  const [px, py] = { working: [r * .3, r * .35], needs_you: [0, -r * .3] }[column] || [0, 0];  // at the screen / up at you
  return `<g class="eyes">${[-dx, dx].map((x) => `<circle class="eye" cx="${50 + x}" cy="${y}" r="${r}"/>
    <circle class="pupil" cx="${50 + x + px}" cy="${y + py}" r="${r * .5}"/>`).join("")}</g>`;
}
function deskSvg(shape, column, desk = DESKS[prefs.desk], faces = prefs.faces) {
  return `<svg viewBox="0 0 120 96" aria-hidden="true">${desk.under || ""}
    <g transform="translate(40 4) scale(.4)"><path class="body" d="${SHAPES[shape].d}"/>${faces ? face(shape, column) : ""}</g>${desk.over}</svg>`;
}
