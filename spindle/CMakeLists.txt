cmake_minimum_required(VERSION 3.20)
project(Spindle VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()

# ---- platform-neutral core (also unit-tested on Linux) ------------------------
add_library(spindle_core STATIC
  src/common.cpp
  src/mesh.cpp
  src/model3mf.cpp
  src/scene.cpp
  src/camera.cpp
  src/sampling.cpp
  src/frame_plan.cpp
  src/environment.cpp)
target_include_directories(spindle_core PUBLIC src)
find_package(Threads REQUIRED)
target_link_libraries(spindle_core PUBLIC Threads::Threads)

if(MSVC)
  # Static CRT: the exe runs without the VC++ redistributable.
  set_property(TARGET spindle_core PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
  target_compile_options(spindle_core PUBLIC /W3 /utf-8 /MP)
  target_compile_definitions(spindle_core PUBLIC _CRT_SECURE_NO_WARNINGS UNICODE _UNICODE)
endif()

enable_testing()
add_executable(spindle_tests tests/test_core.cpp)
target_link_libraries(spindle_tests PRIVATE spindle_core)
if(MSVC)
  set_property(TARGET spindle_tests PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
elseif(MINGW)
  target_link_options(spindle_tests PRIVATE -static -static-libgcc -static-libstdc++)
endif()
if(NOT CMAKE_CROSSCOMPILING)
  add_test(NAME core COMMAND spindle_tests)
endif()

if(NOT WIN32)
  message(STATUS "Not targeting Windows: building the core library and tests only.")
  return()
endif()

# ---- shaders embedded into the executable -------------------------------------
set(SHADER_CPP ${CMAKE_CURRENT_BINARY_DIR}/shaders_embedded.cpp)
add_custom_command(
  OUTPUT ${SHADER_CPP}
  COMMAND ${CMAKE_COMMAND} -DINPUT=${CMAKE_CURRENT_SOURCE_DIR}/src/shaders.hlsl -DOUTPUT=${SHADER_CPP}
          -DSYMBOL=kShaderSource -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/embed.cmake
  DEPENDS src/shaders.hlsl cmake/embed.cmake
  COMMENT "Embedding shaders.hlsl")

# ---- Dear ImGui ---------------------------------------------------------------
add_library(imgui STATIC
  third_party/imgui/imgui.cpp
  third_party/imgui/imgui_draw.cpp
  third_party/imgui/imgui_tables.cpp
  third_party/imgui/imgui_widgets.cpp
  third_party/imgui/backends/imgui_impl_win32.cpp
  third_party/imgui/backends/imgui_impl_dx11.cpp)
target_include_directories(imgui PUBLIC third_party/imgui third_party/imgui/backends)
target_compile_definitions(imgui PUBLIC IMGUI_DISABLE_OBSOLETE_FUNCTIONS UNICODE _UNICODE)
# The DX11 backend compiles its two small shaders with D3DCompile.
if(MSVC)
  target_link_libraries(imgui PUBLIC d3dcompiler)
else()
  target_link_libraries(imgui PUBLIC d3dcompiler_47)
endif()
if(MSVC)
  set_property(TARGET imgui PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# ---- application ----------------------------------------------------------------
add_library(spindle_app STATIC
  src/gpu.cpp
  src/renderer.cpp
  src/encoders.cpp
  src/exporter.cpp
  src/cli.cpp
  src/app.cpp
  src/shaders.hlsl
  ${SHADER_CPP})
set_source_files_properties(src/shaders.hlsl PROPERTIES HEADER_FILE_ONLY TRUE)
target_link_libraries(spindle_app PUBLIC spindle_core imgui
  d3d11 dxgi dwmapi mfplat mfreadwrite mfuuid ole32 shell32 comdlg32 uuid)
target_compile_definitions(spindle_app PUBLIC SPINDLE_VERSION="${PROJECT_VERSION}")
if(MSVC)
  set_property(TARGET spindle_app PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# Spindle.exe (GUI subsystem) and Spindle.com (console subsystem, same code).
# From a command prompt `Spindle render ...` resolves to the .com first, so the
# shell waits for it and sees its output; double-clicking opens the .exe.
set(SPINDLE_MANIFEST ${CMAKE_CURRENT_SOURCE_DIR}/src/app.manifest)
set(SPINDLE_ICON ${CMAKE_CURRENT_SOURCE_DIR}/src/spindle.ico)
configure_file(src/app.rc.in ${CMAKE_CURRENT_BINARY_DIR}/app.rc @ONLY)
set(APP_RC ${CMAKE_CURRENT_BINARY_DIR}/app.rc)
set_source_files_properties(${APP_RC} PROPERTIES OBJECT_DEPENDS "${SPINDLE_MANIFEST};${SPINDLE_ICON}")
enable_language(RC)
add_executable(Spindle WIN32 src/main.cpp ${APP_RC})
add_executable(SpindleConsole src/main.cpp ${APP_RC})
target_compile_definitions(SpindleConsole PRIVATE SPINDLE_CONSOLE)
set_target_properties(SpindleConsole PROPERTIES OUTPUT_NAME Spindle SUFFIX ".com"
  ARCHIVE_OUTPUT_NAME SpindleConsole PDB_NAME SpindleConsole)
foreach(t Spindle SpindleConsole)
  target_link_libraries(${t} PRIVATE spindle_app)
  if(MSVC)
    set_property(TARGET ${t} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    target_link_options(${t} PRIVATE /MANIFEST:NO)  # the manifest comes from app.rc
  else()
    target_link_options(${t} PRIVATE -municode -static -static-libgcc -static-libstdc++)
  endif()
endforeach()

install(TARGETS Spindle SpindleConsole RUNTIME DESTINATION .)
install(FILES README.md DESTINATION .)
install(DIRECTORY presets DESTINATION .)
