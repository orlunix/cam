package com.cam.app;

import java.util.concurrent.ConcurrentHashMap;

/**
 * Per-host mutex only — no session reuse.
 * Reusing one JSch Session for PTY attach + exec capture caused dead/stuck connects on mobile.
 */
public final class MobileSshPool {

    public static final int CONNECT_MS = 120000;

    private static final ConcurrentHashMap<String, Object> LOCKS = new ConcurrentHashMap<>();

    private MobileSshPool() {}

    public static String poolKey(MobileSshAuth.Options opts) {
        if (opts == null) return "";
        int port = opts.port > 0 ? opts.port : 22;
        return opts.user + "@" + opts.host + ":" + port;
    }

    public static Object lockFor(String key) {
        if (key == null || key.isEmpty()) return new Object();
        return LOCKS.computeIfAbsent(key, k -> new Object());
    }

    /** @deprecated session pooling removed — kept for call-site compat */
    public static void evictOnError(MobileSshAuth.Options opts) {
        /* no-op */
    }
}
