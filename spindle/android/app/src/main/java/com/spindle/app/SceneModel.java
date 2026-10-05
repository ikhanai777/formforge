package com.spindle.app;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The scene as the native core describes it (the same JSON as Spindle's preset
 * files), addressed by dotted paths such as "material.roughness".
 */
final class SceneModel {
    private JSONObject root = new JSONObject();

    void load(String json) {
        try {
            root = new JSONObject(json);
        } catch (JSONException e) {
            root = new JSONObject();
        }
    }

    String json() { return root.toString(); }

    private JSONObject parentOf(String path, boolean create) {
        String[] keys = path.split("\\.");
        JSONObject o = root;
        for (int i = 0; i < keys.length - 1; ++i) {
            JSONObject next = o.optJSONObject(keys[i]);
            if (next == null) {
                if (!create) return null;
                next = new JSONObject();
                try {
                    o.put(keys[i], next);
                } catch (JSONException e) {
                    return null;
                }
            }
            o = next;
        }
        return o;
    }

    private static String leaf(String path) { return path.substring(path.lastIndexOf('.') + 1); }

    double getDouble(String path, double fallback) {
        JSONObject p = parentOf(path, false);
        return p == null ? fallback : p.optDouble(leaf(path), fallback);
    }

    boolean getBool(String path, boolean fallback) {
        JSONObject p = parentOf(path, false);
        return p == null ? fallback : p.optBoolean(leaf(path), fallback);
    }

    String getString(String path, String fallback) {
        JSONObject p = parentOf(path, false);
        return p == null ? fallback : p.optString(leaf(path), fallback);
    }

    /** RGB in 0..1 (sRGB, as shown in colour pickers). */
    float[] getColor(String path) {
        JSONObject p = parentOf(path, false);
        JSONArray a = p == null ? null : p.optJSONArray(leaf(path));
        if (a == null || a.length() < 3) return new float[] {0.8f, 0.8f, 0.8f};
        return new float[] {(float) a.optDouble(0), (float) a.optDouble(1), (float) a.optDouble(2)};
    }

    void put(String path, Object value) {
        JSONObject p = parentOf(path, true);
        if (p == null) return;
        try {
            p.put(leaf(path), value);
        } catch (JSONException ignored) {
        }
    }

    void putColor(String path, float[] rgb) {
        JSONArray a = new JSONArray();
        try {
            for (int i = 0; i < 3; ++i) a.put((double) rgb[i]);
        } catch (JSONException ignored) {
        }
        put(path, a);
    }
}
