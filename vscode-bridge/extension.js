// See Your Agents bridge: a unix socket that lets the local dashboard find this window's terminals by
// shell pid and type into them. No network, no dependencies. Protocol: one JSON object per line.
//   {"op":"list"}                          -> {"ok":true,"terminals":[{"name","shellPid"}]}
//   {"op":"send","shellPid":N,"text":"..."} -> bracketed paste of text, then Enter
//   {"op":"focus","shellPid":N}            -> shows the terminal panel
"use strict";

const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const MAX_LINE = 1024 * 1024;
const ENTER_DELAY_MS = 150;

let server = null;
let socketPath = null;

function socketDir() {
  const cache = process.env.SYA_CACHE_DIR || path.join(os.homedir(), ".cache", "see-your-agents");
  return path.join(cache, "vscode");
}

async function terminalsByPid(vscode) {
  const found = [];
  for (const terminal of vscode.window.terminals) {
    let pid;
    try {
      pid = await terminal.processId;
    } catch (_) {
      continue;
    }
    if (typeof pid === "number") found.push({ terminal, name: terminal.name, shellPid: pid });
  }
  return found;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function handle(vscode, request) {
  if (!request || typeof request !== "object") return { ok: false, error: "bad request" };
  const all = await terminalsByPid(vscode);
  if (request.op === "list") {
    return { ok: true, terminals: all.map(({ name, shellPid }) => ({ name, shellPid })) };
  }
  if (request.op !== "send" && request.op !== "focus") return { ok: false, error: "unknown op" };
  const target = all.find((t) => t.shellPid === request.shellPid);
  if (!target) return { ok: false, error: "no such terminal" };
  if (request.op === "focus") {
    target.terminal.show(false);
    return { ok: true };
  }
  if (typeof request.text !== "string" || request.text === "") return { ok: false, error: "empty text" };
  // Bracketed paste keeps newlines in the text from submitting early; the final Enter submits.
  target.terminal.sendText("\x1b[200~" + request.text + "\x1b[201~", false);
  await sleep(ENTER_DELAY_MS);
  target.terminal.sendText("\r", false);
  return { ok: true };
}

function onConnection(vscode, conn) {
  let buffer = "";
  conn.setEncoding("utf8");
  conn.on("error", () => {});
  conn.on("data", (chunk) => {
    buffer += chunk;
    if (buffer.length > MAX_LINE) return conn.destroy();
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      let request;
      try {
        request = JSON.parse(line);
      } catch (_) {
        conn.write(JSON.stringify({ ok: false, error: "bad json" }) + "\n");
        continue;
      }
      handle(vscode, request).then(
        (reply) => conn.write(JSON.stringify(reply) + "\n"),
        (err) => conn.write(JSON.stringify({ ok: false, error: String(err) }) + "\n")
      );
    }
  });
}

function activate() {
  const vscode = require("vscode");
  const dir = socketDir();
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(dir, 0o700);
  socketPath = path.join(dir, `${process.pid}.sock`);
  try {
    fs.unlinkSync(socketPath); // stale file from a previous run with the same pid
  } catch (_) {}
  server = net.createServer((conn) => onConnection(vscode, conn));
  server.on("error", (err) => console.error("sya-bridge:", err));
  server.listen(socketPath, () => fs.chmodSync(socketPath, 0o600));
}

function deactivate() {
  if (server) server.close();
  server = null;
  if (socketPath) {
    try {
      fs.unlinkSync(socketPath);
    } catch (_) {}
  }
}

module.exports = { activate, deactivate };
