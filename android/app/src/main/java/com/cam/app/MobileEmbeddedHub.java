package com.cam.app;

import android.content.Context;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

/**
 * Minimal embedded CAM Hub for Android Direct mode.
 * Loopback HTTP server with JSON store (contexts + agents).
 */
public final class MobileEmbeddedHub {

    private static final int DEFAULT_PORT = 8420;
    private static final int PORT_SCAN = 50;
    private static final Pattern NAME_RE = Pattern.compile("[A-Za-z0-9_-]{1,64}");
    private static final String PRODUCT_VERSION = "cam-mobile-embedded-1";

    private final Context appContext;
    private final ExecutorService executor = Executors.newCachedThreadPool();
    private final AtomicBoolean acceptRunning = new AtomicBoolean(false);

    private ServerSocket serverSocket;
    private int port = -1;
    private String token;
    private boolean owned;
    private long startedAt;
    private String lastError;
    private JSONObject store;
    private File dataDir;
    private File storePath;
    private MobileCredentialStore credentialStore;
    private final List<JSONObject> logs = new ArrayList<>();

    public MobileEmbeddedHub(Context ctx) {
        this.appContext = ctx.getApplicationContext();
        MobileHubLog.setSink(this::log);
    }

    public synchronized JSONObject check() {
        ensureStoreLoaded();
        JSONObject out = new JSONObject();
        try {
            out.put("platform", "android");
            out.put("runtime", "embedded");
            out.put("summary", isRunning() ? "running" : "stopped");
            out.put("apiPort", isRunning() ? port : DEFAULT_PORT);
            JSONObject range = new JSONObject();
            range.put("start", DEFAULT_PORT);
            range.put("end", DEFAULT_PORT + PORT_SCAN - 1);
            out.put("apiPortRange", range);
            out.put("apiPortStatus", new JSONObject().put("state", isRunning() ? "embedded-hub" : "free"));
            out.put("state", publicState());
            if (lastError != null) out.put("lastError", lastError);
        } catch (Exception e) {
            log("error", "check failed: " + e.getMessage());
        }
        return out;
    }

    public synchronized JSONObject start() {
        ensureStoreLoaded();
        if (isRunning()) {
            return okStart(true);
        }
        lastError = null;
        token = genToken();
        ServerSocket ss;
        try {
            ss = new ServerSocket();
        } catch (IOException e) {
            lastError = "socket create failed: " + e.getMessage();
            try {
                return new JSONObject().put("ok", false).put("error", lastError).put("state", publicState());
            } catch (Exception ex) {
                return new JSONObject();
            }
        }
        try {
            ss.setReuseAddress(true);
            int bound = bindLoopback(ss);
            if (bound < 0) {
                try { ss.close(); } catch (IOException ignored) {}
                try {
                    return new JSONObject()
                        .put("ok", false)
                        .put("error", lastError != null ? lastError : "listen failed")
                        .put("state", publicState());
                } catch (Exception e) {
                    return new JSONObject();
                }
            }
            serverSocket = ss;
            port = bound;
            owned = true;
            startedAt = System.currentTimeMillis();
            acceptRunning.set(true);
            executor.execute(this::runAcceptLoop);
            log("info", "embedded Hub listening on 127.0.0.1:" + port);
            return okStart(false);
        } catch (Exception e) {
            lastError = "listen failed: " + e.getMessage();
            port = -1;
            token = null;
            owned = false;
            try { ss.close(); } catch (IOException ignored) {}
            log("error", lastError);
            try {
                return new JSONObject().put("ok", false).put("error", lastError).put("state", publicState());
            } catch (Exception ex) {
                return new JSONObject();
            }
        }
    }

    /** Bind 127.0.0.1 — preferred port range, then OS-assigned (0). */
    private int bindLoopback(ServerSocket ss) {
        for (int p = DEFAULT_PORT; p < DEFAULT_PORT + PORT_SCAN; p++) {
            try {
                ss.bind(new InetSocketAddress("127.0.0.1", p));
                return p;
            } catch (IOException ignored) {}
        }
        try {
            ss.bind(new InetSocketAddress("127.0.0.1", 0));
            return ss.getLocalPort();
        } catch (IOException e) {
            lastError = "listen failed: " + e.getMessage();
            return -1;
        }
    }

    public synchronized JSONObject stop() {
        acceptRunning.set(false);
        if (serverSocket != null) {
            try { serverSocket.close(); } catch (IOException ignored) {}
            serverSocket = null;
        }
        port = -1;
        token = null;
        owned = false;
        startedAt = 0;
        log("info", "embedded Hub stopped");
        try {
            return new JSONObject().put("ok", true).put("state", publicState());
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    public synchronized JSONObject restart() {
        stop();
        return start();
    }

    public JSONObject logs() {
        JSONArray arr = new JSONArray();
        synchronized (logs) {
            for (JSONObject line : logs) arr.put(line);
        }
        try {
            return new JSONObject().put("server", arr);
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    public synchronized JSONObject getProfile() {
        try {
            JSONObject out = new JSONObject();
            out.put("apiUrl", isRunning() ? "http://127.0.0.1:" + port : JSONObject.NULL);
            out.put("state", publicState());
            return out;
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    /** In-process Hub API — bypasses WebView fetch to loopback (CORS/cleartext). */
    public synchronized JSONObject apiRequest(String method, String path, String auth, String bodyJson) {
        ensureStoreLoaded();
        byte[] bodyBytes = new byte[0];
        if (bodyJson != null && !bodyJson.isEmpty()) {
            bodyBytes = bodyJson.getBytes(StandardCharsets.UTF_8);
        }
        String m = method != null ? method.toUpperCase(Locale.US) : "GET";
        String p = path != null ? path : "/";
        int qIdx = p.indexOf('?');
        String query = qIdx >= 0 ? p.substring(qIdx + 1) : "";
        if (qIdx >= 0) p = p.substring(0, qIdx);
        byte[] raw = route(m, p, query, auth != null ? auth : "", bodyBytes);
        return decodeHttpJson(raw);
    }

    private JSONObject decodeHttpJson(byte[] raw) {
        JSONObject out = new JSONObject();
        try {
            String text = new String(raw, StandardCharsets.UTF_8);
            int sep = text.indexOf("\r\n\r\n");
            String head = sep >= 0 ? text.substring(0, sep) : text;
            String body = sep >= 0 ? text.substring(sep + 4) : "";
            int status = 500;
            String[] lines = head.split("\r\n");
            if (lines.length > 0) {
                String[] parts = lines[0].split(" ");
                if (parts.length >= 2) {
                    try { status = Integer.parseInt(parts[1]); } catch (NumberFormatException ignored) {}
                }
            }
            out.put("status", status);
            out.put("ok", status >= 200 && status < 300);
            if (body.isEmpty()) {
                out.put("data", JSONObject.NULL);
            } else {
                try {
                    out.put("data", new JSONObject(body));
                } catch (Exception e) {
                    out.put("data", body);
                }
            }
        } catch (Exception e) {
            try {
                out.put("status", 500);
                out.put("ok", false);
                out.put("data", new JSONObject().put("error", "decode_failed"));
            } catch (Exception ignored) {}
        }
        return out;
    }

    public boolean isRunning() {
        return serverSocket != null && serverSocket.isBound() && !serverSocket.isClosed();
    }

    public String currentToken() {
        return token;
    }

    public int currentPort() {
        return port;
    }

    private JSONObject okStart(boolean reused) {
        try {
            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("apiUrl", "http://127.0.0.1:" + port);
            out.put("apiToken", token);
            out.put("reused", reused);
            out.put("state", publicState());
            return out;
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private JSONObject publicState() {
        try {
            JSONObject server = new JSONObject();
            server.put("owned", owned);
            server.put("pid", android.os.Process.myPid());
            server.put("port", port > 0 ? port : JSONObject.NULL);
            server.put("startedAt", startedAt > 0 ? startedAt : JSONObject.NULL);
            server.put("tokenFingerprint", tokenFingerprint(token));
            return new JSONObject().put("server", server);
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private void runAcceptLoop() {
        while (acceptRunning.get() && serverSocket != null && !serverSocket.isClosed()) {
            try {
                Socket client = serverSocket.accept();
                executor.execute(() -> handleClient(client));
            } catch (IOException e) {
                if (acceptRunning.get()) log("warn", "accept error: " + e.getMessage());
                break;
            }
        }
    }

    private void handleClient(Socket client) {
        try (Socket c = client) {
            c.setSoTimeout(30000);
            InputStream in = c.getInputStream();
            OutputStream out = c.getOutputStream();
            BufferedReader reader = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            String requestLine = reader.readLine();
            if (requestLine == null || requestLine.isEmpty()) return;

            String[] parts = requestLine.split(" ");
            if (parts.length < 2) return;
            String method = parts[0];
            String rawPath = parts[1];
            int qIdx = rawPath.indexOf('?');
            String path = qIdx >= 0 ? rawPath.substring(0, qIdx) : rawPath;
            String query = qIdx >= 0 ? rawPath.substring(qIdx + 1) : "";

            int contentLength = 0;
            String auth = null;
            String line;
            while ((line = reader.readLine()) != null && !line.isEmpty()) {
                String lower = line.toLowerCase(Locale.US);
                if (lower.startsWith("content-length:")) {
                    contentLength = Integer.parseInt(line.substring(15).trim());
                } else if (lower.startsWith("authorization:")) {
                    auth = line.substring(14).trim();
                }
            }

            byte[] bodyBytes = new byte[0];
            if (contentLength > 0 && contentLength < 1024 * 1024) {
                bodyBytes = readFully(in, contentLength);
            }

            byte[] response = route(method, path, query, auth, bodyBytes);
            out.write(response);
            out.flush();
        } catch (Exception e) {
            log("warn", "client error: " + e.getMessage());
        }
    }

    private static final String CORS =
        "Access-Control-Allow-Origin: *\r\n"
        + "Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS\r\n"
        + "Access-Control-Allow-Headers: Authorization, Content-Type, Accept\r\n"
        + "Access-Control-Max-Age: 86400\r\n";

    private byte[] route(String method, String path, String query, String auth, byte[] bodyBytes) {
        try {
            if ("OPTIONS".equals(method)) {
                return rawResponse(204, "No Content", new byte[0]);
            }

            if ("GET".equals(method) && "/api/system/health".equals(path)) {
                return jsonResponse(200, healthBody());
            }
            if (path.startsWith("/api/")) {
                if (!checkBearer(auth)) {
                    return jsonResponse(401, new JSONObject().put("error", "unauthorized"));
                }
            } else if (!path.isEmpty() && !"/".equals(path)) {
                return jsonResponse(404, new JSONObject().put("error", "not_found"));
            }

            if ("GET".equals(method) && "/api/contexts".equals(path)) {
                JSONArray ctx = store.optJSONArray("contexts");
                if (ctx == null) ctx = new JSONArray();
                return jsonResponse(200, new JSONObject().put("contexts", ctx));
            }

            if ("POST".equals(method) && "/api/contexts".equals(path)) {
                JSONObject body = bodyBytes.length > 0
                    ? new JSONObject(new String(bodyBytes, StandardCharsets.UTF_8))
                    : new JSONObject();
                JSONObject built = buildContext(body);
                if (built.has("error")) {
                    return jsonResponse(400, built);
                }
                JSONObject record = built.getJSONObject("record");
                String name = record.getString("name");
                JSONArray ctxArr = store.getJSONArray("contexts");
                for (int i = 0; i < ctxArr.length(); i++) {
                    if (name.equals(ctxArr.getJSONObject(i).optString("name"))) {
                        return jsonResponse(400, new JSONObject()
                            .put("error", "duplicate_name")
                            .put("detail", "context \"" + name + "\" already exists"));
                    }
                }
                ctxArr.put(record);
                saveStore();
                log("info", "context created: " + name);
                return jsonResponse(201, record);
            }

            if ("GET".equals(method) && "/api/agents".equals(path)) {
                JSONArray agents = store.optJSONArray("agents");
                if (agents == null) agents = new JSONArray();
                return jsonResponse(200, new JSONObject().put("agents", agents));
            }

            if ("GET".equals(method) && "/api/system/config".equals(path)) {
                return jsonResponse(200, new JSONObject()
                    .put("version", PRODUCT_VERSION)
                    .put("data_dir", dataDir != null ? dataDir.getAbsolutePath() : JSONObject.NULL));
            }

            if (path.startsWith("/api/contexts/")) {
                return routeContextByName(method, path, bodyBytes);
            }

            if (path.startsWith("/api/agents/")) {
                return routeAgentById(method, path, query, bodyBytes);
            }

            return jsonResponse(404, new JSONObject().put("error", "not_found"));
        } catch (Exception e) {
            log("error", "route error: " + e.getMessage());
            try {
                return jsonResponse(500, new JSONObject().put("error", "internal_error"));
            } catch (Exception ex) {
                return "HTTP/1.1 500 Internal Server Error\r\n\r\n".getBytes(StandardCharsets.UTF_8);
            }
        }
    }

    private byte[] routeContextByName(String method, String path, byte[] bodyBytes) throws Exception {
        String rest = path.substring("/api/contexts/".length());
        int slash = rest.indexOf('/');
        String namePart = slash >= 0 ? rest.substring(0, slash) : rest;
        String sub = slash >= 0 ? rest.substring(slash) : "";
        String ctxName = URLDecoder.decode(namePart, "UTF-8");

        if ("/sync".equals(sub)) {
            if ("POST".equals(method)) {
                JSONObject body = bodyBytes.length > 0
                    ? new JSONObject(new String(bodyBytes, StandardCharsets.UTF_8))
                    : new JSONObject();
                JSONObject existing = resolveContext(ctxName, body);
                if (existing == null) {
                    JSONObject err = new JSONObject().put("error", "not_found")
                        .put("detail", "context \"" + ctxName + "\" not found");
                    if (countContextsByName(ctxName) > 1) {
                        err.put("error", "ambiguous_context");
                        err.put("detail", "multiple contexts named \"" + ctxName
                            + "\" — pass context id or host/user/port in the sync body");
                    }
                    return jsonResponse(404, err);
                }
                return jsonResponse(200, syncContextAgents(existing));
            }
            return jsonResponse(404, new JSONObject().put("error", "not_found"));
        }

        if (!sub.isEmpty()) {
            return jsonResponse(404, new JSONObject().put("error", "not_found"));
        }

        JSONObject existing = resolveContext(ctxName, null);
        if ("GET".equals(method)) {
            if (existing == null) {
                return jsonResponse(404, new JSONObject().put("error", "not_found")
                    .put("detail", "context \"" + ctxName + "\" not found"));
            }
            return jsonResponse(200, existing);
        }
        if ("PUT".equals(method) || "PATCH".equals(method)) {
            JSONObject body = bodyBytes.length > 0
                ? new JSONObject(new String(bodyBytes, StandardCharsets.UTF_8))
                : new JSONObject();
            existing = resolveContext(ctxName, body);
            if (existing == null) {
                JSONObject err = new JSONObject().put("error", "not_found")
                    .put("detail", "context \"" + ctxName + "\" not found");
                if (countContextsByName(ctxName) > 1) {
                    err.put("error", "ambiguous_context");
                    err.put("detail", "multiple contexts named \"" + ctxName
                        + "\" — use context id in the URL or pass host/user/port");
                }
                return jsonResponse(404, err);
            }
            JSONObject upd = applyContextUpdate(existing, body);
            if (upd.has("error")) {
                return jsonResponse(400, upd);
            }
            JSONObject record = upd.getJSONObject("record");
            replaceContextRecord(existing, record);
            saveStore();
            log("info", "context updated: " + ctxName);
            return jsonResponse(200, record);
        }
        if ("DELETE".equals(method)) {
            if (existing == null) {
                JSONObject err = new JSONObject().put("error", "not_found")
                    .put("detail", "context \"" + ctxName + "\" not found");
                if (countContextsByName(ctxName) > 1) {
                    err.put("error", "ambiguous_context");
                    err.put("detail", "multiple contexts named \"" + ctxName
                        + "\" — use context id in the URL");
                }
                return jsonResponse(404, err);
            }
            String ctxId = existing.optString("id", "");
            if (!ctxId.isEmpty()) cascadeDeleteCreds(ctxId);
            removeContextRecord(existing);
            saveStore();
            log("info", "context deleted: " + ctxName);
            return jsonResponse(200, new JSONObject().put("ok", true));
        }
        return jsonResponse(404, new JSONObject().put("error", "not_found"));
    }

    private byte[] routeAgentById(String method, String path, String query, byte[] bodyBytes) throws Exception {
        String rest = path.substring("/api/agents/".length());
        int slash = rest.indexOf('/');
        String idPart = slash >= 0 ? rest.substring(0, slash) : rest;
        String sub = slash >= 0 ? rest.substring(slash) : "";
        String agentId = URLDecoder.decode(idPart, "UTF-8");

        if ("GET".equals(method) && sub.isEmpty()) {
            JSONObject hints = parseAgentEndpointHints(query, null);
            JSONObject agent = findAgentById(agentId, hints);
            if (agent == null) {
                return jsonResponse(404, new JSONObject()
                    .put("error", "agent_not_found")
                    .put("detail", "agent \"" + agentId + "\" not found"));
            }
            return jsonResponse(200, agent);
        }

        if ("GET".equals(method) && "/output".equals(sub)) {
            int lines = 200;
            String clientHash = "";
            if (query != null && !query.isEmpty()) {
                for (String pair : query.split("&")) {
                    int eq = pair.indexOf('=');
                    String k = eq >= 0 ? URLDecoder.decode(pair.substring(0, eq), "UTF-8") : pair;
                    String v = eq >= 0 ? URLDecoder.decode(pair.substring(eq + 1), "UTF-8") : "";
                    if ("lines".equals(k)) {
                        try { lines = Integer.parseInt(v); } catch (Exception ignored) {}
                    } else if ("hash".equals(k)) {
                        clientHash = v;
                    }
                }
            }
            JSONObject captured = captureAgentOutput(agentId, lines, clientHash, parseAgentEndpointHints(query, null));
            if (captured.has("error") && captured.has("ok") && !captured.optBoolean("ok", true)) {
                int status = "agent_not_found".equals(captured.optString("error")) ? 404 : 502;
                return jsonResponse(status, captured);
            }
            if (captured.has("error") && !captured.has("unchanged") && !captured.has("output")) {
                return jsonResponse(502, captured);
            }
            return jsonResponse(200, captured);
        }

        if ("POST".equals(method) && "/input".equals(sub)) {
            JSONObject body = bodyBytes.length > 0
                ? new JSONObject(new String(bodyBytes, StandardCharsets.UTF_8))
                : new JSONObject();
            String text = body.optString("text", "");
            boolean sendEnter = body.optBoolean("send_enter", true);
            if (text.isEmpty()) {
                return jsonResponse(400, new JSONObject()
                    .put("error", "missing_text")
                    .put("detail", "text is required"));
            }
            JSONObject sent = sendAgentInput(agentId, text, sendEnter, body);
            if (sent.has("error") && !sent.optBoolean("ok", false)) {
                String err = sent.optString("error", "send_failed");
                int status = "agent_not_found".equals(err) ? 404
                    : ("missing_text".equals(err) || "invalid_key".equals(err) ? 400 : 502);
                return jsonResponse(status, sent);
            }
            return jsonResponse(200, sent);
        }

        if ("POST".equals(method) && "/key".equals(sub)) {
            JSONObject body = bodyBytes.length > 0
                ? new JSONObject(new String(bodyBytes, StandardCharsets.UTF_8))
                : new JSONObject();
            String key = body.optString("key", "").trim();
            if (key.isEmpty()) {
                return jsonResponse(400, new JSONObject()
                    .put("error", "missing_key")
                    .put("detail", "key is required"));
            }
            if (!validTmuxKey(key)) {
                return jsonResponse(400, new JSONObject()
                    .put("error", "invalid_key")
                    .put("detail", "invalid key: " + key));
            }
            JSONObject sent = sendAgentKey(agentId, key, body);
            if (sent.has("error") && !sent.optBoolean("ok", false)) {
                String err = sent.optString("error", "send_key_failed");
                int status = "agent_not_found".equals(err) ? 404
                    : ("missing_key".equals(err) || "invalid_key".equals(err) ? 400 : 502);
                return jsonResponse(status, sent);
            }
            return jsonResponse(200, sent);
        }

        return jsonResponse(404, new JSONObject().put("error", "not_found"));
    }

    private static boolean validTmuxKey(String key) {
        return key != null && key.matches("^[A-Za-z0-9_-]{1,32}$");
    }

    private static boolean isAgentFinished(String status) {
        if (status == null) return false;
        String s = status.toLowerCase(Locale.US);
        return "completed".equals(s) || "failed".equals(s) || "timeout".equals(s) || "killed".equals(s);
    }

    private MobileSshAuth.Options sshAuthForAgent(JSONObject agent) throws Exception {
        if (agent == null) return null;
        JSONObject ctx = findContextForAgent(agent);
        if (ctx == null) return null;
        JSONObject m = ctx.optJSONObject("machine");
        if (m == null || !"ssh".equals(m.optString("type", ""))) return null;
        MobileSshAuth.Options auth = MobileSshAuth.fromMachine(m, credentialStore);
        if (agent.has("machine_host") && !agent.optString("machine_host", "").isEmpty()) {
            auth.host = agent.optString("machine_host", auth.host);
        }
        if (agent.has("machine_user") && !agent.optString("machine_user", "").isEmpty()) {
            auth.user = agent.optString("machine_user", auth.user);
        }
        if (agent.has("machine_port") && !agent.isNull("machine_port")) {
            auth.port = agent.optInt("machine_port", auth.port);
        }
        return auth;
    }

    private JSONObject sendAgentInput(String agentId, String text, boolean sendEnter, JSONObject hints) throws Exception {
        JSONObject agent = findAgentById(agentId, hints);
        if (agent == null) {
            return new JSONObject()
                .put("error", "agent_not_found")
                .put("detail", "agent \"" + agentId + "\" not found");
        }
        if (isAgentFinished(agent.optString("status", ""))) {
            return new JSONObject()
                .put("error", "agent_not_running")
                .put("detail", "Agent is not running");
        }
        MobileSshAuth.Options auth = sshAuthForAgent(agent);
        if (auth == null) {
            return new JSONObject()
                .put("error", "not_ssh")
                .put("detail", "send requires an SSH-backed agent");
        }
        if ("password".equals(auth.authMethod) && (auth.password == null || auth.password.isEmpty())) {
            return new JSONObject()
                .put("error", "credential_missing")
                .put("detail", "password auth is configured but no remembered password is available");
        }
        if ("key".equals(auth.authMethod) && (auth.keyFile == null || auth.keyFile.isEmpty())) {
            return new JSONObject()
                .put("error", "key_file_missing")
                .put("detail", "SSH key path is required for key auth");
        }
        String id = agent.optString("id", agentId);
        byte[] stdin = text.getBytes(StandardCharsets.UTF_8);
        MobileSshExec.Result res = MobileSshExec.execStdin(
            auth, MobileSshExec.camcSendCommand(id, sendEnter), stdin, SEND_TIMEOUT_MS);
        if (!res.ok) {
            return new JSONObject()
                .put("error", res.error != null ? res.error : "send_failed")
                .put("detail", res.detail != null ? res.detail : "remote send failed");
        }
        return new JSONObject().put("ok", true);
    }

    private JSONObject sendAgentKey(String agentId, String key, JSONObject hints) throws Exception {
        JSONObject agent = findAgentById(agentId, hints);
        if (agent == null) {
            return new JSONObject()
                .put("error", "agent_not_found")
                .put("detail", "agent \"" + agentId + "\" not found");
        }
        if (isAgentFinished(agent.optString("status", ""))) {
            return new JSONObject()
                .put("error", "agent_not_running")
                .put("detail", "Agent is not running");
        }
        MobileSshAuth.Options auth = sshAuthForAgent(agent);
        if (auth == null) {
            return new JSONObject()
                .put("error", "not_ssh")
                .put("detail", "send key requires an SSH-backed agent");
        }
        if ("password".equals(auth.authMethod) && (auth.password == null || auth.password.isEmpty())) {
            return new JSONObject()
                .put("error", "credential_missing")
                .put("detail", "password auth is configured but no remembered password is available");
        }
        if ("key".equals(auth.authMethod) && (auth.keyFile == null || auth.keyFile.isEmpty())) {
            return new JSONObject()
                .put("error", "key_file_missing")
                .put("detail", "SSH key path is required for key auth");
        }
        String id = agent.optString("id", agentId);
        MobileSshExec.Result res = MobileSshExec.exec(
            auth, MobileSshExec.camcKeyCommand(id, key), SEND_TIMEOUT_MS);
        if (!res.ok) {
            return new JSONObject()
                .put("error", res.error != null ? res.error : "send_key_failed")
                .put("detail", res.detail != null ? res.detail : "remote key send failed");
        }
        return new JSONObject().put("ok", true);
    }

    private JSONObject captureAgentOutput(String agentId, int lines, String clientHash, JSONObject hints) throws Exception {
        JSONObject agent = findAgentById(agentId, hints);
        if (agent == null) {
            return new JSONObject()
                .put("error", "agent_not_found")
                .put("detail", "agent \"" + agentId + "\" not found");
        }
        JSONObject ctx = findContextForAgent(agent);
        if (ctx == null) {
            return new JSONObject()
                .put("error", "context_not_found")
                .put("detail", "no context for agent \"" + agentId + "\"");
        }
        JSONObject m = ctx.optJSONObject("machine");
        if (m == null || !"ssh".equals(m.optString("type", ""))) {
            return new JSONObject()
                .put("error", "not_ssh")
                .put("detail", "capture requires an SSH-backed agent");
        }
        MobileSshAuth.Options auth = MobileSshAuth.fromMachine(m, credentialStore);
        if (agent.has("machine_host") && !agent.optString("machine_host", "").isEmpty()) {
            auth.host = agent.optString("machine_host", auth.host);
        }
        if (agent.has("machine_user") && !agent.optString("machine_user", "").isEmpty()) {
            auth.user = agent.optString("machine_user", auth.user);
        }
        if (agent.has("machine_port") && !agent.isNull("machine_port")) {
            auth.port = agent.optInt("machine_port", auth.port);
        }
        String id = agent.optString("id", agentId);
        MobileSshExec.Result cap = MobileSshExec.exec(
            auth, MobileSshExec.camcCaptureCommand(id, lines), SYNC_TIMEOUT_MS);
        if (!cap.ok) {
            return new JSONObject()
                .put("error", cap.error != null ? cap.error : "capture_failed")
                .put("detail", cap.detail != null ? cap.detail : "remote capture failed");
        }
        String output = cap.stdout != null ? cap.stdout : "";
        String hash = md5Hex(output);
        if (clientHash != null && !clientHash.isEmpty() && clientHash.equals(hash)) {
            return new JSONObject().put("unchanged", true).put("hash", hash).put("lines", lines);
        }
        return new JSONObject().put("output", output).put("hash", hash).put("lines", lines);
    }

    private static String md5Hex(String text) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("MD5");
            byte[] dig = md.digest((text != null ? text : "").getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : dig) sb.append(String.format(Locale.US, "%02x", b));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private JSONObject findContextByName(String name) throws Exception {
        JSONArray ctxArr = store.getJSONArray("contexts");
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (name.equals(c.optString("name"))) return c;
        }
        return null;
    }

    private int countContextsByName(String name) throws Exception {
        JSONArray ctxArr = store.getJSONArray("contexts");
        int n = 0;
        for (int i = 0; i < ctxArr.length(); i++) {
            if (name.equals(ctxArr.getJSONObject(i).optString("name"))) n++;
        }
        return n;
    }

    /** Resolve by id first, then name + optional endpoint hints (sync/CRUD). */
    private JSONObject resolveContext(String nameOrId, JSONObject hints) throws Exception {
        if (nameOrId == null || nameOrId.isEmpty()) return null;
        JSONArray ctxArr = store.getJSONArray("contexts");

        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (nameOrId.equals(c.optString("id", ""))) return c;
        }

        String hHost = hints != null ? hints.optString("host", "").trim() : "";
        String hUser = hints != null ? hints.optString("user", "").trim() : "";
        int hPort = hints != null && hints.has("port") && !hints.isNull("port")
            ? hints.optInt("port", 22) : 22;
        boolean hasEndpoint = !hHost.isEmpty();

        JSONObject sole = null;
        int matches = 0;
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (!nameOrId.equals(c.optString("name"))) continue;
            if (hasEndpoint && !contextMatchesAgentEndpoint(c, hHost, hUser, hPort)) continue;
            sole = c;
            matches++;
        }
        if (matches == 1) return sole;
        if (matches > 1) return null;
        return null;
    }

    private JSONObject findContextById(String id) throws Exception {
        if (id == null || id.isEmpty()) return null;
        JSONArray ctxArr = store.getJSONArray("contexts");
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (id.equals(c.optString("id", ""))) return c;
        }
        return null;
    }

    private void replaceContextByName(String name, JSONObject record) throws Exception {
        JSONArray ctxArr = store.getJSONArray("contexts");
        for (int i = 0; i < ctxArr.length(); i++) {
            if (name.equals(ctxArr.getJSONObject(i).optString("name"))) {
                ctxArr.put(i, record);
                return;
            }
        }
    }

    private void replaceContextRecord(JSONObject existing, JSONObject record) throws Exception {
        JSONArray ctxArr = store.getJSONArray("contexts");
        String id = existing.optString("id", "");
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (!id.isEmpty() && id.equals(c.optString("id", ""))) {
                ctxArr.put(i, record);
                return;
            }
        }
        replaceContextByName(existing.optString("name", ""), record);
    }

    private void removeContextByName(String name) throws Exception {
        JSONArray ctxArr = store.getJSONArray("contexts");
        JSONArray next = new JSONArray();
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (!name.equals(c.optString("name"))) next.put(c);
        }
        store.put("contexts", next);
    }

    private void removeContextRecord(JSONObject existing) throws Exception {
        if (existing == null) return;
        String id = existing.optString("id", "");
        JSONArray ctxArr = store.getJSONArray("contexts");
        JSONArray next = new JSONArray();
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (!id.isEmpty() && id.equals(c.optString("id", ""))) continue;
            if (id.isEmpty()) {
                JSONObject m = existing.optJSONObject("machine");
                JSONObject cm = c.optJSONObject("machine");
                if (existing.optString("name", "").equals(c.optString("name"))
                    && m != null && cm != null
                    && contextMatchesAgentEndpoint(c,
                        m.optString("host", "").trim(),
                        m.optString("user", "").trim(),
                        m.optInt("port", 22))) {
                    continue;
                }
            }
            next.put(c);
        }
        store.put("contexts", next);
    }

    private JSONObject applyContextUpdate(JSONObject existing, JSONObject body) throws Exception {
        JSONObject next = new JSONObject(existing.toString());
        if (body.has("path")) {
            String p = body.optString("path", "").trim();
            if (p.isEmpty()) {
                return new JSONObject().put("error", "missing_path").put("detail", "path is required");
            }
            next.put("path", p);
        }
        JSONObject m = existing.optJSONObject("machine");
        if (m == null) m = new JSONObject();
        JSONObject mNext = new JSONObject(m.toString());
        if (body.has("host")) mNext.put("host", body.optString("host", "").trim());
        if (body.has("user")) mNext.put("user", body.optString("user", "").trim());
        if (body.has("key_file")) mNext.put("key_file", body.optString("key_file", "").trim());
        if (body.has("env_setup")) mNext.put("env_setup", body.optString("env_setup", "").trim());
        if (body.has("port") && !body.isNull("port")) {
            int portNum = body.optInt("port", 22);
            if (portNum < 1 || portNum > 65535) {
                return new JSONObject().put("error", "invalid_port")
                    .put("detail", "port must be 1..65535");
            }
            mNext.put("port", portNum);
        }
        String prevMethod = mNext.optString("auth_method", mNext.optString("key_file", "").isEmpty() ? "agent" : "key");
        String nextMethod = prevMethod;
        if (body.has("auth_method")) {
            String am = normalizeAuthMethod(body, mNext.optString("key_file", "").trim());
            if (am == null) {
                return new JSONObject().put("error", "invalid_auth_method")
                    .put("detail", "auth_method must be one of: key, password, agent");
            }
            nextMethod = am;
        }
        if ("ssh".equals(mNext.optString("type", m.optString("type", ""))) || body.has("host")) {
            if (!nextMethod.equals(prevMethod)) {
                cascadeDeleteCreds(existing.optString("id", ""));
                mNext.put("credential_ref", "");
                mNext.put("credential_kind", "");
                mNext.put("credential_saved", false);
            }
            mNext.put("auth_method", nextMethod);
            if (!"key".equals(nextMethod)) mNext.put("key_file", "");
            if ("password".equals(nextMethod)
                && !body.optBoolean("remember_password", false)
                && !mNext.optBoolean("credential_saved", false)) {
                return new JSONObject().put("error", "missing_credential")
                    .put("detail", "auth_method=password requires remember_password=true");
            }
        }
        String host = mNext.optString("host", "").trim();
        mNext.put("type", host.isEmpty() ? "local" : "ssh");
        if ("ssh".equals(mNext.optString("type")) && mNext.optString("user", "").trim().isEmpty()) {
            return new JSONObject().put("error", "missing_user")
                .put("detail", "SSH contexts require host and user");
        }
        if ("ssh".equals(mNext.optString("type"))) {
            JSONObject credErr = applyRememberedSecrets(existing.optString("id", ""), mNext, body);
            if (credErr != null) return credErr;
        } else {
            cascadeDeleteCreds(existing.optString("id", ""));
            mNext.remove("auth_method");
            mNext.remove("credential_ref");
            mNext.remove("credential_kind");
            mNext.remove("credential_saved");
        }
        next.put("machine", mNext);
        return new JSONObject().put("record", next);
    }

    private static final int SYNC_TIMEOUT_MS = 60000;
    private static final int SEND_TIMEOUT_MS = 30000;

    /** Map SSH probe result — only a successful exec with nonzero exit is camc_missing. */
    private JSONObject failRemoteProbe(MobileSshExec.Result r, String user, String host, int port) throws Exception {
        if (r == null) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "exec_failed")
                .put("detail", "remote command failed")
                .put("results", new JSONObject().put("camc", "failed"));
        }
        String endpoint = user + "@" + host + (port != 22 ? (":" + port) : "");
        if ("remote_nonzero".equals(r.error)) {
            String hint = (r.stderr != null && !r.stderr.isEmpty()) ? r.stderr.trim()
                : ("exit " + r.exitCode);
            return new JSONObject()
                .put("ok", false)
                .put("error", "camc_missing")
                .put("detail", "~/.cam/camc missing or not executable on " + endpoint + " (" + hint + ")")
                .put("results", new JSONObject().put("camc", "failed"));
        }
        String err = (r.error != null && !r.error.isEmpty()) ? r.error : "exec_failed";
        String detail = r.detail != null ? r.detail : "remote command failed";
        if ("auth_failed".equals(err)) {
            detail = "SSH authentication failed for " + endpoint + ": " + detail;
        } else if ("connect_failed".equals(err)) {
            if (detail.contains("SSH key not found")) {
                err = "key_file_missing";
                detail = detail + " — on phone, switch Auth to Password and re-save the node";
            } else if (detail.toLowerCase().contains("rekeying")
                || detail.toLowerCase().contains("timeout")
                || detail.toLowerCase().contains("handshake")) {
                detail = "SSH handshake timed out for " + endpoint
                    + " — check WiFi, host/port, and that sshd is reachable; try Sync Host first";
            } else {
                detail = "SSH connection failed for " + endpoint + ": " + detail;
            }
        } else if ("key_file_missing".equals(err)) {
            detail = detail + " — on phone, use Password auth or copy the private key to device storage";
        }
        return new JSONObject()
            .put("ok", false)
            .put("error", err)
            .put("detail", detail)
            .put("results", new JSONObject().put("camc", "failed"));
    }

    private JSONObject failAttachProbe(MobileSshExec.Result r, String user, String host, int port) throws Exception {
        if (r == null) {
            return new JSONObject().put("ok", false).put("error", "exec_failed").put("detail", "remote command failed");
        }
        String endpoint = user + "@" + host + (port != 22 ? (":" + port) : "");
        if ("remote_nonzero".equals(r.error)) {
            String hint = (r.stderr != null && !r.stderr.isEmpty()) ? r.stderr.trim()
                : ("exit " + r.exitCode);
            return new JSONObject()
                .put("ok", false)
                .put("error", "camc_missing")
                .put("detail", "~/.cam/camc missing or not executable on " + endpoint + " (" + hint + ")");
        }
        String err = (r.error != null && !r.error.isEmpty()) ? r.error : "exec_failed";
        String detail = r.detail != null ? r.detail : "remote command failed";
        if ("auth_failed".equals(err)) {
            detail = "SSH authentication failed for " + endpoint + ": " + detail;
        } else if ("connect_failed".equals(err)) {
            if (detail.toLowerCase().contains("rekeying")) {
                detail = "SSH handshake timed out for " + endpoint
                    + " — try WiFi or wait and retry";
            } else {
                detail = "SSH connection failed for " + endpoint + ": " + detail;
            }
        }
        return new JSONObject().put("ok", false).put("error", err).put("detail", detail);
    }

    /** Desktop getAttachConnectOpts — SSH + check camc + camc attach command. */
    public JSONObject resolveAttachConnect(String agentId, JSONObject hints) throws Exception {
        JSONObject agent = findAgentById(agentId, hints);
        if (agent == null) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "agent_not_found")
                .put("detail", "agent \"" + agentId + "\" not found — Sync Host first");
        }
        JSONObject ctx = findContextForAgent(agent);
        if (ctx == null) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "context_not_found")
                .put("detail", "no context for agent \"" + agentId + "\"");
        }
        JSONObject m = ctx.optJSONObject("machine");
        if (m == null || !"ssh".equals(m.optString("type", ""))) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "not_ssh")
                .put("detail", "terminal attach requires an SSH-backed agent");
        }
        String host = m.optString("host", "").trim();
        String user = m.optString("user", "").trim();
        int port = m.optInt("port", 22);
        if (agent.has("machine_host") && !agent.optString("machine_host", "").isEmpty()) {
            host = agent.optString("machine_host", host);
        }
        if (agent.has("machine_user") && !agent.optString("machine_user", "").isEmpty()) {
            user = agent.optString("machine_user", user);
        }
        if (agent.has("machine_port") && !agent.isNull("machine_port")) {
            port = agent.optInt("machine_port", port);
        }

        MobileSshAuth.Options auth = MobileSshAuth.fromMachine(m, credentialStore);
        auth.host = host;
        auth.port = port;
        auth.user = user;
        if ("password".equals(auth.authMethod) && (auth.password == null || auth.password.isEmpty())) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "credential_missing")
                .put("detail", "password auth is configured but no remembered password is available");
        }
        if ("key".equals(auth.authMethod) && (auth.keyFile == null || auth.keyFile.isEmpty())) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "key_file_missing")
                .put("detail", "SSH key path is required for key auth");
        }

        // Sync Host already verified camc; skip a second SSH handshake before attach.
        String id = agent.optString("id", agentId);
        String command = MobileSshExec.camcAttachCommand(id);
        return new JSONObject()
            .put("ok", true)
            .put("host", host)
            .put("port", port)
            .put("user", user)
            .put("auth_method", auth.authMethod)
            .put("key_file", auth.keyFile)
            .put("command", command);
    }

    /** Internal attach plan — includes decrypted auth; never expose to WebView/HTTP. */
    AttachPlan resolveAttachPlan(String agentId, JSONObject hints) throws Exception {
        JSONObject resolved = resolveAttachConnect(agentId, hints);
        AttachPlan plan = new AttachPlan();
        if (!resolved.optBoolean("ok", false)) {
            plan.error = resolved.optString("error", "attach_failed");
            plan.detail = resolved.optString("detail", "attach failed");
            return plan;
        }
        JSONObject agent = findAgentById(agentId, hints);
        JSONObject ctx = findContextForAgent(agent);
        JSONObject m = ctx != null ? ctx.optJSONObject("machine") : null;
        plan.auth = MobileSshAuth.fromMachine(m, credentialStore);
        if (plan.auth != null) {
            plan.auth.host = resolved.optString("host", plan.auth.host);
            plan.auth.port = resolved.optInt("port", plan.auth.port);
            plan.auth.user = resolved.optString("user", plan.auth.user);
        }
        plan.command = resolved.getString("command");
        return plan;
    }

    static final class AttachPlan {
        MobileSshAuth.Options auth;
        String command = "";
        String error = "";
        String detail = "";
        boolean ok() { return error == null || error.isEmpty(); }
    }

    private static String shortHost(String host) {
        if (host == null) return "";
        String h = host.trim();
        if (h.isEmpty()) return "";
        int dot = h.indexOf('.');
        return dot > 0 ? h.substring(0, dot) : h;
    }

    private static boolean hostMatches(String aHost, String bHost) {
        if (aHost == null || bHost == null) return false;
        String a = aHost.trim();
        String b = bHost.trim();
        if (a.isEmpty() || b.isEmpty()) return false;
        return a.equals(b) || shortHost(a).equals(shortHost(b));
    }

    private static JSONObject parseAgentEndpointHints(String query, JSONObject body) throws Exception {
        JSONObject hints = new JSONObject();
        if (query != null && !query.isEmpty()) {
            for (String pair : query.split("&")) {
                int eq = pair.indexOf('=');
                String k = eq >= 0 ? URLDecoder.decode(pair.substring(0, eq), "UTF-8") : pair;
                String v = eq >= 0 ? URLDecoder.decode(pair.substring(eq + 1), "UTF-8") : "";
                if ("machine_host".equals(k) || "host".equals(k)) hints.put("machine_host", v);
                else if ("machine_user".equals(k) || "user".equals(k)) hints.put("machine_user", v);
                else if ("machine_port".equals(k) || "port".equals(k)) hints.put("machine_port", v);
            }
        }
        if (body != null) {
            if (body.has("machine_host") && !body.optString("machine_host", "").isEmpty()) {
                hints.put("machine_host", body.optString("machine_host", ""));
            }
            if (body.has("machine_user")) hints.put("machine_user", body.optString("machine_user", ""));
            if (body.has("machine_port") && !body.isNull("machine_port")) {
                hints.put("machine_port", body.optString("machine_port", ""));
            }
        }
        return hints.length() > 0 ? hints : null;
    }

    private static boolean agentMatchesHints(JSONObject agent, JSONObject hints) {
        if (agent == null || hints == null) return false;
        String hHost = hints.optString("machine_host", "").trim();
        if (hHost.isEmpty()) return false;
        if (!hostMatches(agent.optString("machine_host", ""), hHost)) return false;
        if (hints.has("machine_user")
            && !hints.optString("machine_user", "").trim().isEmpty()
            && !hints.optString("machine_user", "").trim().equals(agent.optString("machine_user", "").trim())) {
            return false;
        }
        if (hints.has("machine_port") && !hints.isNull("machine_port")
            && !hints.optString("machine_port", "").trim().isEmpty()) {
            int hp;
            try { hp = Integer.parseInt(hints.optString("machine_port", "22")); }
            catch (Exception e) { hp = 22; }
            int ap = agent.has("machine_port") && !agent.isNull("machine_port")
                ? agent.optInt("machine_port", 22) : 22;
            if (hp != ap) return false;
        }
        return true;
    }

    private JSONObject findAgentById(String agentId) throws Exception {
        return findAgentById(agentId, null);
    }

    private JSONObject findAgentById(String agentId, JSONObject hints) throws Exception {
        JSONArray agents = store.optJSONArray("agents");
        if (agents == null) return null;
        String id = agentId != null ? agentId.trim() : "";
        JSONObject prefix = null;
        java.util.ArrayList<JSONObject> exact = new java.util.ArrayList<>();
        for (int i = 0; i < agents.length(); i++) {
            JSONObject a = agents.getJSONObject(i);
            String aid = a.optString("id", "");
            if (aid.equals(id)) exact.add(a);
            if (!id.isEmpty() && aid.startsWith(id) && prefix == null) prefix = a;
        }
        if (exact.isEmpty()) return prefix;
        if (exact.size() == 1) return exact.get(0);
        if (hints != null && hints.optString("machine_host", "").trim().length() > 0) {
            for (JSONObject a : exact) {
                if (agentMatchesHints(a, hints)) return a;
            }
        }
        return exact.get(0);
    }

    /** Resolve owning context — endpoint first; context names are not unique across hosts. */
    private JSONObject findContextForAgent(JSONObject agent) throws Exception {
        if (agent == null) return null;
        JSONArray ctxArr = store.getJSONArray("contexts");
        String aHost = agent.optString("machine_host", "").trim();
        String aUser = agent.optString("machine_user", "").trim();
        int aPort = agent.optInt("machine_port", 22);
        boolean hasEndpoint = !aHost.isEmpty();

        if (hasEndpoint) {
            for (int i = 0; i < ctxArr.length(); i++) {
                JSONObject c = ctxArr.getJSONObject(i);
                if (contextMatchesAgentEndpoint(c, aHost, aUser, aPort)) return c;
            }
        }

        String ctxName = agent.optString("context_name", "");
        if (!ctxName.isEmpty()) {
            for (int i = 0; i < ctxArr.length(); i++) {
                JSONObject c = ctxArr.getJSONObject(i);
                if (!ctxName.equals(c.optString("name"))) continue;
                if (hasEndpoint && !contextMatchesAgentEndpoint(c, aHost, aUser, aPort)) continue;
                return c;
            }
        }
        return null;
    }

    private static boolean contextMatchesAgentEndpoint(JSONObject ctx, String host, String user, int port) {
        if (ctx == null || host == null || host.isEmpty()) return false;
        JSONObject m = ctx.optJSONObject("machine");
        if (m == null) return false;
        return hostMatches(host, m.optString("host", "").trim())
            && user.equals(m.optString("user", "").trim())
            && port == m.optInt("port", 22);
    }

    /** Agents replaced on sync — scoped to context name + SSH endpoint, not name alone. */
    private boolean shouldReplaceAgentOnSync(JSONObject agent, JSONObject ctx) {
        if (agent == null || ctx == null) return false;
        JSONObject m = ctx.optJSONObject("machine");
        String ctxName = ctx.optString("name", "");
        if (m == null || !"ssh".equals(m.optString("type", ""))) {
            return ctxName.equals(agent.optString("context_name", ""));
        }
        String host = m.optString("host", "").trim();
        String user = m.optString("user", "").trim();
        int port = m.optInt("port", 22);
        if (!contextMatchesAgentEndpoint(ctx,
                agent.optString("machine_host", "").trim(),
                agent.optString("machine_user", "").trim(),
                agent.optInt("machine_port", 22))) {
            return false;
        }
        return ctxName.equals(agent.optString("context_name", ""));
    }

    /** SSH check camc + `camc --json list` → import agents (no deploy). */
    private JSONObject syncContextAgents(JSONObject ctx) throws Exception {
        JSONObject m = ctx.optJSONObject("machine");
        if (m == null || !"ssh".equals(m.optString("type", ""))) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "not_ssh")
                .put("detail", "Sync Host requires an SSH context")
                .put("results", new JSONObject().put("camc", "failed"));
        }
        String host = m.optString("host", "").trim();
        String user = m.optString("user", "").trim();
        int port = m.optInt("port", 22);
        String auth = m.optString("auth_method", "key");
        if (host.isEmpty() || user.isEmpty()) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "invalid_context")
                .put("detail", "SSH host and user are required")
                .put("results", new JSONObject().put("camc", "failed"));
        }
        MobileSshAuth.Options sshAuth = MobileSshAuth.fromMachine(m, credentialStore);
        MobileHubLog.ssh("sync start ctx=" + ctx.optString("name") + " "
            + MobileHubLog.endpoint(sshAuth));
        if ("password".equals(auth) && (sshAuth.password == null || sshAuth.password.isEmpty())) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "credential_missing")
                .put("detail", "password auth is configured but no remembered password is available — re-save the node with your password")
                .put("results", new JSONObject().put("camc", "failed"));
        }
        if ("key".equals(auth) && (sshAuth.keyFile == null || sshAuth.keyFile.isEmpty())) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "key_file_missing")
                .put("detail", "SSH key path is required for key auth")
                .put("results", new JSONObject().put("camc", "failed"));
        }

        MobileSshExec.SequenceResult syncRun = MobileSshExec.execSequence(
            sshAuth,
            new String[] {
                MobileSshExec.camcCheckCommand(),
                MobileSshExec.camcListCommand(),
            },
            SYNC_TIMEOUT_MS);
        if (!syncRun.ok || syncRun.steps == null || syncRun.steps.length < 2) {
            MobileSshExec.Result check = syncRun.first();
            MobileHubLog.ssh("sync fail ctx=" + ctx.optString("name") + " "
                + (check != null ? check.error + " " + check.detail : "unknown"));
            log("warn", "sync " + ctx.optString("name") + ": probe failed: "
                + check.error + " " + check.detail);
            return failRemoteProbe(check, user, host, port);
        }
        MobileSshExec.Result check = syncRun.steps[0];
        MobileSshExec.Result list = syncRun.steps[1];
        if (!check.ok) {
            log("warn", "sync " + ctx.optString("name") + ": probe failed: "
                + check.error + " " + check.detail);
            return failRemoteProbe(check, user, host, port);
        }
        if (!list.ok) {
            log("warn", "sync " + ctx.optString("name") + ": list failed");
            return new JSONObject()
                .put("ok", false)
                .put("error", list.error != null && !list.error.isEmpty() ? list.error : "exec_failed")
                .put("detail", list.detail != null ? list.detail : "remote camc list failed")
                .put("results", new JSONObject().put("camc", "failed"));
        }

        JSONArray parsed;
        try {
            parsed = new JSONArray(list.stdout.trim());
        } catch (Exception e) {
            return new JSONObject()
                .put("ok", false)
                .put("error", "invalid_json")
                .put("detail", "remote camc did not return JSON array")
                .put("results", new JSONObject().put("camc", "failed"));
        }

        JSONArray normalized = new JSONArray();
        for (int i = 0; i < parsed.length(); i++) {
            JSONObject rec = parsed.optJSONObject(i);
            if (rec == null) continue;
            JSONObject norm = normalizeAgent(rec, ctx);
            if (norm != null && !norm.optString("id", "").isEmpty()) {
                normalized.put(norm);
            }
        }

        int prevCount = countAgentsForContext(ctx);
        upsertAgentsForContext(ctx, normalized);
        markContextUsed(ctx);

        String status = (prevCount == normalized.length()) ? "unchanged" : "updated";
        log("info", "sync " + ctx.optString("name") + ": " + status + " (" + normalized.length() + " agent(s))");
        MobileHubLog.ssh("sync ok ctx=" + ctx.optString("name") + " agents=" + normalized.length());
        return new JSONObject()
            .put("ok", true)
            .put("imported", normalized.length())
            .put("total", parsed.length())
            .put("results", new JSONObject().put("camc", status));
    }

    private JSONObject normalizeAgent(JSONObject rec, JSONObject ctx) throws Exception {
        JSONObject t = rec.optJSONObject("task");
        if (t == null) t = new JSONObject();
        JSONObject m = ctx.optJSONObject("machine");
        if (m == null) m = new JSONObject();
        JSONObject out = new JSONObject();
        out.put("id", rec.optString("id", ""));
        out.put("task_name", t.optString("name", ""));
        out.put("tool", t.optString("tool", rec.optString("tool", "")));
        out.put("prompt", t.optString("prompt", ""));
        out.put("context_name", ctx.optString("name", ""));
        out.put("context_path", rec.optString("context_path", rec.optString("path", ctx.optString("path", ""))));
        out.put("status", rec.optString("status", "unknown"));
        out.put("state", rec.optString("state", ""));
        out.put("tmux_session", rec.optString("tmux_session", rec.optString("session", "")));
        out.put("machine_host", m.optString("host", ""));
        out.put("machine_user", m.optString("user", ""));
        out.put("machine_port", m.has("port") && !m.isNull("port") ? m.optInt("port") : JSONObject.NULL);
        out.put("machine_type", m.optString("type", "ssh"));
        out.put("transport_type", rec.optString("transport_type", "ssh"));
        out.put("started_at", rec.has("started_at") && !rec.isNull("started_at")
            ? rec.optString("started_at") : JSONObject.NULL);
        out.put("completed_at", rec.has("completed_at") ? rec.opt("completed_at") : JSONObject.NULL);
        return out;
    }

    private int countAgentsForContext(JSONObject ctx) throws Exception {
        JSONArray agents = store.optJSONArray("agents");
        if (agents == null || ctx == null) return 0;
        int n = 0;
        for (int i = 0; i < agents.length(); i++) {
            if (shouldReplaceAgentOnSync(agents.getJSONObject(i), ctx)) n++;
        }
        return n;
    }

    private void upsertAgentsForContext(JSONObject ctx, JSONArray incoming) throws Exception {
        JSONArray agents = store.optJSONArray("agents");
        if (agents == null) agents = new JSONArray();
        JSONArray keep = new JSONArray();
        for (int i = 0; i < agents.length(); i++) {
            JSONObject a = agents.getJSONObject(i);
            if (shouldReplaceAgentOnSync(a, ctx)) continue;
            keep.put(a);
        }
        for (int i = 0; i < incoming.length(); i++) {
            keep.put(incoming.getJSONObject(i));
        }
        store.put("agents", keep);
        saveStore();
    }

    private void markContextUsed(JSONObject ctx) throws Exception {
        if (ctx == null) return;
        String id = ctx.optString("id", "");
        JSONArray ctxArr = store.getJSONArray("contexts");
        for (int i = 0; i < ctxArr.length(); i++) {
            JSONObject c = ctxArr.getJSONObject(i);
            if (!id.isEmpty() && id.equals(c.optString("id", ""))) {
                c.put("last_used_at", nowIso());
                saveStore();
                return;
            }
            if (id.isEmpty() && ctx.optString("name", "").equals(c.optString("name"))) {
                JSONObject m = ctx.optJSONObject("machine");
                JSONObject cm = c.optJSONObject("machine");
                if (m != null && cm != null
                    && contextMatchesAgentEndpoint(c,
                        m.optString("host", "").trim(),
                        m.optString("user", "").trim(),
                        m.optInt("port", 22))) {
                    c.put("last_used_at", nowIso());
                    saveStore();
                    return;
                }
            }
        }
    }

    private JSONObject healthBody() throws Exception {
        JSONArray ctx = store.optJSONArray("contexts");
        JSONArray agents = store.optJSONArray("agents");
        JSONObject caps = new JSONObject();
        caps.put("runtime", "mobile-embedded");
        caps.put("context_crud", true);
        caps.put("context_sync", true);
        caps.put("credential_store", credentialStore != null && credentialStore.available());
        caps.put("agent_list", true);
        caps.put("agent_ops", true);
        caps.put("agent_terminal", true);
        caps.put("skillm", false);
        caps.put("ssh_config_import", false);
        return new JSONObject()
            .put("status", "ok")
            .put("version", PRODUCT_VERSION)
            .put("capabilities", caps)
            .put("adapters", new JSONArray().put("claude").put("codex").put("cursor"))
            .put("agents_running", 0)
            .put("agents_total", agents != null ? agents.length() : 0)
            .put("contexts_count", ctx != null ? ctx.length() : 0);
    }

    private JSONObject buildContext(JSONObject body) throws Exception {
        String name = body.optString("name", "").trim();
        if (!NAME_RE.matcher(name).matches()) {
            return new JSONObject().put("error", "invalid_name")
                .put("detail", "name must match [A-Za-z0-9_-]{1,64}");
        }
        String ctxPath = body.optString("path", "").trim();
        if (ctxPath.isEmpty()) {
            return new JSONObject().put("error", "missing_path").put("detail", "path is required");
        }
        String host = body.optString("host", "").trim();
        String user = body.optString("user", "").trim();
        int portNum = body.has("port") && !body.isNull("port") ? body.optInt("port", 22) : 22;
        if (!host.isEmpty() && user.isEmpty()) {
            return new JSONObject().put("error", "missing_user").put("detail", "SSH contexts require host and user");
        }
        boolean isSsh = !host.isEmpty();
        String keyFile = body.optString("key_file", "").trim();
        String authMethod = isSsh ? normalizeAuthMethod(body, keyFile) : "";
        if (isSsh && authMethod == null) {
            return new JSONObject().put("error", "invalid_auth_method")
                .put("detail", "auth_method must be one of: key, password, agent");
        }
        String envSetup = body.optString("env_setup", "").trim();

        JSONObject machine = new JSONObject();
        machine.put("type", isSsh ? "ssh" : "local");
        machine.put("host", isSsh ? host : "");
        machine.put("user", isSsh ? user : "");
        machine.put("port", isSsh ? portNum : JSONObject.NULL);
        machine.put("key_file", isSsh && "key".equals(authMethod) ? keyFile : "");
        machine.put("env_setup", envSetup);
        if (isSsh) {
            machine.put("auth_method", authMethod);
            machine.put("credential_ref", "");
            machine.put("credential_kind", "");
            machine.put("credential_saved", false);
            if ("password".equals(authMethod)
                && !body.optBoolean("remember_password", false)) {
                return new JSONObject().put("error", "missing_credential")
                    .put("detail", "auth_method=password requires remember_password=true");
            }
        }

        JSONObject record = new JSONObject();
        String id = UUID.randomUUID().toString();
        record.put("id", id);
        record.put("name", name);
        record.put("path", ctxPath);
        record.put("machine", machine);
        record.put("tags", new JSONArray());
        record.put("created_at", nowIso());
        record.put("last_used_at", JSONObject.NULL);

        if (isSsh) {
            JSONObject credErr = applyRememberedSecrets(id, machine, body);
            if (credErr != null) return credErr;
            record.put("machine", machine);
        }
        return new JSONObject().put("record", record);
    }

    private String normalizeAuthMethod(JSONObject body, String keyFile) {
        if (body.has("auth_method") && !body.isNull("auth_method")) {
            String m = body.optString("auth_method", "key");
            if ("key".equals(m) || "password".equals(m) || "agent".equals(m)) return m;
            return null;
        }
        return (keyFile != null && !keyFile.isEmpty()) ? "key" : "agent";
    }

    private JSONObject applyRememberedSecrets(String contextId, JSONObject machine, JSONObject body) throws Exception {
        if (contextId == null || contextId.isEmpty() || machine == null) return null;
        String method = machine.optString("auth_method", "key");
        if ("password".equals(method) && body.optBoolean("remember_password", false)) {
            String secret = body.optString("password", "");
            if (secret.isEmpty()) {
                return new JSONObject().put("error", "missing_password")
                    .put("detail", "remember_password=true requires a non-empty password");
            }
            JSONObject saved = saveRememberedSecret(contextId, "password", secret);
            if (saved.has("error")) return saved;
            machine.put("credential_ref", saved.getString("ref"));
            machine.put("credential_kind", "password");
            machine.put("credential_saved", true);
        } else if ("key".equals(method) && body.optBoolean("remember_passphrase", false)) {
            String secret = body.optString("passphrase", "");
            if (secret.isEmpty()) {
                return new JSONObject().put("error", "missing_passphrase")
                    .put("detail", "remember_passphrase=true requires a non-empty passphrase");
            }
            JSONObject saved = saveRememberedSecret(contextId, "passphrase", secret);
            if (saved.has("error")) return saved;
            machine.put("credential_ref", saved.getString("ref"));
            machine.put("credential_kind", "passphrase");
            machine.put("credential_saved", true);
        } else if (body.optBoolean("forget_credential", false)) {
            cascadeDeleteCreds(contextId);
            machine.put("credential_ref", "");
            machine.put("credential_kind", "");
            machine.put("credential_saved", false);
        }
        return null;
    }

    private JSONObject saveRememberedSecret(String contextId, String kind, String secret) throws Exception {
        ensureCredentialStore();
        if (credentialStore == null || !credentialStore.available()) {
            return new JSONObject()
                .put("error", "credential_store_unavailable")
                .put("detail", "Android Keystore is not available; cannot remember secrets.");
        }
        return credentialStore.put(contextId + ":" + kind, kind, secret);
    }

    private void cascadeDeleteCreds(String contextId) {
        try {
            ensureCredentialStore();
            if (credentialStore != null && contextId != null && !contextId.isEmpty()) {
                credentialStore.removeForContext(contextId);
            }
        } catch (Exception e) {
            log("warn", "credential delete failed: " + e.getMessage());
        }
    }

    private void ensureCredentialStore() {
        if (credentialStore != null) return;
        if (dataDir != null) credentialStore = new MobileCredentialStore(dataDir);
    }

    private boolean checkBearer(String auth) {
        if (token == null || token.isEmpty()) return false;
        if (auth == null) return false;
        String prefix = "Bearer ";
        if (!auth.regionMatches(true, 0, prefix, 0, prefix.length())) return false;
        return token.equals(auth.substring(prefix.length()).trim());
    }

    private void ensureStoreLoaded() {
        if (store != null) return;
        dataDir = new File(appContext.getFilesDir(), "cam-hub");
        if (!dataDir.exists()) dataDir.mkdirs();
        storePath = new File(dataDir, "embedded-hub.json");
        try {
            if (storePath.exists()) {
                String raw = readFile(storePath);
                store = new JSONObject(raw);
            } else {
                store = emptyStore();
                saveStore();
            }
        } catch (Exception e) {
            try {
                store = emptyStore();
            } catch (Exception ex) {
                store = new JSONObject();
            }
            log("warn", "store load failed: " + e.getMessage());
        }
        if (!store.has("contexts")) {
            try { store.put("contexts", new JSONArray()); } catch (Exception ignored) {}
        }
        if (!store.has("agents")) {
            try { store.put("agents", new JSONArray()); } catch (Exception ignored) {}
        }
        ensureCredentialStore();
    }

    private JSONObject emptyStore() throws Exception {
        return new JSONObject()
            .put("version", 1)
            .put("contexts", new JSONArray())
            .put("agents", new JSONArray())
            .put("adapters", new JSONArray().put("claude").put("codex").put("cursor"));
    }

    private void saveStore() {
        if (storePath == null || store == null) return;
        try {
            File tmp = new File(storePath.getAbsolutePath() + ".tmp");
            writeFile(tmp, store.toString(2));
            if (storePath.exists() && !storePath.delete()) {
                writeFile(storePath, store.toString(2));
                tmp.delete();
            } else {
                if (!tmp.renameTo(storePath)) {
                    writeFile(storePath, store.toString(2));
                    tmp.delete();
                }
            }
        } catch (Exception e) {
            log("warn", "store save failed: " + e.getMessage());
        }
    }

    private static String genToken() {
        byte[] buf = new byte[24];
        new SecureRandom().nextBytes(buf);
        return Base64.encodeToString(buf, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
    }

    private static String tokenFingerprint(String tok) {
        if (tok == null || tok.isEmpty()) return null;
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] dig = md.digest(tok.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder("sha256:");
            for (int i = 0; i < 12; i++) sb.append(String.format("%02x", dig[i]));
            return sb.toString();
        } catch (Exception e) {
            return null;
        }
    }

    private void log(String kind, String text) {
        JSONObject line = new JSONObject();
        try {
            line.put("kind", kind);
            line.put("text", text);
            line.put("ts", System.currentTimeMillis());
        } catch (Exception ignored) {}
        synchronized (logs) {
            logs.add(line);
            while (logs.size() > 400) logs.remove(0);
        }
    }

    private static byte[] jsonResponse(int code, JSONObject body) throws Exception {
        byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);
        String status = code == 200 ? "OK" : code == 201 ? "Created" : code == 400 ? "Bad Request"
            : code == 401 ? "Unauthorized" : code == 404 ? "Not Found" : "Internal Server Error";
        return rawResponse(code, status, payload);
    }

    private static byte[] rawResponse(int code, String status, byte[] payload) {
        String headers = "HTTP/1.1 " + code + " " + status + "\r\n"
            + CORS
            + (payload.length > 0 ? "Content-Type: application/json; charset=utf-8\r\n" : "")
            + "Content-Length: " + payload.length + "\r\n"
            + "Connection: close\r\n\r\n";
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try {
            out.write(headers.getBytes(StandardCharsets.UTF_8));
            if (payload.length > 0) out.write(payload);
        } catch (IOException ignored) {}
        return out.toByteArray();
    }

    private static byte[] readFully(InputStream in, int len) throws IOException {
        byte[] buf = new byte[len];
        int off = 0;
        while (off < len) {
            int n = in.read(buf, off, len - off);
            if (n < 0) break;
            off += n;
        }
        return buf;
    }

    private static String readFile(File f) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) >= 0) if (n > 0) out.write(buf, 0, n);
        }
        return out.toString(StandardCharsets.UTF_8.name());
    }

    private static void writeFile(File f, String text) throws IOException {
        try (FileOutputStream out = new FileOutputStream(f)) {
            out.write(text.getBytes(StandardCharsets.UTF_8));
        }
    }

    private static String nowIso() {
        SimpleDateFormat fmt = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US);
        fmt.setTimeZone(TimeZone.getTimeZone("UTC"));
        return fmt.format(new Date());
    }
}
