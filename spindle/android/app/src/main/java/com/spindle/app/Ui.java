package com.spindle.app;

import android.app.AlertDialog;
import android.content.Context;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.Spinner;
import android.widget.Switch;
import android.widget.TextView;

import java.util.Locale;

/** Builds the control panel rows: every control edits one scene path. */
final class Ui {
    interface OnChange {
        void changed();
    }

    final Context ctx;
    final LinearLayout box;
    final SceneModel scene;
    final OnChange onChange;
    private final float dp;

    Ui(Context ctx, LinearLayout box, SceneModel scene, OnChange onChange) {
        this.ctx = ctx;
        this.box = box;
        this.scene = scene;
        this.onChange = onChange;
        this.dp = ctx.getResources().getDisplayMetrics().density;
    }

    int px(float dps) { return Math.round(dps * dp); }

    TextView header(String text) {
        TextView t = new TextView(ctx);
        t.setText(text);
        t.setTextSize(13);
        t.setTextColor(0xFFF28C38);
        t.setPadding(0, px(14), 0, px(4));
        box.addView(t);
        return t;
    }

    TextView note(String text) {
        TextView t = new TextView(ctx);
        t.setText(text);
        t.setTextSize(13);
        t.setTextColor(0xFFB8BCC4);
        t.setPadding(0, px(4), 0, px(4));
        box.addView(t);
        return t;
    }

    Button button(String text, View.OnClickListener l) {
        Button b = new Button(ctx);
        b.setText(text);
        b.setAllCaps(false);
        b.setOnClickListener(l);
        box.addView(b, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return b;
    }

    LinearLayout buttonRow(String[] labels, View.OnClickListener[] listeners) {
        LinearLayout row = new LinearLayout(ctx);
        row.setOrientation(LinearLayout.HORIZONTAL);
        for (int i = 0; i < labels.length; ++i) {
            Button b = new Button(ctx);
            b.setText(labels[i]);
            b.setAllCaps(false);
            b.setOnClickListener(listeners[i]);
            row.addView(b, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        }
        box.addView(row);
        return row;
    }

    /** A labelled slider. `log` maps the track logarithmically (for wide ranges). */
    void slider(String label, String path, double min, double max, String format, boolean log) {
        LinearLayout row = new LinearLayout(ctx);
        row.setOrientation(LinearLayout.VERTICAL);
        final TextView title = new TextView(ctx);
        title.setTextColor(0xFFE6E8EC);
        title.setTextSize(14);
        final SeekBar bar = new SeekBar(ctx);
        bar.setMax(1000);
        double v = scene.getDouble(path, min);
        bar.setProgress((int) Math.round(toTrack(v, min, max, log) * 1000));
        title.setText(label + "   " + String.format(Locale.US, format, v));
        bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar s, int p, boolean fromUser) {
                if (!fromUser) return;
                double value = fromTrack(p / 1000.0, min, max, log);
                scene.put(path, value);
                title.setText(label + "   " + String.format(Locale.US, format, value));
                onChange.changed();
            }
            @Override public void onStartTrackingTouch(SeekBar s) {}
            @Override public void onStopTrackingTouch(SeekBar s) {}
        });
        row.addView(title);
        row.addView(bar);
        row.setPadding(0, px(4), 0, px(2));
        box.addView(row);
    }

    private static double toTrack(double v, double min, double max, boolean log) {
        if (log && min > 0) return Math.log(Math.max(v, min) / min) / Math.log(max / min);
        return (v - min) / (max - min);
    }

    private static double fromTrack(double t, double min, double max, boolean log) {
        if (log && min > 0) return min * Math.pow(max / min, t);
        return min + t * (max - min);
    }

    Switch toggle(String label, String path) {
        Switch s = new Switch(ctx);
        s.setText(label);
        s.setTextColor(0xFFE6E8EC);
        s.setTextSize(14);
        s.setChecked(scene.getBool(path, false));
        s.setPadding(0, px(6), 0, px(6));
        s.setOnCheckedChangeListener((CompoundButton b, boolean on) -> {
            scene.put(path, on);
            onChange.changed();
        });
        box.addView(s);
        return s;
    }

    /** A drop-down; `values` are the JSON values matching each label. */
    Spinner choice(String label, String path, String[] labels, Object[] values, java.util.function.IntConsumer after) {
        TextView t = new TextView(ctx);
        t.setText(label);
        t.setTextColor(0xFFE6E8EC);
        t.setTextSize(14);
        t.setPadding(0, px(6), 0, 0);
        box.addView(t);
        Spinner sp = new Spinner(ctx);
        ArrayAdapter<String> a = new ArrayAdapter<>(ctx, android.R.layout.simple_spinner_item, labels);
        a.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        sp.setAdapter(a);
        Object current = path == null ? null : currentValue(path, values);
        for (int i = 0; i < values.length; ++i)
            if (values[i].equals(current)) sp.setSelection(i, false);
        sp.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            boolean first = true;
            @Override public void onItemSelected(AdapterView<?> parent, View view, int pos, long id) {
                if (first) {  // the initial selection is not a user change
                    first = false;
                    return;
                }
                if (path != null) {
                    scene.put(path, values[pos]);
                    onChange.changed();
                }
                if (after != null) after.accept(pos);
            }
            @Override public void onNothingSelected(AdapterView<?> parent) {}
        });
        box.addView(sp);
        return sp;
    }

    private Object currentValue(String path, Object[] values) {
        if (values.length > 0 && values[0] instanceof Integer) return (int) Math.round(scene.getDouble(path, 0));
        return scene.getString(path, "");
    }

    /** A swatch that opens an RGB picker. */
    void color(String label, String path) {
        LinearLayout row = new LinearLayout(ctx);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, px(6), 0, px(6));
        final View swatch = new View(ctx);
        TextView t = new TextView(ctx);
        t.setText(label);
        t.setTextColor(0xFFE6E8EC);
        t.setTextSize(14);
        t.setPadding(px(12), 0, 0, 0);
        paintSwatch(swatch, scene.getColor(path));
        row.addView(swatch, new LinearLayout.LayoutParams(px(36), px(28)));
        row.addView(t);
        row.setOnClickListener(v -> pickColor(label, scene.getColor(path), rgb -> {
            scene.putColor(path, rgb);
            paintSwatch(swatch, rgb);
            onChange.changed();
        }));
        box.addView(row);
    }

    private void paintSwatch(View v, float[] rgb) {
        GradientDrawable d = new GradientDrawable();
        d.setColor(Color.rgb(Math.round(rgb[0] * 255), Math.round(rgb[1] * 255), Math.round(rgb[2] * 255)));
        d.setCornerRadius(px(6));
        d.setStroke(px(1), 0xFF60646C);
        v.setBackground(d);
    }

    interface ColorCallback {
        void picked(float[] rgb);
    }

    void pickColor(String title, float[] initial, ColorCallback cb) {
        final float[] rgb = initial.clone();
        LinearLayout content = new LinearLayout(ctx);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(px(20), px(12), px(20), 0);
        final View preview = new View(ctx);
        paintSwatch(preview, rgb);
        content.addView(preview, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, px(48)));
        final EditText hex = new EditText(ctx);
        hex.setInputType(InputType.TYPE_CLASS_TEXT);
        hex.setText(toHex(rgb));
        final SeekBar[] bars = new SeekBar[3];
        String[] names = {"Red", "Green", "Blue"};
        for (int i = 0; i < 3; ++i) {
            final int c = i;
            TextView t = new TextView(ctx);
            t.setText(names[i]);
            content.addView(t);
            bars[i] = new SeekBar(ctx);
            bars[i].setMax(255);
            bars[i].setProgress(Math.round(rgb[i] * 255));
            bars[i].setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
                @Override public void onProgressChanged(SeekBar s, int p, boolean fromUser) {
                    if (!fromUser) return;
                    rgb[c] = p / 255f;
                    paintSwatch(preview, rgb);
                    hex.setText(toHex(rgb));
                }
                @Override public void onStartTrackingTouch(SeekBar s) {}
                @Override public void onStopTrackingTouch(SeekBar s) {}
            });
            content.addView(bars[i]);
        }
        content.addView(hex);
        new AlertDialog.Builder(ctx)
            .setTitle(title)
            .setView(content)
            .setPositiveButton("OK", (d, w) -> {
                float[] typed = fromHex(hex.getText().toString());
                cb.picked(typed != null ? typed : rgb);
            })
            .setNegativeButton("Cancel", null)
            .show();
    }

    static String toHex(float[] rgb) {
        return String.format(Locale.US, "#%02X%02X%02X", Math.round(rgb[0] * 255), Math.round(rgb[1] * 255),
                             Math.round(rgb[2] * 255));
    }

    static float[] fromHex(String s) {
        s = s.trim();
        if (s.startsWith("#")) s = s.substring(1);
        if (s.length() != 6) return null;
        try {
            int v = Integer.parseInt(s, 16);
            return new float[] {((v >> 16) & 255) / 255f, ((v >> 8) & 255) / 255f, (v & 255) / 255f};
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
