# Run the GPU-B DComp A/B matrix against an isolated profile per variant.
#
# Every variant gets its own --user-data-dir under -OutRoot. The harness never
# uses --no-sandbox and never touches a real user profile. The test window is
# shown with SWP_NOACTIVATE; Win32 focus is not stolen. Captures use Windows
# Graphics Capture (DWM-backed) so occlusion does not invalidate the result.
#
# Example:
#   powershell -ExecutionPolicy Bypass -File run-ab.ps1 `
#     -Bcli D:/c/src/out/WinDev/chrome.exe `
#     -Official "C:/Program Files/Google/Chrome/Application/chrome.exe" `
#     -OutRoot D:/c/logs/gpu-b2

param(
  [string]$Bcli = "D:/c/src/out/WinDev/chrome.exe",
  [string]$Official = "C:/Program Files/Google/Chrome/Application/chrome.exe",
  [string]$OutRoot = "D:/c/logs/gpu-b2",
  [string[]]$Variants = @(
    "bcli-dcomp",
    "bcli-nodcomp",
    "bcli-inproc-dawn-d3d12",
    "official-default",
    "official-inproc-dcomp",
    "official-inproc-nodcomp",
    "official-inproc-dawn-d3d12",
    "official-inproc-software"
  ),
  [switch]$Analyze
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$node = Get-Command node -ErrorAction Stop
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null

$roots = @()
foreach ($variant in $Variants) {
  $chrome = if ($variant -like "bcli*") { $Bcli } else { $Official }
  if (-not (Test-Path $chrome)) {
    Write-Warning "skip ${variant}: chrome not found at $chrome"
    continue
  }
  $root = Join-Path $OutRoot "$stamp-$variant"
  New-Item -ItemType Directory -Force -Path $root | Out-Null
  Write-Output "=== $variant -> $root"
  & $node.Source (Join-Path $here "gpu-dcomp-ab.mjs") --variant $variant --chrome $chrome --root $root
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "$variant exited $LASTEXITCODE (see $root/summary.json)"
  }
  $roots += $root
}

if ($Analyze -and $roots.Count -gt 0) {
  $out = Join-Path $OutRoot "$stamp-evidence-all.json"
  & python (Join-Path $here "analyze-runs.py") --out $out @roots
  Write-Output "evidence: $out"
}
