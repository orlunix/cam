"use strict";

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function safeValue(value) {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 512
    && !/[\r\n\0;&|`$<>]/.test(value);
}

function tmuxMetadataForAgent(agent) {
  if (!agent || typeof agent !== "object") return null;
  const runtimeTmux = agent.runtime && typeof agent.runtime === "object"
    && agent.runtime.tmux && typeof agent.runtime.tmux === "object"
    ? agent.runtime.tmux : {};
  const session = String(agent.tmux_session || agent.session || "");
  const socket = String(agent.tmux_socket || runtimeTmux.socket || "");
  // The recorded binary always wins (it is the exact binary that started
  // the session's server). When older agents have none, bin stays EMPTY
  // here — no hardcoded /bin/tmux guess (it does not exist on e.g.
  // Homebrew macOS). Callers probe the remote once per endpoint
  // (see _probeRemoteTmuxBin in main.cjs); commands are only built once
  // bin is non-empty, so a failed probe degrades controls quietly.
  const bin = String(agent.tmux_bin || runtimeTmux.bin || "");
  if (!safeValue(session) || !safeValue(socket)) return null;
  if (bin && !safeValue(bin)) return null;
  if (!socket.startsWith("/")) return null;
  if (bin && !bin.startsWith("/")) return null;
  return { session, socket, bin };
}

function selectNewClient(before, after) {
  const added = [...after].filter((tty) => !before.has(tty));
  return added.length === 1 ? added[0] : null;
}

function selectOnlyClient(clients) {
  if (!(clients instanceof Set) || clients.size !== 1) return null;
  const [tty] = clients;
  return /^\/dev\/pts\/\d+$/.test(tty) ? tty : null;
}

function parseClientState(raw) {
  const match = /^(\d+):(%\d+):([01])$/.exec(String(raw || "").trim());
  if (!match) return null;
  const activeIndex = Number(match[1]);
  if (!Number.isInteger(activeIndex) || activeIndex < 0 || activeIndex > 9999) return null;
  return {
    activeIndex,
    paneId: match[2],
    copyMode: match[3] === "1",
  };
}

function parseWindowRows(raw) {
  return String(raw || "").split(/\r?\n/).flatMap((line) => {
    const separator = line.indexOf(":");
    if (separator < 1) return [];
    const index = Number(line.slice(0, separator));
    const name = line.slice(separator + 1);
    if (!Number.isInteger(index) || index < 0 || index > 9999 || name.length > 256 || /[\r\n\0]/.test(name)) return [];
    return [{ index, name }];
  });
}

function tmuxCommand(meta, args) {
  if (!meta || !safeValue(meta.session) || !safeValue(meta.socket) || !safeValue(meta.bin)
      || !meta.socket.startsWith("/") || !meta.bin.startsWith("/")) {
    throw new Error("invalid tmux metadata");
  }
  if (!Array.isArray(args) || args.length === 0 || !args.every(safeValue)) {
    throw new Error("unsafe tmux target");
  }
  return [meta.bin, "-S", meta.socket, ...args].map(shellQuote).join(" ");
}

module.exports = {
  tmuxMetadataForAgent,
  selectOnlyClient,
  selectNewClient,
  parseClientState,
  parseWindowRows,
  tmuxCommand,
};
