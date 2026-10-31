param(
  [Parameter(Mandatory=$true)][long]$Hwnd
)
$ErrorActionPreference = "Stop"
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class GpuBSendBottom {
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
}
"@
$h = [IntPtr]$Hwnd
if (-not [GpuBSendBottom]::IsWindow($h)) { throw "not a window: $Hwnd" }
# HWND_BOTTOM = 1; SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_NOOWNERZORDER.
$ok = [GpuBSendBottom]::SetWindowPos($h, [IntPtr]::new(1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0010 -bor 0x0200)
Write-Output ("sent_to_bottom=" + $ok)
