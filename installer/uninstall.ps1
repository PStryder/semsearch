# Removes the SemSearch service and binaries. The index, configuration, state and logs under
# %ProgramData%\SemSearch are KEPT unless -PurgeData is given (and then you are asked to confirm).
param(
  [string]$InstallDir = "$env:ProgramFiles\SemSearch",
  [string]$DataDir = "$env:ProgramData\SemSearch",
  [switch]$PurgeData,
  [switch]$Yes
)
$ErrorActionPreference = "Continue"
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Write-Host "run elevated"; exit 1 }
$svc = "SemSearch"
if (Get-Service $svc -ErrorAction SilentlyContinue) {
  Write-Host "==> stopping and removing service"
  Stop-Service $svc -Force -ErrorAction SilentlyContinue
  $py = (Get-ChildItem "$InstallDir\python\cpython-*\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
  if ($py) { & $py -m semsearch.service remove } else { sc.exe delete $svc | Out-Null }
}
Write-Host "==> removing binaries $InstallDir"
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$InstallDir.previous" -ErrorAction SilentlyContinue
[Environment]::SetEnvironmentVariable("SEMSEARCH_CONFIG", $null, "Machine")
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
[Environment]::SetEnvironmentVariable("Path", (($machinePath -split ';' | Where-Object { $_ -and $_ -ne $InstallDir }) -join ';'), "Machine")
if ($PurgeData) {
  if (-not $Yes) {
    $a = Read-Host "Delete the index, configuration, state and logs under $DataDir? Type DELETE to confirm"
    if ($a -ne 'DELETE') { Write-Host "data kept at $DataDir"; exit 0 }
  }
  Write-Host "==> deleting $DataDir"
  Remove-Item -Recurse -Force $DataDir
} else {
  Write-Host "data kept at $DataDir (index, semsearch.yaml, state, logs). Delete it with: uninstall.ps1 -PurgeData"
}
Write-Host "done"
