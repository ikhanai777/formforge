package com.spindle.app;

/** JNI entry points into libspindle (see app/src/main/cpp/jni_bridge.cpp). */
final class NativeBridge {
    static {
        System.loadLibrary("spindle");
    }

    static final int EXPORT_MP4 = 0;
    static final int EXPORT_GIF = 1;
    static final int EXPORT_STILL = 2;

    private NativeBridge() {}

    static native void init(String logPath);

    // GL thread.
    static native void onSurfaceCreated();
    static native void onSurfaceChanged(int width, int height);
    /** Draws one frame; returns true if another frame is wanted soon. */
    static native boolean onDrawFrame();

    // Any thread.
    static native String getSceneJson();
    static native void setSceneJson(String json);
    static native String applyMaterialPreset(String name);
    static native String listJson(String what);
    static native void loadModel(String path, String displayName);
    static native void orbit(float dxPx, float dyPx);
    static native void zoom(float factor, float ndcX, float ndcY);
    static native void pan(float dxPx, float dyPx);
    static native void frameModel();
    static native void viewPreset(int which);
    static native void setPreview(boolean on);
    /** Returns an error message, or "" when the export was queued. */
    static native String startExport(int kind, String path);
    static native void cancelExport();
    static native String statusJson();
}
