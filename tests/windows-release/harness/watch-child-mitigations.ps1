<#
.SYNOPSIS
  Watch Browser CLI / Chrome child processes appear and record when Windows
  Code Integrity Guard (CIG) and Arbitrary Code Guard (ACG) become effective.

.DESCRIPTION
  Read-only. The harness starts this watcher BEFORE launching the browser (it
  filters by the run's unique --user-data-dir marker, so it cannot pick up any
  other browser). Every poll enumerates chrome.exe processes whose command
  line contains the marker and records, for each new PID:

    * process type derived from --type= / --utility-sub-type=;
    * creation time and first-observation delay;
    * the first raw Signature policy (CIG) and DynamicCode policy (ACG) value
      observed, i.e. whether the mitigation was already present at birth;
    * whether a later poll observed the CIG bit flip 0 -> 1 and the delay from
      process creation.

  This is how the component build (delayed CIG applied by the child's own
  TargetServices::LowerToken) is distinguished from the non-component build
  (pre-start CIG set on the CreateProcess mitigation attribute in
  ChromeContentBrowserClient::PreSpawnChild): in the non-component build the
  very first observable sample already carries CIG, and only Microsoft-signed
  modules plus the explicitly allowlisted chrome.dll/chrome_elf.dll can load.

  Polling cannot see a window shorter than one WMI round-trip; report the
  first-observed value as-is and do not infer a sub-poll timing.

.PARAMETER RootPidFile
  File containing the browser PID; written by the harness immediately after
  spawning the browser. Only this PID's descendants are recorded.

.PARAMETER ExePath
  Exact chrome.exe path expected for every recorded process.

.PARAMETER DurationMs
  How long to watch after the first poll. Default 8000.

.PARAMETER IntervalMs
  Sleep between polls. Default 10.

.PARAMETER OutJsonl
  File to write one JSON object per process. Also written to stdout.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File watch-child-mitigations.ps1 `
    -RootPidFile run/root.pid -ExePath 'D:\c\src\out\WinRelease\chrome.exe' `
    -DurationMs 8000 -OutJsonl run\children.jsonl
#>
[CmdletBinding()]
param(
  # The harness starts this watcher before launching the browser, then writes
  # the new browser PID to -RootPidFile. Only that PID's descendants are
  # recorded, so an unrelated user Chrome can never be measured.
  [Parameter(Mandatory = $true)][string]$RootPidFile,
  [Parameter(Mandatory = $true)][string]$ExePath,
  [int]$DurationMs = 8000,
  [int]$IntervalMs = 10,
  [string]$OutJsonl = ""
)

$ErrorActionPreference = 'Stop'

if (-not ([System.Management.Automation.PSTypeName]'WinReleaseWatch').Type) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace WinReleaseWatch {
  public static class Watch {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessMitigationPolicy(IntPtr h, int policy, IntPtr buffer, IntPtr size);

    const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

    public static int Query(int pid, int policy, out uint flags, out int win32Error) {
      flags = 0;
      win32Error = 0;
      IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
      if (h == IntPtr.Zero) { win32Error = Marshal.GetLastWin32Error(); return -1; }
      IntPtr buf = Marshal.AllocHGlobal(8);
      try {
        for (int i = 0; i < 8; i++) Marshal.WriteByte(buf, i, 0);
        if (!GetProcessMitigationPolicy(h, policy, buf, (IntPtr)4)) {
          win32Error = Marshal.GetLastWin32Error();
          return -1;
        }
        flags = (uint)Marshal.ReadInt32(buf);
        return 0;
      } finally {
        Marshal.FreeHGlobal(buf);
        CloseHandle(h);
      }
    }
  }
}
'@
}

function Get-TypeFromCommandLine([string]$CommandLine) {
  if ($CommandLine -match '--utility-sub-type=([^\s"]+)') {
    $sub = $Matches[1]
    if ($sub -match 'NetworkService') { return 'utility:network' }
    if ($sub -match 'StorageService') { return 'utility:storage' }
    return "utility:$sub"
  }
  if ($CommandLine -match '--type=([^\s"]+)') {
    $t = $Matches[1]
    if ($t -eq 'gpu-process') { return 'gpu' }
    return $t
  }
  return 'browser'
}

function ConvertTo-IsoTime($Value) {
  if ($null -eq $Value) { return $null }
  if ($Value -is [DateTime]) { return $Value.ToString('o') }
  try { return ([Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToString('o') } catch { return [string]$Value }
}

$scriptStart = [DateTime]::UtcNow
$rootPid = 0
$records = @{}     # pid -> ordered dict (hashtable: numeric keys in an OrderedDictionary are treated as indexes)
$writer = $null
if ($OutJsonl) {
  $dir = Split-Path -Parent $OutJsonl
  if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  $writer = New-Object System.IO.StreamWriter($OutJsonl, $false, (New-Object System.Text.UTF8Encoding($false)))
  $writer.AutoFlush = $true
}

$deadline = [DateTime]::UtcNow.AddMilliseconds($DurationMs)
try {
  while ([DateTime]::UtcNow -lt $deadline) {
    $now = [DateTime]::UtcNow
    if ($rootPid -le 0 -and (Test-Path -LiteralPath $RootPidFile)) {
      try {
        $raw = (Get-Content -LiteralPath $RootPidFile -Raw).Trim()
        if ($raw) { $rootPid = [int]$raw }
      } catch {}
    }
    $procs = @()
    try {
      $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name='chrome.exe'" -ErrorAction Stop)
    } catch {
      # WMI hiccup: keep watching.
      $procs = @()
    }
    if ($rootPid -le 0) { Start-Sleep -Milliseconds $IntervalMs; continue }
    # Descendant set of this run's browser PID (exact exe path + ancestry).
    $byPid = @{}
    $kids = @{}
    foreach ($p in $procs) { $byPid[[int]$p.ProcessId] = $p }
    foreach ($p in $procs) {
      $par = [int]$p.ParentProcessId
      if (-not $kids.ContainsKey($par)) { $kids[$par] = New-Object System.Collections.ArrayList }
      [void]$kids[$par].Add($p)
    }
    $inRun = New-Object System.Collections.Generic.HashSet[int]
    $queue = New-Object System.Collections.Generic.Queue[int]
    [void]$inRun.Add($rootPid)
    $queue.Enqueue($rootPid)
    while ($queue.Count -gt 0) {
      $cur = $queue.Dequeue()
      if (-not $kids.ContainsKey($cur)) { continue }
      foreach ($c in $kids[$cur]) {
        $cid = [int]$c.ProcessId
        if ($inRun.Add($cid)) { $queue.Enqueue($cid) }
      }
    }
    foreach ($p in $procs) {
      $pidValue = [int]$p.ProcessId
      if (-not $inRun.Contains($pidValue)) { continue }
      if ($p.ExecutablePath -and ($p.ExecutablePath -ne $ExePath)) { continue }
      if ($null -eq $p.CommandLine) { continue }
      $type = Get-TypeFromCommandLine $p.CommandLine
      $created = $null
      if ($p.CreationDate) {
        $created = [DateTime]$p.CreationDate
        if ($created.Kind -eq 'Utc') { $created = $created.ToLocalTime() }
      }
      if (-not $records.Contains($pidValue)) {
        $cig = 0; $cigErr = 0
        $cigRc = [WinReleaseWatch.Watch]::Query($pidValue, 8, [ref]$cig, [ref]$cigErr)
        $acg = 0; $acgErr = 0
        $acgRc = [WinReleaseWatch.Watch]::Query($pidValue, 2, [ref]$acg, [ref]$acgErr)
        $rec = [ordered]@{
          pid = $pidValue
          type = $type
          creation = (ConvertTo-IsoTime $created)
          firstSeen = $now.ToString('o')
          firstSeenDelayFromLaunchMs = [int]($now - $scriptStart).TotalMilliseconds
          firstSeenDelayFromCreationMs = $(if ($created) { [int]($now.ToLocalTime() - $created).TotalMilliseconds } else { $null })
          firstCigRaw = $(if ($cigRc -eq 0) { ('0x{0:X8}' -f $cig) } else { $null })
          firstCigStatus = $(if ($cigRc -eq 0) { 'OBSERVED' } else { "UNAVAILABLE($cigErr)" })
          firstCigPresentAtBirth = $(if ($cigRc -eq 0) { ($cig -ne 0) } else { $null })
          firstAcgRaw = $(if ($acgRc -eq 0) { ('0x{0:X8}' -f $acg) } else { $null })
          firstAcgStatus = $(if ($acgRc -eq 0) { 'OBSERVED' } else { "UNAVAILABLE($acgErr)" })
          lastCigRaw = $(if ($cigRc -eq 0) { ('0x{0:X8}' -f $cig) } else { $null })
          cigAppliedLater = $false
          cigAppliedAt = $null
          cigAppliedDelayFromCreationMs = $null
        }
        $records[$pidValue] = $rec
        if ($writer) { $writer.WriteLine(($rec | ConvertTo-Json -Depth 6 -Compress)) }
      } else {
        $rec = $records[$pidValue]
        # Only poll processes whose CIG was absent on the first sample.
        if ($rec.firstCigStatus -eq 'OBSERVED' -and -not $rec.firstCigPresentAtBirth -and -not $rec.cigAppliedLater) {
          $cig = 0; $cigErr = 0
          $rc = [WinReleaseWatch.Watch]::Query($pidValue, 8, [ref]$cig, [ref]$cigErr)
          if ($rc -eq 0) {
            $rec.lastCigRaw = ('0x{0:X8}' -f $cig)
            if ($cig -ne 0) {
              $rec.cigAppliedLater = $true
              $rec.cigAppliedAt = $now.ToString('o')
              if ($created) {
                $rec.cigAppliedDelayFromCreationMs = [int]($now.ToLocalTime() - $created).TotalMilliseconds
              }
              if ($writer) { $writer.WriteLine(($rec | ConvertTo-Json -Depth 6 -Compress)) }
            }
          }
        }
      }
    }
    Start-Sleep -Milliseconds $IntervalMs
  }
} finally {
  if ($writer) { $writer.Dispose() }
}

$summary = [ordered]@{
  generatedAt = (Get-Date).ToString('o')
  rootPid = $rootPid
  exePath = $ExePath
  watchedMs = $DurationMs
  intervalMs = $IntervalMs
  processCount = $records.Count
  children = @($records.Values)
}
$summary | ConvertTo-Json -Depth 8
