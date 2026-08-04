package com.cam.app;

import com.jcraft.jsch.Session;

import java.util.ArrayList;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Long-lived SSH exec sessions for mobile Raw/Rich output polling.
 *
 * This is deliberately separate from MobileTerminalManager. Terminal owns its
 * interactive PTY session; Raw/Rich only open serial exec channels over this
 * per-agent SSH session for camc capture. Idle sessions close themselves after
 * ten minutes of inactivity.
 */
public final class MobileAgentOutputSessions {

    static final long IDLE_MS = 10L * 60L * 1000L;
    private static final int MAX_SESSIONS = 8;
    private static final ConcurrentHashMap<String, Entry> SESSIONS = new ConcurrentHashMap<>();

    private static final class Entry {
        final String key;
        final Object lock = new Object();
        Session session;
        long lastUsed;

        Entry(String key) {
            this.key = key;
            this.lastUsed = System.currentTimeMillis();
        }
    }

    private MobileAgentOutputSessions() {}

    public static MobileSshExec.Result exec(MobileSshAuth.Options opts, String agentId,
            String command, int timeoutMs) {
        return execInternal(opts, agentId, command, null, timeoutMs, false);
    }

    public static MobileSshExec.Result execStdin(MobileSshAuth.Options opts, String agentId,
            String command, byte[] stdin, int timeoutMs) {
        return execInternal(opts, agentId, command, stdin, timeoutMs, true);
    }

    private static MobileSshExec.Result execInternal(MobileSshAuth.Options opts, String agentId,
            String command, byte[] stdin, int timeoutMs, boolean hasStdin) {
        pruneIdle();
        String err = validate(opts, command);
        if (err != null) {
            MobileSshExec.Result r = new MobileSshExec.Result();
            r.error = "invalid_args";
            r.detail = err;
            return r;
        }
        String key = sessionKey(opts, agentId);
        Entry ent = SESSIONS.computeIfAbsent(key, Entry::new);
        synchronized (ent.lock) {
            ent.lastUsed = System.currentTimeMillis();
            try {
                ensureConnected(ent, opts);
                MobileSshExec.Result first = MobileSshExec.execOnSession(
                    ent.session, command, Math.max(5000, Math.min(timeoutMs, 120000)), stdin);
                ent.lastUsed = System.currentTimeMillis();
                if (first.ok || !isConnectionFailure(first)) return first;

                disconnect(ent);
                ensureConnected(ent, opts);
                MobileSshExec.Result second = MobileSshExec.execOnSession(
                    ent.session, command, Math.max(5000, Math.min(timeoutMs, 120000)), stdin);
                ent.lastUsed = System.currentTimeMillis();
                if (!second.ok && isConnectionFailure(second)) disconnect(ent);
                return second;
            } catch (Exception e) {
                disconnect(ent);
                MobileSshExec.Result r = new MobileSshExec.Result();
                String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
                r.error = msg.toLowerCase().contains("auth") ? "auth_failed" : "connect_failed";
                r.detail = msg;
                return r;
            } finally {
                ent.lastUsed = System.currentTimeMillis();
                pruneOversize();
            }
        }
    }

    private static String validate(MobileSshAuth.Options opts, String command) {
        if (opts == null || opts.host == null || opts.host.isEmpty()) return "host is required";
        if (opts.user == null || opts.user.isEmpty()) return "user is required";
        if (command == null || command.isEmpty()) return "command is required";
        String method = opts.authMethod != null ? opts.authMethod : "key";
        if ("password".equals(method) && (opts.password == null || opts.password.isEmpty())) {
            return "password auth is configured but no remembered password is available";
        }
        if ("key".equals(method) && (opts.keyFile == null || opts.keyFile.isEmpty())) {
            return "SSH key path is required for key auth";
        }
        return null;
    }

    private static String sessionKey(MobileSshAuth.Options opts, String agentId) {
        String id = agentId != null ? agentId.trim() : "";
        return MobileSshPool.poolKey(opts) + "|agent=" + id;
    }

    private static void ensureConnected(Entry ent, MobileSshAuth.Options opts) throws Exception {
        if (ent.session != null && ent.session.isConnected()) return;
        disconnect(ent);
        MobileHubLog.ssh("output session connect " + MobileHubLog.endpoint(opts));
        ent.session = MobileSshExec.openSession(opts, true);
    }

    private static boolean isConnectionFailure(MobileSshExec.Result r) {
        if (r == null) return true;
        String e = r.error != null ? r.error : "";
        String d = r.detail != null ? r.detail.toLowerCase() : "";
        return "connect_failed".equals(e)
            || "connect_timeout".equals(e)
            || d.contains("session is down")
            || d.contains("channel is not opened")
            || d.contains("socket closed")
            || d.contains("connection reset")
            || d.contains("broken pipe");
    }

    private static void disconnect(Entry ent) {
        if (ent == null || ent.session == null) return;
        try { MobileSshAuth.disconnectFully(ent.session); } catch (Exception ignored) {}
        ent.session = null;
    }

    private static void pruneIdle() {
        long now = System.currentTimeMillis();
        for (Map.Entry<String, Entry> row : SESSIONS.entrySet()) {
            Entry ent = row.getValue();
            if (now - ent.lastUsed <= IDLE_MS) continue;
            synchronized (ent.lock) {
                if (now - ent.lastUsed > IDLE_MS) {
                    disconnect(ent);
                    SESSIONS.remove(row.getKey(), ent);
                }
            }
        }
    }

    private static void pruneOversize() {
        if (SESSIONS.size() <= MAX_SESSIONS) return;
        ArrayList<Map.Entry<String, Entry>> rows = new ArrayList<>(SESSIONS.entrySet());
        rows.sort((a, b) -> Long.compare(a.getValue().lastUsed, b.getValue().lastUsed));
        for (Map.Entry<String, Entry> row : rows) {
            if (SESSIONS.size() <= MAX_SESSIONS) return;
            Entry ent = row.getValue();
            synchronized (ent.lock) {
                disconnect(ent);
                SESSIONS.remove(row.getKey(), ent);
            }
        }
    }

    public static void close(String agentId) {
        String suffix = "|agent=" + (agentId != null ? agentId.trim() : "");
        for (Map.Entry<String, Entry> row : SESSIONS.entrySet()) {
            if (!row.getKey().endsWith(suffix)) continue;
            Entry ent = row.getValue();
            synchronized (ent.lock) {
                disconnect(ent);
                SESSIONS.remove(row.getKey(), ent);
            }
        }
    }
}
