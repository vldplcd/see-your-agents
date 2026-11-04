// The README's GIF: on the demo page, a permission prompt is answered from the board and the card moves on.
//
//   uv run python demo/build.py
//   node scripts/record-demo.mjs docs/images/approve-light.gif light
//   node scripts/record-demo.mjs docs/images/approve-dark.gif dark
//
// Needs Google Chrome and gifski (brew install gifski). Headless Chrome draws no pointer, so the page gets a drawn
// one that moves to each click. Frames come as fast as Chrome takes them and are resampled to a steady rate.
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import { openChrome, sleep } from "./chrome.mjs";

const [out, theme = "light"] = process.argv.slice(2);
if (!out) {
  console.error("usage: node scripts/record-demo.mjs <out.gif> [light|dark]");
  process.exit(2);
}
const W = 1280, H = 800, FPS = 12;
const page = pathToFileURL(new URL("../demo/dist/see-your-agents.html", import.meta.url).pathname).href;

const POINTER = `(() => {
  const p = document.createElement("div");
  p.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M3 2l17 10.5-7.5 1.4L8.6 21z" fill="#14181f" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(p.style, { position: "fixed", left: "-3px", top: "-2px", zIndex: 2147483647, pointerEvents: "none",
    transform: "translate(${W * 0.62}px, ${H * 0.6}px)", transition: "transform 850ms cubic-bezier(.45,0,.2,1)" });
  document.body.append(p);
  const at = (sel) => {
    const r = document.querySelector(sel).getBoundingClientRect();
    return [r.left + Math.min(r.width / 2, 36), r.top + r.height / 2];
  };
  window.pointTo = (sel) => { const [x, y] = at(sel); p.style.transform = \`translate(\${x}px, \${y}px)\`; };
  window.clickOn = (sel) => {
    p.animate([{ scale: 1 }, { scale: 0.75 }, { scale: 1 }], { duration: 260 });
    document.querySelector(sel).click();
  };
})()`;

const { send, errors, close } = await openChrome({ width: W, height: H });
const js = (expression) => send("Runtime.evaluate", { expression, awaitPromise: true });
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `try { localStorage.setItem("sya.prefs", ${JSON.stringify(JSON.stringify({ theme }))}); } catch (e) {}`,
});
await send("Page.navigate", { url: page });
await sleep(4000);  // fonts and the first board
await js(POINTER);

const frames = [];
let recording = true;
const capture = (async () => {
  while (recording) {
    const t = Date.now();
    frames.push({ t, data: (await send("Page.captureScreenshot", { format: "png" })).data });
  }
})();
await sleep(1600);
for (const [sel, before, after] of [[".approvals-open", 1000, 2400], [".approvals-allow", 1000, 4200]]) {
  await js(`pointTo(${JSON.stringify(sel)})`);
  await sleep(before);
  await js(`clickOn(${JSON.stringify(sel)})`);
  await sleep(after);
}
recording = false;
await capture;
close();
if (errors.length) {
  for (const e of errors) console.error(`CONSOLE ${e}`);
  process.exit(1);
}

const dir = mkdtempSync(tmpdir() + "/sya-gif-");
const t0 = frames[0].t, end = frames.at(-1).t, files = [];
for (let k = 0, i = 0; t0 + (k * 1000) / FPS <= end; k++) {
  while (i + 1 < frames.length && frames[i + 1].t <= t0 + (k * 1000) / FPS) i++;
  const file = `${dir}/${String(k).padStart(4, "0")}.png`;
  writeFileSync(file, Buffer.from(frames[i].data, "base64"));
  files.push(file);
}
execFileSync("gifski", ["--fps", String(FPS), "--width", "960", "--quality", "85", "--quiet", "-o", out, ...files]);
console.log(`${out}: ${files.length} frames from ${frames.length} captures`);
