package android.os;

/** JVM stub for android.os.Process. */
public final class Process {
    private Process() {}

    public static int myPid() {
        return (int) ProcessHandle.current().pid();
    }
}
