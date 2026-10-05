// MP4 (H.264) export on Android: frames are drawn straight into MediaCodec's
// input surface from the renderer's own GL context, then muxed with MediaMuxer.
// No pixel readback and no colour-format guessing, so it works with every
// hardware or software AVC encoder that accepts surface input (API 26+).
#include "mp4_sink.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <android/native_window.h>
#include <fcntl.h>
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaFormat.h>
#include <media/NdkMediaMuxer.h>
#include <unistd.h>

#include <cstdio>

namespace spindle {

namespace {

constexpr int32_t kColorFormatSurface = 0x7F000789;  // MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface

typedef EGLBoolean (*PresentationTimeFn)(EGLDisplay, EGLSurface, EGLnsecsANDROID);

class Mp4SurfaceSink : public GlFrameSink {
public:
    ~Mp4SurfaceSink() override { abort(); }

    bool begin(const GlSinkConfig& cfg, std::string& error) override {
        cfg_ = cfg;
        temp_ = cfg.path + ".partial";
        float mbps = cfg.bitrateMbps > 0 ? cfg.bitrateMbps : (float)std::max(2.0, 0.2 * cfg.width * cfg.height * cfg.fps / 1e6);

        AMediaFormat* fmt = AMediaFormat_new();
        AMediaFormat_setString(fmt, AMEDIAFORMAT_KEY_MIME, "video/avc");
        AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_WIDTH, cfg.width);
        AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_HEIGHT, cfg.height);
        AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_BIT_RATE, (int32_t)(mbps * 1e6f));
        AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_FRAME_RATE, cfg.fps);
        AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_I_FRAME_INTERVAL, 1);
        AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_COLOR_FORMAT, kColorFormatSurface);
        codec_ = AMediaCodec_createEncoderByType("video/avc");
        if (!codec_) {
            AMediaFormat_delete(fmt);
            error = "This device has no H.264 encoder. Export a GIF instead.";
            return false;
        }
        media_status_t st = AMediaCodec_configure(codec_, fmt, nullptr, nullptr, AMEDIACODEC_CONFIGURE_FLAG_ENCODE);
        AMediaFormat_delete(fmt);
        if (st != AMEDIA_OK) {
            error = "The H.264 encoder does not accept " + std::to_string(cfg.width) + "x" + std::to_string(cfg.height) +
                    ". Try 1280x720 or 1920x1080.";
            return false;
        }
        if (AMediaCodec_createInputSurface(codec_, &window_) != AMEDIA_OK || !window_) {
            error = "Could not create the encoder's input surface.";
            return false;
        }
        if (AMediaCodec_start(codec_) != AMEDIA_OK) {
            error = "Could not start the H.264 encoder.";
            return false;
        }
        started_ = true;

        fd_ = open(temp_.c_str(), O_CREAT | O_TRUNC | O_RDWR, 0644);
        if (fd_ < 0) {
            error = "Could not create the video file.";
            return false;
        }
        muxer_ = AMediaMuxer_new(fd_, AMEDIAMUXER_OUTPUT_FORMAT_MPEG_4);
        if (!muxer_) {
            error = "Could not create the MP4 muxer.";
            return false;
        }

        // An EGL surface on the encoder's window, compatible with the current context.
        dpy_ = eglGetCurrentDisplay();
        ctx_ = eglGetCurrentContext();
        EGLint configId = 0;
        eglQueryContext(dpy_, ctx_, EGL_CONFIG_ID, &configId);
        const EGLint attrs[] = {EGL_CONFIG_ID, configId, EGL_NONE};
        EGLConfig config;
        EGLint n = 0;
        if (!eglChooseConfig(dpy_, attrs, &config, 1, &n) || n == 0) {
            error = "Could not find the GL configuration for video encoding.";
            return false;
        }
        const EGLint surfaceAttrs[] = {EGL_NONE};
        surface_ = eglCreateWindowSurface(dpy_, config, window_, surfaceAttrs);
        if (surface_ == EGL_NO_SURFACE) {
            error = "This device cannot encode video from OpenGL (EGL error " + std::to_string(eglGetError()) +
                    "). Export a GIF instead.";
            return false;
        }
        presentationTime_ = (PresentationTimeFn)eglGetProcAddress("eglPresentationTimeANDROID");
        return true;
    }

    bool writeFrame(GlRenderer& renderer, GlTargets& rt, int index, std::string& error) override {
        EGLSurface draw = eglGetCurrentSurface(EGL_DRAW), read = eglGetCurrentSurface(EGL_READ);
        if (!eglMakeCurrent(dpy_, surface_, surface_, ctx_)) {
            error = "Could not switch to the encoder surface.";
            return false;
        }
        renderer.blit(rt, 0, 0, 0, cfg_.width, cfg_.height, false);
        if (presentationTime_) presentationTime_(dpy_, surface_, (EGLnsecsANDROID)((int64_t)index * 1000000000LL / cfg_.fps));
        eglSwapBuffers(dpy_, surface_);
        eglMakeCurrent(dpy_, draw, read, ctx_);
        return drain(false, error);
    }

    bool finish(std::string& error) override {
        if (!codec_) {
            error = "The video was not started.";
            return false;
        }
        AMediaCodec_signalEndOfInputStream(codec_);
        bool ok = drain(true, error);
        cleanup();
        if (!ok || !muxerStarted_) {
            if (error.empty()) error = "The encoder produced no video.";
            std::remove(temp_.c_str());
            return false;
        }
        if (std::rename(temp_.c_str(), cfg_.path.c_str()) != 0) {
            error = "Could not save the video.";
            return false;
        }
        return true;
    }

    void abort() override {
        if (!codec_ && fd_ < 0) return;
        cleanup();
        std::remove(temp_.c_str());
    }

private:
    bool drain(bool endOfStream, std::string& error) {
        int idle = 0;
        for (;;) {
            AMediaCodecBufferInfo info;
            ssize_t idx = AMediaCodec_dequeueOutputBuffer(codec_, &info, endOfStream ? 10000 : 0);
            if (idx == AMEDIACODEC_INFO_TRY_AGAIN_LATER) {
                if (!endOfStream) return true;
                if (++idle > 500) {  // ~5 s without output
                    error = "The video encoder stopped responding.";
                    return false;
                }
                continue;
            }
            if (idx == AMEDIACODEC_INFO_OUTPUT_FORMAT_CHANGED) {
                AMediaFormat* fmt = AMediaCodec_getOutputFormat(codec_);
                track_ = AMediaMuxer_addTrack(muxer_, fmt);
                AMediaFormat_delete(fmt);
                if (track_ < 0 || AMediaMuxer_start(muxer_) != AMEDIA_OK) {
                    error = "Could not start writing the MP4.";
                    return false;
                }
                muxerStarted_ = true;
                continue;
            }
            if (idx == AMEDIACODEC_INFO_OUTPUT_BUFFERS_CHANGED) continue;
            if (idx < 0) continue;
            size_t size = 0;
            uint8_t* data = AMediaCodec_getOutputBuffer(codec_, (size_t)idx, &size);
            if (info.flags & AMEDIACODEC_BUFFER_FLAG_CODEC_CONFIG) info.size = 0;  // already in the track format
            if (info.size > 0 && muxerStarted_ && data)
                AMediaMuxer_writeSampleData(muxer_, (size_t)track_, data, &info);
            AMediaCodec_releaseOutputBuffer(codec_, (size_t)idx, false);
            if (info.flags & AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM) return true;
        }
    }

    void cleanup() {
        if (codec_) {
            if (started_) AMediaCodec_stop(codec_);
            AMediaCodec_delete(codec_);
            codec_ = nullptr;
        }
        if (muxer_) {
            if (muxerStarted_) AMediaMuxer_stop(muxer_);
            AMediaMuxer_delete(muxer_);
            muxer_ = nullptr;
        }
        if (fd_ >= 0) {
            close(fd_);
            fd_ = -1;
        }
        if (surface_ != EGL_NO_SURFACE) {
            eglDestroySurface(dpy_, surface_);
            surface_ = EGL_NO_SURFACE;
        }
        if (window_) {
            ANativeWindow_release(window_);
            window_ = nullptr;
        }
        started_ = false;
    }

    GlSinkConfig cfg_;
    std::string temp_;
    AMediaCodec* codec_ = nullptr;
    AMediaMuxer* muxer_ = nullptr;
    ANativeWindow* window_ = nullptr;
    int fd_ = -1;
    ssize_t track_ = -1;
    bool started_ = false, muxerStarted_ = false;
    EGLDisplay dpy_ = EGL_NO_DISPLAY;
    EGLContext ctx_ = EGL_NO_CONTEXT;
    EGLSurface surface_ = EGL_NO_SURFACE;
    PresentationTimeFn presentationTime_ = nullptr;
};

}  // namespace

std::unique_ptr<GlFrameSink> makeMp4SurfaceSink() { return std::unique_ptr<GlFrameSink>(new Mp4SurfaceSink()); }

}  // namespace spindle
