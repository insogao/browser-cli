<#
.SYNOPSIS
  Watch the browser-process Dynamic Code (ACG) policy from launch to exit.

.DESCRIPTION
  On this host, applying ACG to the browser process makes the installed Sogou
  TSF IME terminate the browser with exit code 3 before a normal mitigation
  snapshot can be taken. This script polls
  GetProcessMitigationPolicy(ProcessDynamicCodePolicy) every 10 ms from the
  moment the process is created, so the applied value is captured even when the
  process exits ~0.3 s later.

  Cases:
    bcli-default        control, ACG expected 0
    bcli-dynacode       --enable-features=BrowserDynamicCodeDisabled
    bcli-testconfig     --enable-field-trial-config (config excluded at compile
                        time in this build)
    chrome-default      control, ACG expected 0
    chrome-dynacode     --enable-features=BrowserDynamicCodeDisabled
    chrome-testconfig   --enable-field-trial-config (config compiled in; applies
                        BrowserDynamicCodeDisabled)

  No --no-sandbox, isolated profile per case, only the spawned process is
  terminated (after the watch window).
#>
[CmdletBinding()]
param(
  [string]$OutRoot = 'D:\c\logs\rc-sec\acg',
  [int]$WatchMs = 4000
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$probe = Join-Path $here 'process-mitigations.ps1'

# Load the C# helper (dot-sourcing the probe runs its main body only if
# parameters are supplied; call it with a non-matching name to no-op).
$null = & $probe -ProcessName '__rc_sec_none__'

$bcli = 'D:\c\src\out\WinDev\chrome.exe'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$cases = @(
  @{ label = 'bcli-default';    exe = $bcli;   args = @() },
  @{ label = 'bcli-dynacode';   exe = $bcli;   args = @('--enable-features=BrowserDynamicCodeDisabled') },
  @{ label = 'bcli-testconfig'; exe = $bcli;   args = @('--enable-field-trial-config') },
  @{ label = 'chrome-default';  exe = $chrome; args = @() },
  @{ label = 'chrome-dynacode'; exe = $chrome; args = @('--enable-features=BrowserDynamicCodeDisabled') },
  @{ label = 'chrome-testconfig'; exe = $chrome; args = @('--enable-field-trial-config') }
)

function Quote-Arg([string]$a) { return '"' + ($a -replace '"', '\"') + '"' }

if (-not (Test-Path -LiteralPath $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot -Force | Out-Null }

$results = @()
foreach ($c in $cases) {
  $dir = Join-Path $OutRoot $c.label
  if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
  New-Item -ItemType Directory -Path $dir -Force | Out-Null
  $profile = Join-Path $dir 'profile'
  $argv = @(
    '--headless=new',
    "--user-data-dir=$profile",
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--disable-component-update',
    '--disable-sync',
    '--noerrdialogs',
    '--enable-logging=stderr'
  ) + @($c.args) + @('about:blank')

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $c.exe
  $psi.Arguments = (($argv | ForEach-Object { Quote-Arg $_ }) -join ' ')
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $false
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true
  $p = New-Object System.Diagnostics.Process
  $p.StartInfo = $psi
  [void]$p.Start()
  $errTask = $p.StandardError.ReadToEndAsync()

  $raw = [uint32]0
  $firstMs = -1
  $firstOkMs = -1
  $samples = 0
  $lastErr = 0
  $exitCode = 0
  $rc = [RcSec.Mitigations]::Watch(
    $p.Id, 2, $WatchMs, [ref]$raw, [ref]$firstMs, [ref]$firstOkMs,
    [ref]$samples, [ref]$lastErr, [ref]$exitCode)

  $exited = $p.WaitForExit(2000)
  if (-not $exited) {
    try { $p.Kill() } catch {}
    [void]$p.WaitForExit(3000)
  }
  $p.Refresh()
  $errText = ''
  try { $errText = $errTask.Result } catch {}
  try { [System.IO.File]::WriteAllText((Join-Path $dir 'stderr.txt'), $errText, (New-Object System.Text.UTF8Encoding($false))) } catch {}

  $decoded = [ordered]@{
    ProhibitDynamicCode = (($raw -band 1) -ne 0)
    AllowThreadOptOut   = (($raw -band 2) -ne 0)
    AllowRemoteDowngrade = (($raw -band 4) -ne 0)
  }
  $row = [ordered]@{
    label = $c.label
    exe = $c.exe
    extra_args = $c.args
    pid = $p.Id
    acg_first_nonzero_raw = ('0x{0:X8}' -f $raw)
    acg_first_nonzero_decoded = $decoded
    acg_first_nonzero_ms = $firstMs
    query_samples = $samples
    first_successful_query_ms = $firstOkMs
    last_query_error = $lastErr
    watch_exit_code = $exitCode
    process_exit_code = if ($p.HasExited) { $p.ExitCode } else { $null }
    stderr_excerpt = if ($errText.Length -gt 600) { $errText.Substring($errText.Length - 600) } else { $errText }
  }
  $results += $row
  Write-Output ("[{0}] acg={1} firstNonZeroMs={2} samples={3} watchExit={4} processExit={5}" -f `
      $c.label, $row.acg_first_nonzero_raw, $firstMs, $samples, $exitCode, $row.process_exit_code)
}

$doc = [ordered]@{ captured_at = (Get-Date).ToString('o'); watch_ms = $WatchMs; cases = $results }
[System.IO.File]::WriteAllText(
  (Join-Path $OutRoot 'acg-window.json'),
  ($doc | ConvertTo-Json -Depth 8),
  (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Wrote $(Join-Path $OutRoot 'acg-window.json')"
