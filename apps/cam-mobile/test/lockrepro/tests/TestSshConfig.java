import com.cam.app.MobileEmbeddedHub;

import org.json.JSONObject;

import java.io.File;

/** ssh_config parse endpoint tests (v2.4.68). */
public class TestSshConfig {

    public static void main(String[] args) throws Exception {
        File dataDir = args.length > 0
            ? new File(args[0])
            : new File(System.getProperty("java.io.tmpdir"), "cam-lockrepro-sshconfig");
        android.content.Context ctxStub = new android.content.Context(dataDir);
        MobileEmbeddedHub hub = new MobileEmbeddedHub(ctxStub);
        JSONObject started = hub.start();
        if (!started.optBoolean("ok", false)) {
            System.out.println("hub start failed: " + started);
            System.exit(2);
        }
        String bearer = "Bearer " + hub.currentToken();
        int failures = 0;

        // GET without path → available:false, empty hosts (no ~/.ssh on mobile)
        JSONObject r0 = hub.apiRequest("GET", "/api/system/ssh-config", bearer, "");
        failures += check("no-path → available:false", r0.optInt("status") == 200
            && r0.optJSONObject("data") != null
            && !r0.optJSONObject("data").optBoolean("available", true)
            && r0.optJSONObject("data").optJSONArray("hosts").length() == 0);

        String cfg = "# comment\n"
            + "Host gpu\n"
            + "  HostName 10.0.0.1\n"
            + "  User hren\n"
            + "  Port 2222\n"
            + "  IdentityFile ~/.ssh/id_ed25519\n"
            + "  ProxyJump hren@bastion:22, second@hop2:22\n"
            + "\n"
            + "Host * \n"
            + "  User ignored\n"
            + "\n"
            + "Host db\n"
            + "  HostName=db.internal\n"
            + "  User=svc\n"
            + "\n"
            + "Host incomplete\n"
            + "  Port 22\n";
        JSONObject r1 = hub.apiRequest("POST", "/api/system/ssh-config/parse", bearer,
            new JSONObject().put("text", cfg).toString());
        failures += check("parse → 200", r1.optInt("status") == 200);
        org.json.JSONArray hosts = r1.optJSONObject("data") != null
            ? r1.optJSONObject("data").optJSONArray("hosts") : null;
        failures += check("2 valid hosts (wildcard + incomplete skipped)",
            hosts != null && hosts.length() == 2);
        if (hosts != null && hosts.length() == 2) {
            JSONObject gpu = hosts.getJSONObject(0);
            failures += check("gpu fields", "gpu".equals(gpu.optString("alias"))
                && "10.0.0.1".equals(gpu.optString("host"))
                && "hren".equals(gpu.optString("user"))
                && gpu.optInt("port") == 2222
                && "~/.ssh/id_ed25519".equals(gpu.optString("identity_file"))
                && "hren@bastion:22".equals(gpu.optString("proxyjump")));
            JSONObject db = hosts.getJSONObject(1);
            failures += check("db key=value fields", "db.internal".equals(db.optString("host"))
                && "svc".equals(db.optString("user")));
        }

        hub.stop();
        System.out.println(failures == 0
            ? ">>> PASS: ssh_config parse verified"
            : ">>> FAIL: " + failures + " checks failed");
        System.exit(failures == 0 ? 0 : 1);
    }

    private static int check(String label, boolean ok) {
        System.out.println((ok ? "  ok  " : "  FAIL") + " — " + label);
        return ok ? 0 : 1;
    }
}
