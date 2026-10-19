"use strict";

const fs = require("fs");
const path = require("path");

let pass = 0;
let fail = 0;
function ok(name, condition, detail = "") {
  if (condition) pass++;
  else {
    fail++;
    console.error("FAIL " + name + (detail ? " — " + detail : ""));
  }
}

async function main() {
  const root = path.join(__dirname, "..", "..", "..");
  const source = fs.readFileSync(path.join(root, "web", "js", "shared", "terminal-mount.js"), "utf8");
  const start = source.indexOf("async function revealTerminalBuffer(");
  const end = source.indexOf("async function stabilizeTerminalAfterAttach(", start);
  if (start < 0 || end < 0) throw new Error("revealTerminalBuffer source not found");
  const revealSource = source.slice(start, end);

  const writes = [];
  const statuses = [];
  const fitCalls = [];
  const ent = {
    liveText: "OPEN+",
    bytesReceived: 5,
    suppressDisplay: true,
    hasConnected: true,
    refitRequested: false,
    term: {
      rows: 45,
      clear() {},
      refresh() {},
      write(chunk, callback) {
        writes.push(chunk);
        if (callback) callback();
      },
    },
  };
  const fakeSetTimeout = (callback, delay) => {
    if (delay === 120) ent.liveText += "FIT1+";
    if (delay === 200) ent.liveText += "FIT2+";
    callback();
    return 0;
  };
  const revealTerminalBuffer = new Function(
    "isAndroidWebView",
    "fitTerminalEntry",
    "terminalScrollToBottom",
    "setTerminalConnecting",
    "setTerminalStatus",
    "updateTerminalSizeHint",
    "setTimeout",
    `${revealSource}\nreturn revealTerminalBuffer;`,
  )(
    () => true,
    (entry, opts) => {
      fitCalls.push({ requested: entry.refitRequested, notifyRemote: opts.notifyRemote });
      if (entry.refitRequested) entry.refitRequested = false;
    },
    () => {},
    () => {},
    (host, message) => statuses.push(message),
    () => {},
    fakeSetTimeout,
  );

  await revealTerminalBuffer(ent, {});
  ok("reveal allows one ready resize before freezing the connected grid",
    fitCalls.length === 2 && fitCalls[0].requested === true && fitCalls[0].notifyRemote === true
      && fitCalls[1].requested === false && ent.refitRequested === false,
    JSON.stringify(fitCalls));
  ok("reveal replays the complete attach stream in order",
    writes.length === 1 && writes[0] === "OPEN+FIT1+FIT2+", JSON.stringify(writes));
  ok("reveal preserves the received-byte count", ent.bytesReceived === 5, String(ent.bytesReceived));
  ok("reveal finishes in the ready streaming state",
    ent.suppressDisplay === false && ent.attachState === "ready" && statuses.at(-1) === "");

  console.log(`mobile-terminal-reveal: ${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
