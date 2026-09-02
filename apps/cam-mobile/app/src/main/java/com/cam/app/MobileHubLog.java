package com.cam.app;

/** Ring-buffer sink for embedded Hub + SSH diagnostics (no secrets). */
public final class MobileHubLog {

    public interface Sink {
        void log(String kind, String text);
    }

    private static volatile Sink sink;

    private MobileHubLog() {}

    public static void setSink(Sink s) {
        sink = s;
    }

    public static void info(String text) {
        emit("info", text);
    }

    public static void warn(String text) {
        emit("warn", text);
    }

    public static void ssh(String text) {
        emit("ssh", text);
    }

    public static String endpoint(MobileSshAuth.Options opts) {
        if (opts == null) return "?";
        int port = opts.port > 0 ? opts.port : 22;
        String auth = opts.authMethod != null ? opts.authMethod : "?";
        String cred = "password".equals(auth)
            ? (opts.password != null && !opts.password.isEmpty() ? "saved" : "missing")
            : "key".equals(auth) ? "key" : auth;
        return opts.user + "@" + opts.host + ":" + port + " auth=" + auth + " cred=" + cred;
    }

    private static void emit(String kind, String text) {
        Sink s = sink;
        if (s != null && text != null && !text.isEmpty()) {
            s.log(kind, text);
        }
    }
}
