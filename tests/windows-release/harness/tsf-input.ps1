<#
.SYNOPSIS
  Bring the harness-owned browser window to the foreground and send real
  keystrokes through the OS input queue (WScript.Shell.SendKeys), which the
  active TSF/IME sees.

.DESCRIPTION
  Test-only, Windows-only. It never starts or stops a process, never injects
  text into the page, and never touches a window belonging to another PID.
  Callers must have verified that the PID is the browser they launched.

  Actions:
    foreground  enumerate visible top-level windows of -BrowserPid, make the
                first titled one foreground (AttachThreadInput fallback),
                remember the previously foreground HWND in -StateFile;
    sendkeys    WScript.Shell.SendKeys(-Text) to the current foreground window;
    restore     put the remembered HWND back to the foreground.

  Real IME evidence requires a real key path; CDP Input.insertText,
  Runtime.evaluate value writes and clipboard paste are all bypasses and are
  never used by the caller.

.PARAMETER BrowserPid
  PID of the browser process launched by the harness.

.PARAMETER Action
  foreground | sendkeys | restore.

.PARAMETER Text
  SendKeys syntax for the sendkeys action ("nihao", " ", "{BACKSPACE}").

.PARAMETER StateFile
  JSON file used to remember the previous foreground HWND.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][int]$BrowserPid,
  [Parameter(Mandatory = $true)][ValidateSet('foreground', 'sendkeys', 'restore')][string]$Action,
  [string]$Text = "",
  [string]$StateFile = ""
)

$ErrorActionPreference = 'Stop'

if (-not ([System.Management.Automation.PSTypeName]'TsfInputWin32').Type) {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace TsfInputWin32 {
  public static class W {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr hWnd, StringBuilder text, int max);

    public static List<IntPtr> TopLevel(uint wantPid, bool visibleOnly) {
      List<IntPtr> list = new List<IntPtr>();
      EnumWindows(delegate(IntPtr h, IntPtr l) {
        uint pid; GetWindowThreadProcessId(h, out pid);
        if (pid == wantPid && (!visibleOnly || IsWindowVisible(h))) list.Add(h);
        return true;
      }, IntPtr.Zero);
      return list;
    }
    public static string Text(IntPtr h) { StringBuilder s = new StringBuilder(512); GetWindowTextW(h, s, 512); return s.ToString(); }
    public static string Cls(IntPtr h) { StringBuilder s = new StringBuilder(256); GetClassNameW(h, s, 256); return s.ToString(); }
  }
}
'@
}

function Save-State($obj) {
  if (-not $StateFile) { return }
  $dir = Split-Path -Parent $StateFile
  if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  [System.IO.File]::WriteAllText($StateFile, ($obj | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
}
function Load-State() {
  if ($StateFile -and (Test-Path -LiteralPath $StateFile)) {
    return Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
  }
  return $null
}

switch ($Action) {
  'foreground' {
    $wins = @([TsfInputWin32.W]::TopLevel([uint32]$BrowserPid, $true) |
      Where-Object { ([TsfInputWin32.W]::Text($_)).Trim().Length -gt 0 })
    if ($wins.Count -eq 0) { throw "no visible titled top-level window for pid $BrowserPid" }
    $target = $wins[0]
    $previous = [TsfInputWin32.W]::GetForegroundWindow()
    Save-State ([ordered]@{ previousForeground = $previous.ToInt64(); target = $target.ToInt64(); pid = $BrowserPid })
    $fgOk = [TsfInputWin32.W]::SetForegroundWindow($target)
    if (-not $fgOk) {
      $tmpPid = [uint32]$BrowserPid
      $targetThread = [TsfInputWin32.W]::GetWindowThreadProcessId($target, [ref]$tmpPid)
      $selfThread = [TsfInputWin32.W]::GetCurrentThreadId()
      [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $targetThread, $true)
      $fgOk = [TsfInputWin32.W]::SetForegroundWindow($target)
      [void][TsfInputWin32.W]::SetActiveWindow($target)
      [void][TsfInputWin32.W]::BringWindowToTop($target)
      [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $targetThread, $false)
    }
    Start-Sleep -Milliseconds 150
    $fg = [TsfInputWin32.W]::GetForegroundWindow()
    [ordered]@{
      ok = ($fg -eq $target)
      target = $target.ToInt64()
      previous = $previous.ToInt64()
      foreground = $fg.ToInt64()
      setForegroundReturned = [bool]$fgOk
      title = [TsfInputWin32.W]::Text($target)
      className = [TsfInputWin32.W]::Cls($target)
      candidates = @($wins | ForEach-Object { [ordered]@{ hwnd = $_.ToInt64(); title = [TsfInputWin32.W]::Text($_); className = [TsfInputWin32.W]::Cls($_) } })
    } | ConvertTo-Json -Depth 5
  }
  'sendkeys' {
    if (-not $Text) { throw "-Text is required for sendkeys" }
    $wshell = New-Object -ComObject WScript.Shell
    $wshell.SendKeys($Text)
    [ordered]@{ ok = $true; text = $Text; foreground = [TsfInputWin32.W]::GetForegroundWindow().ToInt64() } | ConvertTo-Json -Depth 3
  }
  'restore' {
    $state = Load-State
    if (-not $state -or -not $state.previousForeground) {
      [ordered]@{ ok = $false; reason = 'no-state' } | ConvertTo-Json -Depth 3
      return
    }
    $h = [IntPtr]([int64]$state.previousForeground)
    $ok = $false
    if ([TsfInputWin32.W]::IsWindow($h)) {
      $ok = [TsfInputWin32.W]::SetForegroundWindow($h)
    }
    [ordered]@{ ok = [bool]$ok; restored = $h.ToInt64(); foreground = [TsfInputWin32.W]::GetForegroundWindow().ToInt64() } | ConvertTo-Json -Depth 3
  }
}
