package com.spindle.app;

import android.content.Context;
import android.opengl.GLSurfaceView;
import android.view.GestureDetector;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;

import javax.microedition.khronos.egl.EGL10;
import javax.microedition.khronos.egl.EGLConfig;
import javax.microedition.khronos.egl.EGLDisplay;
import javax.microedition.khronos.opengles.GL10;

/**
 * The 3D viewport. Rendering happens in native code on GLSurfaceView's thread;
 * frames are only requested while the image is still refining, previewing or
 * exporting, so an idle viewer costs no battery.
 */
final class SpindleView extends GLSurfaceView {
    private final ScaleGestureDetector scale;
    private final GestureDetector gestures;
    private final float density;
    private float lastX, lastY, lastFocusX, lastFocusY;
    private boolean multiTouch;

    SpindleView(Context context) {
        super(context);
        density = context.getResources().getDisplayMetrics().density;
        setEGLContextClientVersion(3);
        setEGLConfigChooser(new RecordableConfigChooser());
        setPreserveEGLContextOnPause(true);
        setRenderer(new Renderer() {
            @Override public void onSurfaceCreated(GL10 gl, EGLConfig config) { NativeBridge.onSurfaceCreated(); }
            @Override public void onSurfaceChanged(GL10 gl, int w, int h) { NativeBridge.onSurfaceChanged(w, h); }
            @Override public void onDrawFrame(GL10 gl) {
                if (NativeBridge.onDrawFrame()) requestRender();
            }
        });
        setRenderMode(RENDERMODE_WHEN_DIRTY);

        scale = new ScaleGestureDetector(context, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector d) {
                float nx = d.getFocusX() / getWidth() * 2f - 1f;
                float ny = 1f - d.getFocusY() / getHeight() * 2f;
                NativeBridge.zoom(d.getScaleFactor(), nx, ny);
                requestRender();
                return true;
            }
        });
        gestures = new GestureDetector(context, new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDoubleTap(MotionEvent e) {
                NativeBridge.frameModel();
                requestRender();
                return true;
            }
        });
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        scale.onTouchEvent(e);
        gestures.onTouchEvent(e);
        int action = e.getActionMasked();
        if (e.getPointerCount() >= 2) {
            float fx = (e.getX(0) + e.getX(1)) / 2f, fy = (e.getY(0) + e.getY(1)) / 2f;
            if (multiTouch && action == MotionEvent.ACTION_MOVE) NativeBridge.pan(fx - lastFocusX, fy - lastFocusY);
            lastFocusX = fx;
            lastFocusY = fy;
            multiTouch = true;
        } else if (action == MotionEvent.ACTION_DOWN) {
            multiTouch = false;
            lastX = e.getX();
            lastY = e.getY();
        } else if (action == MotionEvent.ACTION_MOVE && !multiTouch) {
            // Same feel as a mouse on a ~1x screen, independent of pixel density.
            NativeBridge.orbit((e.getX() - lastX) / density * 1.6f, (e.getY() - lastY) / density * 1.6f);
            lastX = e.getX();
            lastY = e.getY();
        }
        if (action == MotionEvent.ACTION_UP) multiTouch = false;
        requestRender();
        return true;
    }

    /**
     * RGBA8888, OpenGL ES 3, and EGL_RECORDABLE_ANDROID when available, so the
     * MediaCodec encoder surface can share the context for MP4 export.
     */
    private static final class RecordableConfigChooser implements EGLConfigChooser {
        private static final int EGL_RECORDABLE_ANDROID = 0x3142;
        private static final int EGL_OPENGL_ES3_BIT_KHR = 0x40;

        @Override
        public EGLConfig chooseConfig(EGL10 egl, EGLDisplay display) {
            int[][] attempts = {
                {EGL10.EGL_RED_SIZE, 8, EGL10.EGL_GREEN_SIZE, 8, EGL10.EGL_BLUE_SIZE, 8, EGL10.EGL_ALPHA_SIZE, 8,
                 EGL10.EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT_KHR, EGL_RECORDABLE_ANDROID, 1, EGL10.EGL_NONE},
                {EGL10.EGL_RED_SIZE, 8, EGL10.EGL_GREEN_SIZE, 8, EGL10.EGL_BLUE_SIZE, 8,
                 EGL10.EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT_KHR, EGL_RECORDABLE_ANDROID, 1, EGL10.EGL_NONE},
                {EGL10.EGL_RED_SIZE, 8, EGL10.EGL_GREEN_SIZE, 8, EGL10.EGL_BLUE_SIZE, 8,
                 EGL10.EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT_KHR, EGL10.EGL_NONE},
            };
            for (int[] attrs : attempts) {
                EGLConfig[] configs = new EGLConfig[1];
                int[] count = new int[1];
                if (egl.eglChooseConfig(display, attrs, configs, 1, count) && count[0] > 0) return configs[0];
            }
            throw new IllegalStateException("This device does not support OpenGL ES 3.0");
        }
    }
}
