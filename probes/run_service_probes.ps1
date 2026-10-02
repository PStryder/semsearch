# Runs service_probe.py as a Windows service under each candidate identity. Must run elevated.
# Writes C:\ProgramData\SemSearchProbe\report-<tag>.json per identity and a transcript.
$ErrorActionPreference = 'Continue'
$base = 'C:\ProgramData\SemSearchProbe'
$py = 'C:\ProgramData\SemSearchProbe\venv\Scripts\python.exe'
$probe = 'F:\HexyLab\semsearch\probes\service_probe.py'
Start-Transcript -Path "$base\transcript.txt" -Force | Out-Null
"python: $py"
icacls $base /grant 'Users:(OI)(CI)M' /T /Q | Out-Null   # every identity can write its report

$identities = @(
  @{ tag = 'LocalSystem';    obj = 'LocalSystem' },
  @{ tag = 'LocalService';   obj = 'NT AUTHORITY\LocalService' },
  @{ tag = 'NetworkService'; obj = 'NT AUTHORITY\NetworkService' },
  @{ tag = 'VirtualAccount'; obj = 'NT SERVICE\SemSearchProbe' }
)
foreach ($id in $identities) {
  $tag = $id.tag
  "=== $tag ($($id.obj)) ==="
  Set-Content -Path "$base\tag.txt" -Value $tag -Encoding ascii
  Remove-Item "$base\report-$tag.json" -ErrorAction SilentlyContinue
  sc.exe stop SemSearchProbe 2>$null | Out-Null
  Start-Sleep 1
  sc.exe delete SemSearchProbe 2>$null | Out-Null
  Start-Sleep 1
  $bin = "`"$py`" `"$probe`""
  $r = sc.exe create SemSearchProbe binPath= $bin start= demand obj= $($id.obj) DisplayName= "SemSearch identity probe"
  "create: $r"
  if ($id.obj -eq 'NT SERVICE\SemSearchProbe') {
    # the virtual account exists only after creation; give it the same read access a real install would grant on its roots
    icacls 'F:\HexyLab\semsearch' /grant 'NT SERVICE\SemSearchProbe:(OI)(CI)RX' /T /Q | Out-Null
    icacls $base /grant 'NT SERVICE\SemSearchProbe:(OI)(CI)M' /T /Q | Out-Null
  }
  $s = sc.exe start SemSearchProbe
  "start: $($s -join ' ')"
  $deadline = (Get-Date).AddMinutes(3)
  while ((Get-Date) -lt $deadline -and -not (Test-Path "$base\report-$tag.json")) { Start-Sleep 2 }
  if (Test-Path "$base\report-$tag.json") { "report written" } else { "NO REPORT (service failed to run?)"; sc.exe query SemSearchProbe; Get-WinEvent -LogName Application -MaxEvents 5 | Where-Object { $_.ProviderName -like '*SemSearch*' -or $_.ProviderName -eq 'Service Control Manager' } | ForEach-Object { $_.TimeCreated.ToString() + ' ' + $_.Message.Substring(0, [Math]::Min(300, $_.Message.Length)) } }
  sc.exe stop SemSearchProbe 2>$null | Out-Null
  Start-Sleep 2
  sc.exe delete SemSearchProbe | Out-Null
}
icacls 'F:\HexyLab\semsearch' /remove 'NT SERVICE\SemSearchProbe' /T /Q | Out-Null
"done"
Stop-Transcript | Out-Null
