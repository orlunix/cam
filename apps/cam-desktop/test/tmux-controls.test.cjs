"use strict";

const assert = require("assert");
const {
  tmuxMetadataForAgent,
  selectOnlyClient,
  selectNewClient,
  parseClientState,
  parseWindowRows,
  tmuxCommand,
} = require("../electron/tmux-controls.cjs");

let pass = 0;
let fail = 0;
function test(name, fn) {
  try { fn(); pass++; }
  catch (err) { fail++; console.error("FAIL " + name + " — " + err.message); }
}

test("requires complete persisted tmux metadata", () => {
  assert.deepStrictEqual(tmuxMetadataForAgent({
    tmux_session: "cam-a", tmux_socket: "/tmp/cam-a.sock", tmux_bin: "/bin/tmux",
  }), { session: "cam-a", socket: "/tmp/cam-a.sock", bin: "/bin/tmux" });
  assert.deepStrictEqual(tmuxMetadataForAgent({
    tmux_session: "cam-a", tmux_socket: "/tmp/cam-a.sock",
  }), { session: "cam-a", socket: "/tmp/cam-a.sock", bin: "/bin/tmux" });
  assert.deepStrictEqual(tmuxMetadataForAgent({
    tmux_session: "cam-a", runtime: { tmux: { socket: "/tmp/cam-a.sock", bin: "/usr/bin/tmux" } },
  }), { session: "cam-a", socket: "/tmp/cam-a.sock", bin: "/usr/bin/tmux" });
  assert.strictEqual(tmuxMetadataForAgent({ tmux_session: "cam-a" }), null);
});

test("accepts exactly one new attached client", () => {
  assert.strictEqual(selectNewClient(new Set(["/dev/pts/1"]), new Set(["/dev/pts/1", "/dev/pts/7"])), "/dev/pts/7");
  assert.strictEqual(selectNewClient(new Set(["/dev/pts/1"]), new Set(["/dev/pts/1"])), null);
  assert.strictEqual(selectNewClient(new Set(["/dev/pts/1"]), new Set(["/dev/pts/1", "/dev/pts/7", "/dev/pts/8"])), null);
});

test("falls back only when exactly one tmux client is present", () => {
  assert.strictEqual(selectOnlyClient(new Set(["/dev/pts/7"])), "/dev/pts/7");
  assert.strictEqual(selectOnlyClient(new Set()), null);
  assert.strictEqual(selectOnlyClient(new Set(["/dev/pts/7", "/dev/pts/8"])), null);
});

test("parses bounded printable client state", () => {
  assert.deepStrictEqual(parseClientState("0:%0:0\n"), {
    activeIndex: 0, paneId: "%0", copyMode: false,
  });
  assert.deepStrictEqual(parseClientState("2:%17:1\n"), {
    activeIndex: 2, paneId: "%17", copyMode: true,
  });
  assert.strictEqual(parseClientState("0_%0_0\n"), null);
  assert.strictEqual(parseClientState("10000:%0:0\n"), null);
});

test("parses bounded printable window rows and preserves name colons", () => {
  assert.deepStrictEqual(parseWindowRows("0:main\n2:node:server\ninvalid\n"), [
    { index: 0, name: "main" }, { index: 2, name: "node:server" },
  ]);
});

test("builds tmux commands only from trusted metadata and validated values", () => {
  const meta = { session: "cam-a", socket: "/tmp/cam a.sock", bin: "/bin/tmux" };
  const command = tmuxCommand(meta, ["switch-client", "-c", "/dev/pts/7", "-t", "cam-a:2"]);
  assert.ok(command.includes("'/tmp/cam a.sock'"));
  assert.ok(command.includes("'switch-client'"));
  assert.throws(() => tmuxCommand(meta, ["switch-client", "-t", "bad; rm -rf /"]), /unsafe tmux target/);
});

console.log("\n" + pass + " passed, " + fail + " failed");
process.exitCode = fail ? 1 : 0;
