import com.cam.app.MobileSshAuth;
import com.cam.app.MobileSshExec;

/** Real sshd verification for direct and ProxyJump key auth. */
public class TestJumpConnect {

    public static void main(String[] args) {
        String keyFile = args.length > 0 ? args[0] : "/tmp/jumptest/id_rsa_test";
        boolean expectChanged = args.length > 1 && "changed".equals(args[1]);
        boolean direct = expectChanged || (args.length > 1 && "direct".equals(args[1]));
        String user = System.getProperty("user.name", "hren");
        MobileSshAuth.Options jump = new MobileSshAuth.Options();
        jump.host = "127.0.0.1";
        jump.port = 22221;
        jump.user = user;
        jump.authMethod = "key";
        jump.keyFile = keyFile;
        jump.hostKeyStoreFile = new java.io.File("/tmp/jumptest/known-hosts.properties");

        MobileSshAuth.Options target = new MobileSshAuth.Options();
        target.host = "127.0.0.1";
        target.port = 22222;
        target.user = user;
        target.authMethod = "key";
        target.keyFile = keyFile;
        target.hostKeyStoreFile = new java.io.File("/tmp/jumptest/known-hosts.properties");
        if (!direct) target.jump = jump;

        String marker = direct ? "JSCH_ED25519_DIRECT_OK" : "JSCH_JUMP_OK";
        long t0 = System.currentTimeMillis();
        MobileSshExec.Result r = MobileSshExec.exec(target, "echo " + marker, 30000);
        long ms = System.currentTimeMillis() - t0;
        System.out.println("elapsed=" + ms + "ms ok=" + r.ok
            + " stdout=" + r.stdout.trim()
            + (r.ok ? "" : " error=" + r.error + " detail=" + r.detail));

        if (expectChanged) {
            boolean rejected = !r.ok && "host_key_changed".equals(r.error);
            System.out.println(rejected ? ">>> PASS: changed host key rejected" : ">>> FAIL: change not rejected");
            if (!rejected) System.exit(1);
            MobileSshAuth.forgetHostKey(target);
            r = MobileSshExec.exec(target, "echo " + marker, 30000);
            boolean recovered = r.ok && r.stdout.contains(marker);
            System.out.println(recovered ? ">>> PASS: forget/retrust recovers connection" : ">>> FAIL: retrust failed");
            System.exit(recovered ? 0 : 1);
            return;
        }

        boolean pass = r.ok && r.stdout.contains(marker);
        System.out.println(pass ? ">>> PASS: " + (direct ? "ed25519 direct" : "ProxyJump " + keyFile) : ">>> FAIL");
        System.exit(pass ? 0 : 1);
    }
}
