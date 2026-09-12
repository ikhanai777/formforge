<#
.SYNOPSIS
    Registers (or unregisters) DrawingForge with SOLIDWORKS.

.DESCRIPTION
    A SOLIDWORKS add-in is a COM server. Registering it means two things:
    telling Windows the CLSID lives in this DLL, and telling SOLIDWORKS the
    CLSID is an add-in. regasm does the first by running the add-in's own
    ComRegisterFunction, which does the second.

    The machine-wide key needs administrator rights. Run this from an elevated
    PowerShell, or the registration will fail with an access error.

.PARAMETER Configuration
    Build configuration to register. Defaults to Release.

.PARAMETER Unregister
    Removes the registration instead of adding it.

.PARAMETER DllPath
    Registers a specific DLL instead of the one in the build output.

.EXAMPLE
    .\install.ps1
    Builds nothing; registers bin\Release\net48\DrawingForge.AddIn.dll.

.EXAMPLE
    .\install.ps1 -Unregister
#>

[CmdletBinding()]
param(
    [string] $Configuration = 'Release',
    [switch] $Unregister,
    [string] $DllPath
)

$ErrorActionPreference = 'Stop'

function Get-RegAsmPath {
    # The 64-bit regasm is the one that matters: SOLIDWORKS is a 64-bit host.
    $framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\RegAsm.exe'
    if (-not (Test-Path $framework)) {
        throw "RegAsm.exe not found at $framework. Install the .NET Framework 4.x developer pack."
    }
    return $framework
}

function Assert-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated PowerShell: the add-in key lives under HKEY_LOCAL_MACHINE.'
    }
}

Assert-Elevated

if (-not $DllPath) {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    $DllPath = Join-Path $root "src\DrawingForge.AddIn\bin\$Configuration\net48\DrawingForge.AddIn.dll"
}

if (-not (Test-Path $DllPath)) {
    throw "Add-in not found at $DllPath. Build the solution first, or pass -DllPath."
}

$regasm = Get-RegAsmPath
$arguments = if ($Unregister) { @('/unregister', "`"$DllPath`"") } else { @('/codebase', "`"$DllPath`"") }

Write-Host ("{0} {1}" -f $regasm, ($arguments -join ' '))
$process = Start-Process -FilePath $regasm -ArgumentList $arguments -Wait -PassThru -NoNewWindow

if ($process.ExitCode -ne 0) {
    throw "RegAsm failed with exit code $($process.ExitCode)."
}

if ($Unregister) {
    Write-Host 'DrawingForge unregistered.' -ForegroundColor Green
} else {
    Write-Host 'DrawingForge registered.' -ForegroundColor Green
    Write-Host 'Start SOLIDWORKS and switch it on under Tools > Add-Ins if it is not already there.'
}
