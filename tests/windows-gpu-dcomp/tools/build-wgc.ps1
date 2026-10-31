# Build wgc-capture.exe (trusted Windows Graphics Capture helper).
#
# Requires Visual Studio Build Tools with the C++ x64 toolchain and a Windows
# SDK that ships C++/WinRT headers (Windows.Graphics.Capture). No NuGet
# packages, no network.

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $here "wgc-capture.cpp"
$exe = Join-Path $here "wgc-capture.exe"

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
  throw "vswhere.exe not found; install Visual Studio Build Tools (C++ x64)"
}
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "Visual Studio C++ toolchain not found" }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found: $vcvars" }

$sdkRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Include"
$sdk = Get-ChildItem $sdkRoot -Directory |
  Sort-Object Name -Descending |
  Where-Object { Test-Path (Join-Path $_.FullName "cppwinrt\winrt\Windows.Graphics.Capture.h") } |
  Select-Object -First 1
if (-not $sdk) { throw "Windows SDK with C++/WinRT headers not found under $sdkRoot" }

$bat = Join-Path $env:TEMP ("build-wgc-" + [guid]::NewGuid().ToString("N") + ".bat")
$lines = @(
  "@echo off",
  "call `"$vcvars`" >nul 2>&1",
  "if errorlevel 1 exit /b 1",
  "cd /d `"$here`"",
  "cl /nologo /std:c++20 /EHsc /O2 /MD /DUNICODE /D_UNICODE /I `"$($sdk.FullName)\cppwinrt`" wgc-capture.cpp /Fe:wgc-capture.exe /link windowsapp.lib user32.lib gdi32.lib ole32.lib d3d11.lib dxgi.lib dwmapi.lib windowscodecs.lib"
)
Set-Content -Path $bat -Value $lines -Encoding ASCII
try {
  & cmd.exe /c $bat
  $code = $LASTEXITCODE
} finally {
  Remove-Item -Force $bat -ErrorAction SilentlyContinue
  Remove-Item -Force (Join-Path $here "wgc-capture.obj") -ErrorAction SilentlyContinue
}
if ($code -ne 0) { throw "cl failed with exit code $code" }
if (-not (Test-Path $exe)) { throw "build did not produce $exe" }
Write-Output "built $exe"
