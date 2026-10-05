# Native library for the Android app: the platform-neutral core shared with the
# Windows build (../../../../../src), the OpenGL ES renderer, and the
# Android-only JNI bridge and MediaCodec MP4 encoder.
cmake_minimum_required(VERSION 3.22.1)
project(spindle_android CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
get_filename_component(SPINDLE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../.." ABSOLUTE)

add_library(spindle SHARED
  jni_bridge.cpp
  viewer_core.cpp
  mp4_sink.cpp
  ${SPINDLE_ROOT}/src/common.cpp
  ${SPINDLE_ROOT}/src/mesh.cpp
  ${SPINDLE_ROOT}/src/model3mf.cpp
  ${SPINDLE_ROOT}/src/scene.cpp
  ${SPINDLE_ROOT}/src/camera.cpp
  ${SPINDLE_ROOT}/src/sampling.cpp
  ${SPINDLE_ROOT}/src/frame_plan.cpp
  ${SPINDLE_ROOT}/src/environment.cpp
  ${SPINDLE_ROOT}/src/gles/gl_renderer.cpp
  ${SPINDLE_ROOT}/src/gles/gl_shaders.cpp
  ${SPINDLE_ROOT}/src/gles/gl_exporter.cpp)
target_include_directories(spindle PRIVATE ${SPINDLE_ROOT}/src ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(spindle PRIVATE -O2 -Wall -Wno-unused-parameter)
target_link_libraries(spindle GLESv3 EGL mediandk android log)
