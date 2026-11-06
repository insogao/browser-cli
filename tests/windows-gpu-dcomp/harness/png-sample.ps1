param(
  [Parameter(Mandatory=$true)][string]$Path,
  [Parameter(Mandatory=$true)][string]$OutJson
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
$bmp = [System.Drawing.Bitmap]::FromFile((Resolve-Path $Path))
try {
  $points = @()
  $unique = @{}
  $saturated = 0
  $dark = 0
  $samples = 0
  for ($iy = 0; $iy -lt 5; $iy++) {
    for ($ix = 0; $ix -lt 5; $ix++) {
      $fx = 0.10 + 0.20 * $ix
      $fy = 0.12 + 0.20 * $iy
      $x = [int]([math]::Min($bmp.Width - 1, [math]::Max(0, $fx * $bmp.Width)))
      $y = [int]([math]::Min($bmp.Height - 1, [math]::Max(0, $fy * $bmp.Height)))
      $c = $bmp.GetPixel($x, $y)
      $key = ('{0:X2}{1:X2}{2:X2}' -f $c.R, $c.G, $c.B)
      $unique[$key] = 1
      $mx = [math]::Max($c.R, [math]::Max($c.G, $c.B))
      $mn = [math]::Min($c.R, [math]::Min($c.G, $c.B))
      if (($mx - $mn) -ge 80) { $saturated++ }
      if ($c.R -lt 40 -and $c.G -lt 40 -and $c.B -lt 40) { $dark++ }
      $samples++
      $points += [pscustomobject]@{ name = ("p{0}{1}" -f $iy, $ix); x = $x; y = $y; r = $c.R; g = $c.G; b = $c.B; hex = ("#" + $key) }
    }
  }
  # Coarse histogram over the whole image (step 8 px).
  $hist = @{}
  $darkPixels = 0
  $total = 0
  for ($y = 0; $y -lt $bmp.Height; $y += 8) {
    for ($x = 0; $x -lt $bmp.Width; $x += 8) {
      $c = $bmp.GetPixel($x, $y)
      $q = (($c.R -shr 3) -shl 10) -bor (($c.G -shr 3) -shl 5) -bor ($c.B -shr 3)
      if ($hist.ContainsKey($q)) { $hist[$q]++ } else { $hist[$q] = 1 }
      if ($c.R -lt 40 -and $c.G -lt 40 -and $c.B -lt 40) { $darkPixels++ }
      $total++
    }
  }
  $top = $hist.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 8 | ForEach-Object {
    $q = $_.Key
    $r = (($q -shr 10) -band 0x1F) -shl 3
    $g = (($q -shr 5) -band 0x1F) -shl 3
    $b = ($q -band 0x1F) -shl 3
    ('#{0:X2}{1:X2}{2:X2}:{3}' -f $r, $g, $b, $_.Value)
  }
  $result = [pscustomobject]@{
    path = (Resolve-Path $Path).Path
    width = $bmp.Width
    height = $bmp.Height
    sample_points = $points
    sample_unique_colors = $unique.Count
    sample_saturated = $saturated
    sample_dark = $dark
    sample_total = $samples
    unique_quantized_colors = $hist.Count
    dark_pixels = $darkPixels
    total_pixels = $total
    all_dark = ($darkPixels -eq $total)
    top_colors = $top
    colorful = (($unique.Count -ge 4) -and ($saturated -ge 3) -and (-not ($darkPixels -eq $total)))
  }
  $result | ConvertTo-Json -Depth 6 | Set-Content -Path $OutJson -Encoding UTF8
  $result | ConvertTo-Json -Depth 6
} finally {
  $bmp.Dispose()
}
