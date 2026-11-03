<#
.SYNOPSIS
  Launch Browser CLI / official Chrome / official Edge in isolated profiles and
  capture the effective process mitigation policies of every process in the
  instance.

.DESCRIPTION
  One scenario = one browser launch with an isolated --user-data-dir, a local
  HTTP page (to force renderer + network utility creation) and a DevTools
  endpoint. No --no-sandbox, no real user profile, no global process kill:
  only PIDs that are descendants of the root process we spawned are stopped.

  Output per scenario:
    <OutRoot>/<label>/launch.json        command line, root pid, exit code
    <OutRoot>/<label>/mitigations.json   process-mitigations.ps1 snapshot
    <OutRoot>/<label>/processes.txt      one line per probed process

  and <OutRoot>/index.json with the scenario list.

.PARAMETER OutRoot
  Evidence root. Default D:\c\logs\rc-sec.

.PARAMETER Only
  Optional scenario label filter (comma separated).
#>
[CmdletBinding()]
param(
  [string]$OutRoot = 'D:\c\logs\rc-sec',
  [string]$Only = ''
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$probe = Join-Path $here 'process-mitigations.ps1'
$bcli = 'D:\c\src\out\WinDev\chrome.exe'
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'

$scenarios = @(
  [ordered]@{ label = 'bcli-default';  exe = $bcli;   args = @(); headless = $true },
  [ordered]@{ label = 'bcli-inproc';   exe = $bcli;   args = @('--in-process-gpu'); headless = $true },
  [ordered]@{ label = 'bcli-single';   exe = $bcli;   args = @('--single-process'); headless = $true },
  [ordered]@{ label = 'bcli-dynacode'; exe = $bcli;   args = @('--enable-features=BrowserDynamicCodeDisabled'); headless = $true },
  [ordered]@{ label = 'bcli-testconfig'; exe = $bcli; args = @('--enable-field-trial-config'); headless = $true },
  [ordered]@{ label = 'bcli-netsandbox'; exe = $bcli; args = @('--enable-features=NetworkServiceSandbox'); headless = $true },
  [ordered]@{ label = 'chrome-default'; exe = $chrome; args = @(); headless = $true },
  [ordered]@{ label = 'chrome-dynacode'; exe = $chrome; args = @('--enable-features=BrowserDynamicCodeDisabled'); headless = $true },
  [ordered]@{ label = 'chrome-testconfig'; exe = $chrome; args = @('--enable-field-trial-config'); headless = $true },
  [ordered]@{ label = 'chrome-netsandbox'; exe = $chrome; args = @('--enable-features=NetworkServiceSandbox'); headless = $true },
  [ordered]@{ label = 'edge-default';  exe = $edge;   args = @(); headless = $true }
)
if ($Only) {
  $wanted = $Only.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }
  $scenarios = $scenarios | Where-Object { $wanted -contains $_.label }
}

function New-IsolatedDir([string]$Label) {
  $dir = Join-Path $OutRoot $Label
  if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
  New-Item -ItemType Directory -Path $dir -Force | Out-Null
  return $dir
}

# A tiny local page server so the network utility process is actually spawned.
$pageDir = Join-Path $OutRoot '_page'
if (-not (Test-Path -LiteralPath $pageDir)) { New-Item -ItemType Directory -Path $pageDir -Force | Out-Null }
$pagePath = Join-Path $pageDir 'index.html'
@'
<!doctype html><meta charset="utf-8"><title>rc-sec</title>
<body style="background:#1e88e5"><h1 style="color:#fff">rc-sec mitigation probe</h1></body>
'@ | Set-Content -LiteralPath $pagePath -Encoding UTF8

function Start-PageServer([string]$Dir) {
  $tcp = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
  $tcp.Start()
  $port = ($tcp.LocalEndpoint).Port
  $tcp.Stop()
  $python = (Get-Command python -ErrorAction Stop).Source
  $proc = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'http.server', "$port", '--bind', '127.0.0.1', '--directory', $Dir) `
    -WindowStyle Hidden -PassThru
  Start-Sleep -Milliseconds 700
  return [pscustomobject]@{ Proc = $proc; Port = $port }
}

function Stop-PageServer($Server) {
  try {
    if ($Server.Proc -and -not $Server.Proc.HasExited) {
      Stop-Process -Id $Server.Proc.Id -Force -ErrorAction SilentlyContinue
    }
  } catch {}
}

function Get-ProcessTable {
  return Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine, ExecutablePath, CreationDate
}

function Get-Descendants([int]$RootPid, $Table) {
  $byParent = @{}
  foreach ($p in $Table) {
    $pp = [int]$p.ParentProcessId
    if (-not $byParent.ContainsKey($pp)) { $byParent[$pp] = @() }
    $byParent[$pp] += $p
  }
  $out = New-Object System.Collections.ArrayList
  $queue = New-Object System.Collections.Queue
  $queue.Enqueue($RootPid)
  $seen = @{}
  $seen[$RootPid] = $true
  while ($queue.Count -gt 0) {
    $cur = [int]$queue.Dequeue()
    if (-not $byParent.ContainsKey($cur)) { continue }
    foreach ($child in $byParent[$cur]) {
      $cid = [int]$child.ProcessId
      if ($seen.ContainsKey($cid)) { continue }
      $seen[$cid] = $true
      [void]$out.Add($child)
      $queue.Enqueue($cid)
    }
  }
  return $out
}

function Get-ProcType([string]$Cmd) {
  if ($Cmd -match '--type=([a-zA-Z0-9_-]+)') {
    $t = $Matches[1]
    if ($t -eq 'utility' -and $Cmd -match '--utility-sub-type=([^\s"]+)') { return "utility:$($Matches[1])" }
    return $t
  }
  return 'browser'
}

# Returns a snapshot taken while the root process was still alive. Never uses a
# process table fetched after the root died (PIDs can be recycled to unrelated
# processes); callers must only act on this returned table.
function Wait-ForInstance([int]$RootPid, [int]$TimeoutSec, [string]$ExePath, [datetime]$LaunchTime, [switch]$Fast) {
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $lastGood = $null
  while ((Get-Date) -lt $deadline) {
    $table = Get-ProcessTable
    $root = $table | Where-Object { $_.ProcessId -eq $RootPid }
    if (-not $root) { break }
    $lastGood = $table
    if ($Fast) {
      Start-Sleep -Milliseconds 900
      $lastGood = Get-ProcessTable
      if ($lastGood | Where-Object { $_.ProcessId -eq $RootPid }) { break }
      $lastGood = $null
      break
    }
    $desc = @(Get-Descendants $RootPid $table)
    $types = $desc | ForEach-Object { Get-ProcType $_.CommandLine }
    if (($types -contains 'renderer') -and ($types | Where-Object { $_ -like 'gpu*' })) {
      # Let late children (network/storage utility) reach their post-sandbox
      # state; the sandbox applies delayed mitigations after process start.
      Start-Sleep -Seconds 4
      $lastGood = Get-ProcessTable
      if (-not ($lastGood | Where-Object { $_.ProcessId -eq $RootPid })) { $lastGood = $null }
      break
    }
    Start-Sleep -Milliseconds 500
  }
  return $lastGood
}

# Select only processes that belong to this launch: descendants of the root in
# a table where the root was alive, created at/after launch, and running the
# same executable image (or crashpad_handler.exe from the same install dir).
function Select-InstanceProcesses($Table, [int]$RootPid, [string]$ExePath, [datetime]$LaunchTime) {
  if (-not $Table) { return @() }
  $exeDir = Split-Path -Parent $ExePath
  $root = $Table | Where-Object { $_.ProcessId -eq $RootPid }
  if (-not $root) { return @() }
  $desc = @(Get-Descendants $RootPid $Table)
  $allowed = @($root) + $desc
  $result = @()
  foreach ($p in $allowed) {
    $path = [string]$p.ExecutablePath
    $inDir = $path -and $exeDir -and ($path.StartsWith($exeDir, [System.StringComparison]::OrdinalIgnoreCase))
    if (-not $inDir) { continue }
    if ($p.CreationDate -and ([datetime]$p.CreationDate) -lt $LaunchTime.AddSeconds(-2)) { continue }
    $result += $p
  }
  return $result | Sort-Object ProcessId -Unique
}

$index = @()
foreach ($sc in $scenarios) {
  $label = $sc.label
  $dir = New-IsolatedDir $label
  $profile = Join-Path $dir 'profile'
  New-Item -ItemType Directory -Path $profile -Force | Out-Null
  $server = Start-PageServer $pageDir
  Start-Sleep -Milliseconds 300
  $url = "http://127.0.0.1:$($server.Port)/index.html"

  $logFile = Join-Path $dir 'chrome.log'
  $argv = @(
    '--headless=new',
    "--user-data-dir=$profile",
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-sync',
    '--disable-component-update',
    '--disable-background-networking',
    '--remote-debugging-port=0',
    '--window-size=900,700',
    '--noerrdialogs',
    '--enable-logging',
    "--log-file=$logFile",
    '--disable-features=Translate,MediaRouter,OptimizationHints'
  ) + @($sc.args) + @($url)

  $launch = [ordered]@{
    label = $label
    exe = $sc.exe
    args = $argv
    started_at = (Get-Date).ToString('o')
    root_pid = $null
    exit_code = $null
    exit_after_seconds = $null
    process_count = 0
    types = @()
    error = $null
  }

  try {
    $launchTime = Get-Date
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $sc.exe
    $psi.Arguments = (($argv | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    $launch.root_pid = $p.Id
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $fast = @($sc.args) -contains '--single-process'
    $snapshotTable = Wait-ForInstance -RootPid $p.Id -TimeoutSec 25 -ExePath $sc.exe -LaunchTime $launchTime -Fast:$fast
    $targets = @(Select-InstanceProcesses $snapshotTable $p.Id $sc.exe $launchTime)
    $targets = @($targets | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    $targetIds = @($targets | ForEach-Object { [int]$_.ProcessId })

    $launch.types = @($targets | ForEach-Object { Get-ProcType $_.CommandLine } | Sort-Object -Unique)
    $launch.process_count = $targetIds.Count
    $outJson = Join-Path $dir 'mitigations.json'
    if ($targetIds.Count -gt 0) {
      & $probe -ProcessId $targetIds -OutJson $outJson | Out-Null
    }
    $lines = $targets | ForEach-Object { "{0}`t{1}`t{2}`t{3}" -f $_.ProcessId, (Get-ProcType $_.CommandLine), $_.Name, $_.ExecutablePath }
    $lines | Set-Content -LiteralPath (Join-Path $dir 'processes.txt') -Encoding UTF8

    # Stop only this instance: root first, then any surviving descendants, and
    # re-verify image path + creation time so a recycled PID is never touched.
    if (-not $p.HasExited) {
      try { $p.Kill() } catch {}
      [void]$p.WaitForExit(8000)
    }
    $p.Refresh()
    if ($p.HasExited) {
      $launch.exit_code = $p.ExitCode
      $launch.exit_after_seconds = [Math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
    $post = Get-ProcessTable
    foreach ($t in $targets) {
      if ($t.ProcessId -eq $p.Id) { continue }
      $still = $post | Where-Object {
        $_.ProcessId -eq $t.ProcessId -and
        $_.ExecutablePath -eq $t.ExecutablePath -and
        ([datetime]$_.CreationDate) -eq ([datetime]$t.CreationDate)
      }
      if ($still) {
        try { Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
      }
    }
    try { [System.IO.File]::WriteAllText((Join-Path $dir 'stdout.txt'), $outTask.Result, (New-Object System.Text.UTF8Encoding($false))) } catch {}
    try {
      $errText = $errTask.Result
      [System.IO.File]::WriteAllText((Join-Path $dir 'stderr.txt'), $errText, (New-Object System.Text.UTF8Encoding($false)))
      $launch.stderr_excerpt = if ($errText.Length -gt 1200) { $errText.Substring($errText.Length - 1200) } else { $errText }
    } catch {}
  } catch {
    $launch.error = $_.Exception.Message
  } finally {
    Stop-PageServer $server
  }

  [System.IO.File]::WriteAllText(
    (Join-Path $dir 'launch.json'),
    ($launch | ConvertTo-Json -Depth 6),
    (New-Object System.Text.UTF8Encoding($false)))
  $index += $launch
  Write-Output ("[{0}] root={1} procs={2} types={3} exit={4} error={5}" -f `
      $label, $launch.root_pid, $launch.process_count, ($launch.types -join ','), $launch.exit_code, $launch.error)
}

$indexDoc = [ordered]@{ out_root = $OutRoot; captured_at = (Get-Date).ToString('o'); scenarios = $index }
[System.IO.File]::WriteAllText(
  (Join-Path $OutRoot 'index.json'),
  ($indexDoc | ConvertTo-Json -Depth 8),
  (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Wrote $OutRoot\index.json"
