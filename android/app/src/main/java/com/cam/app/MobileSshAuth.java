package com.cam.app;

import com.jcraft.jsch.ChannelDirectTCPIP;
import com.jcraft.jsch.JSch;
import com.jcraft.jsch.Session;
import com.jcraft.jsch.SocketFactory;

import org.json.JSONObject;

import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.util.Properties;

/** Resolve SSH auth from context machine records + credential store. */
public final class MobileSshAuth {

    public static final class Options {
        public String host = "";
        public int port = 22;
        public String user = "";
        public String authMethod = "key";
        public String keyFile = "";
        public String password = "";
        public String passphrase = "";
        /** ProxyJump chain (desktop parity): next hop; resolved by the hub. */
        public Options jump;
    }

    /** Max ProxyJump chain depth (desktop JUMP_MAX_DEPTH parity). */
    public static final int JUMP_MAX_DEPTH = 3;

    /** Jump sessions backing chained target sessions, for disconnectFully(). */
    private static final java.util.Map<Session, Session> JUMP_SESSIONS =
        new java.util.concurrent.ConcurrentHashMap<>();

    private MobileSshAuth() {}

    public static Options fromMachine(JSONObject machine, MobileCredentialStore creds) {
        Options o = new Options();
        if (machine == null) return o;
        o.host = machine.optString("host", "").trim();
        o.port = machine.optInt("port", 22);
        o.user = machine.optString("user", "").trim();
        o.authMethod = machine.optString("auth_method", "key");
        o.keyFile = machine.optString("key_file", "").trim();
        if (machine.optBoolean("credential_saved", false) && creds != null) {
            String ref = machine.optString("credential_ref", "").trim();
            if (!ref.isEmpty()) {
                String secret = creds.get(ref);
                if (secret != null) {
                    String kind = machine.optString("credential_kind", "");
                    if ("password".equals(kind)) o.password = secret;
                    else if ("passphrase".equals(kind)) o.passphrase = secret;
                }
            }
        }
        return o;
    }

    public static Session connect(Options opts, int timeoutMs) throws Exception {
        return connect(opts, timeoutMs, false);
    }

    /** @param longLived terminal attach — disable JSch rekey timeout after connect. */
    public static Session connect(Options opts, int timeoutMs, boolean longLived) throws Exception {
        if (opts == null) throw new IllegalArgumentException("opts required");
        if (opts.host.isEmpty()) throw new IllegalArgumentException("host is required");
        if (opts.user.isEmpty()) throw new IllegalArgumentException("user is required");

        JSch jsch = new JSch();
        String method = opts.authMethod != null ? opts.authMethod : "key";
        if ("password".equals(method)) {
            if (opts.password == null || opts.password.isEmpty()) {
                throw new IllegalArgumentException("password is required for password auth");
            }
        } else if ("key".equals(method)) {
            if (opts.keyFile == null || opts.keyFile.isEmpty()) {
                throw new IllegalArgumentException("key_file is required for key auth");
            }
            File key = new File(opts.keyFile);
            if (!key.isFile()) throw new IllegalArgumentException("SSH key not found: " + opts.keyFile);
            if (opts.passphrase != null && !opts.passphrase.isEmpty()) {
                jsch.addIdentity(key.getAbsolutePath(), opts.passphrase);
            } else {
                jsch.addIdentity(key.getAbsolutePath());
            }
        }

        Session session = jsch.getSession(opts.user, opts.host, opts.port > 0 ? opts.port : 22);
        Properties cfg = new Properties();
        cfg.put("StrictHostKeyChecking", "no");
        cfg.put("enable_server_sig_algs", "yes");
        if ("password".equals(method)) {
            cfg.put("PreferredAuthentications", "password,keyboard-interactive,publickey");
        }
        session.setConfig(cfg);
        if ("password".equals(method)) {
            session.setPassword(opts.password);
        }

        // CRITICAL: setServerAliveInterval() calls setTimeout(ms) internally.
        // Never call it before connect — old code used 25 meaning 25ms, causing
        // "timeout in waiting for rekeying process" during KEX (~1-2s on mobile).
        session.setTimeout(0);

        int budget = Math.max(10000, Math.min(timeoutMs, longLived ? 180000 : 120000));
        long t0 = System.currentTimeMillis();
        MobileHubLog.ssh("connect start " + MobileHubLog.endpoint(opts) + " budget=" + budget + "ms");
        try {
            if (opts.jump != null) {
                connectViaJump(session, opts, budget);
            } else {
                session.connect(budget);
            }
            MobileHubLog.ssh("connect ok " + (System.currentTimeMillis() - t0) + "ms "
                + MobileHubLog.endpoint(opts));
        } catch (Exception e) {
            MobileHubLog.ssh("connect fail " + (System.currentTimeMillis() - t0) + "ms "
                + MobileHubLog.endpoint(opts) + " " + e.getMessage());
            throw e;
        }

        if (longLived) {
            session.setServerAliveInterval(30000);
            session.setServerAliveCountMax(5);
            session.setTimeout(0);
        }
        // Short exec (sync/capture): no keepalive — setServerAliveInterval() sets Session
        // timeout and is pointless when we disconnect within seconds anyway.
        return session;
    }

    /** Chain: connect the jump host, then tunnel the target through direct-tcpip. */
    private static void connectViaJump(Session session, Options opts, int budgetMs) throws Exception {
        MobileHubLog.ssh("jump connect " + MobileHubLog.endpoint(opts.jump)
            + " -> " + MobileHubLog.endpoint(opts));
        Session jump = connect(opts.jump, budgetMs, false);
        ChannelDirectTCPIP ch;
        try {
            ch = (ChannelDirectTCPIP) jump.openChannel("direct-tcpip");
            ch.setHost(opts.host);
            ch.setPort(opts.port > 0 ? opts.port : 22);
            ch.connect(budgetMs);
        } catch (Exception e) {
            try { jump.disconnect(); } catch (Exception ignored) {}
            String msg = e.getMessage() != null ? e.getMessage() : "";
            throw new Exception("jump_forward_failed: bastion cannot open a tunnel to "
                + opts.host + ":" + (opts.port > 0 ? opts.port : 22)
                + " (" + msg + ")", e);
        }
        try {
            // JSch calls socket.setTcpNoDelay() on the factory socket — a null
            // or plain dummy Socket NPEs. Wrap the channel in a Socket subclass.
            Socket tunneled = new Socket() {
                @Override public void setTcpNoDelay(boolean on) { /* tunneled */ }
                @Override public InputStream getInputStream() throws java.io.IOException {
                    return ch.getInputStream();
                }
                @Override public OutputStream getOutputStream() throws java.io.IOException {
                    return ch.getOutputStream();
                }
                @Override public synchronized void close() {
                    try { ch.disconnect(); } catch (Exception ignored) {}
                }
                @Override public boolean isConnected() { return ch.isConnected(); }
                @Override public boolean isClosed() { return !ch.isConnected(); }
            };
            session.setSocketFactory(new SocketFactory() {
                public Socket createSocket(String host, int port) {
                    return tunneled;
                }
                public InputStream getInputStream(Socket socket) throws java.io.IOException {
                    return socket.getInputStream();
                }
                public OutputStream getOutputStream(Socket socket) throws java.io.IOException {
                    return socket.getOutputStream();
                }
            });
            session.connect(budgetMs);
            JUMP_SESSIONS.put(session, jump);
        } catch (Exception e) {
            try { ch.disconnect(); } catch (Exception ignored) {}
            try { jump.disconnect(); } catch (Exception ignored) {}
            String msg = e.getMessage() != null ? e.getMessage() : "";
            // Hop attribution: tunnel opened, but the target's handshake died
            // (bastion→target unreachable / target sshd dropped us).
            throw new Exception("target_handshake_failed via "
                + MobileHubLog.endpoint(opts.jump) + ": " + msg, e);
        }
    }

    /** Disconnect a session and, when chained, its jump session too. */
    public static void disconnectFully(Session session) {
        if (session == null) return;
        Session jump = JUMP_SESSIONS.remove(session);
        try { session.disconnect(); } catch (Exception ignored) {}
        if (jump != null) {
            try { jump.disconnect(); } catch (Exception ignored) {}
        }
    }
}
