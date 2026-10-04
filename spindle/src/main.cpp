// Entry point for both Spindle.exe (GUI subsystem) and Spindle.com (console
// subsystem). Headless commands run without a window; anything else opens the viewer.
#include "app.h"
#include "cli.h"
#include "common.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shellapi.h>

#include <cstdio>

using namespace spindle;

static std::vector<std::string> commandLineArgs() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) args.push_back(narrow(argv[i]));
    LocalFree(argv);
    return args;
}

static int run() {
    SetConsoleOutputCP(CP_UTF8);
    std::vector<std::string> args = commandLineArgs();
    logInit(appDataDir() + "\\log.txt");
    logf("Spindle %s", SPINDLE_VERSION);
    if (isCliInvocation(args)) {
#ifndef SPINDLE_CONSOLE
        // Started from a console as Spindle.exe: borrow the parent's console for output.
        if (AttachConsole(ATTACH_PARENT_PROCESS)) {
            freopen("CONOUT$", "w", stdout);
            freopen("CONOUT$", "w", stderr);
        }
#endif
        int rc = runCli(args);
        std::fflush(stdout);
        return rc;
    }
    return runGui(args);
}

#ifdef SPINDLE_CONSOLE
int wmain() { return run(); }
#else
int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) { return run(); }
#endif
