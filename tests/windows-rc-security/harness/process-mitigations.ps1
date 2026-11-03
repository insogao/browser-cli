<#
.SYNOPSIS
  Read effective Windows process mitigation policies and image properties for
  a set of live processes.

.DESCRIPTION
  For every requested PID this script records:
    * GetProcessMitigationPolicy() for every PROCESS_MITIGATION_POLICY index,
      including the Win32 error for policies the OS/process refuses to expose;
    * token integrity level and AppContainer state;
    * file handle / USER / GDI counts (lifecycle leak probes);
    * the main executable PE flags (DYNAMICBASE, HIGH_ENTROPY_VA, NX_COMPAT,
      GUARD_CF, FORCE_INTEGRITY), load-config SEHOP metadata and GuardFlags;
    * the Authenticode status of the executable and (when present) chrome.dll;
    * loaded module count and how many modules come from the installation
      directory (component-build attack surface).

  A policy whose query succeeds with Flags == 0 is reported as APPLIED=false
  (the policy is absent for this process). A failed query is reported as
  STATUS=UNAVAILABLE with the exact Win32 error, so "policy not applied" is
  never conflated with "field unavailable / access denied".

  The script never starts or stops processes and never writes to the target.

.PARAMETER ProcessId
  One or more PIDs.

.PARAMETER ProcessName
  Process name(s) without extension, e.g. chrome, msedge. All matching
  processes are probed.

.PARAMETER OutJson
  Output JSON path. Defaults to stdout only.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File process-mitigations.ps1 `
    -ProcessName chrome -OutJson D:\c\logs\rc-sec\mitigations.json
#>
[CmdletBinding()]
param(
  [int[]]$ProcessId = @(),
  [string[]]$ProcessName = @(),
  [string]$OutJson = "",
  [switch]$QuickCounters
)

$ErrorActionPreference = 'Stop'

if (-not ([System.Management.Automation.PSTypeName]'RcSec.Mitigations').Type) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace RcSec {
  public static class Mitigations {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessMitigationPolicy(IntPtr h, int policy, IntPtr buffer, IntPtr size);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetProcessHandleCount(IntPtr h, out uint count);

    [DllImport("user32.dll", SetLastError = true)]
    static extern uint GetGuiResources(IntPtr h, uint flags);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool OpenProcessToken(IntPtr h, uint access, out IntPtr token);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool GetTokenInformation(IntPtr token, int cls, IntPtr info, int len, out int needed);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);

    public const uint PROCESS_QUERY_INFORMATION = 0x0400;
    public const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
    public const uint TOKEN_QUERY = 0x0008;
    public const int TokenIntegrityLevel = 25;
    public const int TokenIsAppContainer = 29;
    public const uint GR_GDIOBJECTS = 0;
    public const uint GR_USEROBJECTS = 1;

    static IntPtr OpenQuery(int pid) {
      return OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
                         false, pid);
    }

    // Returns 0 on success. flags receives the raw policy DWORD (or the low
    // 32 bits of the 64-bit ProcessMitigationOptionsMask).
    public static int Query(int pid, int policy, out uint flags, out int win32Error) {
      flags = 0;
      win32Error = 0;
      IntPtr h = OpenQuery(pid);
      if (h == IntPtr.Zero) {
        win32Error = Marshal.GetLastWin32Error();
        return -1;
      }
      IntPtr buf = Marshal.AllocHGlobal(8);
      try {
        for (int i = 0; i < 8; i++) Marshal.WriteByte(buf, i, 0);
        // DEP carries a trailing BOOLEAN; the options mask is 64-bit.
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

    public static int Counters(int pid, out uint handles, out uint gdi, out uint user,
                               out int win32Error) {
      handles = 0; gdi = 0; user = 0; win32Error = 0;
      IntPtr h = OpenQuery(pid);
      if (h == IntPtr.Zero) {
        win32Error = Marshal.GetLastWin32Error();
        return -1;
      }
      try {
        uint v;
        if (!GetProcessHandleCount(h, out v)) {
          win32Error = Marshal.GetLastWin32Error();
          return -1;
        }
        handles = v;
        gdi = GetGuiResources(h, GR_GDIOBJECTS);
        user = GetGuiResources(h, GR_USEROBJECTS);
        return 0;
      } finally {
        CloseHandle(h);
      }
    }

    public static string IntegrityLevel(int pid, out int isAppContainer, out int win32Error) {
      isAppContainer = -1;
      win32Error = 0;
      IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
      if (h == IntPtr.Zero) {
        win32Error = Marshal.GetLastWin32Error();
        return null;
      }
      IntPtr token = IntPtr.Zero;
      string level = null;
      try {
        if (!OpenProcessToken(h, TOKEN_QUERY, out token)) {
          win32Error = Marshal.GetLastWin32Error();
          return null;
        }
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
        // Must not return from inside the integrity block: TokenIsAppContainer
        // is a separate query on the same token handle.
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

    public static string LastErrorText(int code) {
      return new System.ComponentModel.Win32Exception(code).Message;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetExitCodeProcess(IntPtr h, out uint code);

    const uint STILL_ACTIVE = 259;

    // Polls one mitigation policy from process creation through exit. Used for
    // processes that die shortly after the policy is applied (e.g. ACG plus the
    // Sogou TSF IME on this host). Returns the first non-zero raw value seen,
    // or 0 if the policy never became non-zero. firstNonZeroMs is -1 if never.
    public static int Watch(int pid, int policy, int durationMs, out uint firstNonZero,
                            out int firstNonZeroMs, out int firstOkMs, out int samples,
                            out int lastError, out uint exitCode) {
      firstNonZero = 0;
      firstNonZeroMs = -1;
      firstOkMs = -1;
      samples = 0;
      lastError = 0;
      exitCode = 0;
      var sw = System.Diagnostics.Stopwatch.StartNew();
      IntPtr h = IntPtr.Zero;
      try {
        while (sw.ElapsedMilliseconds < durationMs) {
          if (h == IntPtr.Zero) {
            h = OpenQuery(pid);
            if (h == IntPtr.Zero) {
              lastError = Marshal.GetLastWin32Error();
              System.Threading.Thread.Sleep(10);
              continue;
            }
          }
          uint code;
          if (GetExitCodeProcess(h, out code) && code != STILL_ACTIVE) {
            exitCode = code;
            break;
          }
          IntPtr buf = Marshal.AllocHGlobal(8);
          try {
            for (int i = 0; i < 8; i++) Marshal.WriteByte(buf, i, 0);
            int size = (policy == 0 || policy == 5) ? 8 : 4;
            if (GetProcessMitigationPolicy(h, policy, buf, (IntPtr)size)) {
              samples++;
              if (firstOkMs < 0) firstOkMs = (int)sw.ElapsedMilliseconds;
              uint raw = (uint)Marshal.ReadInt32(buf);
              if (raw != 0 && firstNonZeroMs < 0) {
                firstNonZero = raw;
                firstNonZeroMs = (int)sw.ElapsedMilliseconds;
              }
            } else {
              lastError = Marshal.GetLastWin32Error();
            }
          } finally {
            Marshal.FreeHGlobal(buf);
          }
          System.Threading.Thread.Sleep(10);
        }
      } finally {
        if (h != IntPtr.Zero) CloseHandle(h);
      }
      return firstNonZeroMs >= 0 ? 1 : 0;
    }
  }
}
'@
}

function Get-Win32Text([int]$code) {
  try { return [RcSec.Mitigations]::LastErrorText($code) } catch { return "error $code" }
}

# PROCESS_MITIGATION_POLICY values (winnt.h).
$PolicyDefs = @(
  # PROCESS_MITIGATION_DEP_POLICY carries an extra BOOLEAN Permanent field,
  # so its size is 8, not 4. Everything else below is a 4-byte DWORD struct.
  @{ Name = 'DEP';                    Index = 0;  Size = 8; Bits = @(
      @{ Name='Enable'; Bit=0 }, @{ Name='DisableAtlThunkEmulation'; Bit=1 }, @{ Name='Disable'; Bit=2 }) },
  @{ Name = 'ASLR';                   Index = 1;  Size = 4; Bits = @(
      @{ Name='EnableBottomUpRandomization'; Bit=0 }, @{ Name='EnableForceRelocateImages'; Bit=1 },
      @{ Name='EnableHighEntropy'; Bit=2 }, @{ Name='DisallowStrippedImages'; Bit=3 }) },
  @{ Name = 'DynamicCode';            Index = 2;  Size = 4; Bits = @(
      @{ Name='ProhibitDynamicCode'; Bit=0 }, @{ Name='AllowThreadOptOut'; Bit=1 },
      @{ Name='AllowRemoteDowngrade'; Bit=2 }) },
  @{ Name = 'StrictHandle';           Index = 3;  Size = 4; Bits = @(
      @{ Name='RaiseExceptionOnInvalidHandleReference'; Bit=0 },
      @{ Name='HandleExceptionsPermanentlyEnabled'; Bit=1 }) },
  @{ Name = 'SystemCallDisable';      Index = 4;  Size = 4; Bits = @(
      @{ Name='DisableWin32kSystemCalls'; Bit=0 }, @{ Name='AuditSystemCallDisable'; Bit=1 }) },
  @{ Name = 'MitigationOptionsMask';  Index = 5;  Size = 8; Bits = @() },
  @{ Name = 'ExtensionPointDisable'; Index = 6;  Size = 4; Bits = @(
      @{ Name='DisableExtensionPoints'; Bit=0 }) },
  @{ Name = 'ControlFlowGuard';       Index = 7;  Size = 4; Bits = @(
      @{ Name='EnableControlFlowGuard'; Bit=0 }, @{ Name='EnableExportSuppression'; Bit=1 },
      @{ Name='StrictMode'; Bit=2 }) },
  @{ Name = 'Signature';              Index = 8;  Size = 4; Bits = @(
      @{ Name='MicrosoftSignedOnly'; Bit=0 }, @{ Name='StoreSignedOnly'; Bit=1 },
      @{ Name='MitigationOptIn'; Bit=2 }, @{ Name='AuditMicrosoftSignedOnly'; Bit=3 },
      @{ Name='AuditStoreSignedOnly'; Bit=4 }, @{ Name='AuditMitigationOptIn'; Bit=5 }) },
  @{ Name = 'FontDisable';            Index = 9;  Size = 4; Bits = @(
      @{ Name='DisableNonSystemFonts'; Bit=0 }) },
  @{ Name = 'ImageLoad';              Index = 10; Size = 4; Bits = @(
      @{ Name='NoRemoteImages'; Bit=0 }, @{ Name='NoLowMandatoryLabelImages'; Bit=1 },
      @{ Name='PreferSystem32Images'; Bit=2 }, @{ Name='AuditNoRemoteImages'; Bit=3 },
      @{ Name='AuditNoLowMandatoryLabelImages'; Bit=4 }) },
  @{ Name = 'SystemCallFilter';       Index = 11; Size = 4; Bits = @() },
  @{ Name = 'PayloadRestriction';     Index = 12; Size = 4; Bits = @(
      @{ Name='EnableExportAddressFilter'; Bit=0 }, @{ Name='AuditExportAddressFilter'; Bit=1 },
      @{ Name='EnableExportAddressFilterPlus'; Bit=2 }, @{ Name='AuditExportAddressFilterPlus'; Bit=3 },
      @{ Name='EnableImportAddressFilter'; Bit=4 }, @{ Name='AuditImportAddressFilter'; Bit=5 },
      @{ Name='EnableRopStackPivot'; Bit=6 }, @{ Name='AuditRopStackPivot'; Bit=7 },
      @{ Name='EnableRopCallerCheck'; Bit=8 }, @{ Name='AuditRopCallerCheck'; Bit=9 },
      @{ Name='EnableRopSimExec'; Bit=10 }, @{ Name='AuditRopSimExec'; Bit=11 }) },
  @{ Name = 'ChildProcess';           Index = 13; Size = 4; Bits = @(
      @{ Name='NoChildProcessCreation'; Bit=0 }, @{ Name='AuditNoChildProcessCreation'; Bit=1 },
      @{ Name='AllowSecureProcessCreation'; Bit=2 }) },
  @{ Name = 'SideChannelIsolation';   Index = 14; Size = 4; Bits = @(
      @{ Name='SmtBranchTargetIsolation'; Bit=0 }, @{ Name='IsolateSecurityDomain'; Bit=1 },
      @{ Name='DisablePageCombine'; Bit=2 }, @{ Name='SpeculativeStoreBypassDisable'; Bit=3 },
      @{ Name='RestrictCoreSharing'; Bit=4 }) },
  @{ Name = 'UserShadowStack';        Index = 15; Size = 4; Bits = @(
      @{ Name='EnableUserShadowStack'; Bit=0 }, @{ Name='AuditUserShadowStack'; Bit=1 },
      @{ Name='SetContextIpValidation'; Bit=2 }, @{ Name='AuditSetContextIpValidation'; Bit=3 },
      @{ Name='EnableUserShadowStackStrictMode'; Bit=4 }, @{ Name='BlockNonCetBinaries'; Bit=5 },
      @{ Name='BlockNonCetBinariesNonEhcont'; Bit=6 }, @{ Name='AuditBlockNonCetBinaries'; Bit=7 }) },
  @{ Name = 'RedirectionTrust';       Index = 16; Size = 4; Bits = @(
      @{ Name='EnforceRedirectionTrust'; Bit=0 }, @{ Name='AuditRedirectionTrust'; Bit=1 }) },
  @{ Name = 'UserPointer';            Index = 17; Size = 4; Bits = @(
      @{ Name='BlockUserPointer'; Bit=0 }) }
)

function Invoke-PolicyQuery([int]$TargetPid, $Def) {
  $raw = [uint32]0
  $err = 0
  $rc = [RcSec.Mitigations]::Query($TargetPid, [int]$Def.Index, [ref]$raw, [ref]$err)
  if ($rc -ne 0) {
    return [ordered]@{
      status       = 'UNAVAILABLE'
      applied      = $null
      raw          = $null
      win32_error  = $err
      error_text   = (Get-Win32Text $err)
      decoded      = @{}
    }
  }
  $decoded = [ordered]@{}
  foreach ($b in $Def.Bits) {
    $decoded[$b.Name] = (($raw -band ([uint32](1 -shl $b.Bit))) -ne 0)
  }
  return [ordered]@{
    status      = 'OK'
    applied     = ($raw -ne 0)
    raw         = ('0x{0:X8}' -f $raw)
    win32_error = 0
    decoded     = $decoded
  }
}

function Get-PeInfo([string]$Path) {
  $result = [ordered]@{
    path = $Path
    exists = $false
    error = $null
    dll_characteristics = $null
    flags = @{}
    load_config = @{}
    sha256 = $null
  }
  if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $result }
  $result.exists = $true
  try {
    $result.sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
      $br = New-Object System.IO.BinaryReader($fs)
      $fs.Position = 0x3C
      $peOff = $br.ReadInt32()
      $fs.Position = $peOff
      if ($br.ReadUInt32() -ne 0x00004550) { throw 'not a PE file' }
      $coff = $peOff + 4
      $fs.Position = $coff + 2       # Machine
      $numSections = $br.ReadUInt16()
      $fs.Position = $coff + 16      # SizeOfOptionalHeader
      $optSize = $br.ReadUInt16()
      $opt = $coff + 20
      $fs.Position = $opt
      $magic = $br.ReadUInt16()
      $is64 = ($magic -eq 0x20B)
      $fs.Position = $opt + 70
      $dllChars = $br.ReadUInt16()
      $result.dll_characteristics = ('0x{0:X4}' -f $dllChars)
      $result.flags = [ordered]@{
        high_entropy_va   = (($dllChars -band 0x0020) -ne 0)
        dynamic_base      = (($dllChars -band 0x0040) -ne 0)
        force_integrity   = (($dllChars -band 0x0080) -ne 0)
        nx_compat         = (($dllChars -band 0x0100) -ne 0)
        no_isolation      = (($dllChars -band 0x0200) -ne 0)
        no_seh            = (($dllChars -band 0x0400) -ne 0)
        no_bind           = (($dllChars -band 0x0800) -ne 0)
        guard_cf          = (($dllChars -band 0x4000) -ne 0)
        terminal_aware    = (($dllChars -band 0x8000) -ne 0)
      }
      # Data directories: load config is index 10.
      $ddOff = $opt + $(if ($is64) { 112 } else { 96 })
      $fs.Position = $ddOff + (10 * 8)
      $lcRva = $br.ReadUInt32()
      $lcSize = $br.ReadUInt32()
      # Section table.
      $sections = @()
      $secOff = $opt + $optSize
      for ($i = 0; $i -lt $numSections; $i++) {
        $fs.Position = $secOff + ($i * 40) + 8
        $vsize = $br.ReadUInt32()
        $va = $br.ReadUInt32()
        $rawSize = $br.ReadUInt32()
        $rawPtr = $br.ReadUInt32()
        $sections += [pscustomobject]@{ VA = $va; VSize = $vsize; RawPtr = $rawPtr; RawSize = $rawSize }
      }
      $lcOffset = $null
      if ($lcRva -ne 0) {
        foreach ($s in $sections) {
          if ($lcRva -ge $s.VA -and $lcRva -lt ($s.VA + [Math]::Max($s.VSize, $s.RawSize))) {
            $lcOffset = $s.RawPtr + ($lcRva - $s.VA)
            break
          }
        }
      }
      if ($lcOffset -ne $null) {
        $fs.Position = $lcOffset
        $lc = New-Object byte[] 160
        [void]$fs.Read($lc, 0, $lc.Length)
        $guardFlags = [BitConverter]::ToUInt32($lc, 144)
        $sehTable = [BitConverter]::ToUInt64($lc, 96)
        $sehCount = [BitConverter]::ToUInt64($lc, 104)
        $securityCookie = [BitConverter]::ToUInt64($lc, 88)
        $result.load_config = [ordered]@{
          size                  = [BitConverter]::ToUInt32($lc, 0)
          seh_handler_table     = ('0x{0:X16}' -f $sehTable)
          seh_handler_count     = $sehCount
          sehop_metadata        = (($sehTable -ne 0) -and ($sehCount -ne 0))
          security_cookie       = ('0x{0:X16}' -f $securityCookie)
          guard_flags           = ('0x{0:X8}' -f $guardFlags)
          cfg_instrumented      = (($guardFlags -band 0x00000100) -ne 0)
          cfg_function_table    = (($guardFlags -band 0x00000400) -ne 0)
          cfg_export_suppression = (($guardFlags -band 0x00004000) -ne 0)
          cfg_enable_export_suppression = (($guardFlags -band 0x00008000) -ne 0)
        }
      }
    } finally { $fs.Dispose() }
  } catch {
    $result.error = $_.Exception.Message
  }
  return $result
}

function Get-SignatureInfo([string]$Path) {
  if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $null }
  try {
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    return [ordered]@{
      status = [string]$sig.Status
      signer = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { $null }
      not_after = if ($sig.SignerCertificate) { $sig.SignerCertificate.NotAfter.ToString('o') } else { $null }
    }
  } catch {
    return [ordered]@{ status = 'ERROR'; error = $_.Exception.Message }
  }
}

function Get-ProcessType([string]$CommandLine) {
  if ($CommandLine -match '--type=([a-zA-Z0-9_-]+)') {
    $t = $Matches[1]
    if ($t -eq 'utility') {
      if ($CommandLine -match '--utility-sub-type=([^\s"]+)') { return "utility:$($Matches[1])" }
      return 'utility'
    }
    return $t
  }
  return 'browser'
}

function Get-TargetProcesses {
  $result = @()
  if ($ProcessId.Count -gt 0) {
    foreach ($p in $ProcessId) {
      $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
      if ($proc) { $result += $proc }
    }
  }
  if ($ProcessName.Count -gt 0) {
    foreach ($n in $ProcessName) {
      $result += Get-CimInstance Win32_Process -Filter "Name='$n.exe'" -ErrorAction SilentlyContinue
    }
  }
  return $result | Sort-Object ProcessId -Unique
}

$targets = Get-TargetProcesses

if ($QuickCounters) {
  $quick = @()
  foreach ($t in $targets) {
    $il = $null; $isAc = -1; $ilErr = 0
    $il = [RcSec.Mitigations]::IntegrityLevel([int]$t.ProcessId, [ref]$isAc, [ref]$ilErr)
    $handles = [uint32]0; $gdi = [uint32]0; $user = [uint32]0; $cErr = 0
    $crc = [RcSec.Mitigations]::Counters([int]$t.ProcessId, [ref]$handles, [ref]$gdi, [ref]$user, [ref]$cErr)
    $quick += [ordered]@{
      pid = [int]$t.ProcessId
      type = (Get-ProcessType $t.CommandLine)
      integrity_level = $il
      app_container = $isAc
      counters = [ordered]@{
        status = if ($crc -eq 0) { 'OK' } else { 'UNAVAILABLE' }
        handle_count = if ($crc -eq 0) { [int]$handles } else { $null }
        gdi_objects = if ($crc -eq 0) { [int]$gdi } else { $null }
        user_objects = if ($crc -eq 0) { [int]$user } else { $null }
        win32_error = if ($crc -ne 0) { $cErr } else { 0 }
      }
    }
  }
  $qdoc = [ordered]@{ captured_at = (Get-Date).ToString('o'); quick_counters = $true; processes = $quick }
  $qjson = $qdoc | ConvertTo-Json -Depth 6
  if ($OutJson) {
    [System.IO.File]::WriteAllText($OutJson, $qjson, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "Wrote $OutJson ($($quick.Count) processes)"
  }
  $qjson
  return
}

$processes = @()
foreach ($t in $targets) {
  $type = Get-ProcessType $t.CommandLine
  $il = $null
  $isAc = -1
  $ilErr = 0
  $il = [RcSec.Mitigations]::IntegrityLevel([int]$t.ProcessId, [ref]$isAc, [ref]$ilErr)

  $handles = [uint32]0; $gdi = [uint32]0; $user = [uint32]0; $cErr = 0
  $crc = [RcSec.Mitigations]::Counters([int]$t.ProcessId, [ref]$handles, [ref]$gdi, [ref]$user, [ref]$cErr)

  $mitigations = [ordered]@{}
  foreach ($def in $PolicyDefs) {
    $mitigations[$def.Name] = Invoke-PolicyQuery -TargetPid ([int]$t.ProcessId) -Def $def
  }

  $exePath = $t.ExecutablePath
  $moduleCount = $null
  $installCount = $null
  $moduleError = $null
  try {
    $mods = (Get-Process -Id $t.ProcessId -ErrorAction Stop).Modules
    $moduleCount = $mods.Count
    $installDir = if ($exePath) { Split-Path -Parent $exePath } else { $null }
    if ($installDir) {
      $installCount = ($mods | Where-Object { $_.FileName -like "$installDir*" }).Count
    }
  } catch {
    $moduleError = $_.Exception.Message
  }

  $chromeDll = if ($exePath) { Join-Path (Split-Path -Parent $exePath) 'chrome.dll' } else { $null }

  $processes += [ordered]@{
    pid = [int]$t.ProcessId
    name = $t.Name
    type = $type
    command_line = $t.CommandLine
    executable_path = $exePath
    integrity_level = $il
    integrity_level_error = if ($ilErr -ne 0) { Get-Win32Text $ilErr } else { $null }
    app_container = $isAc
    counters = [ordered]@{
      status = if ($crc -eq 0) { 'OK' } else { 'UNAVAILABLE' }
      handle_count = if ($crc -eq 0) { [int]$handles } else { $null }
      gdi_objects = if ($crc -eq 0) { [int]$gdi } else { $null }
      user_objects = if ($crc -eq 0) { [int]$user } else { $null }
      win32_error = if ($crc -ne 0) { $cErr } else { 0 }
    }
    mitigations = $mitigations
    modules = [ordered]@{
      count = $moduleCount
      from_install_dir = $installCount
      error = $moduleError
    }
    exe_pe = Get-PeInfo $exePath
    chrome_dll_pe = Get-PeInfo $chromeDll
    exe_signature = Get-SignatureInfo $exePath
    chrome_dll_signature = Get-SignatureInfo $chromeDll
  }
}

$doc = [ordered]@{
  captured_at = (Get-Date).ToString('o')
  computer = $env:COMPUTERNAME
  windows = [System.Environment]::OSVersion.VersionString
  pshost = $PSVersionTable.PSVersion.ToString()
  processes = $processes
}
$json = $doc | ConvertTo-Json -Depth 10
if ($OutJson) {
  $dir = Split-Path -Parent $OutJson
  if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  [System.IO.File]::WriteAllText($OutJson, $json, (New-Object System.Text.UTF8Encoding($false)))
  Write-Output "Wrote $OutJson ($($processes.Count) processes)"
}
$json
