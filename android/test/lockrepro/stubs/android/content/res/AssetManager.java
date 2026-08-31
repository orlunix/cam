package android.content.res;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;

/** JVM stub for android.content.res.AssetManager (file-backed when rooted). */
public class AssetManager {
    private final File root;

    public AssetManager() {
        this.root = null;
    }

    public AssetManager(File root) {
        this.root = root;
    }

    public InputStream open(String name) throws IOException {
        File f = root != null ? new File(root, name) : null;
        if (f != null && f.isFile()) return new FileInputStream(f);
        return new ByteArrayInputStream(new byte[0]);
    }

    public String[] list(String path) {
        File d = root != null ? new File(root, path) : null;
        String[] names = d != null ? d.list() : null;
        return names != null ? names : new String[0];
    }
}
