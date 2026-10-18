package com.cam.app;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.Session;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/** Desktop-shaped terminal: SSH PTY + `~/.cam/camc attach <id>`. */
public final class MobileTerminalManager {

    private static final int TERM_MIN_COLS = 40;
    private static final int TERM_MIN_ROWS = 4;
    private static final int CONNECT_MS = MobileSshPool.CONNECT_MS;
    private static final int MAX_PENDING_BYTES = 512 * 1024;

    private final MobileEmbeddedHub hub;
    private final AtomicInteger seq = new AtomicInteger();
    private final Map<String, Entry> sessions = new ConcurrentHashMap<>();
    private final Map<String, String> agentSessions = new ConcurrentHashMap<>();

    public MobileTerminalManager(MobileEmbeddedHub hub) {
        this.hub = hub;
    }

    private static final class Entry {
        Session session;
        ChannelExec channel;
        OutputStream stdin;
        Thread reader;
        MobileWebSocket.Connection client;
        volatile boolean active = true;
        volatile boolean streamReady = false;
        final ByteArrayOutputStream pendingOut = new ByteArrayOutputStream();
        String sessionId;
        String agentId;
        int cols;
        int rows;
    }

    public JSONObject open(String agentId, int cols, int rows, JSONObject hints,
            MobileWebSocket.Connection client) {
        String aid = agentId != null ? agentId.trim() : "";
        if (aid.isEmpty()) return err("invalid_args", "agentId is required");
        if (client == null) return err("invalid_args", "terminal WebSocket is required");
        int c = clampCols(cols);
        int r = clampRows(rows);
        String sessionKey = agentSessionKey(aid, hints);

        String existing = agentSessions.get(sessionKey);
        if (existing != null && sessions.containsKey(existing)) {
            Entry ent = sessions.get(existing);
            if (ent != null && ent.active && channelAlive(ent)) {
                replaceClient(ent, client);
                resizeEntry(ent, c, r);
                try {
                    return okSession(existing, true);
                } catch (Exception e) {
                    return err("internal_error", e.getMessage());
                }
            }
            if (existing != null) drop(existing);
        }

        JSONObject resolved;
        MobileEmbeddedHub.AttachPlan plan;
        try {
            plan = hub.resolveAttachPlan(aid, hints);
        } catch (Exception e) {
            return err("internal_error", e.getMessage());
        }
        if (!plan.ok()) {
            return err(plan.error != null && !plan.error.isEmpty() ? plan.error : "attach_failed",
                plan.detail != null ? plan.detail : "attach failed");
        }

        Session session = null;
        ChannelExec channel = null;
        try {
            String command = plan.command;
            String sessionId = "t" + seq.incrementAndGet() + "-" + System.currentTimeMillis();
            MobileHubLog.ssh("terminal open start agent=" + aid + " "
                + MobileHubLog.endpoint(plan.auth));

            String key = MobileSshPool.poolKey(plan.auth);
            synchronized (MobileSshPool.lockFor(key)) {
                session = MobileSshExec.openSession(plan.auth, true);
            }

            channel = (ChannelExec) session.openChannel("exec");
            channel.setCommand(command);
            channel.setPty(true);
            channel.setPtyType("xterm-256color");
            channel.setPtySize(c, r, c * 8, r * 16);
            channel.connect(CONNECT_MS);

            Entry ent = new Entry();
            ent.session = session;
            ent.channel = channel;
            ent.stdin = channel.getOutputStream();
            ent.agentId = aid;
            ent.sessionId = sessionId;
            ent.client = client;
            ent.cols = c;
            ent.rows = r;

            sessions.put(sessionId, ent);
            agentSessions.put(sessionKey, sessionId);

            ent.reader = new Thread(() -> pump(sessionId, ent), "cam-term-" + sessionId);
            ent.reader.setDaemon(true);
            ent.reader.start();

            MobileHubLog.ssh("terminal open ok session=" + sessionId + " agent=" + aid);
            return okSession(sessionId, false);
        } catch (Exception e) {
            try { if (channel != null) channel.disconnect(); } catch (Exception ignored) {}
            try { if (session != null) MobileSshAuth.disconnectFully(session); } catch (Exception ignored) {}
            MobileHubLog.ssh("terminal open fail agent=" + aid + " " + e.getMessage());
            String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            String error = msg.toLowerCase().contains("host_key_changed") ? "host_key_changed"
                : (msg.toLowerCase().contains("auth") ? "auth_failed" : "connect_failed");
            return err(error, msg);
        }
    }

    public JSONObject input(String sessionId, byte[] data) {
        try {
            Entry ent = sessions.get(sessionId);
            if (ent == null || !ent.active) return err("not_found", "terminal session not found");
            if (data != null && data.length > 0) {
                ent.stdin.write(data);
                ent.stdin.flush();
            }
            return new JSONObject().put("ok", true);
        } catch (Exception e) {
            return err("input_failed", e.getMessage());
        }
    }

    public JSONObject resize(String sessionId, int cols, int rows) {
        try {
            Entry ent = sessions.get(sessionId);
            if (ent == null || !ent.active) return err("not_found", "terminal session not found");
            int c = clampCols(cols);
            int r = clampRows(rows);
            resizeEntry(ent, c, r);
            return new JSONObject().put("ok", true);
        } catch (Exception e) {
            return err("resize_failed", e.getMessage());
        }
    }

    public JSONObject close(String sessionId) {
        try {
            drop(sessionId);
            return new JSONObject().put("ok", true);
        } catch (Exception e) {
            return err("close_failed", e.getMessage());
        }
    }

    public void closeClient(String sessionId, MobileWebSocket.Connection client) {
        Entry ent = sessions.get(sessionId);
        if (ent != null && ent.client == client) drop(sessionId);
    }

    /** tmux copy-mode control on the terminal's long-lived SSH session. */
    public JSONObject copyMode(String sessionId, String action) {
        try {
            Entry ent = sessions.get(sessionId);
            if (ent == null || !ent.active) return err("not_found", "terminal session not found");
            if (ent.session != null && ent.session.isConnected()) {
                JSONObject fast = hub.terminalCopyModeOnSession(ent.session, ent.agentId, action);
                if (fast != null && fast.optBoolean("ok", false)) return fast;
                MobileHubLog.ssh("copymode fast-path failed, falling back: "
                    + (fast != null ? fast.optString("detail", "") : "null"));
            }
            return hub.terminalCopyMode(ent.agentId, null, action);
        } catch (Exception e) {
            return err("internal_error", e.getMessage());
        }
    }

    public boolean hasActiveSessions() {
        for (Entry ent : sessions.values()) {
            if (ent != null && ent.active && channelAlive(ent)) return true;
        }
        return false;
    }

    public int activeSessionCount() {
        int count = 0;
        for (Entry ent : sessions.values()) {
            if (ent != null && ent.active && channelAlive(ent)) count++;
        }
        return count;
    }

    public void closeAllSessions() {
        for (String sessionId : sessions.keySet()) drop(sessionId);
    }

    private static boolean channelAlive(Entry ent) {
        try {
            return ent != null && ent.channel != null && ent.channel.isConnected();
        } catch (Exception e) {
            return false;
        }
    }

    private void resizeEntry(Entry ent, int cols, int rows) {
        ent.cols = cols;
        ent.rows = rows;
        try {
            if (ent.channel != null && ent.channel.isConnected()) {
                ent.channel.setPtySize(cols, rows, cols * 8, rows * 16);
            }
        } catch (Exception ignored) {}
        markStreamReady(ent);
    }

    /** First resize from JS: drop pre-layout PTY bytes, then stream live output. */
    private void markStreamReady(Entry ent) {
        if (ent == null || ent.streamReady) return;
        synchronized (ent.pendingOut) {
            ent.pendingOut.reset();
        }
        ent.streamReady = true;
    }

    private void pump(String sessionId, Entry ent) {
        try {
            InputStream in = ent.channel.getInputStream();
            byte[] buf = new byte[8192];
            while (ent.active) {
                int n = in.read(buf);
                if (n < 0) break;
                if (n > 0) emitData(ent, buf, n);
            }
        } catch (Exception ignored) {
        } finally {
            emitStatus(ent, "closed", 0, null);
            drop(sessionId);
        }
    }

    private static String agentSessionKey(String agentId, JSONObject hints) {
        if (hints == null) return agentId;
        String host = hints.optString("machine_host", "").trim();
        if (host.isEmpty()) return agentId;
        String user = hints.optString("machine_user", "").trim();
        String port = hints.has("machine_port") && !hints.isNull("machine_port")
            ? hints.optString("machine_port", "22") : "22";
        return agentId + "|" + user + "@" + host + ":" + port;
    }

    private void replaceClient(Entry ent, MobileWebSocket.Connection client) {
        MobileWebSocket.Connection previous = ent.client;
        ent.client = client;
        if (previous != null && previous != client) previous.close();
    }

    private void drop(String sessionId) {
        Entry ent = sessions.remove(sessionId);
        if (ent == null) return;
        ent.active = false;
        if (ent.agentId != null) {
            for (Map.Entry<String, String> e : agentSessions.entrySet()) {
                if (sessionId.equals(e.getValue())) {
                    agentSessions.remove(e.getKey());
                    break;
                }
            }
        }
        try { if (ent.channel != null) ent.channel.disconnect(); } catch (Exception ignored) {}
        try { if (ent.session != null) MobileSshAuth.disconnectFully(ent.session); } catch (Exception ignored) {}
        MobileWebSocket.Connection client = ent.client;
        ent.client = null;
        ent.session = null;
        if (client != null) client.close();
    }

    private void emitData(Entry ent, byte[] data, int length) throws Exception {
        if (data == null || length <= 0) return;
        if (!ent.streamReady || ent.client == null) {
            synchronized (ent.pendingOut) {
                int remaining = MAX_PENDING_BYTES - ent.pendingOut.size();
                if (remaining > 0) ent.pendingOut.write(data, 0, Math.min(remaining, length));
            }
            return;
        }
        MobileWebSocket.Connection client = ent.client;
        if (client != null) client.sendBinary(data, 0, length);
    }

    private void emitStatus(Entry ent, String kind, int code, String signal) {
        try {
            MobileWebSocket.Connection client = ent.client;
            if (client == null) return;
            JSONObject msg = new JSONObject();
            msg.put("type", "status");
            msg.put("sessionId", ent.sessionId);
            msg.put("kind", kind);
            if (code != 0) msg.put("code", code);
            if (signal != null) msg.put("signal", signal);
            client.sendText(msg.toString());
        } catch (Exception ignored) {}
    }

    private static int clampCols(int cols) {
        if (cols < TERM_MIN_COLS) return TERM_MIN_COLS;
        return Math.min(500, cols);
    }

    private static int clampRows(int rows) {
        if (rows < TERM_MIN_ROWS) return TERM_MIN_ROWS;
        return Math.min(500, rows);
    }

    private static JSONObject okSession(String sessionId, boolean reused) throws Exception {
        return new JSONObject().put("ok", true).put("sessionId", sessionId).put("reused", reused);
    }

    private static JSONObject err(String error, String detail) {
        try {
            return new JSONObject().put("ok", false).put("error", error).put("detail", detail != null ? detail : "");
        } catch (Exception e) {
            return new JSONObject();
        }
    }
}
