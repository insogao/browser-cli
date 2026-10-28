$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class Tok {
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint a, bool inh, int pid);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr h);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool OpenProcessToken(IntPtr p, uint acc, out IntPtr tok);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern bool GetTokenInformation(IntPtr tok, int cls, IntPtr buf, int len, out int ret);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool IsProcessInJob(IntPtr p, IntPtr job, out bool result);
  // Returns PUCHAR (pointer to the count byte), NOT the count itself.
  [DllImport("advapi32.dll", SetLastError=true)] public static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
  [DllImport("advapi32.dll", SetLastError=true)] public static extern IntPtr GetSidSubAuthority(IntPtr sid, uint idx);

  const uint TOKEN_QUERY = 0x0008;
  const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

  // Fixed buffer: a size query is easy to get wrong (a zero length silently
  // produces a zero-byte allocation and the next read faults).
  const int BUFSZ = 4096;

  static IntPtr OpenTok(int pid, out IntPtr proc) {
    proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
    if (proc == IntPtr.Zero) return IntPtr.Zero;
    IntPtr tok;
    if (!OpenProcessToken(proc, TOKEN_QUERY, out tok)) { CloseHandle(proc); proc = IntPtr.Zero; return IntPtr.Zero; }
    return tok;
  }

  public static int IntegrityRid(int pid) {
    IntPtr proc, tok = OpenTok(pid, out proc);
    if (tok == IntPtr.Zero) return -1;
    IntPtr buf = Marshal.AllocHGlobal(BUFSZ);
    try {
      int len;
      if (!GetTokenInformation(tok, 25 /*TokenIntegrityLevel*/, buf, BUFSZ, out len)) return -2;
      IntPtr sid = Marshal.ReadIntPtr(buf);          // SID_AND_ATTRIBUTES.Sid
      if (sid == IntPtr.Zero) return -3;
      IntPtr cntPtr = GetSidSubAuthorityCount(sid);
      if (cntPtr == IntPtr.Zero) return -4;
      byte count = Marshal.ReadByte(cntPtr);
      if (count == 0) return -6;
      IntPtr sub = GetSidSubAuthority(sid, (uint)(count - 1));
      if (sub == IntPtr.Zero) return -5;
      return Marshal.ReadInt32(sub);
    } finally { Marshal.FreeHGlobal(buf); CloseHandle(tok); CloseHandle(proc); }
  }

  public static int CountEntries(int pid, int cls) {
    IntPtr proc, tok = OpenTok(pid, out proc);
    if (tok == IntPtr.Zero) return -1;
    IntPtr buf = Marshal.AllocHGlobal(BUFSZ);
    try {
      int len;
      if (!GetTokenInformation(tok, cls, buf, BUFSZ, out len)) return -2;
      return Marshal.ReadInt32(buf);                 // first DWORD is the count
    } finally { Marshal.FreeHGlobal(buf); CloseHandle(tok); CloseHandle(proc); }
  }

  public static int Dword(int pid, int cls) {
    IntPtr proc, tok = OpenTok(pid, out proc);
    if (tok == IntPtr.Zero) return -1;
    IntPtr buf = Marshal.AllocHGlobal(BUFSZ);
    try {
      int len;
      if (!GetTokenInformation(tok, cls, buf, BUFSZ, out len)) return -2;
      return Marshal.ReadInt32(buf);
    } finally { Marshal.FreeHGlobal(buf); CloseHandle(tok); CloseHandle(proc); }
  }

  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool LookupPrivilegeNameW(string system, ref long luid, System.Text.StringBuilder name, ref int size);

  // Returns "SeXxx=enabled,SeYyy=disabled,..." or an error marker.
  public static string PrivilegeList(int pid) {
    IntPtr proc, tok = OpenTok(pid, out proc);
    if (tok == IntPtr.Zero) return "<no-token>";
    IntPtr buf = Marshal.AllocHGlobal(BUFSZ);
    try {
      int len;
      if (!GetTokenInformation(tok, 3 /*TokenPrivileges*/, buf, BUFSZ, out len)) return "<query-failed>";
      int count = Marshal.ReadInt32(buf);
      var sb = new System.Text.StringBuilder();
      for (int i = 0; i < count; i++) {
        int baseOff = 4 + i * 12;
        if (baseOff + 12 > BUFSZ) break;
        long luid = Marshal.ReadInt64(buf, baseOff);
        int attrs = Marshal.ReadInt32(buf, baseOff + 8);
        int size = 128;
        var name = new System.Text.StringBuilder(size);
        bool enabled = (attrs & 2) != 0;   // SE_PRIVILEGE_ENABLED
        if (LookupPrivilegeNameW(null, ref luid, name, ref size)) {
          if (sb.Length > 0) sb.Append(",");
          sb.Append(name.ToString()).Append(enabled ? "=on" : "=off");
        }
      }
      return sb.ToString();
    } finally { Marshal.FreeHGlobal(buf); CloseHandle(tok); CloseHandle(proc); }
  }

  public static int InJob(int pid) {
    IntPtr p = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
    if (p == IntPtr.Zero) return -1;
    try { bool r; if (!IsProcessInJob(p, IntPtr.Zero, out r)) return -1; return r ? 1 : 0; }
    finally { CloseHandle(p); }
  }

  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool GetProcessMitigationPolicy(IntPtr h, int policy, ref ulong buf, IntPtr size);
  public static ulong Mitigation(int pid, int policy) {
    IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
    if (h == IntPtr.Zero) return ulong.MaxValue;
    try { ulong v = 0; if (!GetProcessMitigationPolicy(h, policy, ref v, (IntPtr)8)) return ulong.MaxValue; return v; }
    finally { CloseHandle(h); }
  }
}
"@

$marker = '--browser-cli-diag-target=tsfprobe'
$proc = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) -and -not $_.CommandLine.Contains('--type=') } |
  Select-Object -First 1
if (-not $proc) { Write-Output 'SNAPSHOT: target not found'; exit 1 }
$procId = $proc.ProcessId

$il = [Tok]::IntegrityRid($procId)
$ilName = switch ($il) {
  0x0000 { 'Untrusted' } 0x1000 { 'Low' } 0x2000 { 'Medium' } 0x2100 { 'MediumPlus' }
  0x3000 { 'High' } 0x4000 { 'System' } default { ('0x{0:X}' -f $il) }
}

Write-Output "=== SNAPSHOT pid=$procId ==="
Write-Output ("integrity_level      = {0} ({1})" -f $ilName, $il)
Write-Output ("app_container        = {0}  (0=no 1=yes)" -f ([Tok]::Dword($procId, 29)))
Write-Output ("elevation_type       = {0}  (0=default 1=full 2=limited)" -f ([Tok]::Dword($procId, 18)))
Write-Output ("restricted_sid_count = {0}" -f ([Tok]::CountEntries($procId, 11)))
Write-Output ("privileges           = {0}" -f ([Tok]::PrivilegeList($procId)))
Write-Output ("in_job               = {0}  (1=yes)" -f ([Tok]::InJob($procId)))
Write-Output ("session_id           = {0}" -f $proc.SessionId)

$policies = [ordered]@{ 'DEP'=0; 'ASLR'=1; 'DynamicCode'=2; 'StrictHandle'=3; 'SystemCallFilter'=4;
  'ExtensionPoint'=5; 'BinarySignature'=8; 'ImageLoad'=9; 'ControlFlowGuard'=11;
  'ChildProcess'=13; 'PayloadRestriction'=14 }
$parts = @()
foreach ($k in $policies.Keys) {
  $v = [Tok]::Mitigation($procId, $policies[$k])
  if ($v -ne [uint64]::MaxValue) { $parts += ('{0}=0x{1:X}' -f $k, $v) }
}
Write-Output ("mitigations_set      = {0}" -f ($(if ($parts.Count) { $parts -join ' ' } else { 'none reported' })))
