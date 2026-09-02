import com.cam.app.MobileEmbeddedHub;

import org.json.JSONObject;

import java.io.File;

/**
 * ProxyJump validation tests (v2.4.68): create/update contexts with
 * machine.jump and check the validation outcomes — no real SSH needed,
 * everything fails or passes at the hub's validation layer.
 */
public class TestProxyJump {

    public static void main(String[] args) throws Exception {
        File dataDir = args.length > 0
            ? new File(args[0])
            : new File(System.getProperty("java.io.tmpdir"), "cam-lockrepro-jump");
        android.content.Context ctxStub = new android.content.Context(dataDir);
        MobileEmbeddedHub hub = new MobileEmbeddedHub(ctxStub);
        JSONObject started = hub.start();
        if (!started.optBoolean("ok", false)) {
            System.out.println("hub start failed: " + started);
            System.exit(2);
        }
        String bearer = "Bearer " + hub.currentToken();
        int failures = 0;

        // Jump host: key auth, no credential needed at validation time.
        failures += check("create jump host", create(hub, bearer,
            ctx("jhost", "10.0.0.1", null)) == 201);

        // Valid chained node: jump points at the registered host key.
        failures += check("create chained node", create(hub, bearer,
            ctx("t2", "10.0.0.2", "repro@10.0.0.1:22")) == 201);

        // Self jump → 400 invalid_jump
        failures += check("self jump rejected", create(hub, bearer,
            ctx("selfish", "10.0.0.3", "repro@10.0.0.3:22")) == 400);

        // Unregistered jump key → 400 invalid_jump (jump_unreachable)
        int unreachable = create(hub, bearer, ctx("ghosted", "10.0.0.4", "ghost@9.9.9.9:22"));
        failures += check("unreachable jump rejected", unreachable == 400);

        // Loop: point jhost at t2 (which jumps back to jhost) → invalid_jump
        JSONObject loop = hub.apiRequest("PUT", "/api/contexts/jhost", bearer,
            new JSONObject().put("jump", "repro@10.0.0.2:22").toString());
        failures += check("jump loop rejected", loop.optInt("status") == 400);

        // Clear jump on t2 → OK, then chaining jhost → t2 validates
        JSONObject clear = hub.apiRequest("PUT", "/api/contexts/t2", bearer,
            new JSONObject().put("jump", "").toString());
        failures += check("clear jump", clear.optInt("status") == 200);

        hub.stop();
        System.out.println(failures == 0
            ? ">>> PASS: ProxyJump validation verified"
            : ">>> FAIL: " + failures + " checks failed");
        System.exit(failures == 0 ? 0 : 1);
    }

    private static JSONObject ctx(String name, String host, String jump) throws Exception {
        JSONObject c = new JSONObject()
            .put("name", name)
            .put("path", "/tmp")
            .put("host", host)
            .put("user", "repro")
            .put("auth_method", "key")
            .put("key_file", "/tmp/id_test");
        if (jump != null) c.put("jump", jump);
        return c;
    }

    private static int create(MobileEmbeddedHub hub, String bearer, JSONObject body) {
        JSONObject r = hub.apiRequest("POST", "/api/contexts", bearer, body.toString());
        if (r.optInt("status") != 201) {
            System.out.println("    create " + body.optString("name") + " → " + r);
        }
        return r.optInt("status");
    }

    private static int check(String label, boolean ok) {
        System.out.println((ok ? "  ok  " : "  FAIL") + " — " + label);
        return ok ? 0 : 1;
    }
}
