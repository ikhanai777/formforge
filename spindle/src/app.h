// The interactive viewer window.
#pragma once

#include <string>
#include <vector>

namespace spindle {

// Opens the main window; args may contain a file to open. Returns the exit code.
int runGui(const std::vector<std::string>& args);

}  // namespace spindle
