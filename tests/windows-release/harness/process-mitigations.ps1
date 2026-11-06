<#
.SYNOPSIS
  Read effective Windows process mitigation policies, token state, module
  surface and image properties for Browser CLI / Chrome processes.

.DESCRIPTION
  This is the read-only measurement half of the Windows release-shape
  validation (tests/windows-release/harness). It never starts, stops or
  modifies a process and never writes to the target.

  Selection is by an explicit process-id list or by a marker string that must
  appear in the process command line (the harness uses the run's unique
  --user-data-dir path, so it can never pick up the user's real browser).

  For every selected process it records:
    * process type derived from --type= / --utility-sub-type= (browser, gpu,
      renderer, utility:network, utility:storage, crashpad-handler, ...);
    * GetProcessMitigationPolicy() for every PROCESS_MITIGATION_POLICY index,
      with raw bits and decoded booleans. A policy whose query succeeds with
      Flags == 0 is NOT-APPLIED (the policy is absent); a failed query is
      UNAVAILABLE(win32-error) and is never conflated with 0;
    * token integrity level and AppContainer state;
    * loaded module count, modules from the installation directory, and an
      Authenticode status count for every unique module path (the component vs
      non-component attack-surface evidence);
    * Authenticode status of the main executable and chrome.dll;
    * PE DllCharacteristics of the main executable.

.PARAMETER Marker
  Substring that must appear in the process command line (e.g. the isolated
  --user-data-dir path). Required unless -ProcessId is given.

.PARAMETER ProcessId
  Explicit PID list.

.PARAMETER OutJson
  Write the result as JSON to this path as well as returning it.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File process-mitigations.ps1 `
    -Marker 'D:\c\logs\winrelease\matrix\winrelease-default\profile' `
    -OutJson D:\c\logs\winrelease\matrix\winrelease-default\mitigations.json
#>
[CmdletBinding()]
param(
  [int]$RootPid = 0,
  [string]$Marker = "",
  [int[]]$ProcessId = @(),
  [string]$OutJson = ""
)

$ErrorActionPreference = 'Stop'

if (-not ([System.Management.Automation.PSTypeName]'WinReleaseProbe').Type) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace WinReleaseProbe {
  public static class Probe {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessMitigationPolicy(IntPtr h, int policy, IntPtr buffer, IntPtr size);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool OpenProcessToken(IntPtr h, uint access, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool GetTokenInformation(IntPtr token, int cls, IntPtr info, int len, out int needed);

    [DllImport("advapi32.dll")]
    static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);

    [DllImport("advapi32.dll")]
    static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);

    const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    const uint TOKEN_QUERY = 0x0008;
    const int TokenIntegrityLevel = 25;
    const int TokenIsAppContainer = 29;

    // Returns 0 on success. flags gets the raw policy DWORD (or the low 32
    // bits for the 64-bit mitigation options mask).
    public static int Query(int pid, int policy, out uint flags, out int win32Error) {
      flags = 0;
      win32Error = 0;
      IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
      if (h == IntPtr.Zero) { win32Error = Marshal.GetLastWin32Error(); return -1; }
      IntPtr buf = Marshal.AllocHGlobal(8);
      try {
        for (int i = 0; i < 8; i++) Marshal.WriteByte(buf, i, 0);
        int size = (policy == 0 || policy == 5) ? 8 : 4;
        if (!GetProcessMitigationPolicy(h, policy, buf, (IntPtr)size)) {
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

    public static string IntegrityLevel(int pid, out int isAppContainer, out int win32Error) {
      isAppContainer = -1;
      win32Error = 0;
      IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
      if (h == IntPtr.Zero) { win32Error = Marshal.GetLastWin32Error(); return null; }
      IntPtr token = IntPtr.Zero;
      try {
        if (!OpenProcessToken(h, TOKEN_QUERY, out token)) {
          win32Error = Marshal.GetLastWin32Error();
          return null;
        }
        string level = null;
        int needed = 0;
        IntPtr buf = Marshal.AllocHGlobal(64);
        try {
          if (GetTokenInformation(token, TokenIntegrityLevel, buf, 64, out needed)) {
            IntPtr sid = Marshal.ReadIntPtr(buf, 0);
            byte count = Marshal.ReadByte(GetSidSubAuthorityCount(sid), 0);
            IntPtr last = GetSidSubAuthority(sid, (uint)(count - 1));
            uint rid = (uint)Marshal.ReadInt32(last);
            if (rid < 0x1000) level = "Untrusted";
            else if (rid < 0x2000) level = "Low";
            else if (rid < 0x3000) level = "Medium";
            else if (rid < 0x4000) level = "High";
            else if (rid < 0x5000) level = "System";
            else level = "Protected";
          } else {
            win32Error = Marshal.GetLastWin32Error();
          }
        } finally {
          Marshal.FreeHGlobal(buf);
        }
        // Separate query on the same token handle: never return early inside
        // the integrity block, or AppContainer would stay -1.
        IntPtr acbuf = Marshal.AllocHGlobal(4);
        try {
          int acNeeded = 0;
          if (GetTokenInformation(token, TokenIsAppContainer, acbuf, 4, out acNeeded)) {
            isAppContainer = Marshal.ReadInt32(acbuf) != 0 ? 1 : 0;
          }
        } finally {
          Marshal.FreeHGlobal(acbuf);
        }
        return level;
      } finally {
        if (token != IntPtr.Zero) CloseHandle(token);
        CloseHandle(h);
      }
    }
  }
}
'@
}

$POLICIES = @(
  @{ Index = 0;  Name = 'DEP';                 Bits = @(@{ Mask = 0x1; Name = 'Enabled' }) },
  @{ Index = 1;  Name = 'ASLR';                Bits = @(@{ Mask = 0x1; Name = 'BottomUp' }, @{ Mask = 0x2; Name = 'HighEntropy' }, @{ Mask = 0x4; Name = 'ForceRelocate' }) },
  @{ Index = 2;  Name = 'DynamicCode';         Bits = @(@{ Mask = 0x1; Name = 'ProhibitDynamicCode' }, @{ Mask = 0x2; Name = 'AllowThreadOptOut' }, @{ Mask = 0x4; Name = 'AllowRemoteDowngrade' }) },
  @{ Index = 3;  Name = 'StrictHandle';        Bits = @(@{ Mask = 0x1; Name = 'Enabled' }) },
  @{ Index = 4;  Name = 'SystemCallDisable';   Bits = @(@{ Mask = 0x1; Name = 'DisableSystemCalls' }) },
  @{ Index = 5;  Name = 'MitigationOptionsMask'; Bits = @() },
  @{ Index = 6;  Name = 'ExtensionPointDisable'; Bits = @(@{ Mask = 0x1; Name = 'DisableExtensionPoints' }) },
  @{ Index = 7;  Name = 'ControlFlowGuard';    Bits = @(@{ Mask = 0x1; Name = 'CFGEnabled' }, @{ Mask = 0x2; Name = 'StrictCFG' }) },
  @{ Index = 8;  Name = 'Signature';           Bits = @(@{ Mask = 0x1; Name = 'MicrosoftSignedOnly' }, @{ Mask = 0x2; Name = 'StoreSignedOnly' }, @{ Mask = 0x4; Name = 'MitigationOptIn' }) },
  @{ Index = 9;  Name = 'FontDisable';         Bits = @(@{ Mask = 0x1; Name = 'DisableNonSystemFonts' }) },
  @{ Index = 10; Name = 'ImageLoad';           Bits = @(@{ Mask = 0x1; Name = 'NoRemoteImages' }, @{ Mask = 0x2; Name = 'NoLowLabelImages' }, @{ Mask = 0x4; Name = 'PreferSystem32' }) },
  @{ Index = 11; Name = 'SystemCallFilter';    Bits = @() },
  @{ Index = 12; Name = 'PayloadRestriction';  Bits = @(@{ Mask = 0x1; Name = 'EnableExportAddressFilter' }, @{ Mask = 0x2; Name = 'AuditExportAddressFilter' }, @{ Mask = 0x4; Name = 'EnableImportAddressFilter' }, @{ Mask = 0x8; Name = 'AuditImportAddressFilter' }, @{ Mask = 0x10; Name = 'EnableRopStackPivot' }, @{ Mask = 0x20; Name = 'AuditRopStackPivot' }, @{ Mask = 0x40; Name = 'EnableRopCallerCheck' }, @{ Mask = 0x80; Name = 'AuditRopCallerCheck' }, @{ Mask = 0x100; Name = 'EnableRopSimExec' }, @{ Mask = 0x200; Name = 'AuditRopSimExec' }) },
  @{ Index = 13; Name = 'ChildProcess';        Bits = @(@{ Mask = 0x1; Name = 'NoChildProcessCreation' }, @{ Mask = 0x2; Name = 'AuditNoChildProcessCreation' }, @{ Mask = 0x4; Name = 'AllowSecureProcessCreation' }) },
  @{ Index = 14; Name = 'SideChannelIsolation'; Bits = @(@{ Mask = 0x1; Name = 'SmtBranchTargetIsolation' }, @{ Mask = 0x2; Name = 'IsolateSecurityDomain' }, @{ Mask = 0x4; Name = 'DisablePageCombine' }, @{ Mask = 0x8; Name = 'SpeculativeStoreBypassDisable' }, @{ Mask = 0x10; Name = 'RestrictCoreSharing' }) },
  @{ Index = 15; Name = 'UserShadowStack';     Bits = @(@{ Mask = 0x1; Name = 'UserShadowStack' }, @{ Mask = 0x2; Name = 'SetContextIpValidation' }) },
  @{ Index = 16; Name = 'RedirectionTrust';    Bits = @(@{ Mask = 0x1; Name = 'RedirectionTrust' }) },
  @{ Index = 17; Name = 'UserPointer';         Bits = @(@{ Mask = 0x1; Name = 'UserPointer' }) }
)

function Get-ProcessType([string]$CommandLine) {
  if ($CommandLine -match '--utility-sub-type=([^\s"]+)') {
    $sub = $Matches[1]
    if ($sub -match 'NetworkService') { return 'utility:network' }
    if ($sub -match 'StorageService') { return 'utility:storage' }
    return "utility:$sub"
  }
  if ($CommandLine -match '--type=([^\s"]+)') {
    $t = $Matches[1]
    if ($t -eq 'utility') { return 'utility' }
    if ($t -eq 'gpu-process') { return 'gpu' }
    return $t
  }
  return 'browser'
}

function Test-Marker($proc) {
  if ($ProcessId.Count -gt 0) { return ($ProcessId -contains [int]$proc.ProcessId) }
  if ([string]::IsNullOrEmpty($Marker)) { return $false }
  return ($null -ne $proc.CommandLine -and $proc.CommandLine.Contains($Marker))
}

function Get-PeFlags([string]$Path) {
  try {
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
      $br = New-Object System.IO.BinaryReader($fs)
      if ($br.ReadUInt16() -ne 0x5A4D) { return $null }   # MZ
      $fs.Position = 0x3C
      $e_lfanew = $br.ReadInt32()
      $fs.Position = $e_lfanew
      if ($br.ReadUInt32() -ne 0x00004550) { return $null } # PE\0\0
      $fs.Position = $e_lfanew + 24        # optional header start
      $magic = $br.ReadUInt16()
      $fs.Position = $e_lfanew + 24 + 70   # DllCharacteristics offset
      $dllChar = $br.ReadUInt16()
      return [ordered]@{
        magic = ('0x{0:X4}' -f $magic)
        dllCharacteristics = ('0x{0:X4}' -f $dllChar)
        dynamicBase = [bool]($dllChar -band 0x40)
        highEntropyVA = [bool]($dllChar -band 0x20)
        nxCompat = [bool]($dllChar -band 0x100)
        guardCF = [bool]($dllChar -band 0x4000)
        forceIntegrity = [bool]($dllChar -band 0x80)
      }
    } finally { $fs.Dispose() }
  } catch { return $null }
}

function ConvertTo-IsoTime($Value) {
  if ($null -eq $Value) { return $null }
  if ($Value -is [DateTime]) { return $Value.ToString('o') }
  try { return ([Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToString('o') } catch { return [string]$Value }
}

$sigCache = @{}
function Get-SignatureInfo([string]$Path) {
  if ([string]::IsNullOrEmpty($Path)) { return $null }
  if ($sigCache.ContainsKey($Path)) { return $sigCache[$Path] }
  $info = [ordered]@{ status = 'MISSING'; subject = $null; thumbprint = $null }
  try {
    if (Test-Path -LiteralPath $Path) {
      $s = Get-AuthenticodeSignature -LiteralPath $Path
      $info.status = [string]$s.Status
      if ($s.SignerCertificate) {
        $info.subject = $s.SignerCertificate.Subject
        $info.thumbprint = $s.SignerCertificate.Thumbprint
      }
    }
  } catch {
    $info.status = 'ERROR'
    $info.subject = $_.Exception.Message
  }
  $sigCache[$Path] = $info
  return $info
}

# Selection: an explicit root PID plus all of its descendants (Chromium child
# command lines do not reliably carry --user-data-dir, so a marker match would
# silently drop renderer/GPU/utility processes). PID-reuse is guarded by
# requiring a descendant's creation time to be at or after the root's.
$allProcs = @(Get-CimInstance Win32_Process)
$byParent = @{}
foreach ($p in $allProcs) {
  $k = [int]$p.ParentProcessId
  if (-not $byParent.ContainsKey($k)) { $byParent[$k] = New-Object System.Collections.ArrayList }
  [void]$byParent[$k].Add($p)
}
$selected = @()
if ($RootPid -gt 0) {
  $rootProc = $allProcs | Where-Object { [int]$_.ProcessId -eq $RootPid } | Select-Object -First 1
  if (-not $rootProc) { throw "root pid $RootPid is not running" }
  $rootCreated = $null
  if ($rootProc.CreationDate) {
    $rootCreated = [DateTime]$rootProc.CreationDate
    if ($rootCreated.Kind -eq 'Utc') { $rootCreated = $rootCreated.ToLocalTime() }
  }
  $want = New-Object System.Collections.Generic.HashSet[int]
  $queue = New-Object System.Collections.Generic.Queue[int]
  [void]$want.Add($RootPid)
  $queue.Enqueue($RootPid)
  while ($queue.Count -gt 0) {
    $cur = $queue.Dequeue()
    if (-not $byParent.ContainsKey($cur)) { continue }
    foreach ($c in $byParent[$cur]) {
      $cid = [int]$c.ProcessId
      if ($want.Contains($cid)) { continue }
      $cCreated = $null
      if ($c.CreationDate) {
        $cCreated = [DateTime]$c.CreationDate
        if ($cCreated.Kind -eq 'Utc') { $cCreated = $cCreated.ToLocalTime() }
      }
      if ($rootCreated -and $cCreated -and $cCreated -lt $rootCreated.AddSeconds(-1)) { continue }
      [void]$want.Add($cid)
      $queue.Enqueue($cid)
    }
  }
  $selected = @($allProcs | Where-Object { $want.Contains([int]$_.ProcessId) })
} elseif ($ProcessId.Count -gt 0) {
  $selected = @($allProcs | Where-Object { $ProcessId -contains [int]$_.ProcessId })
} elseif (-not [string]::IsNullOrEmpty($Marker)) {
  $selected = @($allProcs | Where-Object { Test-Marker $_ })
}
if ($selected.Count -eq 0) {
  throw "no process matched root pid '$RootPid' / marker '$Marker' / PIDs '$($ProcessId -join ',')'"
}

$root = $selected | Where-Object { (Get-ProcessType $_.CommandLine) -eq 'browser' } | Select-Object -First 1
$installDir = $null
if ($root) { $installDir = Split-Path -Parent $root.ExecutablePath }

$results = @()
foreach ($p in ($selected | Sort-Object ProcessId)) {
  $pidValue = [int]$p.ProcessId
  $type = Get-ProcessType $p.CommandLine
  $entry = [ordered]@{
    pid = $pidValue
    parentPid = [int]$p.ParentProcessId
    type = $type
    exe = $p.ExecutablePath
    commandLine = $p.CommandLine
    creationDate = (ConvertTo-IsoTime $p.CreationDate)
    integrityLevel = $null
    appContainer = $null
    tokenError = $null
    mitigations = [ordered]@{}
    moduleCount = $null
    installDirModuleCount = $null
    moduleSignature = $null
    signature = (Get-SignatureInfo $p.ExecutablePath)
    exePeFlags = (Get-PeFlags $p.ExecutablePath)
  }

  $ac = -1; $tokErr = 0
  $il = [WinReleaseProbe.Probe]::IntegrityLevel($pidValue, [ref]$ac, [ref]$tokErr)
  $entry.integrityLevel = $il
  $entry.appContainer = $ac
  if ($null -eq $il) { $entry.tokenError = "win32=$tokErr $((New-Object ComponentModel.Win32Exception($tokErr)).Message)" }

  foreach ($pol in $POLICIES) {
    $flags = 0; $err = 0
    $rc = [WinReleaseProbe.Probe]::Query($pidValue, $pol.Index, [ref]$flags, [ref]$err)
    $m = [ordered]@{ status = 'APPLIED'; raw = ('0x{0:X8}' -f $flags); bits = [ordered]@{} }
    if ($rc -ne 0) {
      $m.status = "UNAVAILABLE($err)"
      $m.raw = $null
      $m.win32Message = (New-Object ComponentModel.Win32Exception($err)).Message
    } else {
      if ($flags -eq 0) { $m.status = 'NOT-APPLIED' }
      foreach ($bit in $pol.Bits) {
        $m.bits[$bit.Name] = [bool]($flags -band $bit.Mask)
      }
    }
    $entry.mitigations[$pol.Name] = $m
  }

  try {
    $proc = Get-Process -Id $pidValue -ErrorAction Stop
    $mods = @($proc.Modules)
    $entry.moduleCount = $mods.Count
    $paths = @($mods | ForEach-Object { $_.FileName } | Sort-Object -Unique)
    $installDirModules = @()
    if ($installDir) {
      $installDirPaths = @($paths | Where-Object { $_ -and $_.StartsWith($installDir, [StringComparison]::OrdinalIgnoreCase) })
      $entry.installDirModuleCount = $installDirPaths.Count
      foreach ($path in $installDirPaths) {
        $si = Get-SignatureInfo $path
        $installDirModules += [ordered]@{
          file = (Split-Path -Leaf $path)
          signature = [string]$si.status
          subject = $si.subject
        }
      }
      $entry.installDirModules = $installDirModules
    }
    $statusCounts = @{}
    $signers = @{}
    $nonMs = @()
    foreach ($path in $paths) {
      $si = Get-SignatureInfo $path
      $st = [string]$si.status
      if (-not $statusCounts.ContainsKey($st)) { $statusCounts[$st] = 0 }
      $statusCounts[$st]++
      if ($si.subject) {
        $subj = [string]$si.subject
        if (-not $signers.ContainsKey($subj)) { $signers[$subj] = 0 }
        $signers[$subj]++
        if ($st -eq 'Valid' -and ($subj -notmatch 'Microsoft')) { $nonMs += $path }
      }
    }
    $entry.moduleSignature = [ordered]@{
      statusCounts = $statusCounts
      signers = $signers
      nonMicrosoftSigned = @($nonMs | Select-Object -First 20)
      nonMicrosoftSignedCount = $nonMs.Count
    }
  } catch {
    $entry.moduleError = $_.Exception.Message
  }

  $results += $entry
}

$out = [ordered]@{
  generatedAt = (Get-Date).ToString('o')
  marker = $Marker
  installDir = $installDir
  processCount = $results.Count
  types = @($results | ForEach-Object { $_.type } | Sort-Object -Unique)
  processes = $results
}

$json = $out | ConvertTo-Json -Depth 10
if ($OutJson) {
  $dir = Split-Path -Parent $OutJson
  if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  [System.IO.File]::WriteAllText($OutJson, $json, (New-Object System.Text.UTF8Encoding($false)))
}
$json
