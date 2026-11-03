<#
.SYNOPSIS
  Read-only packaging/signing/update preflight for a Windows Browser CLI
  artifact directory (normally <checkout>/out/WinRelease).

.DESCRIPTION
  This script NEVER signs, NEVER imports/exports certificates or private keys,
  NEVER contacts a timestamp server, NEVER starts a browser and NEVER modifies
  the artifact. It only reads files and Authenticode status.

  It distinguishes two artifact classes explicitly:

    internal-unsigned-test-build
        Allowed to be unsigned. The script records every finding and exits 0
        with RELEASABLE=false and the exact release blockers. This is the only
        class this repository can produce today.

    public-signed-artifact
        Requires a valid Authenticode signature on chrome.exe and chrome.dll
        from the configured publisher, a matching version resource, a complete
        SHA-256 manifest, no private key material, and an update/package
        metadata contract. Any missing item is a hard failure (exit 1).

  Exit codes: 0 = the requested class was validated; 1 = fail closed; 2 = bad
  invocation. A "public" run can never pass without the signing identity and
  update contract being configured in advance.

.PARAMETER Artifact
  Directory containing chrome.exe (out/WinRelease or a staged package).

.PARAMETER Mode
  internal (default) or public.

.PARAMETER ExpectedProductName
  Expected ProductName version resource. Default: Browser CLI.

.PARAMETER ExpectedPublisher
  Expected Authenticode subject substring for public mode. Empty means the
  Windows signing identity is not configured -> public mode fails closed.

.PARAMETER ExpectedVersion
  Expected product version (default: read from the repo's chromium/VERSION).

.PARAMETER OutJson
  Write the full report JSON here.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Artifact,
  [ValidateSet('internal', 'public')][string]$Mode = 'internal',
  [string]$ExpectedProductName = 'Browser CLI',
  [string]$ExpectedPublisher = '',
  [string]$ExpectedVersion = '',
  [string]$OutJson = ''
)

$ErrorActionPreference = 'Stop'
$checks = New-Object System.Collections.ArrayList
function Add-Check([string]$Name, [string]$Status, $Detail) {
  [void]$checks.Add([ordered]@{ name = $Name; status = $Status; detail = $Detail })
  return ($Status -eq 'PASS')
}
function Fail($msg) { throw "$msg" }

if (-not (Test-Path -LiteralPath $Artifact -PathType Container)) { Fail "artifact directory not found: $Artifact" }
$artifactFull = [System.IO.Path]::GetFullPath($Artifact)
$exe = Join-Path $artifactFull 'chrome.exe'
$dll = Join-Path $artifactFull 'chrome.dll'
if (-not (Test-Path -LiteralPath $exe)) { Fail "missing $exe" }

$report = [ordered]@{
  generatedAt = (Get-Date).ToString('o')
  artifact = $artifactFull
  mode = $Mode
  expectedProductName = $ExpectedProductName
  expectedPublisher = $ExpectedPublisher
  expectedVersion = $ExpectedVersion
  artifactClass = $null
  releasable = $false
  signingPerformed = $false
  privateKeyMaterialFound = @()
  files = @()
  checks = @()
  releaseBlockers = @()
}

# ---- 1. build shape -------------------------------------------------------
$argsGn = Join-Path $artifactFull 'args.gn'
$component = $null
if (Test-Path -LiteralPath $argsGn) {
  $gn = Get-Content -LiteralPath $argsGn -Raw
  if ($gn -match '(?m)^\s*is_component_build\s*=\s*true\s*$') { $component = $true }
  elseif ($gn -match '(?m)^\s*is_component_build\s*=\s*false\s*$') { $component = $false }
  [void](Add-Check 'args.gn present' 'PASS' $argsGn)
  if ($gn -match '(?m)^\s*disable_fieldtrial_testing_config\s*=\s*true\s*$') {
    [void](Add-Check 'field-trial test config disabled' 'PASS' 'disable_fieldtrial_testing_config = true')
  } else {
    [void](Add-Check 'field-trial test config disabled' 'FAIL' 'disable_fieldtrial_testing_config=true is required for the product build (Sogou TSF)')
  }
  if ($gn -match '(?m)^\s*is_official_build\s*=\s*true\s*$') {
    [void](Add-Check 'official build' 'PASS' 'is_official_build = true')
  } else {
    [void](Add-Check 'official build' 'WARN' 'is_official_build is not true: no PGO/ThinLTO; this cannot be a public artifact')
  }
} else {
  [void](Add-Check 'args.gn present' 'WARN' 'no args.gn in the artifact directory (staged package?), component/non-component must be established from the build record')
}
if ($component -eq $true) {
  [void](Add-Check 'non-component build shape' 'FAIL' 'is_component_build=true (pre-start CIG is compiled out; not a release shape)')
} elseif ($component -eq $false) {
  [void](Add-Check 'non-component build shape' 'PASS' 'is_component_build=false')
} else {
  [void](Add-Check 'non-component build shape' 'WARN' 'unknown (no args.gn)')
}

# ---- 2. version resources -------------------------------------------------
$vi = (Get-Item -LiteralPath $exe).VersionInfo
$versionDetail = [ordered]@{
  productName = $vi.ProductName
  fileDescription = $vi.FileDescription
  companyName = $vi.CompanyName
  fileVersion = $vi.FileVersion
  productVersion = $vi.ProductVersion
}
$versionOk = $true
if ($ExpectedProductName -and $vi.ProductName -ne $ExpectedProductName) { $versionOk = $false }
if ($ExpectedVersion) {
  # Chromium's FileVersion has 4 parts (156.0.8067.0); ProductVersion may carry
  # a suffix. Compare the leading version-shaped token.
  $m = [regex]::Match([string]$vi.FileVersion, '\d+(\.\d+){1,3}')
  if (-not $m.Success -or $m.Value -ne $ExpectedVersion) { $versionOk = $false }
}
[void](Add-Check 'version resources' $(if ($versionOk) { 'PASS' } else { 'FAIL' }) $versionDetail)
$report.versionInfo = $versionDetail

# ---- 3. Authenticode ------------------------------------------------------
$exeSig = Get-AuthenticodeSignature -LiteralPath $exe
$report.signature = [ordered]@{
  exeStatus = [string]$exeSig.Status
  exeSubject = $(if ($exeSig.SignerCertificate) { $exeSig.SignerCertificate.Subject } else { $null })
  exeThumbprint = $(if ($exeSig.SignerCertificate) { $exeSig.SignerCertificate.Thumbprint } else { $null })
}
if (Test-Path -LiteralPath $dll) {
  $dllSig = Get-AuthenticodeSignature -LiteralPath $dll
  $report.signature.dllStatus = [string]$dllSig.Status
  $report.signature.dllSubject = $(if ($dllSig.SignerCertificate) { $dllSig.SignerCertificate.Subject } else { $null })
}
$signedOk = $false
if ($Mode -eq 'public') {
  if (-not $ExpectedPublisher) {
    [void](Add-Check 'Authenticode publisher configured' 'FAIL' 'no expected Windows publisher is configured; do not sign and do not claim a public artifact')
  } elseif ([string]$exeSig.Status -ne 'Valid') {
    [void](Add-Check 'chrome.exe Authenticode' 'FAIL' "status=$($exeSig.Status)")
  } elseif (-not ([string]$exeSig.SignerCertificate.Subject).Contains($ExpectedPublisher)) {
    [void](Add-Check 'chrome.exe Authenticode' 'FAIL' "signer does not contain '$ExpectedPublisher': $($exeSig.SignerCertificate.Subject)")
  } else {
    $signedOk = $true
    [void](Add-Check 'chrome.exe Authenticode' 'PASS' "Valid / $($exeSig.SignerCertificate.Subject)")
  }
  if (Test-Path -LiteralPath $dll) {
    if ([string]$dllSig.Status -ne 'Valid') {
      [void](Add-Check 'chrome.dll Authenticode' 'FAIL' "status=$($dllSig.Status)")
      $signedOk = $false
    } else {
      [void](Add-Check 'chrome.dll Authenticode' 'PASS' [string]$dllSig.Status)
    }
  }
} else {
  switch ([string]$exeSig.Status) {
    'NotSigned' { [void](Add-Check 'chrome.exe Authenticode' 'PASS' 'NotSigned (internal unsigned test build)') }
    'Valid'     { [void](Add-Check 'chrome.exe Authenticode' 'PASS' "Valid / $($exeSig.SignerCertificate.Subject) (internal run, already signed outside this preflight)") }
    default     { [void](Add-Check 'chrome.exe Authenticode' 'WARN' "status=$($exeSig.Status)") }
  }
}

# ---- 4. private key / certificate material --------------------------------
$keyPatterns = @('*.pfx', '*.p12', '*.pem', '*.key', '*.pvk', '*.spc', '*.cer', '*.crt')
$found = @()
foreach ($pat in $keyPatterns) {
  $found += @(Get-ChildItem -LiteralPath $artifactFull -Filter $pat -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
}
$report.privateKeyMaterialFound = $found
[void](Add-Check 'no private key material in artifact' $(if ($found.Count -eq 0) { 'PASS' } else { 'FAIL' }) $found)

# ---- 5. update/packaging contract ----------------------------------------
$updaterNames = @('chrome_updater.exe', 'updater.exe', 'setup.exe', 'chrome_installer.exe')
$updaters = @()
foreach ($n in $updaterNames) {
  $p = Join-Path $artifactFull $n
  if (Test-Path -LiteralPath $p) { $updaters += $p }
}
$report.updaterArtifacts = $updaters
$updateMeta = Join-Path $artifactFull 'update-metadata.json'
if ($Mode -eq 'public') {
  if (-not (Test-Path -LiteralPath $updateMeta)) {
    [void](Add-Check 'update metadata contract' 'FAIL' 'public mode needs update-metadata.json (channel/version/sha256/rollback); no updater contract exists yet')
  } else {
    [void](Add-Check 'update metadata contract' 'PASS' $updateMeta)
  }
} else {
  [void](Add-Check 'update metadata contract' 'WARN' 'none (internal test build has no updater; documented as a release blocker)')
}
if ($updaters.Count -gt 0) {
  [void](Add-Check 'no unverified updater binaries' 'WARN' $updaters)
}

# ---- 6. hash manifest -----------------------------------------------------
$manifest = @()
$binaries = @(Get-ChildItem -LiteralPath $artifactFull -File -ErrorAction SilentlyContinue |
  Where-Object { $_.Extension -in @('.exe', '.dll') })
foreach ($f in $binaries) {
  $h = Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256
  $manifest += [ordered]@{ file = $f.Name; bytes = $f.Length; sha256 = $h.Hash }
}
$report.files = $manifest
$report.fileCount = $manifest.Count
[void](Add-Check 'SHA-256 manifest computed' 'PASS' "$($manifest.Count) exe/dll files")

# ---- 7. PE hardening flags ------------------------------------------------
function Get-PeFlags([string]$Path) {
  $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
  try {
    $br = New-Object System.IO.BinaryReader($fs)
    $fs.Position = 0x3C
    $e_lfanew = $br.ReadInt32()
    $fs.Position = $e_lfanew + 24 + 70
    $dllChar = $br.ReadUInt16()
    return [ordered]@{
      raw = ('0x{0:X4}' -f $dllChar)
      dynamicBase = [bool]($dllChar -band 0x40)
      highEntropyVA = [bool]($dllChar -band 0x20)
      nxCompat = [bool]($dllChar -band 0x100)
      guardCF = [bool]($dllChar -band 0x4000)
      forceIntegrity = [bool]($dllChar -band 0x80)
    }
  } finally { $fs.Dispose() }
}
$pe = Get-PeFlags $exe
$report.exePeFlags = $pe
$peOk = $pe.dynamicBase -and $pe.nxCompat -and $pe.guardCF
[void](Add-Check 'PE hardening flags (chrome.exe)' $(if ($peOk) { 'PASS' } else { 'FAIL' }) $pe)

# ---- 8. verdict -----------------------------------------------------------
$fails = @($checks | Where-Object { $_.status -eq 'FAIL' })
$warns = @($checks | Where-Object { $_.status -eq 'WARN' })
if ($Mode -eq 'public') {
  $report.artifactClass = 'public-signed-artifact'
  $report.releasable = ($fails.Count -eq 0)
} else {
  $report.artifactClass = 'internal-unsigned-test-build'
  # An internal run is validated even when it is unsigned; blockers are still
  # recorded so nobody can mistake it for a release.
  $report.releasable = $false
  $report.releaseBlockers = @(
    'not Authenticode-signed (internal test build)',
    'is_official_build is not true (no PGO/ThinLTO official pipeline)',
    'no update-metadata.json / updater contract',
    'no Windows publisher identity configured in config/product-identity.env',
    'per-module signing and SmartScreen/AppLocker/EDR behavior not verified'
  )
}
$report.checks = $checks
$report.failCount = $fails.Count
$report.warnCount = $warns.Count

$json = $report | ConvertTo-Json -Depth 10
if ($OutJson) {
  $dir = Split-Path -Parent $OutJson
  if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  [System.IO.File]::WriteAllText($OutJson, $json, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Output "artifact class: $($report.artifactClass)"
Write-Output "releasable: $($report.releasable)"
foreach ($c in $checks) { Write-Output ("  {0,-6} {1}" -f $c.status, $c.name) }
if ($report.releaseBlockers.Count) {
  Write-Output 'release blockers:'
  foreach ($b in $report.releaseBlockers) { Write-Output "  - $b" }
}

if ($Mode -eq 'public' -and -not $report.releasable) { exit 1 }
exit 0
