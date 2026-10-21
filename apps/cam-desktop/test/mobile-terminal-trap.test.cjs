"use strict";

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

function fakeEl(tag = "div") {
  const listeners = {};
  const el = {
    tagName: tag,
    style: {},
    dataset: {},
    children: [],
    parentNode: null,
    offsetWidth: 180,
    textContent: "",
    type: "",
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); },
      remove(c) { this.set.delete(c); },
      contains(c) { return this.set.has(c); },
    },
    setAttribute() {},
    append(...nodes) { for (const node of nodes) el.appendChild(node); },
    appendChild(node) { node.parentNode = el; el.children.push(node); return node; },
    removeChild(node) {
      const index = el.children.indexOf(node);
      if (index >= 0) el.children.splice(index, 1);
      node.parentNode = null;
    },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      const arr = listeners[type];
      if (!arr) return;
      const index = arr.indexOf(fn);
      if (index >= 0) arr.splice(index, 1);
    },
    closest() { return null; },
    listeners,
  };
  return el;
}

async function main() {
  const root = path.join(__dirname, "..", "..", "..");
  const { attachTerminalTouchTrap } = await import(
    pathToFileURL(path.join(root, "web", "js", "shared", "terminal-trap.js"))
  );

  const doc = {
    body: fakeEl("body"),
    createElement: (tag) => fakeEl(tag),
  };
  const screen = {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 100 }),
  };

  let selectionStart = null;
  let selectionLength = 0;
  let selectedText = "";
  let selectedFlag = false;
  let scrollLinesCalls = [];
  let scrollPagesCalls = [];
  const line = {
    getCell: (index) => ({
      getWidth: () => 1,
      getChars: () => (index < 10 ? "a" : " "),
    }),
  };
  const term = {
    cols: 20,
    rows: 5,
    element: { querySelector: () => screen },
    _core: { _renderService: { dimensions: { css: { cell: { width: 10, height: 20 } } } } },
    buffer: { active: { viewportY: 0, baseY: 10, getLine: () => line } },
    onSelectionChange() { return { dispose() {} }; },
    onScroll() { return { dispose() {} }; },
    select(col, row, length) {
      selectionStart = { col, row };
      selectionLength = length;
      selectedFlag = true;
      selectedText = "a".repeat(length);
    },
    getSelectionPosition() {
      if (!selectedFlag) return undefined;
      const total = selectionStart.col + selectionLength;
      const endRow = selectionStart.row + Math.floor((total - 1) / term.cols);
      return {
        start: { x: selectionStart.col, y: selectionStart.row },
        end: { x: total % term.cols, y: endRow },
      };
    },
    hasSelection: () => selectedFlag,
    getSelection: () => selectedText,
    clearSelection() { selectedFlag = false; selectedText = ""; },
    selectAll() {},
    scrollLines(n) { scrollLinesCalls.push(n); },
    scrollPages(n) { scrollPagesCalls.push(n); },
    paste() {},
    focus() {},
  };
  const host = fakeEl("div");
  host.ownerDocument = doc;

  globalThis.window = { innerWidth: 360, addEventListener() {}, removeEventListener() {} };
  globalThis.document = doc;
  globalThis.CustomEvent = class CustomEvent {
    constructor(type, init) { this.type = type; this.detail = init && init.detail; }
  };

  let fallback = null;
  const entry = { term, sessionId: "s1", copyModeActive: false };
  const trap = attachTerminalTouchTrap(entry, {
    container: host,
    longPressMs: 10,
    onScrollUnavailable: (direction, count, page) => {
      fallback = { direction, count, page };
    },
  });

  const touch = (x, y, id = 1) => ({ identifier: id, clientX: x, clientY: y });
  const fire = (type, ev) => {
    for (const fn of host.listeners[type] || []) fn(ev);
  };
  const touchEvent = (t) => ({
    touches: t,
    cancelable: true,
    preventDefault() {},
    stopPropagation() {},
  });

  fire("touchstart", touchEvent([touch(15, 15)]));
  await new Promise((resolve) => setTimeout(resolve, 25));
  ok("long press selects a word", selectedFlag && selectionLength > 1,
    `flag=${selectedFlag} length=${selectionLength}`);

  fire("touchmove", touchEvent([touch(55, 15)]));
  ok("drag adjusts the selection", selectionLength === 5, `length=${selectionLength}`);
  fire("touchend", touchEvent([]));

  fire("touchstart", touchEvent([touch(15, 15)]));
  fire("touchend", touchEvent([]));
  ok("tap clears the selection", !selectedFlag, `flag=${selectedFlag}`);

  // Copy mode OFF: the trap owns swipes and scrolls the local buffer.
  scrollLinesCalls = [];
  fire("touchstart", touchEvent([touch(5, 5)]));
  fire("touchmove", touchEvent([touch(5, 45)]));
  fire("touchend", touchEvent([]));
  ok("inactive vertical swipe scrolls the local buffer",
    JSON.stringify(scrollLinesCalls) === "[-2]",
    JSON.stringify(scrollLinesCalls));

  scrollPagesCalls = [];
  fire("touchstart", touchEvent([touch(180, 30)]));
  fire("touchmove", touchEvent([touch(100, 32)]));
  fire("touchend", touchEvent([]));
  ok("inactive horizontal swipe pages the local buffer",
    JSON.stringify(scrollPagesCalls) === "[1]",
    JSON.stringify(scrollPagesCalls));

  // Copy mode ON: leave the gesture to the view's Up/Down/PPage/NPage handler.
  entry.copyModeActive = true;
  scrollLinesCalls = [];
  scrollPagesCalls = [];
  fire("touchstart", touchEvent([touch(5, 5)]));
  fire("touchmove", touchEvent([touch(5, 45)]));
  fire("touchend", touchEvent([]));
  fire("touchstart", touchEvent([touch(180, 30)]));
  fire("touchmove", touchEvent([touch(100, 32)]));
  fire("touchend", touchEvent([]));
  ok("copy-mode swipes are left to the key handler",
    scrollLinesCalls.length === 0 && scrollPagesCalls.length === 0,
    `lines=${JSON.stringify(scrollLinesCalls)} pages=${JSON.stringify(scrollPagesCalls)}`);
  entry.copyModeActive = false;

  // Alternate buffer has no local scrollback: hand the gesture to copy mode.
  fallback = null;
  term.buffer.active.type = "alternate";
  term.buffer.active.baseY = 0;
  fire("touchstart", touchEvent([touch(5, 5)]));
  fire("touchmove", touchEvent([touch(5, 45)]));
  fire("touchend", touchEvent([]));
  ok("alternate buffer falls back to copy mode",
    !!fallback && fallback.direction === "up" && fallback.page === false,
    JSON.stringify(fallback));
  term.buffer.active.type = "normal";
  term.buffer.active.baseY = 10;

  trap.detach();
  ok("detach removes touch listeners", (host.listeners.touchstart || []).length === 0);

  console.log(`mobile-terminal-trap: ${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
