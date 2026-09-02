package android.content;

import java.io.File;

/** JVM stub for android.content.Context (only what MobileEmbeddedHub uses). */
public class Context {
    private final File filesDir;

    public Context(File filesDir) {
        this.filesDir = filesDir;
    }

    public Context getApplicationContext() {
        return this;
    }

    public File getFilesDir() {
        return filesDir;
    }

    public android.content.res.AssetManager getAssets() {
        return new android.content.res.AssetManager(new File(filesDir, "assets"));
    }
}
