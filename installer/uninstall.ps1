# Removes the SemSearch service, its binaries, the machine PATH/environment entries it added and
# the read grants it placed on the configured roots. The index, configuration, state and logs
# (the data directory) are KEPT unless -PurgeData is given (and then you are asked to confirm).
# Locations come from the installation manifest written by install.ps1 (install-manifest.json
# in the install directory), falling back to the machine SEMSEARCH_CONFIG variable and the
# parameters below.
param(
  [string]$InstallDir = "$env:ProgramFiles\SemSearch",
  [string]$DataDir = "",
  [switch]$PurgeData,
  [switch]$Yes
)
$ErrorActionPreference = "Continue"
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Write-Host "run elevated"; exit 1 }
$svc = "SemSearch"
$problems = 0
function Is-Reparse($p) { $i = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue; return ($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)) }
# Windows PowerShell 5.1's Remove-Item -Recurse follows junctions into their targets: remove a link
# as a link, and recurse only with rmdir /s, which does not traverse junctions
function Remove-Tree($p) {
  if (-not (Test-Path -LiteralPath $p)) { return }
  if (Is-Reparse $p) { cmd /c rmdir "$p" | Out-Null; return }
  cmd /c rmdir /s /q "$p" | Out-Null
}
function Note($m) { Write-Host "==> $m" }
function Problem($m) { Write-Host "PROBLEM: $m" -ForegroundColor Yellow; $script:problems++ }

$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$manifest = $null
if (Test-Path "$InstallDir\install-manifest.json") {
  try { $manifest = Get-Content "$InstallDir\install-manifest.json" -Raw | ConvertFrom-Json } catch { Problem "install-manifest.json unreadable: $_" }
}
$cfgPath = [Environment]::GetEnvironmentVariable("SEMSEARCH_CONFIG", "Machine")
if ($manifest -and $manifest.data_dir) { $DataDir = $manifest.data_dir }
elseif (-not $DataDir -and $cfgPath) { $DataDir = Split-Path -Parent $cfgPath }
elseif (-not $DataDir) { $DataDir = "$env:ProgramData\SemSearch" }
$DataDir = [IO.Path]::GetFullPath($DataDir).TrimEnd('\')
if ($DataDir -match '^[A-Za-z]:$' -or $InstallDir -match '^[A-Za-z]:$') { Write-Host "refusing to operate on a drive root"; exit 1 }
Note "install dir: $InstallDir   data dir: $DataDir   (source: $(if ($manifest) { 'install manifest' } elseif ($cfgPath) { 'SEMSEARCH_CONFIG' } else { 'defaults' }))"

$py = (Get-ChildItem "$InstallDir\python\cpython-*\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
# roots the installer granted read access on: from the manifest, else from the configuration
$roots = @()
if ($manifest -and $manifest.root_grants) { $roots = @($manifest.root_grants) }
elseif ($py -and $cfgPath -and (Test-Path $cfgPath)) { $roots = @(& $py -s -c "from semsearch.config import load_config; [print(str(r)) for r in load_config(r'$cfgPath').roots]" 2>$null) }
$aclAcct = if ($manifest -and $manifest.acl_account) { $manifest.acl_account } else { "NT SERVICE\SemSearch" }

if (Get-ScheduledTask -TaskName "SemSearch Tray" -ErrorAction SilentlyContinue) {
  Note "removing the tray logon task"
  Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object { $_.CommandLine -like '*semsearch.tray*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  try { Unregister-ScheduledTask -TaskName "SemSearch Tray" -Confirm:$false -ErrorAction Stop } catch { Problem "could not delete the 'SemSearch Tray' scheduled task: $_" }
}
if (Get-Service $svc -ErrorAction SilentlyContinue) {
  Note "stopping and removing service"
  sc.exe failure $svc reset= 0 actions= "" | Out-Null
  Stop-Service $svc -Force -ErrorAction SilentlyContinue
  try { (Get-Service $svc).WaitForStatus('Stopped', (New-TimeSpan -Seconds 90)) } catch { Problem "service did not stop within 90 s" }
  if ($py) { & $py -s -m semsearch.service remove; if ($LASTEXITCODE -ne 0) { sc.exe delete $svc | Out-Null } } else { sc.exe delete $svc | Out-Null }
  if (Get-Service $svc -ErrorAction SilentlyContinue) { Problem "service still registered (a handle may be open; it is marked for deletion and disappears after the next reboot)" }
}

# the virtual account disappears with the service, but its explicit ACEs stay on the roots as orphaned SIDs: remove them
foreach ($r in $roots) {
  if (-not (Test-Path $r)) { continue }
  $out = icacls $r /remove:g "$aclAcct" /Q 2>&1
  if ($LASTEXITCODE -ne 0) { Problem "could not remove the read grant for $aclAcct on $r ($out)" } else { Note "removed read grant for $aclAcct on $r" }
}

Note "removing binaries $InstallDir"
foreach ($d in @($InstallDir, "$InstallDir.previous")) {
  if (Test-Path $d) {
    Remove-Tree $d
    if (Test-Path $d) { Problem "could not remove $d completely (a file is in use?); delete it after a reboot" }
  }
}
[Environment]::SetEnvironmentVariable("SEMSEARCH_CONFIG", $null, "Machine")
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
[Environment]::SetEnvironmentVariable("Path", (($machinePath -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ine $InstallDir) }) -join ';'), "Machine")
if ($PurgeData) {
  if (-not (Test-Path $DataDir)) { Note "no data directory at $DataDir" }
  elseif (Is-Reparse $DataDir) { Problem "$DataDir is a junction or symbolic link; not purging (remove the link yourself)" }
  elseif (@('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM') -notcontains (Get-Acl -LiteralPath $DataDir).Owner) {
    Problem "$DataDir is owned by '$((Get-Acl -LiteralPath $DataDir).Owner)', not by Administrators; not purging a directory this installer did not create"
  }
  else {
    if (-not $Yes) {
      $a = Read-Host "Delete the index, configuration, state and logs under $DataDir? Type DELETE to confirm"
      if ($a -ne 'DELETE') { Write-Host "data kept at $DataDir"; exit 0 }
    }
    Note "deleting $DataDir"
    Remove-Tree $DataDir
    if (Test-Path $DataDir) { Problem "could not delete $DataDir completely" }
  }
} else {
  Write-Host "data kept at $DataDir (index, semsearch.yaml, state, logs). Delete it with: uninstall.ps1 -PurgeData"
}
if ($problems) { Write-Host "done with $problems problem(s) listed above" -ForegroundColor Yellow; exit 2 }
Write-Host "done"
