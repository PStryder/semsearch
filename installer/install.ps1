# SemSearch installer / upgrader. Run from an elevated PowerShell inside a staged release directory
# (dist\SemSearch-<version>\) produced by build_release.ps1. Re-running is safe: it stops the
# service, replaces binaries, preserves %ProgramData%\SemSearch (config, index, state, logs),
# migrates if needed, restarts and verifies health.
#
#   .\install.ps1                                   install/upgrade with defaults
#   .\install.ps1 -Roots "F:\HexyLab","C:\Users\me\Documents"   seed roots into a NEW config
#   .\install.ps1 -Account LocalSystem               override the service identity (see docs/windows-service.md)
#   .\install.ps1 -Operator DOMAIN\user              account allowed to control the service / read the admin token (default: installing user)
#   .\install.ps1 -NoStart                           install but do not start
param(
  [string]$InstallDir = "$env:ProgramFiles\SemSearch",
  [string]$DataDir = "$env:ProgramData\SemSearch",
  [string]$Account = "NT SERVICE\SemSearch",
  [string]$Operator = "",
  [string[]]$Roots = @(),
  [int]$Port = 8765,
  [switch]$NoStart,
  [switch]$SkipGpuCheck
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# ---- prerequisites ----
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail "run this from an elevated PowerShell" }
$src = $PSScriptRoot
if (-not (Test-Path "$src\venv\Scripts\python.exe") -or -not (Test-Path "$src\python")) { Fail "run install.ps1 from inside a staged release directory (build_release.ps1)" }
$version = (Get-Content "$src\VERSION").Trim()
$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -lt 19041) { Fail "Windows 10 2004 / Windows 11 required (build $($os.BuildNumber))" }
if ((Get-Service WSearch -ErrorAction SilentlyContinue).Status -ne 'Running') { Write-Warning "Windows Search service (WSearch) is not running; semsearch will fall back to filesystem walks" }
if (-not $Operator) {
  # the user who launched the elevated prompt; fall back to the current identity
  $Operator = (Get-CimInstance Win32_ComputerSystem).UserName
  if (-not $Operator) { $Operator = $id.Name }
}
$svc = "SemSearch"
Step "SemSearch $version -> $InstallDir  (data: $DataDir, service account: $Account, operator: $Operator)"

# ---- stop existing service ----
$existing = Get-Service $svc -ErrorAction SilentlyContinue
if ($existing) {
  Step "stopping existing service"
  if ($existing.Status -ne 'Stopped') { Stop-Service $svc -Force -ErrorAction SilentlyContinue; (Get-Service $svc).WaitForStatus('Stopped', (New-TimeSpan -Seconds 90)) }
}

# ---- runtime: replace binaries, never touch data ----
Step "installing runtime"
if (Test-Path $InstallDir) {
  $prev = "$InstallDir.previous"
  if (Test-Path $prev) { Remove-Item -Recurse -Force $prev }
  Rename-Item $InstallDir $prev   # kept for rollback until the next successful install
}
New-Item -ItemType Directory -Force $InstallDir | Out-Null
Copy-Item "$src\python" "$InstallDir\python" -Recurse
Copy-Item "$src\venv" "$InstallDir\venv" -Recurse
Copy-Item "$src\VERSION" "$InstallDir\VERSION"
Copy-Item "$src\install.ps1", "$src\uninstall.ps1", "$src\validate.ps1" "$InstallDir\"
# re-point the venv at the installed base interpreter
$base = (Get-ChildItem "$InstallDir\python\cpython-*\python.exe" | Select-Object -First 1).FullName
$cfgv = Get-Content "$InstallDir\venv\pyvenv.cfg"
$cfgv = $cfgv | ForEach-Object { if ($_ -match '^home = ') { "home = " + (Split-Path $base) } else { $_ } }
Set-Content "$InstallDir\venv\pyvenv.cfg" $cfgv -Encoding ascii
$py = "$InstallDir\venv\Scripts\python.exe"
& $py -c "import semsearch, onnxruntime; print('runtime', semsearch.__version__, onnxruntime.get_available_providers())"
if ($LASTEXITCODE -ne 0) { Fail "installed runtime does not import" }
# a 'semsearch' command for operators: wrapper in the install dir (added to the machine PATH)
Set-Content "$InstallDir\semsearch.cmd" "@echo off`r`n`"$InstallDir\venv\Scripts\python.exe`" -m semsearch.cli %*" -Encoding ascii
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
if ($machinePath -notlike "*$InstallDir*") { [Environment]::SetEnvironmentVariable("Path", "$machinePath;$InstallDir", "Machine") }
# install dir: read/execute for everyone, write for admins only (defaults of Program Files already do this)

# ---- data directories and ACLs ----
Step "preparing data directories"
foreach ($d in @("$DataDir", "$DataDir\index", "$DataDir\state", "$DataDir\logs", "$DataDir\models")) { New-Item -ItemType Directory -Force $d | Out-Null }
# the service account exists only once the service is registered (virtual accounts); register first, grant after
# ---- config: write once, never overwrite ----
$cfgPath = "$DataDir\semsearch.yaml"
if (-not (Test-Path $cfgPath)) {
  Step "writing initial configuration"
  $gpuJson = & $py -c "import json; from semsearch.devices import enumerate_adapters, selector_for; print(json.dumps([{'name': a.name, 'integrated': a.integrated, 'software': a.software, 'selector': selector_for(a)} for a in enumerate_adapters()]))"
  $gpus = @()
  if ($LASTEXITCODE -eq 0 -and $gpuJson) { $gpus = $gpuJson | ConvertFrom-Json }
  $devices = @()
  $steady = "cpu"; $bulk = "cpu"
  $integrated = $gpus | Where-Object { -not $_.software -and $_.integrated } | Select-Object -First 1
  $discrete = $gpus | Where-Object { -not $_.software -and -not $_.integrated } | Select-Object -First 1
  if ($integrated) { $devices += "    integrated-gpu:   # $($integrated.name)`n      vendor: `"$($integrated.selector.vendor)`"`n      device: `"$($integrated.selector.device)`"`n      subsys: `"$($integrated.selector.subsys)`"" + $(if ($integrated.selector.address) { "`n      address: `"$($integrated.selector.address)`"" } else { "" }); $steady = "integrated-gpu" }
  if ($discrete) { $devices += "    discrete-gpu:     # $($discrete.name)`n      vendor: `"$($discrete.selector.vendor)`"`n      device: `"$($discrete.selector.device)`"`n      subsys: `"$($discrete.selector.subsys)`"" + $(if ($discrete.selector.address) { "`n      address: `"$($discrete.selector.address)`"" } else { "" }); $bulk = "discrete-gpu" }
  if (-not $integrated -and $discrete) { $steady = "discrete-gpu" }
  $rootsYaml = if ($Roots.Count) { ($Roots | ForEach-Object { "  - `"$($_ -replace '\\','/')`"" }) -join "`n" } else { "  # - `"F:/HexyLab`"   <- add your folders, then: semsearch service restart" }
  $yaml = @"
# SemSearch configuration (machine-wide). Edit, then: semsearch service restart
data_dir: "$($DataDir -replace '\\','/')"
roots:
$rootsYaml
server:
  host: 127.0.0.1
  port: $Port
embedding:
  model: BAAI/bge-small-en-v1.5
  devices:              # stable selectors written by the installer from the adapters it found
$($devices -join "`n")
  steady_state_device: $steady
  bulk_device: $bulk
  query_device: cpu
  fallback_device: cpu
  bulk_threshold: 500
indexing:
  use_windows_search: true
  poll_interval_s: 30
  reconcile_interval_s: 3600
  startup_reconcile_delay_s: 600
  watch_filesystem: true
  skip_suspected_secrets: true
  low_priority: true
service:
  shutdown_timeout_s: 30
  event_log: true
  integrity_check: quick
log_level: INFO
"@
  Set-Content $cfgPath $yaml -Encoding utf8
} else { Step "keeping existing configuration $cfgPath" }

# ---- model: pre-fetch into the machine cache so the service never needs the operator's profile ----
Step "ensuring embedding model is cached under $DataDir\models"
$env:HF_HOME = "$DataDir\models"
& $py -c "from semsearch.config import load_config; from semsearch.embed.onnx_provider import _resolve_model_files; c=load_config(r'$cfgPath'); print(_resolve_model_files(c.embedding.model, c.embedding.revision, True)[0])"
if ($LASTEXITCODE -ne 0) { Fail "could not fetch the embedding model (network needed once; or place the files locally and set embedding.model to that directory)" }

# ---- GPU / DirectML validation ----
if (-not $SkipGpuCheck) {
  Step "validating accelerators"
  & $py -m semsearch.cli devices
  & $py -c "from semsearch.config import load_config; from semsearch.app_state import resolve_devices; cfg, rep = resolve_devices(load_config(r'$cfgPath')); [print('  %-13s %-16s -> %-7s %s' % (k, v['configured'], v['resolved'], v['why'])) for k, v in rep['roles'].items()]"
}

# ---- service registration ----
Step "registering service"
$acct = $Account
# icacls spelling of the account (sc.exe accepts 'LocalSystem'; ACLs need the SID-resolvable name)
$aclAcct = switch -Regex ($Account) { '^LocalSystem$' { 'NT AUTHORITY\SYSTEM' } '^NT AUTHORITY\\LocalService$' { 'NT AUTHORITY\LOCAL SERVICE' } '^NT AUTHORITY\\NetworkService$' { 'NT AUTHORITY\NETWORK SERVICE' } default { $Account } }
& $py -m semsearch.service remove 2>$null | Out-Null
& $py -m semsearch.service install --account $acct --start delayed
if ($LASTEXITCODE -ne 0) { Fail "service registration failed" }
# recovery: restart after failure, with back-off; reset the failure count after a day
sc.exe failure $svc reset= 86400 actions= restart/5000/restart/30000/restart/120000 | Out-Null
sc.exe failureflag $svc 1 | Out-Null
# bounded pre-shutdown time
# ACLs: service account full control on data, operator read on config/logs/state (token), admins full
Step "applying ACLs"
icacls $DataDir /inheritance:r /grant "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${aclAcct}:(OI)(CI)F" "${Operator}:(OI)(CI)RX" /Q | Out-Null
icacls "$DataDir\semsearch.yaml" /grant "${Operator}:M" /Q | Out-Null
# allow the operator to start/stop/query the service without elevation
$opSid = (New-Object System.Security.Principal.NTAccount($Operator)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$sd = (sc.exe sdshow $svc | Where-Object { $_ -match '^D:' }).Trim()
if ($sd -notmatch [regex]::Escape($opSid)) {
  $sd = $sd -replace '^D:', "D:(A;;CCLCSWRPWPDTLOCRRC;;;$opSid)"
  sc.exe sdset $svc $sd | Out-Null
}
# the service account needs READ on every root (virtual accounts have no rights by default)
$roots = & $py -c "from semsearch.config import load_config; [print(str(r)) for r in load_config(r'$cfgPath').roots]"
foreach ($r in $roots) {
  if (Test-Path $r) { Step "granting $acct read access to root $r"; icacls $r /grant "${aclAcct}:(OI)(CI)RX" /T /Q | Out-Null } else { Write-Warning "root does not exist yet: $r" }
}

# ---- start and verify ----
if ($NoStart) { Step "installed (not started). Start with: semsearch service start"; exit 0 }
Step "starting service"
Start-Service $svc
(Get-Service $svc).WaitForStatus('Running', (New-TimeSpan -Seconds 240))
Step "waiting for health"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5; if ($h.ok) { $ok = $true; break } } catch {}
  Start-Sleep 2
}
if (-not $ok) { Write-Host "service is running but /health did not answer; see $DataDir\logs\semsearch.log" -ForegroundColor Yellow; exit 2 }
"health: version $($h.version), $($h.documents) documents, embedding $($h.embedding)"
Step "smoke check"
& "$InstallDir\semsearch.cmd" status
& "$InstallDir\semsearch.cmd" query "semantic search smoke test" -n 1 | Select-Object -First 3
Write-Host "`nSemSearch $version installed and running. Edit $cfgPath (roots) then: semsearch service restart" -ForegroundColor Green
if (Test-Path "$InstallDir.previous") { Remove-Item -Recurse -Force "$InstallDir.previous" -ErrorAction SilentlyContinue }
