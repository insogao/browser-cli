$ErrorActionPreference = 'Stop'

# Enumerates the handles of the browser process found by --browser-cli-diag-target
# and prints (a) a type histogram and (b) the names of the interesting object
# types. Read-only: opens the target, duplicates handles for inspection only.
#
# Hangs are the classic failure mode of NtQueryObject(ObjectNameInformation) on
# File/Pipe handles, so names are queried only for a small allowlist of types.

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class H {
  [DllImport("ntdll.dll")] public static extern int NtQuerySystemInformation(int cls, IntPtr buf, int len, out int ret);
  [DllImport("ntdll.dll")] public static extern int NtQueryObject(IntPtr h, int cls, IntPtr buf, int len, out int ret);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint a, bool inh, int pid);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr h);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool DuplicateHandle(IntPtr src, IntPtr h, IntPtr dst, out IntPtr outH, uint access, bool inherit, uint opts);

  public const uint PROCESS_DUP_HANDLE = 0x0040;
  const int SystemExtendedHandleInformation = 64;

  public static byte[] AllHandles() {
    int len = 1 << 20;
    for (int i = 0; i < 12; i++) {
      IntPtr buf = Marshal.AllocHGlobal(len);
      int ret;
      int st = NtQuerySystemInformation(SystemExtendedHandleInformation, buf, len, out ret);
      if (st == 0) {
        byte[] copy = new byte[len];
        Marshal.Copy(buf, copy, 0, len);
        Marshal.FreeHGlobal(buf);
        return copy;
      }
      Marshal.FreeHGlobal(buf);
      len *= 2;
    }
    throw new Exception("NtQuerySystemInformation failed");
  }

  public static string TypeName(IntPtr h) {
    IntPtr buf = Marshal.AllocHGlobal(4096);
    try {
      int ret;
      if (NtQueryObject(h, 2 /*ObjectTypeInformation*/, buf, 4096, out ret) != 0) return "<err>";
      IntPtr namePtr = buf;                       // UNICODE_STRING at offset 0
      ushort len = (ushort)Marshal.ReadInt16(buf);
      IntPtr str = Marshal.ReadIntPtr(buf, 8);     // Buffer
      if (str == IntPtr.Zero || len == 0) return "<noname>";
      return Marshal.PtrToStringUni(str, len / 2);
    } finally { Marshal.FreeHGlobal(buf); }
  }

  public static string ObjectName(IntPtr h) {
    IntPtr buf = Marshal.AllocHGlobal(4096);
    try {
      int ret;
      if (NtQueryObject(h, 1 /*ObjectNameInformation*/, buf, 4096, out ret) != 0) return "";
      ushort len = (ushort)Marshal.ReadInt16(buf);
      IntPtr str = Marshal.ReadIntPtr(buf, 8);
      if (str == IntPtr.Zero || len == 0) return "";
      return Marshal.PtrToStringUni(str, len / 2);
    } finally { Marshal.FreeHGlobal(buf); }
  }

  public static IntPtr OpenTarget(int pid) { return OpenProcess(PROCESS_DUP_HANDLE, false, pid); }

  public static bool Dup(IntPtr proc, IntPtr h, out IntPtr outH) {
    return DuplicateHandle(proc, h, new IntPtr(-1), out outH, 0, false, 2 /*DUPLICATE_SAME_ACCESS*/);
  }
}
"@

$marker = '--browser-cli-diag-target=tsfprobe'
$proc = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" |
  Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) -and -not $_.CommandLine.Contains('--type=') } |
  Select-Object -First 1
if (-not $proc) { Write-Output 'HANDLES: target not found'; exit 1 }
$procId = $proc.ProcessId

$raw = [H]::AllHandles()
$count = [BitConverter]::ToInt64($raw, 0)
$entrySize = 40
$header = 16
$hproc = [H]::OpenTarget($procId)
if ($hproc -eq [IntPtr]::Zero) { Write-Output 'HANDLES: OpenProcess(PROCESS_DUP_HANDLE) failed'; exit 1 }

# Types whose names are safe to query (File/Pipe can block inside NtQueryObject).
$safe = @('ALPC Port','Event','Mutant','Section','Semaphore','Desktop','WindowStation',
          'Directory','SymbolicLink','Timer','Key','IoCompletion','Job','Token','Process','Thread')

$hist = @{}
$names = New-Object System.Collections.Generic.List[string]
try {
  for ($i = 0; $i -lt $count; $i++) {
    $off = $header + $i * $entrySize
    if ($off + $entrySize -gt $raw.Length) { break }
    $pidInEntry = [BitConverter]::ToInt64($raw, $off + 8)
    if ($pidInEntry -ne $procId) { continue }
    $handleVal = [IntPtr][BitConverter]::ToInt64($raw, $off + 16)
    $dup = [IntPtr]::Zero
    if (-not [H]::Dup($hproc, $handleVal, [ref]$dup)) { continue }
    try {
      $t = [H]::TypeName($dup)
      if (-not $hist.ContainsKey($t)) { $hist[$t] = 0 }
      $hist[$t] = $hist[$t] + 1
      if ($safe -contains $t -and $env:BCLI_NO_NAMES -ne "1") {
        $n = [H]::ObjectName($dup)
        if ($n) { $names.Add(("{0}={1}" -f $t, $n)) }
      }
    } finally { [void][H]::CloseHandle($dup) }
  }
} finally { [void][H]::CloseHandle($hproc) }

Write-Output "=== HANDLE HISTOGRAM pid=$procId total=$count ==="
foreach ($k in ($hist.Keys | Sort-Object)) { Write-Output ("  {0,-16} {1}" -f $k, $hist[$k]) }
Write-Output "=== NAMED OBJECTS (allowlisted types) ==="
foreach ($n in ($names | Sort-Object -Unique)) { Write-Output ("  " + $n) }
