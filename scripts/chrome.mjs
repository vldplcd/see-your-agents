// Headless Google Chrome over the DevTools protocol, for shoot.mjs and record-demo.mjs: one page, with every
// console error and uncaught exception collected in `errors`.
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function openChrome({ width = 1440, height = 900 } = {}) {
  const port = 9300 + Math.floor(Math.random() * 600);
  const chrome = spawn(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ["--headless=new", "--disable-gpu", "--hide-scrollbars", `--user-data-dir=${mkdtempSync(tmpdir() + "/sya-shoot-")}`,
      `--remote-debugging-port=${port}`, `--window-size=${width},${height}`, "about:blank"],
    { stdio: "ignore" },
  );
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
  return { send, errors, close: () => { ws.close(); chrome.kill(); } };
}
