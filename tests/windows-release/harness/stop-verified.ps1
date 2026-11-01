<#
.SYNOPSIS
  Stop only the processes that provably belong to one harness run.

.DESCRIPTION
  The Windows release harness is not allowed to use image-name kills
  (taskkill /IM, Stop-Process -Name) or to touch any browser instance it did
  not start. This helper stops an explicit PID only when all of these hold:

    * the PID currently exists;
    * its ExecutablePath equals -ExpectedExe (case-insensitive);
    * it is -RootPid or a descendant of -RootPid (ancestry is re-derived from
      Win32_Process, and child creation must not predate the root);
    * its creation time is at or after -NotBeforeUtc (ISO-8601).

  Anything that does not match is reported and left running. It never stops a
  process by name and never enumerates other browsers for stopping.

.PARAMETER ProcessId
  PIDs to consider (normally the snapshot's process list).

.PARAMETER RootPid
  PID of the browser process this run launched. A candidate is stopped only
  when its parent chain reaches this PID.

.PARAMETER ExpectedExe
  Full path of the chrome.exe that this run launched.

.PARAMETER NotBeforeUtc
  ISO-8601 UTC timestamp captured immediately before launch.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File stop-verified.ps1 `
    -ProcessId 123,456 -Marker 'D:\temp\wr-abc' `
    -ExpectedExe 'D:\c\src\out\WinRelease\chrome.exe' `
    -NotBeforeUtc '2026-11-01T08:00:00Z'
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ProcessId,
  [Parameter(Mandatory = $true)][int]$RootPid,
  [Parameter(Mandatory = $true)][string]$ExpectedExe,
  [Parameter(Mandatory = $true)][string]$NotBeforeUtc
)

$ErrorActionPreference = 'Stop'
$notBefore = [DateTime]::Parse($NotBeforeUtc, [Globalization.CultureInfo]::InvariantCulture,
                               [Globalization.DateTimeStyles]::AdjustToUniversal)
$expected = [System.IO.Path]::GetFullPath($ExpectedExe)
$stopped = @()
$skipped = @()

# Snapshot the process table once and derive the root's descendants. A recycled
# PID is rejected when its parent chain does not reach the root.
$allProcs = @(Get-CimInstance Win32_Process)
$byPid = @{}
foreach ($p in $allProcs) { $byPid[[int]$p.ProcessId] = $p }
function Test-Descendant([int]$Candidate) {
  $seen = New-Object System.Collections.Generic.HashSet[int]
  $cur = $Candidate
  for ($i = 0; $i -lt 32; $i++) {
    if ($cur -eq $RootPid) { return $true }
    if (-not $seen.Add($cur)) { return $false }
    $p = $byPid[$cur]
    if (-not $p) { return $false }
    $cur = [int]$p.ParentProcessId
    if ($cur -le 0) { return $false }
  }
  return $false
}

$pidList = @($ProcessId.Split(',') | ForEach-Object { [int]$_.Trim() } | Where-Object { $_ -gt 0 })
foreach ($pidValue in ($pidList | Sort-Object -Unique)) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
  if (-not $p) { $skipped += [ordered]@{ pid = $pidValue; reason = 'not-running' }; continue }
  $reason = $null
  $exe = $null
  if ($p.ExecutablePath) { $exe = [System.IO.Path]::GetFullPath($p.ExecutablePath) }
  if (-not $exe -or $exe -ne $expected) { $reason = "exe-mismatch:$exe" }
  elseif (-not (Test-Descendant $pidValue)) { $reason = "not-a-descendant-of-$RootPid" }
  else {
    $created = [DateTime]$p.CreationDate
    if ($created.Kind -eq 'Local') { $created = $created.ToUniversalTime() }
    if ($created -lt $notBefore.AddSeconds(-2)) { $reason = "created-before-launch:$($created.ToString('o'))" }
  }
  if ($reason) { $skipped += [ordered]@{ pid = $pidValue; reason = $reason }; continue }
  try {
    Stop-Process -Id $pidValue -Force -ErrorAction Stop
    $stopped += $pidValue
  } catch {
    $skipped += [ordered]@{ pid = $pidValue; reason = "stop-failed:$($_.Exception.Message)" }
  }
}

[ordered]@{ stopped = $stopped; skipped = $skipped } | ConvertTo-Json -Depth 4
