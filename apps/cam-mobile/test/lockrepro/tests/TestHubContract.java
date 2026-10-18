import com.cam.app.MobileEmbeddedHub;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.nio.file.Files;

public final class TestHubContract {
    public static void main(String[] args) throws Exception {
        File dataDir = new File(args[0]);
        String contractPath = args[1];
        JSONObject contract = new JSONObject(Files.readString(new File(contractPath).toPath()));
        MobileEmbeddedHub hub = new MobileEmbeddedHub(new android.content.Context(dataDir));
        try {
            JSONObject started = hub.start();
            require(started.optBoolean("ok", false), "hub starts for contract");
            String bearer = "Bearer " + hub.currentToken();
            JSONArray routes = contract.getJSONArray("routes");
            for (int i = 0; i < routes.length(); i++) {
                JSONObject route = routes.getJSONObject(i);
                String name = route.getString("name");
                Object bodySpec = route.opt("body");
                String body = "";
                if ("$context".equals(bodySpec)) body = contract.getJSONObject("context").toString();
                else if (bodySpec instanceof JSONObject) body = bodySpec.toString();
                JSONObject res = hub.apiRequest(route.getString("method"), route.getString("path"), bearer, body);
                int expectedStatus = route.getInt("status");
                require(res.optInt("status") == expectedStatus,
                    name + " status=" + res.optInt("status") + " expected=" + expectedStatus + " body=" + res);
                Object raw = res.opt("data");
                require(raw instanceof JSONObject, name + " returns JSON object: " + res);
                JSONObject data = (JSONObject) raw;
                for (String key : stringList(route.optJSONArray("required"))) {
                    require(data.has(key), name + " missing key " + key + ": " + data);
                }
                for (String key : stringList(route.optJSONArray("arrayKeys"))) {
                    require(data.opt(key) instanceof JSONArray, name + " key is not array: " + key);
                }
                JSONObject fields = route.optJSONObject("fields");
                if (fields != null) {
                    for (String key : fields.keySet()) {
                        Object actual = fieldValue(data, key);
                        require(fields.opt(key).equals(actual),
                            name + " field " + key + "=" + actual + " expected=" + fields.opt(key));
                    }
                }
                String expectedError = route.optString("error", "");
                if (!expectedError.isEmpty()) {
                    require(expectedError.equals(data.optString("error", "")),
                        name + " error=" + data.optString("error", "") + " expected=" + expectedError);
                }
                System.out.println("  ok   — " + name);
            }
        } finally {
            hub.stop();
        }
        System.out.println(">>> PASS: Android Hub satisfies shared API contract");
    }

    private static Object fieldValue(JSONObject data, String path) {
        Object current = data;
        for (String part : path.split("\\.")) {
            if (!(current instanceof JSONObject)) return null;
            current = ((JSONObject) current).opt(part);
        }
        return current;
    }

    private static String[] stringList(JSONArray arr) {
        if (arr == null) return new String[0];
        String[] out = new String[arr.length()];
        for (int i = 0; i < arr.length(); i++) out[i] = arr.getString(i);
        return out;
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
