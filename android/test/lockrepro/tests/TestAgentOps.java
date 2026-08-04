import com.cam.app.MobileEmbeddedHub;

import org.json.JSONObject;

import java.io.File;
import java.nio.file.Files;

/**
 * Routing tests for agent stop/remove (v2.4.66):
 *   DELETE /api/agents/:id          → camc stop over SSH
 *   DELETE /api/agents/:id/history  → camc rm over SSH
 *
 * No live host is available on the JVM, so the SSH attempt to the
 * blackhole context (192.0.2.1) must fail with 502 — but routing,
 * auth resolution, and error mapping must work (no 404/500 confusion),
 * and a local (non-SSH) agent must fail fast with 400 not_ssh.
 */
public class TestAgentOps {

    public static void main(String[] args) throws Exception {
        File dataDir = args.length > 0
            ? new File(args[0])
            : new File(System.getProperty("java.io.tmpdir"), "cam-lockrepro-agentops");
        File hubDir = new File(dataDir, "cam-hub");
        hubDir.mkdirs();

        // Pre-seed the hub store: one SSH context + two agents.
        JSONObject machine = new JSONObject()
            .put("type", "ssh").put("host", "192.0.2.1").put("user", "repro")
            .put("port", 22).put("auth_method", "password")
            .put("key_file", "").put("env_setup", "")
            .put("credential_ref", "c1:password").put("credential_kind", "password")
            .put("credential_saved", true);
        JSONObject ctx = new JSONObject()
            .put("id", "c1").put("name", "bh").put("path", "/tmp")
            .put("machine", machine).put("tags", new org.json.JSONArray());
        JSONObject store = new JSONObject()
            .put("contexts", new org.json.JSONArray().put(ctx))
            .put("agents", new org.json.JSONArray()
                .put(new JSONObject().put("id", "a1").put("name", "ssh-agent")
                    .put("status", "running").put("context_name", "bh"))
                .put(new JSONObject().put("id", "a2").put("name", "local-agent")
                    .put("status", "running").put("context_name", "").put("context_path", "/tmp"))
                .put(new JSONObject().put("id", "a3").put("name", "done-agent")
                    .put("status", "completed").put("context_name", "bh")));
        Files.write(new File(hubDir, "embedded-hub.json").toPath(),
            store.toString(2).getBytes("UTF-8"));

        // Seed the credential store stub via the hub's own save path is not
        // reachable here; instead run password-less: SSH fails at connect
        // anyway (blackhole), which is what we assert.

        android.content.Context ctxStub = new android.content.Context(dataDir);
        MobileEmbeddedHub hub = new MobileEmbeddedHub(ctxStub);
        JSONObject started = hub.start();
        if (!started.optBoolean("ok", false)) {
            System.out.println("hub start failed: " + started);
            System.exit(2);
        }
        String bearer = "Bearer " + hub.currentToken();
        int failures = 0;

        // 1. Unknown agent → 404 agent_not_found
        JSONObject r1 = hub.apiRequest("DELETE", "/api/agents/nope", bearer, "");
        failures += check("unknown agent → 404", r1.optInt("status") == 404
            && "agent_not_found".equals(errOf(r1)));

        // 2. Local (non-SSH) agent stop → 400 not_ssh (no SSH attempt)
        JSONObject r2 = hub.apiRequest("DELETE", "/api/agents/a2", bearer, "");
        failures += check("local agent stop → 400 not_ssh", r2.optInt("status") == 400
            && "not_ssh".equals(errOf(r2)));

        // 3. SSH agent stop → routes and reaches SSH; blackhole → 502
        JSONObject r3 = hub.apiRequest("DELETE", "/api/agents/a1", bearer, "");
        failures += check("ssh agent stop → 502 (blackhole)", r3.optInt("status") == 502);

        // 4. SSH agent remove → same routing, 502 on blackhole
        JSONObject r4 = hub.apiRequest("DELETE", "/api/agents/a1/history", bearer, "");
        failures += check("ssh agent remove → 502 (blackhole)", r4.optInt("status") == 502);

        // 5. Failed ops must not touch the local store
        JSONObject r5 = hub.apiRequest("GET", "/api/agents", bearer, "");
        String body = r5.opt("data") != null ? r5.opt("data").toString() : "";
        failures += check("store untouched after failed ops", body.contains("a1") && body.contains("a2"));

        // 6. Terminal-state agent stop → 200 no_op without any SSH attempt
        JSONObject r6 = hub.apiRequest("DELETE", "/api/agents/a3", bearer, "");
        failures += check("completed agent stop → 200 no_op", r6.optInt("status") == 200
            && r6.optJSONObject("data") != null
            && r6.optJSONObject("data").optBoolean("no_op", false));

        // 7. sync-status idle → 200 {ok, progress:null}
        JSONObject r7 = hub.apiRequest("GET", "/api/contexts/bh/sync-status", bearer, "");
        failures += check("sync-status idle → 200 progress:null", r7.optInt("status") == 200
            && r7.optJSONObject("data") != null
            && r7.optJSONObject("data").optBoolean("ok", false)
            && r7.optJSONObject("data").isNull("progress"));

        // 8. sync-status unknown context → 404
        JSONObject r8 = hub.apiRequest("GET", "/api/contexts/nope/sync-status", bearer, "");
        failures += check("sync-status unknown context → 404", r8.optInt("status") == 404);

        hub.stop();
        System.out.println(failures == 0
            ? ">>> PASS: agent stop/remove routing verified"
            : ">>> FAIL: " + failures + " checks failed");
        System.exit(failures == 0 ? 0 : 1);
    }

    private static String errOf(JSONObject r) {
        Object data = r.opt("data");
        if (data instanceof JSONObject) return ((JSONObject) data).optString("error", "");
        return "";
    }

    private static int check(String label, boolean ok) {
        System.out.println((ok ? "  ok  " : "  FAIL") + " — " + label);
        return ok ? 0 : 1;
    }
}
