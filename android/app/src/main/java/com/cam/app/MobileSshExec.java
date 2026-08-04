package com.cam.app;

import com.jcraft.jsch.ChannelExec;
import com.jcraft.jsch.ChannelSftp;
import com.jcraft.jsch.SftpException;
import com.jcraft.jsch.SftpProgressMonitor;
import com.jcraft.jsch.Session;

import java.nio.charset.StandardCharsets;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Vector;

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

    /** Probe: camc present AND runnable — prints its version on success. */
    public static String camcProbeCommand() {
        return shellCommand("test -x \"$HOME/.cam/camc\" && \"$HOME/.cam/camc\" version | head -1");
    }

    public static String camcListCommand() {
        return shellCommand("\"$HOME/.cam/camc\" --json list");
    }

    public static String camcRunCommand(String tool, String path, String prompt, String name, boolean autoExit) {
        StringBuilder inner = new StringBuilder("\"$HOME/.cam/camc\" --json run -t ")
            .append(argQuote(tool != null && !tool.trim().isEmpty() ? tool.trim() : "claude"))
            .append(" -p ").append(argQuote(path != null ? path.trim() : ""));
        if (name != null && !name.trim().isEmpty()) {
            inner.append(" -n ").append(argQuote(name.trim()));
        }
        if (autoExit) inner.append(" --auto-exit");
        inner.append(" ").append(argQuote(prompt != null ? prompt : ""));
        return shellCommand(inner.toString());
    }

    public static String camcStatusCommand(String agentId) {
        String id = agentId != null ? agentId.trim() : "";
        return shellCommand("\"$HOME/.cam/camc\" --json status " + argQuote(id));
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

    public static String camcStopCommand(String agentId) {
        String id = agentId != null ? agentId.trim() : "";
        return shellCommand(
            "\"$HOME/.cam/camc\" stop '" + id.replace("'", "'\\''") + "'");
    }

    public static String camcKillCommand(String agentId) {
        String id = agentId != null ? agentId.trim() : "";
        return shellCommand(
            "\"$HOME/.cam/camc\" kill '" + id.replace("'", "'\\''") + "'");
    }

    public static String camcRemoveCommand(String agentId) {
        String id = agentId != null ? agentId.trim() : "";
        // --kill matches desktop parity; it is a deprecated no-op in modern
        // camc (rm always kills tmux).
        return shellCommand(
            "\"$HOME/.cam/camc\" rm '" + id.replace("'", "'\\''") + "' --kill");
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

    private static String argQuote(String s) {
        return "'" + (s != null ? s : "").replace("'", "'\\''") + "'";
    }

    private MobileSshExec() {}

    private static String commandLabel(String command) {
        if (command == null) return "?";
        if (command.contains("camc\" --json list") || command.contains("camc --json list")) return "camc list";
        if (command.contains(" --json run ")) return "camc run";
        if (command.contains(" --json status ")) return "camc status";
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

    /** Upload bytes to an absolute remote path through the same authenticated SSH route. */
    public static Result uploadFile(MobileSshAuth.Options opts, String remotePath, byte[] content, int timeoutMs) {
        Result out = new Result();
        if (opts == null || opts.host == null || opts.host.isEmpty()) {
            out.error = "invalid_args";
            out.detail = "host is required";
            return out;
        }
        if (opts.user == null || opts.user.isEmpty()) {
            out.error = "invalid_args";
            out.detail = "user is required";
            return out;
        }
        if (remotePath == null || !remotePath.startsWith("/")) {
            out.error = "invalid_args";
            out.detail = "absolute remote path is required";
            return out;
        }
        String method = opts.authMethod != null ? opts.authMethod : "key";
        if ("password".equals(method) && (opts.password == null || opts.password.isEmpty())) {
            out.error = "credential_missing";
            out.detail = "password auth is configured but no remembered password is available";
            return out;
        }
        if ("key".equals(method) && (opts.keyFile == null || opts.keyFile.isEmpty())) {
            out.error = "key_file_missing";
            out.detail = "SSH key path is required for key auth";
            return out;
        }
        String key = MobileSshPool.poolKey(opts);
        Object lock = MobileSshPool.lockFor(key);
        Session session = null;
        ChannelSftp channel = null;
        synchronized (lock) {
            try {
                MobileHubLog.ssh("upload connect " + MobileHubLog.endpoint(opts));
                session = connectWithRetry(opts, false, 3);
                channel = (ChannelSftp) session.openChannel("sftp");
                channel.connect(Math.max(5000, Math.min(timeoutMs, 120000)));
                ensureRemoteDirectories(channel, remotePath.substring(0, remotePath.lastIndexOf('/')));
                channel.put(new ByteArrayInputStream(content != null ? content : new byte[0]), remotePath);
                out.ok = true;
                out.exitCode = 0;
                MobileHubLog.ssh("upload ok " + MobileHubLog.endpoint(opts));
                return out;
            } catch (Exception e) {
                String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
                out.error = msg.toLowerCase().contains("auth") ? "auth_failed" : "upload_failed";
                out.detail = msg;
                MobileHubLog.ssh("upload fail " + MobileHubLog.endpoint(opts) + " " + msg);
                return out;
            } finally {
                if (channel != null) try { channel.disconnect(); } catch (Exception ignored) {}
                if (session != null) try { session.disconnect(); } catch (Exception ignored) {}
            }
        }
    }

    /** Progress callback for uploadFileProgress (sent bytes so far, total). */
    public interface UploadProgress {
        void onProgress(long sent, long total);
    }

    /** uploadFile with byte-level progress (SFTP monitor), for sync step display. */
    public static Result uploadFileProgress(MobileSshAuth.Options opts, String remotePath,
            byte[] content, int timeoutMs, UploadProgress progress) {
        Result out = new Result();
        if (opts == null || opts.host == null || opts.host.isEmpty()) {
            out.error = "invalid_args";
            out.detail = "host is required";
            return out;
        }
        if (opts.user == null || opts.user.isEmpty()) {
            out.error = "invalid_args";
            out.detail = "user is required";
            return out;
        }
        String key = MobileSshPool.poolKey(opts);
        Object lock = MobileSshPool.lockFor(key);
        Session session = null;
        ChannelSftp channel = null;
        synchronized (lock) {
            try {
                MobileHubLog.ssh("upload connect " + MobileHubLog.endpoint(opts));
                session = connectWithRetry(opts, false, 3);
                channel = (ChannelSftp) session.openChannel("sftp");
                channel.connect(Math.max(5000, Math.min(timeoutMs, 120000)));
                ensureRemoteDirectories(channel, remotePath.substring(0, remotePath.lastIndexOf('/')));
                final long total = content != null ? content.length : 0;
                channel.put(new ByteArrayInputStream(content != null ? content : new byte[0]),
                    remotePath, new SftpProgressMonitor() {
                        public void init(int op, String src, String dest, long max) {
                            if (progress != null) progress.onProgress(0, total);
                        }
                        public boolean count(long n) {
                            if (progress != null) progress.onProgress(n, total);
                            return true;
                        }
                        public void end() {
                            if (progress != null) progress.onProgress(total, total);
                        }
                    });
                out.ok = true;
                out.exitCode = 0;
                MobileHubLog.ssh("upload ok " + MobileHubLog.endpoint(opts));
                return out;
            } catch (Exception e) {
                String msg = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
                out.error = msg.toLowerCase().contains("auth") ? "auth_failed" : "upload_failed";
                out.detail = msg;
                MobileHubLog.ssh("upload fail " + MobileHubLog.endpoint(opts) + " " + msg);
                return out;
            } finally {
                if (channel != null) try { channel.disconnect(); } catch (Exception ignored) {}
                if (session != null) try { session.disconnect(); } catch (Exception ignored) {}
            }
        }
    }

    private static void ensureRemoteDirectories(ChannelSftp channel, String directory) throws SftpException {
        if (directory == null || directory.isEmpty() || "/".equals(directory)) return;
        String current = directory.startsWith("/") ? "/" : "";
        for (String part : directory.split("/")) {
            if (part == null || part.isEmpty()) continue;
            current = current.isEmpty() || "/".equals(current) ? current + part : current + "/" + part;
            try { channel.stat(current); }
            catch (SftpException missing) { channel.mkdir(current); }
        }
    }

    public static final class RemoteEntry {
        public String name;
        public boolean directory;
        public long size;
        public long modified;
    }

    public static final class DirectoryResult {
        public boolean ok;
        public List<RemoteEntry> entries = new ArrayList<>();
        public String error = "";
        public String detail = "";
    }

    public static final class FileResult {
        public boolean ok;
        public byte[] content = new byte[0];
        public String error = "";
        public String detail = "";
    }

    public static DirectoryResult listDirectory(MobileSshAuth.Options opts, String remotePath, int timeoutMs) {
        DirectoryResult out = new DirectoryResult();
        Session session = null;
        ChannelSftp channel = null;
        try {
            validateSftpArgs(opts, remotePath);
            session = connectWithRetry(opts, false, 3);
            channel = (ChannelSftp) session.openChannel("sftp");
            channel.connect(Math.max(5000, Math.min(timeoutMs, 120000)));
            @SuppressWarnings("unchecked") Vector<ChannelSftp.LsEntry> rows = channel.ls(remotePath);
            for (ChannelSftp.LsEntry row : rows) {
                String name = row.getFilename();
                if (".".equals(name) || "..".equals(name)) continue;
                RemoteEntry entry = new RemoteEntry();
                entry.name = name;
                entry.directory = row.getAttrs().isDir();
                entry.size = row.getAttrs().getSize();
                entry.modified = ((long) row.getAttrs().getMTime()) * 1000L;
                out.entries.add(entry);
            }
            out.ok = true;
        } catch (Exception e) {
            out.error = "list_failed";
            out.detail = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
        } finally {
            if (channel != null) try { channel.disconnect(); } catch (Exception ignored) {}
            if (session != null) try { session.disconnect(); } catch (Exception ignored) {}
        }
        return out;
    }

    public static FileResult readFile(MobileSshAuth.Options opts, String remotePath, int timeoutMs, int maxBytes) {
        FileResult out = new FileResult();
        Session session = null;
        ChannelSftp channel = null;
        try {
            validateSftpArgs(opts, remotePath);
            session = connectWithRetry(opts, false, 3);
            channel = (ChannelSftp) session.openChannel("sftp");
            channel.connect(Math.max(5000, Math.min(timeoutMs, 120000)));
            try (InputStream in = channel.get(remotePath); ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int total = 0;
                int count;
                while ((count = in.read(buffer)) >= 0) {
                    total += count;
                    if (total > maxBytes) throw new IllegalArgumentException("file exceeds read limit");
                    bytes.write(buffer, 0, count);
                }
                out.content = bytes.toByteArray();
            }
            out.ok = true;
        } catch (Exception e) {
            out.error = "read_failed";
            out.detail = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
        } finally {
            if (channel != null) try { channel.disconnect(); } catch (Exception ignored) {}
            if (session != null) try { session.disconnect(); } catch (Exception ignored) {}
        }
        return out;
    }

    private static void validateSftpArgs(MobileSshAuth.Options opts, String remotePath) {
        if (opts == null || opts.host == null || opts.host.isEmpty()) throw new IllegalArgumentException("host is required");
        if (opts.user == null || opts.user.isEmpty()) throw new IllegalArgumentException("user is required");
        if (remotePath == null || !remotePath.startsWith("/")) throw new IllegalArgumentException("absolute remote path is required");
        String method = opts.authMethod != null ? opts.authMethod : "key";
        if ("password".equals(method) && (opts.password == null || opts.password.isEmpty())) throw new IllegalArgumentException("password credential missing");
        if ("key".equals(method) && (opts.keyFile == null || opts.keyFile.isEmpty())) throw new IllegalArgumentException("SSH key path is required");
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
        long totalStartedAt = System.currentTimeMillis();
        long lockStartedAt = totalStartedAt;
        synchronized (lock) {
            seq.lockWaitMs = System.currentTimeMillis() - lockStartedAt;
            try {
                MobileHubLog.ssh("exec connect " + MobileHubLog.endpoint(opts));
                long connectStartedAt = System.currentTimeMillis();
                session = connectWithRetry(opts, false, Math.max(1, connectAttempts));
                seq.connectMs = System.currentTimeMillis() - connectStartedAt;
                seq.commandMs = new long[commands.length];
                for (int i = 0; i < commands.length; i++) {
                    String label = commandLabel(commands[i]);
                    MobileHubLog.ssh("exec start " + label + " " + MobileHubLog.endpoint(opts));
                    long t0 = System.currentTimeMillis();
                    Result step = execOnSession(session, commands[i], cmdBudget, null);
                    seq.commandMs[i] = System.currentTimeMillis() - t0;
                    MobileHubLog.ssh("exec " + (step.ok ? "ok" : "fail") + " "
                        + seq.commandMs[i] + "ms " + label
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
                seq.totalMs = System.currentTimeMillis() - totalStartedAt;
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

    static Result execOnSession(Session session, String command, int cmdBudget, byte[] stdin) {
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
        public long lockWaitMs;
        public long connectMs;
        public long[] commandMs;
        public long totalMs;

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
