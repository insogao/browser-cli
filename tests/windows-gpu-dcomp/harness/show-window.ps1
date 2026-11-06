param(
  [Parameter(Mandatory=$true)][long]$Hwnd,
  [switch]$Activate,
  [switch]$Hide
)
$ErrorActionPreference = "Stop"
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class GpuBShow {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int X, int Y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
}
"@
$h = [IntPtr]$Hwnd
if (-not [GpuBShow]::IsWindow($h)) { throw "not a window: $Hwnd" }
if ($Hide) {
  [void][GpuBShow]::ShowWindow($h, 0)  # SW_HIDE
  Start-Sleep -Milliseconds 300
  [pscustomobject]@{
    visible = [GpuBShow]::IsWindowVisible($h)
    foreground = ([GpuBShow]::GetForegroundWindow() -eq $h)
    foreground_hwnd = [GpuBShow]::GetForegroundWindow().ToInt64()
    hidden = $true
  } | ConvertTo-Json -Compress
  return
}
[void][GpuBShow]::ShowWindow($h, 5)  # SW_SHOW
# SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_SHOWWINDOW; HWND_TOP.
$flags = 0x0001 -bor 0x0002 -bor 0x0010 -bor 0x0040
[void][GpuBShow]::SetWindowPos($h, [IntPtr]::Zero, 0, 0, 0, 0, $flags)
if ($Activate) {
  [void][GpuBShow]::BringWindowToTop($h)
  [void][GpuBShow]::SetForegroundWindow($h)
}
Start-Sleep -Milliseconds 300
[pscustomobject]@{
  visible = [GpuBShow]::IsWindowVisible($h)
  foreground = ([GpuBShow]::GetForegroundWindow() -eq $h)
  foreground_hwnd = [GpuBShow]::GetForegroundWindow().ToInt64()
  activated = [bool]$Activate
} | ConvertTo-Json -Compress
