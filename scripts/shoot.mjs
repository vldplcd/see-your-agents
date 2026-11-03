// Screenshots of the dashboard in given states, plus every console error, through headless Chrome (CDP).
//
//   node scripts/shoot.mjs http://127.0.0.1:8771 /tmp/shots shots.json
//
// shots.json: [{"name": "todo-dark", "hash": "#board", "prefs": {"skin": "dispatch", "theme": "dark"},
//               "storage": {"sya.dock.right": {"open": true}}, "eval": "document.querySelector('.x').click()",
//               "wait": 3500}]
// Only "name" is required. localStorage is set before the page's own scripts run (a later write would race
// the page's saves). Exits 1 when the page logged an error or threw.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";

const [base, out, specFile] = process.argv.slice(2);
if (!base || !out || !specFile) {
  console.error("usage: node scripts/shoot.mjs <base-url> <out-dir> <shots.json>");
  process.exit(2);
}
const shots = JSON.parse(readFileSync(specFile, "utf8"));
mkdirSync(out, { recursive: true });
const port = 9300 + Math.floor(Math.random() * 600);
const chrome = spawn(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ["--headless=new", "--disable-gpu", "--hide-scrollbars", `--user-data-dir=${mkdtempSync(tmpdir() + "/sya-shoot-")}`,
    `--remote-debugging-port=${port}`, "--window-size=1440,900", "about:blank"],
  { stdio: "ignore" },
);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let targets = [];
for (let i = 0; i < 50 && !targets.some((t) => t.type === "page"); i++) {
  try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch { await sleep(200); }
}
const ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let seq = 0;
const waiting = new Map();
const errors = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && waiting.has(d.id)) { waiting.get(d.id)(d.result); waiting.delete(d.id); }
  if (d.method === "Runtime.exceptionThrown") errors.push(d.params.exceptionDetails.exception?.description ?? d.params.exceptionDetails.text);
  if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") errors.push(d.params.args.map((a) => a.value ?? a.description).join(" "));
  if (d.method === "Log.entryAdded" && d.params.entry.level === "error") errors.push(`${d.params.entry.text} ${d.params.entry.url ?? ""}`);
};
const send = (method, params = {}) => new Promise((r) => { const id = ++seq; waiting.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");

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
ws.close();
chrome.kill();
for (const e of errors) console.log(`CONSOLE ${e}`);
process.exit(errors.length ? 1 : 0);
