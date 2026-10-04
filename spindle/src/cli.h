// Headless command line: `Spindle render`, `Spindle still`, `--list-gpus`.
#pragma once

#include <string>
#include <vector>

namespace spindle {

// True if the arguments ask for a headless command rather than the GUI.
bool isCliInvocation(const std::vector<std::string>& args);
// Runs the command. Exit codes: 0 ok, 1 usage, 2 load error, 3 GPU error, 4 encode error.
int runCli(const std::vector<std::string>& args);

// Shared by the GUI: per-user folders (created on demand).
std::string appDataDir();       // %APPDATA%\Spindle
std::string localCacheDir();    // %LOCALAPPDATA%\Spindle\shadercache
std::string exeDir();

}  // namespace spindle
