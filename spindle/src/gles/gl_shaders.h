// GLSL ES 3.00 sources for the OpenGL ES renderer (a port of shaders.hlsl).
#pragma once

#include <string>

namespace spindle {

enum class GlVertexShader { Mesh, Shadow, Ground, Fullscreen };
enum class GlFragmentShader {
    Empty, Model, Ground, Ssao, AoBlur, TonemapAccumulate, Resolve, Blit, EquirectToCube, Prefilter, BrdfLut
};

std::string glslVertex(GlVertexShader v);
std::string glslFragment(GlFragmentShader f);

}  // namespace spindle
