<#
.SYNOPSIS
  Bring the harness-owned browser window to the foreground and send real
  keystrokes through the OS input queue (WScript.Shell.SendKeys), which the
  active TSF/IME sees.

.DESCRIPTION
  Test-only, Windows-only. It never starts or stops a process, never injects
  text into the page, and never touches a window belonging to another PID.
  Callers must have verified that the PID is the browser they launched.

  Safety rule: sendkeys refuses to send anything unless the OS foreground
  window is the target browser's own top-level window. A failed foreground
  attempt must never leak keystrokes into whatever window the user had focused.

  Actions:
    foreground  make the browser's visible top-level window foreground using
                the AttachThreadInput/SetWindowPos sequence; remember the
                previously foreground HWND in -StateFile;
    check       report whether the browser's top-level window is currently the
                OS foreground window (no side effects);
    sendkeys    verify foreground, then WScript.Shell.SendKeys(-Text);
    restore     put the remembered HWND back to the foreground.

  Real IME evidence requires a real key path; CDP Input.insertText,
  Runtime.evaluate value writes and clipboard paste are all bypasses and are
  never used by the caller.

.PARAMETER BrowserPid
  PID of the browser process launched by the harness.

.PARAMETER Action
  foreground | check | sendkeys | restore.

.PARAMETER Text
  SendKeys syntax for the sendkeys action ("nihao", " ", "{BACKSPACE}").

.PARAMETER StateFile
  JSON file used to remember the previous foreground HWND.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][int]$BrowserPid,
  [Parameter(Mandatory = $true)][ValidateSet('foreground', 'check', 'sendkeys', 'restore')][string]$Action,
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
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr hWnd, StringBuilder text, int max);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr hWnd, bool altTab);

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
    public static uint ThreadOf(IntPtr h) {
      uint pid; return GetWindowThreadProcessId(h, out pid);
    }
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
function Get-TargetWindow() {
  $wins = @([TsfInputWin32.W]::TopLevel([uint32]$BrowserPid, $true) |
    Where-Object { ([TsfInputWin32.W]::Text($_)).Trim().Length -gt 0 })
  if ($wins.Count -eq 0) { return $null }
  return $wins[0]
}
function Test-IsForeground([IntPtr]$Target) {
  return ([TsfInputWin32.W]::GetForegroundWindow() -eq $Target)
}

switch ($Action) {
  'check' {
    $target = Get-TargetWindow
    $fg = [TsfInputWin32.W]::GetForegroundWindow()
    [ordered]@{
      ok = ($null -ne $target -and $fg -eq $target)
      target = $(if ($target) { $target.ToInt64() } else { $null })
      foreground = $fg.ToInt64()
      foregroundTitle = $(if ($fg -ne [IntPtr]::Zero) { [TsfInputWin32.W]::Text($fg) } else { $null })
      foregroundPid = $(if ($fg -ne [IntPtr]::Zero) { $p = [uint32]0; [void][TsfInputWin32.W]::GetWindowThreadProcessId($fg, [ref]$p); [int]$p } else { $null })
    } | ConvertTo-Json -Depth 4
  }
  'foreground' {
    $target = Get-TargetWindow
    if (-not $target) { throw "no visible titled top-level window for pid $BrowserPid" }
    $previous = [TsfInputWin32.W]::GetForegroundWindow()
    Save-State ([ordered]@{ previousForeground = $previous.ToInt64(); target = $target.ToInt64(); pid = $BrowserPid })

    $selfThread = [TsfInputWin32.W]::GetCurrentThreadId()
    $targetThread = [TsfInputWin32.W]::ThreadOf($target)
    $fgThread = $(if ($previous -ne [IntPtr]::Zero) { [TsfInputWin32.W]::ThreadOf($previous) } else { 0 })
    $attempts = @()
    $attached = @()
    try {
      if ($fgThread -ne 0 -and $fgThread -ne $selfThread) {
        [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $fgThread, $true); $attached += $fgThread
      }
      if ($targetThread -ne 0 -and $targetThread -ne $selfThread) {
        [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $targetThread, $true); $attached += $targetThread
      }
      if ([TsfInputWin32.W]::IsIconic($target)) { [void][TsfInputWin32.W]::ShowWindow($target, 9) } # SW_RESTORE
      [void][TsfInputWin32.W]::ShowWindow($target, 5)                                       # SW_SHOW
      # Bounce topmost so the window reaches the top of the z-order without
      # leaving it permanently topmost.
      [void][TsfInputWin32.W]::SetWindowPos($target, [IntPtr](-1), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) # TOPMOST|NOSIZE|NOMOVE|SHOWWINDOW
      [void][TsfInputWin32.W]::SetWindowPos($target, [IntPtr](-2), 0, 0, 0, 0, 0x0001 -bor 0x0002 -bor 0x0040) # NOTOPMOST
      [void][TsfInputWin32.W]::BringWindowToTop($target)
      [void][TsfInputWin32.W]::SetActiveWindow($target)
      $setFg = [TsfInputWin32.W]::SetForegroundWindow($target)
      $attempts += [ordered]@{ method = 'attach+show+zorder'; returned = [bool]$setFg }
      Start-Sleep -Milliseconds 150
      if (-not (Test-IsForeground $target)) {
        # ALT press/release unlocks the foreground lock in many cases.
        [TsfInputWin32.W]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
        [TsfInputWin32.W]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
        $setFg = [TsfInputWin32.W]::SetForegroundWindow($target)
        $attempts += [ordered]@{ method = 'alt-unlock'; returned = [bool]$setFg }
        Start-Sleep -Milliseconds 150
      }
      if (-not (Test-IsForeground $target)) {
        [TsfInputWin32.W]::SwitchToThisWindow($target, $true)
        $attempts += [ordered]@{ method = 'SwitchToThisWindow'; returned = $null }
        Start-Sleep -Milliseconds 200
      }
    } finally {
      foreach ($t in $attached) { [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $t, $false) }
    }
    $fg = [TsfInputWin32.W]::GetForegroundWindow()
    $winText = [TsfInputWin32.W]::Text($target)
    [ordered]@{
      ok = ($fg -eq $target)
      target = $target.ToInt64()
      previous = $previous.ToInt64()
      foreground = $fg.ToInt64()
      foregroundTitle = $(if ($fg -ne [IntPtr]::Zero) { [TsfInputWin32.W]::Text($fg) } else { $null })
      title = $winText
      className = [TsfInputWin32.W]::Cls($target)
      attempts = $attempts
    } | ConvertTo-Json -Depth 6
  }
  'sendkeys' {
    if (-not $Text) { throw "-Text is required for sendkeys" }
    $target = Get-TargetWindow
    if (-not $target) { throw "no visible titled top-level window for pid $BrowserPid" }
    if (-not (Test-IsForeground $target)) {
      # Fail closed: never leak keystrokes into an unrelated foreground window.
      [ordered]@{
        ok = $false
        reason = 'target-not-foreground'
        target = $target.ToInt64()
        foreground = [TsfInputWin32.W]::GetForegroundWindow().ToInt64()
        text = $Text
      } | ConvertTo-Json -Depth 4
      return
    }
    $wshell = New-Object -ComObject WScript.Shell
    $wshell.SendKeys($Text)
    [ordered]@{ ok = $true; text = $Text; foreground = [TsfInputWin32.W]::GetForegroundWindow().ToInt64(); target = $target.ToInt64() } | ConvertTo-Json -Depth 4
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
      $selfThread = [TsfInputWin32.W]::GetCurrentThreadId()
      $fgThread = [TsfInputWin32.W]::ThreadOf([TsfInputWin32.W]::GetForegroundWindow())
      if ($fgThread -ne 0 -and $fgThread -ne $selfThread) {
        [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $fgThread, $true)
      }
      $ok = [TsfInputWin32.W]::SetForegroundWindow($h)
      if ($fgThread -ne 0 -and $fgThread -ne $selfThread) {
        [void][TsfInputWin32.W]::AttachThreadInput($selfThread, $fgThread, $false)
      }
    }
    [ordered]@{ ok = [bool]$ok; restored = $h.ToInt64(); foreground = [TsfInputWin32.W]::GetForegroundWindow().ToInt64() } | ConvertTo-Json -Depth 3
  }
}
