# SemSearch installer / upgrader. Run from an elevated PowerShell inside a staged release directory
# (dist\SemSearch-<version>\) produced by build_release.ps1. Re-running is safe: it stops the
# service, replaces binaries, preserves %ProgramData%\SemSearch (config, index, state, logs),
# migrates if needed, restarts and verifies health. If any step of the binary replacement fails,
# the previous installation is put back and the service is restarted (rollback).
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
  [switch]$SkipGpuCheck,
  [switch]$NoWindowsScope,   # do not seed a NEW config's roots from the Windows Search content scope
  [switch]$NoTray            # do not register / start the per-user tray icon
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }
function Native($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
function Assert-SafeDir($p, $name) {
  if (-not [IO.Path]::IsPathRooted($p)) { Fail "$name must be an absolute path: $p" }
  $full = [IO.Path]::GetFullPath($p).TrimEnd('\')
  if ($full -match '^[A-Za-z]:$' -or $full -match '^\\\\[^\\]+\\[^\\]+$') { Fail "$name must not be a drive or share root: $p" }
  if ($full -eq [IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\') -or $full -eq [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') -or $full -eq [IO.Path]::GetFullPath($env:ProgramData).TrimEnd('\')) { Fail "$name must be a dedicated directory, not $full" }
  return $full
}
function Inside($child, $parent) { return ($child + '\').StartsWith($parent + '\', [StringComparison]::OrdinalIgnoreCase) }
function Is-Reparse($p) { $i = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue; return ($i -and ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)) }
# Remove-Item -Recurse in Windows PowerShell 5.1 follows directory junctions and deletes the
# TARGET's contents. Remove a link as a link; recurse only into real directories (rmdir /s does
# not traverse junctions).
function Remove-Tree($p) {
  if (-not (Test-Path -LiteralPath $p)) { return }
  if (Is-Reparse $p) { cmd /c rmdir "$p" | Out-Null; return }
  cmd /c rmdir /s /q "$p" | Out-Null
}
# an existing data directory must belong to this machine's administrators: any standard user can
# create folders under %ProgramData%, and a pre-planted SemSearch folder (with its own
# semsearch.yaml naming roots) would turn this elevated installer into a grant oracle
function Assert-OwnedDataDir($p) {
  if (-not (Test-Path -LiteralPath $p)) { return }
  if (Is-Reparse $p) { Fail "$p is a junction or symbolic link; refusing to install into it" }
  $owner = (Get-Acl -LiteralPath $p).Owner
  $trusted = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM', 'NT SERVICE\TrustedInstaller')
  if ($trusted -notcontains $owner) {
    Fail "$p already exists and is owned by '$owner', not by Administrators/SYSTEM. It was not created by this installer; inspect it, then remove it (or take ownership) before installing."
  }
}

# ---- prerequisites ----
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail "run this from an elevated PowerShell" }
$src = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$srcPy = (Get-ChildItem "$src\python\cpython-*\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
if (-not $srcPy) { Fail "run install.ps1 from inside a staged release directory (build_release.ps1)" }
$version = (Get-Content "$src\VERSION").Trim()
$InstallDir = Assert-SafeDir $InstallDir "InstallDir"
$DataDir = Assert-SafeDir $DataDir "DataDir"
if ($InstallDir -eq $DataDir -or (Inside $InstallDir $DataDir) -or (Inside $DataDir $InstallDir)) { Fail "InstallDir and DataDir must be separate directories" }
if ((Inside $src $InstallDir) -or (Inside $InstallDir $src) -or (Inside $src $DataDir)) { Fail "the release directory must not overlap InstallDir or DataDir" }
$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -lt 19041) { Fail "Windows 10 2004 / Windows 11 required (build $($os.BuildNumber))" }
if ((Get-Service WSearch -ErrorAction SilentlyContinue).Status -ne 'Running') { Write-Warning "Windows Search service (WSearch) is not running; semsearch will fall back to filesystem walks" }
if (-not $Operator) {
  # The operator gets read access to the admin token and the index, modify on the config and
  # service control. Default: the account running this elevated installer. If a DIFFERENT user is
  # logged on at the console (installing over RDP or from a second session), do not guess: the
  # console user would otherwise receive all of the above.
  $console = (Get-CimInstance Win32_ComputerSystem).UserName
  $Operator = $id.Name
  if ($console -and ($console -ne $id.Name)) {
    Fail "the console user ($console) is not the account running this installer ($($id.Name)); pass -Operator explicitly"
  }
}
# a privileged service identity makes the operator-writable config a privilege boundary: refuse
# that combination unless the operator is an administrator anyway
if ($Account -notmatch '^NT SERVICE\\') {
  $opIsAdmin = $false
  try {
    $opSid = (New-Object System.Security.Principal.NTAccount($Operator)).Translate([System.Security.Principal.SecurityIdentifier])
    $admins = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $opIsAdmin = [bool](Get-LocalGroupMember -SID $admins | Where-Object { $_.SID -eq $opSid })
  } catch {}
  if (-not $opIsAdmin) {
    Fail "service account '$Account' is not a per-service virtual account: the operator ($Operator) could steer it through semsearch.yaml. Use the default NT SERVICE\SemSearch, or make the operator an administrator."
  }
}
$svc = "SemSearch"
Step "SemSearch $version -> $InstallDir  (data: $DataDir, service account: $Account, operator: $Operator)"
Assert-OwnedDataDir $DataDir
if ((Test-Path -LiteralPath $InstallDir) -and (Is-Reparse $InstallDir)) { Fail "$InstallDir is a junction or symbolic link; refusing" }

# the source stage is verified BEFORE anything on the machine is touched
Step "verifying the release (manifest + native dependencies)"
if (Test-Path "$src\release-manifest.json") {
  & $srcPy -s "$src\release_manifest.py" verify "$src" "$src\release-manifest.json"; Native "release manifest verification (whole release)"
}
& $srcPy -s "$src\verify_runtime.py"; Native "release runtime verification"

# ---- transactional section: stop service, swap binaries; any failure rolls back ----
$script:svcExisted = $false; $script:svcWasRunning = $false; $script:prevMoved = $false; $script:newCopied = $false
function Rollback($why) {
  Write-Host "ERROR: $why" -ForegroundColor Red
  Write-Host "==> rolling back" -ForegroundColor Yellow
  $ErrorActionPreference = "Continue"
  if ($script:newCopied -and (Test-Path $InstallDir)) { Remove-Tree $InstallDir }
  if ($script:prevMoved -and (Test-Path "$InstallDir.previous")) { Rename-Item "$InstallDir.previous" $InstallDir -ErrorAction SilentlyContinue }
  if ($script:svcExisted) {
    sc.exe config $svc start= delayed-auto | Out-Null
    sc.exe failure $svc reset= 86400 actions= restart/5000/restart/30000/restart/120000 | Out-Null
    if ($script:svcWasRunning) { Start-Service $svc -ErrorAction SilentlyContinue; Write-Host "previous version restarted" }
  }
  Write-Host "previous installation restored; nothing under $DataDir was changed"
  exit 1
}
try {
  $existing = Get-Service $svc -ErrorAction SilentlyContinue
  if ($existing) {
    $script:svcExisted = $true
    $script:svcWasRunning = ($existing.Status -ne 'Stopped')
    Step "stopping existing service"
    # disable failure recovery first: otherwise a pending 'restart after failure' can relaunch the
    # service in the middle of the runtime copy (seen in testing: it started with half the packages)
    sc.exe failure $svc reset= 0 actions= "" | Out-Null
    sc.exe config $svc start= disabled | Out-Null
    $wsvc = Get-CimInstance Win32_Service -Filter "Name='$svc'"
    $svcPid = if ($wsvc) { [int]$wsvc.ProcessId } else { 0 }
    if ($existing.Status -ne 'Stopped') { Stop-Service $svc -Force -ErrorAction SilentlyContinue; (Get-Service $svc).WaitForStatus('Stopped', (New-TimeSpan -Seconds 90)) }
    # only the registered service process is ever killed (never other python processes on the machine)
    if ($svcPid -gt 0) {
      $p = Get-Process -Id $svcPid -ErrorAction SilentlyContinue
      # the service reports STOPPED a moment before its process ends: give it time to exit by itself
      if ($p) { $p.WaitForExit(10000) | Out-Null; $p = Get-Process -Id $svcPid -ErrorAction SilentlyContinue }
      if ($p -and $p.ProcessName -match '^pythonw?$') { Stop-Process -Id $svcPid -Force -ErrorAction SilentlyContinue; Write-Host "service process $svcPid did not exit within 10 s of stopping; terminated" }
    }
  }
  # anything still executing out of the install directory (an orphaned extractor child of an older
  # version, a stray operator CLI) holds the directory open; identified by executable path, nothing else
  $holders = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir + '\', [StringComparison]::OrdinalIgnoreCase) }
  foreach ($hp in $holders) { Write-Host "terminating $($hp.Name) (pid $($hp.ProcessId)) still running from $InstallDir"; Stop-Process -Id $hp.ProcessId -Force -ErrorAction SilentlyContinue }
  if ($holders) { Start-Sleep 2 }

  # ---- runtime: replace binaries, never touch data ----
  Step "installing runtime"
  if (Test-Path $InstallDir) {
    $prev = "$InstallDir.previous"
    if (Test-Path $prev) { Remove-Tree $prev }
    # handles can linger a moment after a process is terminated: retry the rename briefly
    $renamed = $false
    for ($try = 0; $try -lt 10 -and -not $renamed; $try++) {
      try { Rename-Item $InstallDir $prev -ErrorAction Stop; $renamed = $true } catch { Start-Sleep 2 }
    }
    if (-not $renamed) { throw "cannot rename $InstallDir (a process still holds it open; see above)" }
    $script:prevMoved = $true   # kept for rollback until the next successful install
  }
  New-Item -ItemType Directory -Force $InstallDir | Out-Null
  $script:newCopied = $true
  Copy-Item "$src\python" "$InstallDir\python" -Recurse
  foreach ($f in @("VERSION", "install.ps1", "uninstall.ps1", "validate.ps1", "verify_runtime.py", "relocate_launchers.py", "release_manifest.py", "collect_notices.py", "LICENSE")) {
    if (Test-Path "$src\$f") { Copy-Item "$src\$f" "$InstallDir\" }
  }
  foreach ($f in @("THIRD-PARTY-NOTICES.txt", "sbom.json", "model-manifest.json", "requirements.lock.txt", "release-manifest.json")) {
    if (Test-Path "$src\$f") { Copy-Item "$src\$f" "$InstallDir\" }
  }
  # the service is hosted by the real interpreter (pythonw.exe beside it), never a venv launcher
  $py = (Get-ChildItem "$InstallDir\python\cpython-*\python.exe" | Select-Object -First 1).FullName
  if (Test-Path "$src\release-manifest.json") {
    & $py -s "$InstallDir\release_manifest.py" verify "$InstallDir\python" "$src\release-manifest.json" --subdir python; Native "copied runtime verification"
  }
  # -s everywhere: the runtime must never see a user's %APPDATA%\Python site-packages (the service account has none)
  & $py -s "$InstallDir\verify_runtime.py"; Native "installed runtime verification"
  # console launchers carry the interpreter path inside them: regenerate for THIS location
  & $py -s "$InstallDir\relocate_launchers.py" --copy-to $InstallDir; Native "launcher relocation"
  Set-Content "$InstallDir\semsearch.cmd" "@echo off`r`n`"$py`" -s -m semsearch.cli %*" -Encoding ascii
  if (-not (Inside $InstallDir ([IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\')))) {
    # outside Program Files the inherited ACL may let users write: code must be admin-writable only
    Step "applying code-directory ACL (custom InstallDir)"
    icacls $InstallDir /inheritance:r /grant "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "Users:(OI)(CI)RX" /Q | Out-Null; Native "icacls InstallDir"
  }
} catch { Rollback "$_" }
# from here on the new binaries are in place; failures below are reported, not rolled back
# (the data directory and the service registration are idempotent and re-runnable)

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$pathAdded = $false
if (($machinePath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $InstallDir }).Count -eq 0) { [Environment]::SetEnvironmentVariable("Path", "$machinePath;$InstallDir", "Machine"); $pathAdded = $true }

# ---- data directories ----
Step "preparing data directories"
foreach ($d in @("$DataDir", "$DataDir\index", "$DataDir\state", "$DataDir\logs", "$DataDir\models", "$DataDir\backups")) { New-Item -ItemType Directory -Force $d | Out-Null }
# ---- config: write once, never overwrite ----
$cfgPath = "$DataDir\semsearch.yaml"
$cfgWritten = $false
if (-not (Test-Path $cfgPath)) {
  Step "writing initial configuration"
  $gpuJson = & $py -s -c "import json; from semsearch.devices import enumerate_adapters, selector_for; print(json.dumps([{'name': a.name, 'integrated': a.integrated, 'software': a.software, 'selector': selector_for(a)} for a in enumerate_adapters()]))"
  $gpus = @()
  if ($LASTEXITCODE -eq 0 -and $gpuJson) { $gpus = $gpuJson | ConvertFrom-Json }
  $devices = @()
  $steady = "cpu"; $bulk = "cpu"
  $integrated = $gpus | Where-Object { -not $_.software -and $_.integrated } | Select-Object -First 1
  $discrete = $gpus | Where-Object { -not $_.software -and -not $_.integrated } | Select-Object -First 1
  if ($integrated) { $devices += "    integrated-gpu:   # $($integrated.name)`n      vendor: `"$($integrated.selector.vendor)`"`n      device: `"$($integrated.selector.device)`"`n      subsys: `"$($integrated.selector.subsys)`"" + $(if ($integrated.selector.address) { "`n      address: `"$($integrated.selector.address)`"" } else { "" }); $steady = "integrated-gpu" }
  if ($discrete) { $devices += "    discrete-gpu:     # $($discrete.name)`n      vendor: `"$($discrete.selector.vendor)`"`n      device: `"$($discrete.selector.device)`"`n      subsys: `"$($discrete.selector.subsys)`"" + $(if ($discrete.selector.address) { "`n      address: `"$($discrete.selector.address)`"" } else { "" }); $bulk = "discrete-gpu" }
  if (-not $integrated -and $discrete) { $steady = "discrete-gpu" }
  $scopeExcludes = @()
  if (-not $Roots.Count -and -not $NoWindowsScope) {
    # default scope = what the Windows Search indexer already covers for CONTENT in the operator's
    # profile (Documents, Desktop, Downloads, OneDrive, ...), plus its exclusion rules
    $opProfile = (Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq (New-Object System.Security.Principal.NTAccount($Operator)).Translate([System.Security.Principal.SecurityIdentifier]).Value } | Select-Object -First 1).LocalPath
    $scopeJson = & $py -s -c "import json; from semsearch.inventory.scope import windows_scope_suggestion; s = windows_scope_suggestion(r'$opProfile' or None); print(json.dumps({'roots': s.roots, 'excludes': s.excludes}))"
    if ($LASTEXITCODE -eq 0 -and $scopeJson) {
      $scope = $scopeJson | ConvertFrom-Json
      $Roots = @($scope.roots)
      $scopeExcludes = @($scope.excludes)
      Step "seeding roots from the Windows Search content scope: $($Roots -join ', ')  (+ $($scopeExcludes.Count) exclusion rules)"
    }
  }
  $rootsYaml = if ($Roots.Count) { ($Roots | ForEach-Object { "  - `"$($_ -replace '\\','/')`"" }) -join "`n" } else { "  # - `"F:/HexyLab`"   <- add your folders, then: semsearch service restart" }
  $opProfileYaml = ""
  try { $opProfileYaml = "  operator_profile: `"$(((Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq (New-Object System.Security.Principal.NTAccount($Operator)).Translate([System.Security.Principal.SecurityIdentifier]).Value } | Select-Object -First 1).LocalPath) -replace '\\','/')`"" } catch {}
  $yaml = @"
# SemSearch configuration (machine-wide). Edit, then: semsearch service restart
data_dir: "$($DataDir -replace '\\','/')"
roots:
$rootsYaml
server:
  host: 127.0.0.1
  port: $Port
$opProfileYaml
  # read_token: true   # on a SHARED machine: require the admin token for search/document reads too
$(if ($scopeExcludes.Count) { "excludes:   # the default list plus the Windows Search exclusion rules found at install time`n" + ((& $py -s -c "from semsearch.config import DEFAULT_EXCLUDES; [print(e) for e in DEFAULT_EXCLUDES]") + $scopeExcludes | Sort-Object -Unique | ForEach-Object { "  - `"$($_ -replace '\\','/')`"" }) -join "`n" } else { "" })
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
  $cfgWritten = $true
} else { Step "keeping existing configuration $cfgPath" }

# ---- model: bundled files are copied into the machine cache; download only as a fallback ----
$cacheDir = (& $py -s -c "from semsearch.config import load_config; print(load_config(r'$cfgPath').model_cache_dir)").Trim()
Native "reading model cache dir from config"
Step "ensuring the embedding model is available under $cacheDir"
if (Test-Path "$src\models\bundled") {
  New-Item -ItemType Directory -Force "$cacheDir\bundled" | Out-Null
  Copy-Item "$src\models\bundled\*" "$cacheDir\bundled" -Recurse -Force
}
$env:HF_HOME = $cacheDir
& $py -s -c "from semsearch.config import load_config; from semsearch.embed.onnx_provider import _resolve_model_files; c=load_config(r'$cfgPath'); print('model:', _resolve_model_files(c.embedding.model, c.embedding.revision, False)[0])"
if ($LASTEXITCODE -ne 0) {
  Write-Host "model not bundled/cached; downloading once (network)" -ForegroundColor Yellow
  & $py -s -c "from semsearch.config import load_config; from semsearch.embed.onnx_provider import _resolve_model_files; c=load_config(r'$cfgPath'); print('model:', _resolve_model_files(c.embedding.model, c.embedding.revision, True)[0])"
  if ($LASTEXITCODE -ne 0) { Fail "could not obtain the embedding model (place the files locally and set embedding.model to that directory)" }
}

# ---- GPU / DirectML validation ----
if (-not $SkipGpuCheck) {
  Step "validating accelerators"
  & $py -s -m semsearch.cli devices
  & $py -s -c "from semsearch.config import load_config; from semsearch.app_state import resolve_devices; cfg, rep = resolve_devices(load_config(r'$cfgPath')); [print('  %-13s %-16s -> %-7s %s' % (k, v['configured'], v['resolved'], v['why'])) for k, v in rep['roles'].items()]"
}

# ---- service registration ----
Step "registering service"
$acct = $Account
if ($Account -notmatch '^(LocalSystem|NT AUTHORITY\\|NT SERVICE\\)') {
  Write-Host "service will log on as user account $Account; sc.exe will need its password and the account the 'Log on as a service' right" -ForegroundColor Yellow
}
# PowerShell 5.1 turns any native stderr output into a terminating error under Stop; native
# tools below are allowed to write to stderr, their exit codes are checked explicitly
$ErrorActionPreference = "Continue"
# icacls spelling of the account (sc.exe accepts 'LocalSystem'; ACLs need the SID-resolvable name)
$aclAcct = switch -Regex ($Account) { '^LocalSystem$' { 'NT AUTHORITY\SYSTEM' } '^NT AUTHORITY\\LocalService$' { 'NT AUTHORITY\LOCAL SERVICE' } '^NT AUTHORITY\\NetworkService$' { 'NT AUTHORITY\NETWORK SERVICE' } default { $Account } }
& $py -s -m semsearch.service install --account $acct --start delayed --config $cfgPath
if ($LASTEXITCODE -ne 0) { Fail "service registration failed" }
# the CLI finds a non-default DataDir through the machine environment (new shells)
[Environment]::SetEnvironmentVariable("SEMSEARCH_CONFIG", $cfgPath, "Machine")
# recovery: restart after failure, with back-off; reset the failure count after a day
sc.exe failure $svc reset= 86400 actions= restart/5000/restart/30000/restart/120000 | Out-Null
sc.exe failureflag $svc 1 | Out-Null
# ACLs: service account full control on data, operator read on config/logs/state (token), admins full
Step "applying ACLs"
icacls $DataDir /inheritance:r /grant "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" "${aclAcct}:(OI)(CI)F" "${Operator}:(OI)(CI)RX" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "icacls on $DataDir failed (exit $LASTEXITCODE)" }
icacls "$DataDir\semsearch.yaml" /grant "${Operator}:M" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "icacls on $DataDir\semsearch.yaml failed (exit $LASTEXITCODE)" }
# allow the operator to start/stop/query the service without elevation
$opSid = (New-Object System.Security.Principal.NTAccount($Operator)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$sd = (sc.exe sdshow $svc | Where-Object { $_ -match '^D:' }).Trim()
if ($sd -notmatch [regex]::Escape($opSid)) {
  $sd = $sd -replace '^D:', "D:(A;;CCLCSWRPWPDTLOCRRC;;;$opSid)"
  sc.exe sdset $svc $sd | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Warning "could not grant service control to $Operator (sc sdset exit $LASTEXITCODE)" }
}
# the service account needs READ on every root (virtual accounts have no rights by default)
$roots = & $py -s -c "from semsearch.config import load_config; [print(str(r)) for r in load_config(r'$cfgPath').roots]"
$granted = @()
foreach ($r in $roots) {
  # one inheritable ACE on the root directory (no /T: that would rewrite every file's ACL explicitly and take minutes on
  # large trees; the inheritable entry propagates on its own, and files that block inheritance are not indexable anyway)
  if (-not (Test-Path $r)) { Write-Warning "root does not exist yet: $r"; continue }
  $have = (icacls $r | Select-String -SimpleMatch "$aclAcct`:") -ne $null
  if ($have) { Step "read access for $acct on $r already present" }
  else {
    Step "granting $acct read access to root $r (propagation over a large tree can take minutes)"
    icacls $r /grant "${aclAcct}:(OI)(CI)RX" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "icacls grant on $r failed (exit $LASTEXITCODE); that root will not be readable by the service" } else { $granted += $r }
  }
}

# ---- tray icon: a per-user logon task (runs unelevated as the operator; the service has no UI) ----
$trayTask = $false
if (-not $NoTray) {
  Step "registering the tray icon logon task for $Operator"
  $pyw = Join-Path (Split-Path $py) "pythonw.exe"
  # an interactive-logon principal needs no stored password (schtasks /RU without /RP cannot do this non-interactively)
  try {
    $action = New-ScheduledTaskAction -Execute $pyw -Argument "-s -m semsearch.tray" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $Operator
    $principal = New-ScheduledTaskPrincipal -UserId $Operator -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "SemSearch Tray" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
    $trayTask = $true
  } catch { Write-Warning "could not register the tray logon task ($_); start it with: semsearch tray" }
}

# ---- installation manifest: what this installer owns (read by uninstall.ps1) ----
$manifest = @{
  version = $version; installed_at = (Get-Date).ToString("o"); install_dir = $InstallDir; data_dir = $DataDir; config_path = $cfgPath
  service = $svc; account = $Account; acl_account = $aclAcct; operator = $Operator; model_cache_dir = $cacheDir
  path_entry_added = $pathAdded; machine_env_semsearch_config = $true; config_written_by_installer = $cfgWritten
  root_grants = @($granted); tray_task = $trayTask
}
$prevManifest = "$InstallDir.previous\install-manifest.json"
if (Test-Path $prevManifest) {
  try { $old = Get-Content $prevManifest -Raw | ConvertFrom-Json; $manifest.root_grants = @(@($old.root_grants) + $granted | Select-Object -Unique); if ($old.path_entry_added) { $manifest.path_entry_added = $true } } catch {}
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content "$InstallDir\install-manifest.json" -Encoding utf8

# ---- start and verify ----
if ($NoStart) { Step "installed (not started). Start with: semsearch service start"; exit 0 }
Step "starting service"
$ErrorActionPreference = "Stop"
try { Start-Service $svc; (Get-Service $svc).WaitForStatus('Running', (New-TimeSpan -Seconds 240)) }
catch { Write-Host "service did not reach Running: $_" -ForegroundColor Red; Write-Host "--- last log lines:"; Get-Content "$DataDir\logs\semsearch.log" -Tail 30 -ErrorAction SilentlyContinue; Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='SemSearch'} -MaxEvents 5 -ErrorAction SilentlyContinue | ForEach-Object { $_.Message.Substring(0, [Math]::Min(400, $_.Message.Length)) }; Write-Host "previous binaries are at $InstallDir.previous (see docs/windows-service.md, Upgrade)"; exit 2 }
$ErrorActionPreference = "Continue"
Step "waiting for health"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
  # the token is rotated at service start: read it fresh (details in /health need it)
  $hdr = @{}; try { $hdr = @{ "x-semsearch-token" = (Get-Content "$DataDir\state\admin.token" -ErrorAction Stop).Trim() } } catch {}
  try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -Headers $hdr -TimeoutSec 5; if ($h.ok -and $h.documents -ne $null) { $ok = $true; break } } catch {}
  Start-Sleep 2
}
if (-not $ok) { Write-Host "service is running but /health did not answer; see $DataDir\logs\semsearch.log" -ForegroundColor Yellow; exit 2 }
"health: version $($h.version), $($h.documents) documents, embedding $($h.embedding)"
Step "smoke check"
& "$InstallDir\semsearch.exe" status
& "$InstallDir\semsearch.cmd" query "semantic search smoke test" -n 1 | Select-Object -First 3
if ($trayTask) {
  # (re)start the tray as the operator, unelevated, through the task (Start-Process from here would run it elevated)
  Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" | Where-Object { $_.CommandLine -like '*semsearch.tray*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  try { Start-ScheduledTask -TaskName "SemSearch Tray" -ErrorAction Stop } catch { Write-Warning "tray not started ($_); it starts at the next logon, or now with: semsearch tray" }
}
Write-Host "`nSemSearch $version installed and running. Folders: tray icon > Folders, or: semsearch roots add <folder>" -ForegroundColor Green
if (Test-Path "$InstallDir.previous") { Remove-Tree "$InstallDir.previous" }
exit 0
