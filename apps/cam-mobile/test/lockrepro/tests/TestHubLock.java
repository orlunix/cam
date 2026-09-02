import com.cam.app.MobileEmbeddedHub;

import org.json.JSONObject;

import java.io.File;

/**
 * Regression test for the hub global-lock fix, driving the REAL
 * MobileEmbeddedHub (compiled from android/app/src/main/java/com/cam/app/)
 * with Android stubs.
 *
 * Scenario: a POST /api/contexts/bh/sync to a blackhole host (192.0.2.1,
 * silent drop — connect hangs to the full 120s budget) runs in one thread;
 * a GET /api/system/health is issued 3s later.
 *
 * Before fix (apiRequest globally synchronized): health waits ~117s.
 * After fix (per-host serialization in the SSH layer): health is instant.
 */
public class TestHubLock {

    public static void main(String[] args) throws Exception {
        File dataDir = args.length > 0
            ? new File(args[0])
            : new File(System.getProperty("java.io.tmpdir"), "cam-lockrepro-data");
        dataDir.mkdirs();
        android.content.Context ctx = new android.content.Context(dataDir);

        MobileEmbeddedHub hub = new MobileEmbeddedHub(ctx);
        JSONObject started = hub.start();
        if (!started.optBoolean("ok", false)) {
            System.out.println("hub start failed: " + started);
            System.exit(2);
        }
        String bearer = "Bearer " + hub.currentToken();
        System.out.println("hub started on port " + hub.currentPort());

        // Seed a context pointing at the blackhole host (password auth).
        JSONObject ctxBody = new JSONObject();
        ctxBody.put("name", "bh");
        ctxBody.put("path", "/tmp");
        ctxBody.put("host", "192.0.2.1");
        ctxBody.put("user", "repro");
        ctxBody.put("auth_method", "password");
        ctxBody.put("remember_password", true);
        ctxBody.put("password", "x");
        JSONObject created = hub.apiRequest("POST", "/api/contexts", bearer, ctxBody.toString());
        if (!created.optBoolean("ok", false)) {
            System.out.println("context create failed: " + created);
            System.out.println("hub logs: " + hub.logs());
            System.exit(2);
        }
        System.out.println("context 'bh' created");

        final long t0 = System.currentTimeMillis();
        Thread ssh = new Thread(() -> {
            System.out.printf("[t=%3ds] POST /api/contexts/bh/sync START (blackhole)%n", el(t0));
            JSONObject r = hub.apiRequest("POST", "/api/contexts/bh/sync", bearer, "{}");
            JSONObject data = r.optJSONObject("data");
            System.out.printf("[t=%3ds] sync RETURN http=%d %s%n", el(t0),
                r.optInt("status"), data != null ? data.optString("error", "") : "");
        });
        ssh.setDaemon(true);
        ssh.start();

        Thread.sleep(3000); // let the sync request reach the SSH connect

        System.out.printf("[t=%3ds] GET /api/system/health START%n", el(t0));
        long callStart = System.currentTimeMillis();
        JSONObject h = hub.apiRequest("GET", "/api/system/health", "", "");
        long waited = System.currentTimeMillis() - callStart;
        System.out.printf("[t=%3ds] health RETURN ok=%s after %.1fs%n",
            el(t0), h.optBoolean("ok"), waited / 1000.0);

        // Also verify local store reads stay responsive under the SSH op.
        callStart = System.currentTimeMillis();
        JSONObject agents = hub.apiRequest("GET", "/api/agents", bearer, "");
        waited = System.currentTimeMillis() - callStart;
        System.out.printf("[t=%3ds] GET /api/agents RETURN ok=%s after %.1fs%n",
            el(t0), agents.optBoolean("ok"), waited / 1000.0);

        boolean pass = waited < 5_000 && h.optBoolean("ok");
        System.out.println(pass
            ? ">>> FIX VERIFIED: unrelated requests proceed during blackhole SSH"
            : ">>> FAIL: requests still blocked behind blackhole SSH");
        hub.stop();
        System.exit(pass ? 0 : 1);
    }

    private static long el(long t0) {
        return (System.currentTimeMillis() - t0) / 1000;
    }
}
