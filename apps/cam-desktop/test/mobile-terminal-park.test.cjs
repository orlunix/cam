"use strict";

const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

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
  const { Terminal } = await import(pathToFileURL(path.join(root, "web", "vendor", "xterm", "xterm.mjs")));
  const source = fs.readFileSync(path.join(root, "web", "js", "shared", "terminal-mount.js"), "utf8");
  const start = source.indexOf("function fitTerminalEntry(");
  const end = source.indexOf("function setTerminalConnecting(", start);
  if (start < 0 || end < 0) throw new Error("fitTerminalEntry source not found");
  const fitSource = source.slice(start, end);

  function loadFit(desiredRows, hidden = false, bridge = null) {
    class FakeCustomEvent {
      constructor(type, init) {
        this.type = type;
        this.detail = init?.detail;
      }
    }
    return new Function(
      "isAndroidWebView",
      "document",
      "terminalViewportPixels",
      "fittedColsForWidth",
      "fittedRowsForHeight",
      "terminalMeasuredCellSize",
      "terminalFontSize",
      "terminalEntryCanAutoResize",
      "globalBridge",
      "window",
      "CustomEvent",
      "TERMINAL_MIN_NOTIFY_WIDTH",
      `${fitSource}\nreturn fitTerminalEntry;`,
    )(
      () => true,
      { hidden },
      () => ({ width: 470, height: 720 }),
      () => 47,
      () => desiredRows,
      () => ({ width: 10, height: 16 }),
      () => 12,
      () => !!bridge,
      bridge,
      { dispatchEvent() {} },
      FakeCustomEvent,
      120,
    );
  }

  async function gridTerminal() {
    const term = new Terminal({ cols: 47, rows: 45 });
    let data = "\x1b[?1049h";
    for (let row = 0; row < 45; row++) {
      data += `ROW${String(row).padStart(2, "0")}`;
      if (row < 44) data += "\r\n";
    }
    await new Promise((resolve) => term.write(data, resolve));
    return term;
  }

  function gridLines(term) {
    return Array.from({ length: 45 }, (_, row) =>
      term.buffer.active.getLine(row)?.translateToString(true) || "");
  }

  function entry(term) {
    return {
      term,
      container: { style: { visibility: "" } },
      hostEl: { clientWidth: 470, clientHeight: 720 },
      viewActive: true,
      refitRequested: false,
    };
  }

  const damaged = await gridTerminal();
  damaged.resize(47, 30);
  damaged.resize(47, 45);
  const damagedLines = gridLines(damaged);
  ok("headless xterm reproduces blank rows after shrink and regrow",
    damagedLines[0] === "ROW15" && damagedLines.slice(30).every((line) => line === ""),
    JSON.stringify(damagedLines));

  const protectedTerm = await gridTerminal();
  const ent = entry(protectedTerm);
  ent.hasConnected = true;
  ent.viewActive = false;
  ent.hostEl = null;
  const parkedFit = loadFit(30);
  ok("parked fit cannot shrink the cached terminal",
    parkedFit(ent) === false && protectedTerm.rows === 45
      && gridLines(protectedTerm)[44] === "ROW44");

  ent.viewActive = true;
  ent.hostEl = { clientWidth: 0, clientHeight: 0 };
  ok("cached resume does not automatically refit a connected terminal",
    parkedFit(ent) === false && protectedTerm.rows === 45);

  ent.hostEl = { clientWidth: 470, clientHeight: 720 };
  const resumeFit = loadFit(45);
  const originalLines = Array.from({ length: 45 }, (_, row) => `ROW${String(row).padStart(2, "0")}`);
  ok("park and resume preserve every row without another resize",
    resumeFit(ent) === false
      && JSON.stringify(gridLines(protectedTerm)) === JSON.stringify(originalLines),
    JSON.stringify(gridLines(protectedTerm)));

  const backgroundTerm = await gridTerminal();
  const backgroundEntry = entry(backgroundTerm);
  backgroundEntry.hasConnected = true;
  ok("Android background resize cannot shrink the live cached terminal",
    loadFit(30, true)(backgroundEntry) === false && backgroundTerm.rows === 45
      && gridLines(backgroundTerm)[44] === "ROW44");

  backgroundEntry.hostEl = { clientWidth: 0, clientHeight: 0 };
  ok("foreground automatic fit cannot shrink a connected terminal",
    loadFit(30)(backgroundEntry) === false && backgroundTerm.rows === 45
      && gridLines(backgroundTerm)[44] === "ROW44");

  backgroundEntry.hostEl = { clientWidth: 470, clientHeight: 720 };
  ok("foreground recovery keeps the same attach and every terminal row",
    loadFit(45)(backgroundEntry) === false
      && JSON.stringify(gridLines(backgroundTerm)) === JSON.stringify(originalLines),
    JSON.stringify(gridLines(backgroundTerm)));

  const readyTerm = new Terminal({ cols: 47, rows: 45 });
  const readyEntry = entry(readyTerm);
  readyEntry.hasConnected = true;
  readyEntry.refitRequested = true;
  readyEntry.sessionId = "t1";
  readyEntry._notifiedCols = 0;
  readyEntry._notifiedRows = 0;
  const resizeCalls = [];
  const readyFit = loadFit(45, false, { resize: (payload) => resizeCalls.push(payload) });
  ok("the attach-time ready fit notifies the remote PTY once",
    readyFit(readyEntry) === true && resizeCalls.length === 1
      && resizeCalls[0].cols === 47 && resizeCalls[0].rows === 45
      && readyEntry.refitRequested === false, JSON.stringify(resizeCalls));
  ok("the grid freezes immediately after the ready fit",
    readyFit(readyEntry) === false && resizeCalls.length === 1, JSON.stringify(resizeCalls));

  const manualTerm = new Terminal({ cols: 47, rows: 45 });
  const manualEntry = entry(manualTerm);
  manualEntry.hasConnected = true;
  manualEntry.refitRequested = true;
  ok("an explicit Fit can still resize a connected terminal once",
    loadFit(30)(manualEntry) === true && manualTerm.rows === 30
      && manualEntry.refitRequested === false);

  const prepareTerm = new Terminal({ cols: 47, rows: 45 });
  const prepareEntry = entry(prepareTerm);
  prepareEntry.container.style.visibility = "hidden";
  ok("initial hidden preparation can still fit locally",
    loadFit(30)(prepareEntry) === true && prepareTerm.rows === 30);

  damaged.dispose();
  protectedTerm.dispose();
  backgroundTerm.dispose();
  readyTerm.dispose();
  manualTerm.dispose();
  prepareTerm.dispose();
  console.log(`mobile-terminal-park: ${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
