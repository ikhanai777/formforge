// JNI entry points for com.spindle.app.NativeBridge. All state lives in one
// ViewerCore; the GL methods are called from GLSurfaceView's render thread,
// everything else from the UI thread.
#include "mp4_sink.h"
#include "viewer_core.h"

#include <jni.h>

#include "../../../../../third_party/nlohmann/json.hpp"

using namespace spindle;

namespace {

ViewerCore* core() {
    static ViewerCore* instance = [] {
        auto* c = new ViewerCore();
        c->setVideoSinkFactory([] { return makeMp4SurfaceSink(); });
        return c;
    }();
    return instance;
}

std::string str(JNIEnv* env, jstring s) {
    if (!s) return {};
    const char* c = env->GetStringUTFChars(s, nullptr);
    std::string out(c ? c : "");
    if (c) env->ReleaseStringUTFChars(s, c);
    return out;
}

jstring jstr(JNIEnv* env, const std::string& s) { return env->NewStringUTF(s.c_str()); }

}  // namespace

#define FN(ret, name) extern "C" JNIEXPORT ret JNICALL Java_com_spindle_app_NativeBridge_##name

FN(void, init)(JNIEnv* env, jclass, jstring logPath) {
    logInit(str(env, logPath));
    core();
}

FN(void, onSurfaceCreated)(JNIEnv*, jclass) { core()->onSurfaceCreated(); }
FN(void, onSurfaceChanged)(JNIEnv*, jclass, jint w, jint h) { core()->onSurfaceChanged(w, h); }
FN(jboolean, onDrawFrame)(JNIEnv*, jclass) { return core()->onDrawFrame() ? JNI_TRUE : JNI_FALSE; }

FN(jstring, getSceneJson)(JNIEnv* env, jclass) { return jstr(env, core()->sceneJson()); }
FN(void, setSceneJson)(JNIEnv* env, jclass, jstring json) { core()->setSceneJson(str(env, json)); }
FN(jstring, applyMaterialPreset)(JNIEnv* env, jclass, jstring name) {
    return jstr(env, core()->applyMaterialPreset(str(env, name)));
}

FN(jstring, listJson)(JNIEnv* env, jclass, jstring what) {
    std::string w = str(env, what);
    nlohmann::json j = nlohmann::json::array();
    if (w == "materials")
        for (auto& n : materialPresetNames()) j.push_back(n);
    else if (w == "environments")
        for (auto& n : proceduralEnvironmentNames()) j.push_back(n);
    return jstr(env, j.dump());
}

FN(void, loadModel)(JNIEnv* env, jclass, jstring path, jstring name) { core()->loadModel(str(env, path), str(env, name)); }
FN(void, orbit)(JNIEnv*, jclass, jfloat dx, jfloat dy) { core()->orbit(dx, dy); }
FN(void, zoom)(JNIEnv*, jclass, jfloat f, jfloat x, jfloat y) { core()->zoom(f, x, y); }
FN(void, pan)(JNIEnv*, jclass, jfloat dx, jfloat dy) { core()->pan(dx, dy); }
FN(void, frameModel)(JNIEnv*, jclass) { core()->frameModel(); }
FN(void, viewPreset)(JNIEnv*, jclass, jint which) { core()->viewPreset(which); }
FN(void, setPreview)(JNIEnv*, jclass, jboolean on) { core()->setPreview(on == JNI_TRUE); }

FN(jstring, startExport)(JNIEnv* env, jclass, jint kind, jstring path) {
    return jstr(env, core()->startExport((ExportKind)kind, str(env, path)));
}
FN(void, cancelExport)(JNIEnv*, jclass) { core()->cancelExport(); }
FN(jstring, statusJson)(JNIEnv* env, jclass) { return jstr(env, core()->statusJson()); }
