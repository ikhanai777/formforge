package com.spindle.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.database.Cursor;
import android.graphics.Color;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.provider.OpenableColumns;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Spindle for Android: one viewport and a tabbed control panel (below the
 * viewport in portrait, beside it in landscape). The 3D work is all native.
 */
public final class MainActivity extends Activity {
    private static final String TAG = "Spindle";
    private static final int REQ_MODEL = 1, REQ_HDR = 2, REQ_TEXTURE = 3, REQ_STORAGE = 10;
    private static final String[] TABS = {"Model", "Look", "Scene", "Spin", "Explode", "Export"};

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final SceneModel scene = new SceneModel();

    private SpindleView view;
    private LinearLayout root, side, panel, tabBar, exportRow;
    private TextView statusLine, exportText;
    private ProgressBar exportBar;
    private String tab = "Model";
    private JSONObject status = new JSONObject();
    private String modelSignature = "";
    private String[] materials = {}, environments = {};
    private final List<String> customHdris = new ArrayList<>();
    private boolean previewing;

    private int exportKind = NativeBridge.EXPORT_MP4;
    private String exportPath, exportDisplayName;
    private Runnable pendingAfterPermission;

    // CI emulator test: `am start ... --es autotest /path/model.3mf`
    private String autotestModel;
    private int autotestStage;
    private final StringBuilder autotestLog = new StringBuilder();

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        NativeBridge.init(new File(getFilesDir(), "log.txt").getPath());
        getExternalFilesDir(null);  // creates the app's shared folder (used by the CI autotest)
        scene.load(NativeBridge.getSceneJson());
        materials = stringArray(NativeBridge.listJson("materials"));
        environments = stringArray(NativeBridge.listJson("environments"));
        getWindow().setStatusBarColor(0xFF15171B);
        getWindow().setNavigationBarColor(0xFF15171B);

        view = new SpindleView(this);
        buildLayout(getResources().getConfiguration().orientation);
        handleIntent(getIntent());
        handler.post(pollTask);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        view.onResume();
        view.requestRender();
    }

    @Override
    protected void onPause() {
        view.onPause();
        super.onPause();
    }

    @Override
    public void onConfigurationChanged(Configuration c) {
        super.onConfigurationChanged(c);
        buildLayout(c.orientation);
    }

    // ---------------------------------------------------------------- layout

    private void buildLayout(int orientation) {
        boolean landscape = orientation == Configuration.ORIENTATION_LANDSCAPE;
        if (root != null) root.removeAllViews();
        if (view.getParent() != null) ((ViewGroup) view.getParent()).removeView(view);
        root = new LinearLayout(this);
        root.setOrientation(landscape ? LinearLayout.HORIZONTAL : LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF121418);

        side = new LinearLayout(this);
        side.setOrientation(LinearLayout.VERTICAL);
        side.setBackgroundColor(0xFF1B1E23);
        int pad = dp(10);
        side.setPadding(pad, dp(6), pad, 0);

        statusLine = new TextView(this);
        statusLine.setTextColor(0xFFB8BCC4);
        statusLine.setTextSize(12);
        statusLine.setSingleLine(false);
        statusLine.setMaxLines(2);
        side.addView(statusLine);

        exportRow = new LinearLayout(this);
        exportRow.setOrientation(LinearLayout.VERTICAL);
        exportBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        exportBar.setMax(1000);
        exportText = new TextView(this);
        exportText.setTextColor(0xFFE6E8EC);
        exportText.setTextSize(13);
        Button cancel = new Button(this);
        cancel.setText("Cancel render");
        cancel.setAllCaps(false);
        cancel.setOnClickListener(v -> NativeBridge.cancelExport());
        exportRow.addView(exportText);
        exportRow.addView(exportBar);
        exportRow.addView(cancel);
        exportRow.setVisibility(View.GONE);
        side.addView(exportRow);

        tabBar = new LinearLayout(this);
        tabBar.setOrientation(LinearLayout.HORIZONTAL);
        HorizontalScrollView tabScroll = new HorizontalScrollView(this);
        tabScroll.setHorizontalScrollBarEnabled(false);
        tabScroll.addView(tabBar);
        side.addView(tabScroll);
        for (String t : TABS) {
            Button b = new Button(this, null, android.R.attr.borderlessButtonStyle);
            b.setText(t);
            b.setAllCaps(false);
            b.setOnClickListener(v -> {
                tab = t;
                rebuildPanel();
            });
            tabBar.addView(b);
        }

        ScrollView scroll = new ScrollView(this);
        panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(0, 0, 0, dp(24));
        scroll.addView(panel);
        side.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        if (landscape) {
            root.addView(view, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 0.62f));
            root.addView(side, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 0.38f));
        } else {
            root.addView(view, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 0.55f));
            root.addView(side, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 0.45f));
        }
        setContentView(root);
        rebuildPanel();
    }

    private int dp(float v) { return Math.round(v * getResources().getDisplayMetrics().density); }

    private void pushScene() {
        NativeBridge.setSceneJson(scene.json());
        view.requestRender();
    }

    private void rebuildPanel() {
        for (int i = 0; i < tabBar.getChildCount(); ++i) {
            Button b = (Button) tabBar.getChildAt(i);
            b.setTextColor(b.getText().toString().equals(tab) ? 0xFFF28C38 : 0xFFB8BCC4);
        }
        panel.removeAllViews();
        Ui ui = new Ui(this, panel, scene, this::pushScene);
        switch (tab) {
            case "Model": buildModel(ui); break;
            case "Look": buildLook(ui); break;
            case "Scene": buildScene(ui); break;
            case "Spin": buildSpin(ui); break;
            case "Explode": buildExplode(ui); break;
            default: buildExport(ui); break;
        }
    }

    // ---------------------------------------------------------------- panels

    private JSONObject model() { return status.optJSONObject("model"); }

    private void buildModel(Ui ui) {
        ui.button("Open STL / 3MF…", v -> pick(REQ_MODEL, "*/*"));
        JSONObject m = model();
        if (m == null) {
            ui.note(status.optBoolean("loading") ? "Loading…" : "No model yet. Open a file, or share one to Spindle from a file manager.");
        } else {
            JSONArray size = m.optJSONArray("size");
            ui.note(String.format(Locale.US, "%s\n%,d triangles · %.1f × %.1f × %.1f mm · %d part(s)%s",
                                  m.optString("name"), m.optLong("triangles"), size.optDouble(0), size.optDouble(1),
                                  size.optDouble(2), m.optJSONArray("parts").length(),
                                  m.optBoolean("hasColors") ? " · colours from file" : ""));
        }
        JSONArray warnings = status.optJSONArray("warnings");
        if (warnings != null)
            for (int i = 0; i < warnings.length(); ++i) ui.note("⚠ " + warnings.optString(i)).setTextColor(0xFFFFC066);
        ui.choice("Up axis", "model.up", new String[] {"Z (3D printing)", "Y"}, new Object[] {"z", "y"}, null);
        ui.buttonRow(new String[] {"Rotate X", "Rotate Y", "Rotate Z", "Reset"}, new View.OnClickListener[] {
            v -> turn(0), v -> turn(1), v -> turn(2), v -> {
                scene.put("model.quarterTurns", new JSONArray(java.util.Arrays.asList(0, 0, 0)));
                pushScene();
            }});
        ui.slider("Smoothing angle", "model.creaseAngle", 0, 180, "%.0f°", false);
        ui.header("View");
        ui.buttonRow(new String[] {"Front", "Side", "Top", "Frame"}, new View.OnClickListener[] {
            v -> preset(0), v -> preset(1), v -> preset(2), v -> {
                NativeBridge.frameModel();
                view.requestRender();
            }});
        ui.note("Drag to orbit · pinch to zoom · two fingers to pan · double-tap to frame.");
        String gl = status.optString("gl");
        if (!gl.isEmpty()) ui.note("GPU: " + gl);
    }

    private void turn(int axis) {
        try {
            JSONArray q = new JSONObject(scene.json()).getJSONObject("model").getJSONArray("quarterTurns");
            q.put(axis, (q.optInt(axis) + 1) & 3);
            scene.put("model.quarterTurns", q);
            pushScene();
        } catch (JSONException ignored) {
        }
    }

    private void preset(int which) {
        NativeBridge.viewPreset(which);
        view.requestRender();
    }

    private void buildLook(Ui ui) {
        String current = scene.getString("material.preset", "");
        int sel = 0;
        for (int i = 0; i < materials.length; ++i)
            if (materials[i].equals(current)) sel = i;
        android.widget.Spinner sp = ui.choice("Material", null, materials, materials, pos -> {
            scene.load(NativeBridge.applyMaterialPreset(materials[pos]));
            view.requestRender();
            rebuildPanel();
        });
        sp.setSelection(sel, false);
        JSONObject m = model();
        if (m != null && m.optBoolean("hasColors")) ui.toggle("Colours from file", "material.useFileColors");
        if (m != null && m.optBoolean("hasPbr")) ui.toggle("Metal / roughness from file", "material.useFileFinish");
        ui.color(m != null && m.optBoolean("hasColors") ? "Colour (unpainted parts)" : "Colour", "material.baseColor");
        ui.slider("Roughness", "material.roughness", 0, 1, "%.2f", false);
        ui.slider("Metalness", "material.metalness", 0, 1, "%.2f", false);
        ui.slider("Clearcoat", "material.clearcoat", 0, 1, "%.2f", false);
        ui.slider("Sheen", "material.sheen", 0, 1, "%.2f", false);
        ui.slider("Transmission (glass)", "material.transmission", 0, 1, "%.2f", false);
        ui.header("Surface detail");
        ui.toggle("FDM layer lines", "material.layerLines.enabled");
        ui.slider("Layer height", "material.layerLines.height", 0.04, 0.6, "%.2f mm", false);
        ui.slider("Layer depth", "material.layerLines.depth", 0, 1, "%.2f", false);
        ui.slider("Noise bump", "material.noise.strength", 0, 1, "%.2f", false);
        ui.slider("Noise scale", "material.noise.scale", 0.05, 20, "%.2f mm", true);
        ui.header("Pattern / texture");
        ui.choice("Pattern", "material.pattern.type",
                  new String[] {"None", "Image texture…", "Wood", "Marble", "Carbon fibre", "Checker", "Granite"},
                  new Object[] {"none", "image", "wood", "marble", "carbon", "checker", "granite"}, pos -> {
                      if (pos == 1) pick(REQ_TEXTURE, "image/*");
                  });
        ui.color("Pattern colour", "material.pattern.color");
        ui.slider("Pattern scale", "material.pattern.scale", 0.5, 200, "%.1f mm", true);
        ui.slider("Pattern strength", "material.pattern.strength", 0, 1, "%.2f", false);
        ui.toggle("Height gradient", "material.gradient.enabled");
        ui.color("Gradient colour", "material.gradient.color");
    }

    private void buildScene(Ui ui) {
        List<String> labels = new ArrayList<>();
        List<Object> values = new ArrayList<>();
        for (String e : environments) {
            labels.add(e);
            values.add(e);
        }
        for (String h : customHdris) {
            labels.add(new File(h).getName());
            values.add(h);
        }
        labels.add("Load an .hdr file…");
        values.add("__load__");
        ui.choice("Lighting", "environment.source", labels.toArray(new String[0]), values.toArray(), pos -> {
            if ("__load__".equals(values.get(pos))) pick(REQ_HDR, "*/*");
        });
        ui.slider("Rotation", "environment.rotation", -180, 180, "%.0f°", false);
        ui.slider("Intensity", "environment.intensity", 0, 4, "%.2f", false);
        ui.slider("Saturation", "environment.saturation", 0, 2, "%.2f", false);
        ui.header("Background");
        ui.choice("Background", "environment.background",
                  new String[] {"Environment", "Solid colour", "Vertical gradient", "Radial gradient", "Transparent"},
                  new Object[] {"environment", "solid", "gradient", "radial", "transparent"}, null);
        ui.color("Colour / top / centre", "environment.backgroundColor");
        ui.color("Bottom / edge", "environment.backgroundColor2");
        ui.slider("Environment blur", "environment.backgroundBlur", 0, 1, "%.2f", false);
        ui.header("Ground and shadows");
        ui.choice("Ground", "environment.ground", new String[] {"None", "Shadow catcher", "Floor", "Reflective floor"},
                  new Object[] {"none", "shadow_catcher", "floor", "reflective"}, null);
        ui.color("Floor colour", "environment.floorColor");
        ui.slider("Floor roughness", "environment.floorRoughness", 0, 1, "%.2f", false);
        ui.slider("Reflection", "environment.reflection", 0, 1, "%.2f", false);
        ui.slider("Shadow", "environment.shadowStrength", 0, 1, "%.2f", false);
        ui.slider("Shadow softness", "environment.shadowSoftness", 0, 30, "%.0f°", false);
        ui.slider("Ambient occlusion", "environment.aoStrength", 0, 1, "%.2f", false);
        ui.header("Lights");
        ui.toggle("Three-point light rig", "environment.lights.enabled");
        ui.slider("Light intensity", "environment.lights.intensity", 0, 10, "%.1f", false);
        ui.slider("Key azimuth", "environment.lights.keyAzimuth", -180, 180, "%.0f°", false);
        ui.slider("Key elevation", "environment.lights.keyElevation", 5, 89, "%.0f°", false);
        ui.choice("Tonemap", "environment.tonemap", new String[] {"ACES", "AgX", "Linear"},
                  new Object[] {"aces", "agx", "linear"}, null);
        ui.slider("Exposure", "environment.exposure", -4, 4, "%+.1f EV", false);
    }

    private void buildSpin(Ui ui) {
        Button b = ui.button(previewing ? "Stop preview" : "Preview turntable", v -> {
            previewing = !previewing;
            NativeBridge.setPreview(previewing);
            view.requestRender();
            rebuildPanel();
        });
        b.setTextColor(0xFFF28C38);
        ui.choice("Motion", "turntable.mode", new String[] {"Rotate object", "Orbit camera"},
                  new Object[] {"rotate_object", "orbit_camera"}, null);
        ui.slider("Duration", "turntable.seconds", 2, 60, "%.1f s", true);
        ui.choice("Frame rate", "turntable.fps", new String[] {"24 fps", "25 fps", "30 fps", "50 fps", "60 fps"},
                  new Object[] {24, 25, 30, 50, 60}, null);
        ui.slider("Rotations", "turntable.rotations", 1, 4, "%.0f", false);
        ui.choice("Direction", "turntable.direction", new String[] {"Counter-clockwise", "Clockwise"},
                  new Object[] {"ccw", "cw"}, null);
        ui.choice("Easing", "turntable.easing", new String[] {"Linear (seamless loop)", "Ease in-out", "Hold, then spin"},
                  new Object[] {"linear", "ease_in_out", "hold"}, null);
        ui.slider("Vertical bob", "turntable.bob", 0, 20, "%.0f°", false);
        ui.slider("Motion blur", "turntable.shutter", 0, 1, "%.2f", false);
        ui.header("Camera");
        ui.slider("Elevation", "camera.elevation", -30, 80, "%.0f°", false);
        ui.slider("Focal length", "camera.focalLength", 18, 200, "%.0f mm", true);
        ui.slider("Fill frame", "camera.fill", 0.3, 1, "%.2f", false);
        ui.toggle("Orthographic", "camera.orthographic");
        ui.toggle("Depth of field", "camera.depthOfField");
        ui.slider("f-stop", "camera.fStop", 0.7, 22, "f/%.1f", true);
    }

    private void buildExplode(Ui ui) {
        JSONObject m = model();
        int parts = m == null ? 0 : m.optJSONArray("parts").length();
        if (parts < 2) {
            ui.note("This model is one piece: there is nothing to explode. Multi-part 3MF files and STLs made of "
                    + "separate pieces can be exploded.");
            return;
        }
        ui.note(parts + " parts; the outermost leave first.");
        ui.slider("Explode (view)", "explode.manual", 0, 1, "%.2f", false);
        ui.slider("Distance", "explode.distance", 0.1, 4, "%.2f×", false);
        ui.slider("Stagger", "explode.stagger", 0, 1, "%.2f", false);
        ui.toggle("Animate during the turntable", "explode.animate");
        ui.choice("Timing", "explode.timing", new String[] {"Explode, then reassemble", "Explode and hold", "Assemble from parts"},
                  new Object[] {"explode_return", "explode_hold", "assemble"}, null);
        ui.slider("Starts at", "explode.start", 0, 1, "%.2f of the spin", false);
        ui.slider("Ends at", "explode.end", 0, 1, "%.2f of the spin", false);
        ui.note("Use Spin → Preview to watch it.");
    }

    private void buildExport(Ui ui) {
        ui.choice("Format", null, new String[] {"Video (MP4)", "Animated GIF", "Still image (PNG)"},
                  new Object[] {"mp4", "gif", "png"}, pos -> {
                      exportKind = pos;
                      rebuildPanel();
                  }).setSelection(exportKind, false);
        int w = (int) scene.getDouble("output.width", 1280), h = (int) scene.getDouble("output.height", 720);
        String[] resLabels = {"720p (1280×720)", "1080p (1920×1080)", "Square (1080×1080)", "Vertical (1080×1920)",
                              "Small square (600×600)", "Small (800×450)"};
        int[][] res = {{1280, 720}, {1920, 1080}, {1080, 1080}, {1080, 1920}, {600, 600}, {800, 450}};
        android.widget.Spinner rs = ui.choice("Size", null, resLabels, resLabels, pos -> {
            scene.put("output.width", res[pos][0]);
            scene.put("output.height", res[pos][1]);
            pushScene();
        });
        for (int i = 0; i < res.length; ++i)
            if (res[i][0] == w && res[i][1] == h) rs.setSelection(i, false);
        ui.choice("Quality", "output.quality", new String[] {"Draft (1 sample)", "Standard (16)", "High (64)", "Ultra (256)"},
                  new Object[] {"draft", "standard", "high", "ultra"}, null);
        int frames = (int) Math.round(scene.getDouble("turntable.seconds", 8) * scene.getDouble("turntable.fps", 30));
        ui.note(exportKind == NativeBridge.EXPORT_STILL ? "One image, from the turntable's first frame."
                                                       : frames + " frames (set the length on the Spin tab).");
        Button go = ui.button(exportKind == NativeBridge.EXPORT_STILL ? "Render image" : "Render turntable", v -> startExport());
        go.setTextColor(0xFFF28C38);
        ui.note("Videos are saved to Movies/Spindle, GIFs and images to Pictures/Spindle.");
    }

    // ---------------------------------------------------------------- export

    private void startExport() {
        if (Build.VERSION.SDK_INT < 29 &&
            checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            pendingAfterPermission = this::startExport;
            requestPermissions(new String[] {Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_STORAGE);
            return;
        }
        JSONObject m = model();
        String base = m == null ? "spindle" : m.optString("name", "spindle").replaceAll("\\.[^.]*$", "");
        base = base.replaceAll("[^A-Za-z0-9_-]+", "_");
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        String ext = exportKind == NativeBridge.EXPORT_MP4 ? ".mp4" : exportKind == NativeBridge.EXPORT_GIF ? ".gif" : ".png";
        File dir = new File(getCacheDir(), "exports");
        dir.mkdirs();
        exportDisplayName = base + "_" + (exportKind == NativeBridge.EXPORT_STILL ? "" : "turntable_") + stamp + ext;
        exportPath = new File(dir, exportDisplayName).getPath();
        String err = NativeBridge.startExport(exportKind, exportPath);
        if (!err.isEmpty()) {
            toast(err);
            return;
        }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        view.requestRender();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code == REQ_STORAGE && results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED &&
            pendingAfterPermission != null) {
            pendingAfterPermission.run();
        } else if (code == REQ_STORAGE) {
            toast("Spindle needs storage access to save renders on this Android version.");
        }
        pendingAfterPermission = null;
    }

    private void onExportFinished(JSONObject ex) {
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (!ex.optBoolean("ok")) {
            String e = ex.optString("error");
            if (!e.equals("Cancelled.")) new AlertDialog.Builder(this).setTitle("Render failed").setMessage(e)
                .setPositiveButton("OK", null).show();
            return;
        }
        final File file = new File(exportPath);
        final int kind = exportKind;
        final String name = exportDisplayName;
        io.execute(() -> {
            try {
                Uri uri = saveToGallery(file, name, kind);
                handler.post(() -> showSaved(uri, kind));
            } catch (IOException e) {
                handler.post(() -> toast("Could not save: " + e.getMessage()));
            }
        });
    }

    private static String mimeOf(int kind) {
        return kind == NativeBridge.EXPORT_MP4 ? "video/mp4" : kind == NativeBridge.EXPORT_GIF ? "image/gif" : "image/png";
    }

    /** Copies a finished render into the shared Movies / Pictures collection. */
    private Uri saveToGallery(File file, String name, int kind) throws IOException {
        boolean video = kind == NativeBridge.EXPORT_MP4;
        String mime = mimeOf(kind);
        ContentResolver cr = getContentResolver();
        if (Build.VERSION.SDK_INT >= 29) {
            ContentValues v = new ContentValues();
            v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
            v.put(MediaStore.MediaColumns.MIME_TYPE, mime);
            v.put(MediaStore.MediaColumns.RELATIVE_PATH, (video ? Environment.DIRECTORY_MOVIES : Environment.DIRECTORY_PICTURES) + "/Spindle");
            v.put(MediaStore.MediaColumns.IS_PENDING, 1);
            Uri collection = video ? MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
                                   : MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY);
            Uri uri = cr.insert(collection, v);
            if (uri == null) throw new IOException("the media store refused the file");
            try (InputStream in = new FileInputStream(file); OutputStream out = cr.openOutputStream(uri)) {
                copy(in, out);
            }
            v.clear();
            v.put(MediaStore.MediaColumns.IS_PENDING, 0);
            cr.update(uri, v, null, null);
            file.delete();
            return uri;
        }
        File dir = new File(Environment.getExternalStoragePublicDirectory(video ? Environment.DIRECTORY_MOVIES
                                                                                 : Environment.DIRECTORY_PICTURES), "Spindle");
        dir.mkdirs();
        File out = new File(dir, name);
        try (InputStream in = new FileInputStream(file); OutputStream o = new FileOutputStream(out)) {
            copy(in, o);
        }
        file.delete();
        final Uri[] result = new Uri[1];
        final Object lock = new Object();
        MediaScannerConnection.scanFile(this, new String[] {out.getPath()}, new String[] {mime}, (p, uri) -> {
            synchronized (lock) {
                result[0] = uri;
                lock.notifyAll();
            }
        });
        synchronized (lock) {
            try {
                if (result[0] == null) lock.wait(5000);
            } catch (InterruptedException ignored) {
            }
        }
        return result[0];
    }

    private void showSaved(Uri uri, int kind) {
        String where = kind == NativeBridge.EXPORT_MP4 ? "Movies/Spindle" : "Pictures/Spindle";
        AlertDialog.Builder b = new AlertDialog.Builder(this).setTitle("Saved").setMessage("Saved to " + where + ".")
            .setNegativeButton("Close", null);
        if (uri != null) {
            b.setPositiveButton("Share", (d, w) -> {
                Intent s = new Intent(Intent.ACTION_SEND).setType(mimeOf(kind)).putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                startActivity(Intent.createChooser(s, "Share render"));
            });
            b.setNeutralButton("Open", (d, w) -> {
                Intent o = new Intent(Intent.ACTION_VIEW).setDataAndType(uri, mimeOf(kind))
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                try {
                    startActivity(o);
                } catch (Exception e) {
                    toast("No app can open this file.");
                }
            });
        }
        b.show();
    }

    // ---------------------------------------------------------------- files

    private void pick(int request, String mime) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(mime);
        try {
            startActivityForResult(i, request);
        } catch (Exception e) {
            toast("No file picker is available.");
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (result != RESULT_OK || data == null || data.getData() == null) {
            if (request == REQ_TEXTURE && "image".equals(scene.getString("material.pattern.type", ""))
                && scene.getString("material.pattern.texture", "").isEmpty()) {
                scene.put("material.pattern.type", "none");
                pushScene();
                rebuildPanel();
            }
            return;
        }
        importUri(data.getData(), request);
    }

    private void handleIntent(Intent intent) {
        if (intent == null) return;
        autotestModel = intent.getStringExtra("autotest");
        if (autotestModel != null) {
            autotestStage = 0;
            return;
        }
        Uri uri = null;
        if (Intent.ACTION_VIEW.equals(intent.getAction())) uri = intent.getData();
        else if (Intent.ACTION_SEND.equals(intent.getAction())) uri = intent.getParcelableExtra(Intent.EXTRA_STREAM);
        if (uri != null) importUri(uri, REQ_MODEL);
    }

    /** Copies a picked document into the app's cache (native code reads plain files). */
    private void importUri(Uri uri, int request) {
        final String name = displayName(uri);
        io.execute(() -> {
            String ext = name.contains(".") ? name.substring(name.lastIndexOf('.')).toLowerCase(Locale.US) : "";
            String folder = request == REQ_MODEL ? "models" : request == REQ_HDR ? "hdri" : "textures";
            File dir = new File(getCacheDir(), folder);
            dir.mkdirs();
            File out = new File(dir, request == REQ_MODEL ? "current" + ext : name.replaceAll("[^A-Za-z0-9._-]+", "_"));
            try (InputStream in = getContentResolver().openInputStream(uri); OutputStream o = new FileOutputStream(out)) {
                if (in == null) throw new IOException("cannot open");
                copy(in, o);
            } catch (IOException e) {
                handler.post(() -> toast("Could not read " + name));
                return;
            }
            handler.post(() -> {
                if (request == REQ_MODEL) {
                    NativeBridge.loadModel(out.getPath(), name);
                    tab = "Model";
                } else if (request == REQ_HDR) {
                    if (!customHdris.contains(out.getPath())) customHdris.add(out.getPath());
                    scene.put("environment.source", out.getPath());
                    scene.put("environment.background", "environment");
                    pushScene();
                } else {
                    scene.put("material.pattern.texture", out.getPath());
                    scene.put("material.pattern.type", "image");
                    pushScene();
                }
                rebuildPanel();
                view.requestRender();
            });
        });
    }

    private String displayName(Uri uri) {
        String name = null;
        try (Cursor c = getContentResolver().query(uri, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst()) name = c.getString(0);
        } catch (Exception ignored) {
        }
        if (name == null) name = uri.getLastPathSegment();
        return name == null ? "model" : name;
    }

    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[1 << 16];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
    }

    // ---------------------------------------------------------------- status

    private final Runnable pollTask = new Runnable() {
        @Override
        public void run() {
            poll();
            handler.postDelayed(this, 250);
        }
    };

    private void poll() {
        try {
            status = new JSONObject(NativeBridge.statusJson());
        } catch (JSONException e) {
            return;
        }
        String err = status.optString("error");
        if (!err.isEmpty()) toast(err);
        String glErr = status.optString("glError");
        if (!glErr.isEmpty()) statusLine.setText(glErr);

        JSONObject m = model();
        StringBuilder line = new StringBuilder();
        if (status.optBoolean("loading")) {
            line.append(String.format(Locale.US, "Loading… %d%%", Math.round(status.optDouble("loadProgress") * 100)));
        } else if (m != null) {
            line.append(m.optString("name")).append(String.format(Locale.US, " · %,d tris", m.optLong("triangles")));
            int s = status.optInt("samples"), max = status.optInt("maxSamples");
            if (s < max && !status.optBoolean("preview")) line.append(" · refining ").append(s).append('/').append(max);
        } else {
            line.append("Open an STL or 3MF file to begin.");
        }
        if (status.optBoolean("envLoading")) line.append(" · building lighting…");
        if (glErr.isEmpty()) statusLine.setText(line);

        // Rebuild model-dependent panels when a different model (or part set) arrives.
        String sig = m == null ? "" : m.optString("name") + "/" + m.optLong("triangles") + "/" + m.optJSONArray("parts").length();
        if (!sig.equals(modelSignature)) {
            modelSignature = sig;
            scene.load(NativeBridge.getSceneJson());
            rebuildPanel();
        }

        JSONObject ex = status.optJSONObject("export");
        if (ex != null) {
            boolean active = ex.optBoolean("active");
            exportRow.setVisibility(active ? View.VISIBLE : View.GONE);
            if (active) {
                exportBar.setProgress((int) Math.round(ex.optDouble("progress") * 1000));
                double eta = ex.optDouble("eta", -1);
                exportText.setText(String.format(Locale.US, "Rendering frame %d of %d%s", Math.min(ex.optInt("frame") + 1,
                                   Math.max(1, ex.optInt("total"))), Math.max(1, ex.optInt("total")),
                                   eta >= 0 ? String.format(Locale.US, " · about %s left", formatSeconds(eta)) : ""));
                view.requestRender();
            }
            if (ex.optBoolean("finished")) {
                if (autotestModel != null) autotestRecord(ex);
                else onExportFinished(ex);
            }
        }
        if (autotestModel != null) autotestStep(m);
    }

    private static String formatSeconds(double s) {
        long t = Math.round(s);
        return t >= 60 ? String.format(Locale.US, "%dm %02ds", t / 60, t % 60) : t + "s";
    }

    private void toast(String text) { Toast.makeText(this, text, Toast.LENGTH_LONG).show(); }

    private static String[] stringArray(String json) {
        try {
            JSONArray a = new JSONArray(json);
            String[] out = new String[a.length()];
            for (int i = 0; i < a.length(); ++i) out[i] = a.getString(i);
            return out;
        } catch (JSONException e) {
            return new String[0];
        }
    }

    // ---------------------------------------------------------------- CI autotest

    private File autotestDir() {
        File d = getExternalFilesDir(null);
        return d != null ? d : getFilesDir();
    }

    private void autotestStep(JSONObject m) {
        File dir = autotestDir();
        switch (autotestStage) {
            case 0:
                Log.i(TAG, "autotest: loading " + autotestModel);
                NativeBridge.loadModel(autotestModel, new File(autotestModel).getName());
                autotestStage = 1;
                break;
            case 1:
                if (m != null && status.optInt("samples") >= status.optInt("maxSamples")) {
                    scene.load(NativeBridge.getSceneJson());
                    scene.put("output.width", 640);
                    scene.put("output.height", 360);
                    scene.put("output.quality", "draft");
                    scene.put("turntable.seconds", 2.0);
                    scene.put("turntable.fps", 15);
                    scene.put("explode.animate", true);
                    pushScene();
                    autotestLog.append("model ").append(m.optString("name")).append(' ').append(m.optLong("triangles"))
                        .append(" tris ").append(m.optJSONArray("parts").length()).append(" parts\n");
                    autotestLog.append("gl ").append(status.optString("gl")).append('\n');
                    exportKind = NativeBridge.EXPORT_STILL;
                    exportPath = new File(dir, "autotest_still.png").getPath();
                    autotestLog.append("still start ").append(NativeBridge.startExport(exportKind, exportPath)).append('\n');
                    autotestStage = 2;
                }
                break;
            case 3:
                exportKind = NativeBridge.EXPORT_MP4;
                exportPath = new File(dir, "autotest.mp4").getPath();
                autotestLog.append("mp4 start ").append(NativeBridge.startExport(exportKind, exportPath)).append('\n');
                autotestStage = 4;
                break;
            case 5:
                exportKind = NativeBridge.EXPORT_GIF;
                exportPath = new File(dir, "autotest.gif").getPath();
                autotestLog.append("gif start ").append(NativeBridge.startExport(exportKind, exportPath)).append('\n');
                autotestStage = 6;
                break;
            case 7:
                autotestLog.append("done\n");
                try (OutputStream o = new FileOutputStream(new File(dir, "autotest_result.txt"))) {
                    o.write(autotestLog.toString().getBytes("UTF-8"));
                } catch (IOException e) {
                    Log.e(TAG, "autotest: cannot write result", e);
                }
                Log.i(TAG, "autotest: finished\n" + autotestLog);
                autotestStage = 8;
                break;
            default:
                break;
        }
        view.requestRender();
    }

    private void autotestRecord(JSONObject ex) {
        String what = autotestStage == 2 ? "still" : autotestStage == 4 ? "mp4" : "gif";
        autotestLog.append(what).append(ex.optBoolean("ok") ? " ok" : " FAILED: " + ex.optString("error")).append('\n');
        Log.i(TAG, "autotest: " + what + " ok=" + ex.optBoolean("ok") + " " + ex.optString("error"));
        autotestStage += 1;
    }
}
