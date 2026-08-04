import com.cam.app.MobileSshAuth;
import com.cam.app.MobileSshExec;

/**
 * Real two-hop verification for the ProxyJump transport (v2.4.68):
 * target sshd on 127.0.0.1:22222 reachable only through the jump sshd
 * on 127.0.0.1:22221, key auth on both hops. Uses the unmodified
 * MobileSshAuth/MobileSshExec classes (connectViaJump).
 */
public class TestJumpConnect {

    public static void main(String[] args) {
        String user = System.getProperty("user.name", "hren");
        MobileSshAuth.Options jump = new MobileSshAuth.Options();
        jump.host = "127.0.0.1";
        jump.port = 22221;
        jump.user = user;
        jump.authMethod = "key";
        jump.keyFile = "/tmp/jumptest/id_rsa_test";

        MobileSshAuth.Options target = new MobileSshAuth.Options();
        target.host = "127.0.0.1";
        target.port = 22222;
        target.user = user;
        target.authMethod = "key";
        target.keyFile = "/tmp/jumptest/id_rsa_test";
        target.jump = jump;

        long t0 = System.currentTimeMillis();
        MobileSshExec.Result r = MobileSshExec.exec(target, "echo JSCH_JUMP_OK", 30000);
        long ms = System.currentTimeMillis() - t0;
        System.out.println("elapsed=" + ms + "ms ok=" + r.ok
            + " stdout=" + r.stdout.trim()
            + (r.ok ? "" : " error=" + r.error + " detail=" + r.detail));
        System.out.println(r.ok && r.stdout.contains("JSCH_JUMP_OK")
            ? ">>> PASS: JSch ProxyJump chain works"
            : ">>> FAIL");
        System.exit(r.ok ? 0 : 1);
    }
}
