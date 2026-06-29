package com.cam.app;

import android.webkit.WebView;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.Session;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;

/** Desktop-shaped terminal: SSH PTY + `~/.cam/camc attach <id>`. */
public final class MobileTerminalManager {

    private static final int TERM_MIN_COLS = 40;
    private static final int TERM_MIN_ROWS = 4;
    private static final int CONNECT_MS = MobileSshPool.CONNECT_MS;

    private final MobileEmbeddedHub hub;
    private final WebView webView;
    private final AtomicInteger seq = new AtomicInteger();
    private final Map<String, Entry> sessions = new ConcurrentHashMap<>();
    private final Map<String, String> agentSessions = new ConcurrentHashMap<>();

    public MobileTerminalManager(MobileEmbeddedHub hub, WebView webView) {
        this.hub = hub;
        this.webView = webView;
    }

    private static final class Entry {
        Session session;
        ChannelExec channel;
        OutputStream stdin;
        Thread reader;
        volatile boolean active = true;
        volatile boolean streamReady = false;
        final StringBuilder pendingOut = new StringBuilder();
        String sessionId;
        String agentId;
        int cols;
        int rows;
    }

    public JSONObject open(String agentId, int cols, int rows, JSONObject hints) {
        String aid = agentId != null ? agentId.trim() : "";
        if (aid.isEmpty()) {
            return err("invalid_args", "agentId is required");
        }
        int c = clampCols(cols);
        int r = clampRows(rows);
        String sessionKey = agentSessionKey(aid, hints);

        String existing = agentSessions.get(sessionKey);
        if (existing != null && sessions.containsKey(existing)) {
            Entry ent = sessions.get(existing);
            if (ent != null && ent.active && channelAlive(ent)) {
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

        try {
            String command = plan.command;
            String sessionId = "t" + seq.incrementAndGet() + "-" + System.currentTimeMillis();
            MobileHubLog.ssh("terminal open start agent=" + aid + " "
                + MobileHubLog.endpoint(plan.auth));

            String key = MobileSshPool.poolKey(plan.auth);
            Session session;
            synchronized (MobileSshPool.lockFor(key)) {
                session = MobileSshExec.openSession(plan.auth, true);
            }

            ChannelExec channel = (ChannelExec) session.openChannel("exec");
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
            ent.cols = c;
            ent.rows = r;

            sessions.put(sessionId, ent);
            agentSessions.put(sessionKey, sessionId);

            ent.reader = new Thread(() -> pump(sessionId, ent), "cam-term-" + sessionId);
            ent.reader.setDaemon(true);
            ent.reader.start();

            // Hold PTY output until JS sends the first resize (final layout).
            MobileHubLog.ssh("terminal open ok session=" + sessionId + " agent=" + aid);
            return okSession(sessionId, false);
        } catch (Exception e) {
            MobileHubLog.ssh("terminal open fail agent=" + aid + " " + e.getMessage());
            String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            return err(msg.toLowerCase().contains("auth") ? "auth_failed" : "connect_failed", msg);
        }
    }

    public JSONObject input(String sessionId, String data) {
        try {
            Entry ent = sessions.get(sessionId);
            if (ent == null || !ent.active) return err("not_found", "terminal session not found");
            if (data != null && !data.isEmpty()) {
                ent.stdin.write(data.getBytes(StandardCharsets.UTF_8));
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
            if (c < TERM_MIN_COLS || r < TERM_MIN_ROWS) {
                return new JSONObject().put("ok", true).put("ignored", true);
            }
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
        markStreamReady(ent, ent.sessionId);
    }

    /** First resize from JS: drop pre-layout PTY bytes, then stream live output. */
    private void markStreamReady(Entry ent, String sessionId) {
        if (ent == null || sessionId == null || ent.streamReady) return;
        synchronized (ent.pendingOut) {
            ent.pendingOut.setLength(0);
        }
        ent.streamReady = true;
    }

    private void flushPending(Entry ent, String sessionId) {
        if (ent == null || sessionId == null || !ent.streamReady) return;
        String pending;
        synchronized (ent.pendingOut) {
            pending = ent.pendingOut.toString();
            ent.pendingOut.setLength(0);
        }
        if (!pending.isEmpty()) emitDataNow(sessionId, pending);
    }

    private void pump(String sessionId, Entry ent) {
        try {
            InputStream in = ent.channel.getInputStream();
            byte[] buf = new byte[8192];
            while (ent.active) {
                int n = in.read(buf);
                if (n < 0) break;
                if (n > 0) {
                    emitData(sessionId, new String(buf, 0, n, StandardCharsets.UTF_8));
                }
            }
        } catch (Exception ignored) {
        } finally {
            emitStatus(sessionId, "closed", 0, null);
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
        try { if (ent.session != null) ent.session.disconnect(); } catch (Exception ignored) {}
        ent.session = null;
    }

    private void emitData(String sessionId, String data) {
        if (webView == null || data == null || data.isEmpty()) return;
        Entry ent = sessions.get(sessionId);
        if (ent != null && !ent.streamReady) {
            synchronized (ent.pendingOut) {
                if (ent.pendingOut.length() < 512 * 1024) ent.pendingOut.append(data);
            }
            return;
        }
        emitDataNow(sessionId, data);
    }

    private void emitDataNow(String sessionId, String data) {
        if (webView == null || data == null || data.isEmpty()) return;
        try {
            JSONObject msg = new JSONObject();
            msg.put("sessionId", sessionId);
            msg.put("data", data);
            final String js = "window.__camTermEvent&&window.__camTermEvent('data',"
                + JSONObject.quote(msg.toString()) + ")";
            webView.post(() -> webView.evaluateJavascript(js, null));
        } catch (Exception ignored) {}
    }

    private void emitStatus(String sessionId, String kind, int code, String signal) {
        if (webView == null) return;
        try {
            JSONObject msg = new JSONObject();
            msg.put("sessionId", sessionId);
            msg.put("kind", kind);
            if (code != 0) msg.put("code", code);
            if (signal != null) msg.put("signal", signal);
            final String js = "window.__camTermEvent&&window.__camTermEvent('status',"
                + JSONObject.quote(msg.toString()) + ")";
            webView.post(() -> webView.evaluateJavascript(js, null));
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
