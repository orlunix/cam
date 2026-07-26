package android.util;

/** JVM stub for android.util.Base64 (flags matched to usage in MobileEmbeddedHub). */
public final class Base64 {
    public static final int DEFAULT = 0;
    public static final int NO_PADDING = 1;
    public static final int NO_WRAP = 2;
    public static final int URL_SAFE = 8;

    private Base64() {}

    public static byte[] decode(String str, int flags) {
        return java.util.Base64.getDecoder().decode(str);
    }

    public static String encodeToString(byte[] input, int flags) {
        if ((flags & URL_SAFE) != 0) {
            String s = java.util.Base64.getUrlEncoder().encodeToString(input);
            if ((flags & NO_PADDING) != 0) s = s.replace("=", "");
            return s;
        }
        return java.util.Base64.getEncoder().encodeToString(input);
    }
}
