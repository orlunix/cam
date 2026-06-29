package com.cam.app;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.Session;

import java.nio.charset.StandardCharsets;
import java.io.ByteArrayOutputStream;

/** SSH exec for Sync Host (check camc + list agents). Key or password auth. */
public final class MobileSshExec {

    public static final class Result {
        public boolean ok;
        public String stdout = "";
        public String stderr = "";
        public int exitCode = -1;
        public String error = "";
        public String detail = "";
    }

    /** Run via login bash so $HOME expands reliably (JSch exec has no shell). */
    public static String shellCommand(String inner) {
        return "bash -lc " + shellQuote(inner);
    }

    public static String camcPath() {
        return "$HOME/.cam/camc";
    }

    public static String camcCheckCommand() {
        return shellCommand("test -x \"$HOME/.cam/camc\"");
    }

    public static String camcListCommand() {
        return shellCommand("\"$HOME/.cam/camc\" --json list");
    }

    public static String camcCaptureCommand(String agentId, int lines) {
        String id = agentId != null ? agentId.trim() : "";
        int n = lines > 0 ? Math.min(lines, 5000) : 200;
        return shellCommand(
            "\"$HOME/.cam/camc\" capture '" + id.replace("'", "'\\''") + "' --lines " + n);
    }

    public static String camcSendCommand(String agentId, boolean sendEnter) {
        String id = agentId != null ? agentId.trim() : "";
        String enterFlag = sendEnter ? "" : " --no-enter";
        return shellCommand(
            "\"$HOME/.cam/camc\" send '" + id.replace("'", "'\\''") + "' --stdin" + enterFlag);
    }

    public static String camcKeyCommand(String agentId, String key) {
        String id = agentId != null ? agentId.trim() : "";
        String k = key != null ? key.trim() : "";
        return shellCommand(
            "\"$HOME/.cam/camc\" key '" + id.replace("'", "'\\''") + "' --key '"
                + k.replace("'", "'\\''") + "'");
    }

    public static String camcAttachCommand(String agentId) {
        String id = agentId != null ? agentId.trim() : "";
        // Mobile: keep SSH exec channel alive — re-attach when tmux client exits
        // (app background, accidental detach, resize glitch) instead of EOF.
        return shellCommand(
            "export TERM=xterm-256color; "
                + "while :; do "
                + "\"$HOME/.cam/camc\" attach '" + id.replace("'", "'\\''") + "' "
                + "|| sleep 1; "
                + "done");
    }

    private static String shellQuote(String s) {
        return "'" + s.replace("'", "'\\''") + "'";
    }

    private MobileSshExec() {}

    private static String commandLabel(String command) {
        if (command == null) return "?";
        if (command.contains("camc\" --json list") || command.contains("camc --json list")) return "camc list";
        if (command.contains("test -x")) return "camc check";
        if (command.contains(" capture ")) return "camc capture";
        if (command.contains(" send ")) return "camc send";
        if (command.contains(" key ")) return "camc key";
        if (command.contains(" attach ")) return "camc attach";
        return "exec";
    }

    /** @deprecated use {@link MobileSshPool#CONNECT_MS} */
    private static final int SSH_CONNECT_MS = MobileSshPool.CONNECT_MS;

    public static Result exec(MobileSshAuth.Options opts, String command, int timeoutMs) {
        return execSequence(opts, new String[] { command }, timeoutMs).first();
    }

    /** Run one remote command with UTF-8 stdin (camc send --stdin). */
    public static Result execStdin(MobileSshAuth.Options opts, String command, byte[] stdin, int timeoutMs) {
        SequenceResult seq = new SequenceResult();
        if (opts == null || opts.host.isEmpty()) {
            seq.error = "invalid_args";
            seq.detail = "host is required";
            return seq.first();
        }
        if (opts.user.isEmpty()) {
            seq.error = "invalid_args";
            seq.detail = "user is required";
            return seq.first();
        }
        String method = opts.authMethod != null ? opts.authMethod : "key";
        if ("password".equals(method)) {
            if (opts.password == null || opts.password.isEmpty()) {
                seq.error = "credential_missing";
                seq.detail = "password auth is configured but no remembered password is available";
                return seq.first();
            }
        } else if ("key".equals(method)) {
            if (opts.keyFile == null || opts.keyFile.isEmpty()) {
                seq.error = "key_file_missing";
                seq.detail = "SSH key path is required for key auth";
                return seq.first();
            }
        }

        String key = MobileSshPool.poolKey(opts);
        Object lock = MobileSshPool.lockFor(key);
        Session session = null;
        int cmdBudget = Math.max(5000, Math.min(timeoutMs, 120000));
        synchronized (lock) {
            try {
                MobileHubLog.ssh("exec connect " + MobileHubLog.endpoint(opts));
                session = connectWithRetry(opts, false, 3);
                String label = commandLabel(command);
                MobileHubLog.ssh("exec start " + label + " " + MobileHubLog.endpoint(opts));
                long t0 = System.currentTimeMillis();
                Result step = execOnSession(session, command, cmdBudget, stdin);
                MobileHubLog.ssh("exec " + (step.ok ? "ok" : "fail") + " "
                    + (System.currentTimeMillis() - t0) + "ms " + label
                    + (step.ok ? "" : " " + step.detail));
                return step;
            } catch (Exception e) {
                String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
                Result r = new Result();
                r.error = msg.toLowerCase().contains("auth") ? "auth_failed" : "connect_failed";
                r.detail = msg;
                return r;
            } finally {
                if (session != null) {
                    try { session.disconnect(); } catch (Exception ignored) {}
                }
            }
        }
    }

    /** Connect with transient-failure retries (sync, capture, terminal attach). */
    public static Session openSession(MobileSshAuth.Options opts, boolean longLived) throws Exception {
        return connectWithRetry(opts, longLived, 3);
    }

    /** One SSH session, multiple exec channels — Sync Host runs check + list in one connect. */
    public static SequenceResult execSequence(MobileSshAuth.Options opts, String[] commands, int timeoutMs) {
        return execSequence(opts, commands, timeoutMs, 3);
    }

    public static SequenceResult execSequence(MobileSshAuth.Options opts, String[] commands,
            int timeoutMs, int connectAttempts) {
        SequenceResult seq = new SequenceResult();
        if (commands == null || commands.length == 0) {
            seq.error = "invalid_args";
            seq.detail = "command required";
            return seq;
        }
        seq.steps = new Result[commands.length];
        if (opts == null || opts.host.isEmpty()) {
            seq.error = "invalid_args";
            seq.detail = "host is required";
            return seq;
        }
        if (opts.user.isEmpty()) {
            seq.error = "invalid_args";
            seq.detail = "user is required";
            return seq;
        }
        String method = opts.authMethod != null ? opts.authMethod : "key";
        if ("password".equals(method)) {
            if (opts.password == null || opts.password.isEmpty()) {
                seq.error = "credential_missing";
                seq.detail = "password auth is configured but no remembered password is available";
                return seq;
            }
        } else if ("key".equals(method)) {
            if (opts.keyFile == null || opts.keyFile.isEmpty()) {
                seq.error = "key_file_missing";
                seq.detail = "SSH key path is required for key auth";
                return seq;
            }
        }

        String key = MobileSshPool.poolKey(opts);
        Object lock = MobileSshPool.lockFor(key);
        Session session = null;
        int cmdBudget = Math.max(5000, Math.min(timeoutMs, 120000));
        synchronized (lock) {
            try {
                MobileHubLog.ssh("exec connect " + MobileHubLog.endpoint(opts));
                session = connectWithRetry(opts, false, Math.max(1, connectAttempts));
                for (int i = 0; i < commands.length; i++) {
                    String label = commandLabel(commands[i]);
                    MobileHubLog.ssh("exec start " + label + " " + MobileHubLog.endpoint(opts));
                    long t0 = System.currentTimeMillis();
                    Result step = execOnSession(session, commands[i], cmdBudget, null);
                    MobileHubLog.ssh("exec " + (step.ok ? "ok" : "fail") + " "
                        + (System.currentTimeMillis() - t0) + "ms " + label
                        + (step.ok ? "" : " " + step.detail));
                    seq.steps[i] = step;
                    if (!step.ok) {
                        seq.error = step.error;
                        seq.detail = step.detail;
                        return seq;
                    }
                }
                seq.ok = true;
                return seq;
            } catch (Exception e) {
                String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
                seq.error = msg.toLowerCase().contains("auth") ? "auth_failed" : "connect_failed";
                seq.detail = msg;
                return seq;
            } finally {
                if (session != null) {
                    try { session.disconnect(); } catch (Exception ignored) {}
                }
            }
        }
    }

    private static Session connectWithRetry(MobileSshAuth.Options opts, boolean longLived, int attempts)
            throws Exception {
        Exception last = null;
        int max = Math.max(1, attempts);
        for (int i = 1; i <= max; i++) {
            try {
                if (i > 1) {
                    MobileHubLog.ssh("connect retry " + i + "/" + max + " "
                        + MobileHubLog.endpoint(opts));
                }
                return MobileSshAuth.connect(opts, MobileSshPool.CONNECT_MS, longLived);
            } catch (Exception e) {
                last = e;
                if (i >= max || !isTransientConnectError(e)) throw e;
                long backoff = i == 1 ? 600L : 1800L;
                MobileHubLog.ssh("connect backoff " + backoff + "ms after "
                    + (e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName()));
                try { Thread.sleep(backoff); } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    throw e;
                }
            }
        }
        if (last != null) throw last;
        throw new IllegalStateException("connectWithRetry exhausted");
    }

    private static boolean isTransientConnectError(Exception e) {
        String msg = e.getMessage() != null ? e.getMessage().toLowerCase() : "";
        return msg.contains("rekeying")
            || msg.contains("timeout")
            || msg.contains("connection reset")
            || msg.contains("broken pipe")
            || msg.contains("connection closed")
            || msg.contains("socket closed")
            || msg.contains("failed to connect");
    }

    private static Result execOnSession(Session session, String command, int cmdBudget, byte[] stdin) {
        Result out = new Result();
        ChannelExec channel = null;
        try {
            channel = (ChannelExec) session.openChannel("exec");
            channel.setCommand(command);
            if (stdin != null && stdin.length > 0) {
                channel.setInputStream(new java.io.ByteArrayInputStream(stdin));
            }
            ByteArrayOutputStream stdout = new ByteArrayOutputStream();
            ByteArrayOutputStream stderr = new ByteArrayOutputStream();
            channel.setOutputStream(stdout);
            channel.setErrStream(stderr);
            channel.connect(MobileSshPool.CONNECT_MS);

            long deadline = System.currentTimeMillis() + cmdBudget;
            while (!channel.isClosed()) {
                if (System.currentTimeMillis() > deadline) {
                    out.error = "connect_timeout";
                    out.detail = "SSH command timed out";
                    return out;
                }
                try { Thread.sleep(50); } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    out.error = "interrupted";
                    out.detail = "SSH command interrupted";
                    return out;
                }
            }

            out.exitCode = channel.getExitStatus();
            out.stdout = stdout.toString(StandardCharsets.UTF_8.name());
            out.stderr = stderr.toString(StandardCharsets.UTF_8.name());
            out.ok = out.exitCode == 0;
            if (!out.ok) {
                out.error = "remote_nonzero";
                out.detail = (out.stderr != null && !out.stderr.isEmpty()) ? out.stderr.trim()
                    : ("remote command exited " + out.exitCode);
            }
            return out;
        } catch (Exception e) {
            String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            out.error = msg.toLowerCase().contains("auth") ? "auth_failed" : "connect_failed";
            out.detail = msg;
            return out;
        } finally {
            if (channel != null) try { channel.disconnect(); } catch (Exception ignored) {}
        }
    }

    public static final class SequenceResult {
        public boolean ok;
        public Result[] steps;
        public String error = "";
        public String detail = "";

        public Result first() {
            if (steps != null && steps.length > 0 && steps[0] != null) return steps[0];
            Result r = new Result();
            r.ok = ok;
            r.error = error;
            r.detail = detail;
            return r;
        }
    }
}
