// Android MediaCodec + MediaMuxer MP4 sink for GlExporter.
#pragma once

#include "gles/gl_exporter.h"

#include <memory>

namespace spindle {

std::unique_ptr<GlFrameSink> makeMp4SurfaceSink();

}  // namespace spindle
