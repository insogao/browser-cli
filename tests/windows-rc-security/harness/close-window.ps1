param(
  [Parameter(Mandatory=$true)][long]$Hwnd
)
$ErrorActionPreference = "Stop"
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class RcSecCloseWindow {
  [DllImport("user32.dll", SetLastError=true)]
  public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll")]
  public static extern bool IsWindow(IntPtr hWnd);
}
"@
$h = [IntPtr]$Hwnd
if (-not [RcSecCloseWindow]::IsWindow($h)) {
  Write-Output "not-a-window hwnd=$Hwnd"
  exit 1
}
# WM_CLOSE (0x0010). Never calls ShowWindow / SW_SHOW.
$ok = [RcSecCloseWindow]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
if (-not $ok) {
  $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
  Write-Output "post-failed hwnd=$Hwnd win32_error=$err"
  exit 1
}
Write-Output "wm-close-posted hwnd=$Hwnd"
