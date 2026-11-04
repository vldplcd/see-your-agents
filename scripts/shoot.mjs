// Screenshots of the dashboard in given states, plus every console error, through headless Chrome (CDP).
//
//   node scripts/shoot.mjs http://127.0.0.1:8771 /tmp/shots shots.json
//
// shots.json: [{"name": "todo-dark", "hash": "#board", "prefs": {"skin": "dispatch", "theme": "dark"},
//               "storage": {"sya.dock.right": {"open": true}}, "eval": "document.querySelector('.x').click()",
//               "wait": 3500}]
// Only "name" is required. localStorage is set before the page's own scripts run (a later write would race
// the page's saves). Exits 1 when the page logged an error or threw.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { openChrome, sleep } from "./chrome.mjs";

const [base, out, specFile] = process.argv.slice(2);
if (!base || !out || !specFile) {
  console.error("usage: node scripts/shoot.mjs <base-url> <out-dir> <shots.json>");
  process.exit(2);
}
const shots = JSON.parse(readFileSync(specFile, "utf8"));
mkdirSync(out, { recursive: true });
const { send, errors, close } = await openChrome();

let injected = null;
for (const s of shots) {
  if (injected) await send("Page.removeScriptToEvaluateOnNewDocument", { identifier: injected });
  const storage = { ...(s.storage ?? {}), ...(s.prefs ? { "sya.prefs": s.prefs } : {}) };
  const sets = Object.entries(storage).map(([k, v]) => `localStorage.setItem(${JSON.stringify(k)}, ${JSON.stringify(JSON.stringify(v))});`);
  injected = (await send("Page.addScriptToEvaluateOnNewDocument", { source: `try { ${sets.join(" ")} } catch (e) {}` })).identifier;
  await send("Page.navigate", { url: "about:blank" }); // a hash change alone would not reload the page
  await sleep(200);
  await send("Page.navigate", { url: base + "/" + (s.hash ?? "") });
  await sleep(s.wait ?? 3500);
  if (s.eval) {
    await send("Runtime.evaluate", { expression: s.eval, awaitPromise: true });
    await sleep(800);
  }
  const png = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(`${out}/${s.name}.png`, Buffer.from(png.data, "base64"));
  console.log(`${out}/${s.name}.png`);
}
close();
for (const e of errors) console.log(`CONSOLE ${e}`);
process.exit(errors.length ? 1 : 0);
