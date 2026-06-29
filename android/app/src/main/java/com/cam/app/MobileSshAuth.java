package com.cam.app;

import com.jcraft.jsch.JSch;
import com.jcraft.jsch.Session;

import org.json.JSONObject;

import java.io.File;
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
    }

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
            session.connect(budget);
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
}
