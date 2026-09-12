#!/usr/bin/env bash
# Everything that can be verified without SOLIDWORKS installed.
#
#   * the planning library builds
#   * its test suite passes
#   * the add-in's own source compiles against the stubbed SOLIDWORKS interfaces
#
# What this does NOT check: that the SOLIDWORKS API calls match a real release.
# That needs a workstation. See docs/api-notes.md.

set -euo pipefail

cd "$(dirname "$0")"

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

echo "==> building the planning library"
dotnet build src/DrawingForge.Core/DrawingForge.Core.csproj -v q

echo "==> running the planning tests"
dotnet test tests/DrawingForge.Core.Tests/DrawingForge.Core.Tests.csproj -v q

echo "==> compile-checking the add-in against stubbed SOLIDWORKS interfaces"
dotnet build tests/DrawingForge.AddIn.CompileCheck/DrawingForge.AddIn.CompileCheck.csproj -v q

echo
echo "All checks passed."
echo "The add-in itself still has to be built on a workstation with SOLIDWORKS:"
echo "  msbuild DrawingForge.sln /p:Configuration=Release"
