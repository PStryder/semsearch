# Post-install validation of the running service: identity, session, devices, Windows Search,
# API binding, change propagation, restart behaviour. Prints a report; exit code 0 when all pass.
# Needs the operator account (or elevation) for service control; does not modify configuration.
param([string]$DataDir = "$env:ProgramData\SemSearch", [int]$Port = 8765, [string]$ProbeRoot = "")
$ErrorActionPreference = "Continue"
$fail = 0
function Check($name, $ok, $detail) { if ($ok) { Write-Host ("PASS  {0,-44} {1}" -f $name, $detail) } else { Write-Host ("FAIL  {0,-44} {1}" -f $name, $detail) -ForegroundColor Red; $script:fail++ } }
$semsearch = "$env:ProgramFiles\SemSearch\semsearch.cmd"
$svc = Get-Service SemSearch -ErrorAction SilentlyContinue
Check "service installed and running" ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
$proc = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*semsearch.service*' } | Select-Object -First 1
Check "service process exists (no console)" ($null -ne $proc) "pid $($proc.ProcessId) session $($proc.SessionId) cmd: $($proc.CommandLine)"
if ($proc) { Check "runs in Session 0" ($proc.SessionId -eq 0) "session $($proc.SessionId)"; $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner; Check "service identity" $true "$($owner.Domain)\$($owner.User)" }
$h = $null; try { $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 10 } catch {}
Check "/health ok" ($h -and $h.ok) "version $($h.version), $($h.documents) docs"
$lan = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | Select-Object -First 1).IPAddress
if ($lan) { $reach = $false; try { Invoke-RestMethod "http://$lan`:$Port/health" -TimeoutSec 3 | Out-Null; $reach = $true } catch {}; Check "API not reachable on LAN address" (-not $reach) "$lan`:$Port" }
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
Check "listening on loopback only" ($listen -and $listen.LocalAddress -eq '127.0.0.1') "$($listen.LocalAddress):$($listen.LocalPort)"
$rb = $null; try { Invoke-WebRequest "http://127.0.0.1:$Port/health" -Headers @{Host='evil.example'} -TimeoutSec 5 -UseBasicParsing | Out-Null } catch { $rb = $_.Exception.Response.StatusCode.value__ }
Check "DNS-rebinding Host rejected (421)" ($rb -eq 421) "$rb"
$st = $null; try { $st = Invoke-RestMethod "http://127.0.0.1:$Port/status" -TimeoutSec 10 } catch {}
if ($st) {
  foreach ($role in @('device','bulk_device','query_device')) { $r = $st.devices.roles.$role; Check "device role $role" ($null -ne $r) "$($r.configured) -> $($r.resolved)  ($($r.why))" }
  Check "Windows Search reachable from service" ($st.windows_search.available -eq $true) "$($st.windows_search.status) items=$($st.windows_search.items)"
  Check "watcher active" ($st.indexer.watcher -eq $true) ""
  Check "store schema" ($null -ne $st.store.schema_version) "v$($st.store.schema_version) recovered=$($st.store.recovered_from_corruption)"
  $tok = $null; try { $tok = (Get-Content "$DataDir\state\admin.token" -ErrorAction Stop).Trim() } catch {}
  Check "operator can read admin token" ($null -ne $tok) "$DataDir\state\admin.token"
  $noTok = $null; try { Invoke-RestMethod -Method Post "http://127.0.0.1:$Port/indexer/pause" -TimeoutSec 5 | Out-Null } catch { $noTok = $_.Exception.Response.StatusCode.value__ }
  Check "maintenance endpoint refuses without token" ($noTok -eq 403) "$noTok"
  # change propagation inside the first configured root
  $root = if ($ProbeRoot) { $ProbeRoot } else { $st.indexer.roots[0] }
  if ($root -and (Test-Path $root)) {
    $dir = Join-Path $root "semsearch-validate-$PID"; New-Item -ItemType Directory -Force $dir | Out-Null
    $f = Join-Path $dir "validate_note.md"; $marker = "zq" + [guid]::NewGuid().ToString('N').Substring(0, 10)
    Set-Content $f "# Validation note`n`nThe marker is $marker and this file was created by validate.ps1.`n" -Encoding utf8
    $found = $false; for ($i = 0; $i -lt 45; $i++) { Start-Sleep 2; try { $r = Invoke-RestMethod "http://127.0.0.1:$Port/search?q=$marker&mode=literal&limit=1" -TimeoutSec 10; if ($r.results.Count -and $r.results[0].filename -eq 'validate_note.md') { $found = $true; break } } catch {} }
    Check "create -> searchable" $found "$([int]($i*2))s"
    Add-Content $f "`nAppended line with second marker ${marker}B.`n"
    $found2 = $false; for ($i = 0; $i -lt 45; $i++) { Start-Sleep 2; try { $r = Invoke-RestMethod "http://127.0.0.1:$Port/search?q=${marker}B&mode=literal&limit=1" -TimeoutSec 10; if ($r.results.Count) { $found2 = $true; break } } catch {} }
    Check "modify -> searchable" $found2 "$([int]($i*2))s"
    $f2 = Join-Path $dir "validate_note_renamed.md"; Rename-Item $f $f2
    $moved = $false; for ($i = 0; $i -lt 45; $i++) { Start-Sleep 2; try { $r = Invoke-RestMethod "http://127.0.0.1:$Port/search?q=$marker&mode=literal&limit=1" -TimeoutSec 10; if ($r.results.Count -and $r.results[0].filename -eq 'validate_note_renamed.md') { $moved = $true; break } } catch {} }
    Check "rename -> path updated" $moved "$([int]($i*2))s"
    Remove-Item -Recurse -Force $dir
    $gone = $false; for ($i = 0; $i -lt 45; $i++) { Start-Sleep 2; try { $r = Invoke-RestMethod "http://127.0.0.1:$Port/search?q=$marker&mode=literal&limit=1" -TimeoutSec 10; if (-not $r.results.Count) { $gone = $true; break } } catch {} }
    Check "delete -> gone from results" $gone "$([int]($i*2))s"
  } else { Check "change propagation (needs a root)" $false "no root" }
  # restart: devices must resolve identically and the index must not be rebuilt
  $docsBefore = $h.documents; $embBefore = $st.indexer.counters.chunks_embedded
  $r = & $semsearch service restart 2>&1; $ok = $LASTEXITCODE -eq 0
  Check "service restart" $ok "$r"
  $h2 = $null; for ($i = 0; $i -lt 60; $i++) { try { $h2 = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 5; if ($h2.ok) { break } } catch {}; Start-Sleep 2 }
  $st2 = $null; try { $st2 = Invoke-RestMethod "http://127.0.0.1:$Port/status" -TimeoutSec 10 } catch {}
  Check "healthy after restart" ($h2 -and $h2.ok) "$([int]($i*2))s, $($h2.documents) docs"
  if ($st2) {
    $same = $true; foreach ($role in @('device','bulk_device','query_device')) { if ($st.devices.roles.$role.resolved -ne $st2.devices.roles.$role.resolved) { $same = $false } }
    Check "devices resolve identically after restart" $same ("steady=" + $st2.devices.roles.device.resolved + " bulk=" + $st2.devices.roles.bulk_device.resolved + " query=" + $st2.devices.roles.query_device.resolved)
    Start-Sleep 20
    $st3 = Invoke-RestMethod "http://127.0.0.1:$Port/status" -TimeoutSec 10
    if ($st.indexer.queue.pending -gt 0 -or $st.indexer.full_build_in_progress) {
      Check "no re-embedding of unchanged content after restart" $true "skipped: initial build still in progress (queue $($st.indexer.queue.pending)); unchanged files are reported as skipped_unchanged=$($st3.indexer.counters.skipped_unchanged)"
    } else {
      Check "no re-embedding of unchanged content after restart" ($st3.indexer.counters.chunks_embedded -le 5) "chunks embedded since restart: $($st3.indexer.counters.chunks_embedded)"
    }
  }
}
# event log entries
$ev = Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='SemSearch'} -MaxEvents 3 -ErrorAction SilentlyContinue
Check "event log lifecycle entries" ($null -ne $ev) ("" + ($ev | Select-Object -First 1 | ForEach-Object { $_.Message.Substring(0, [Math]::Min(80, $_.Message.Length)) }))
Check "log file present" (Test-Path "$DataDir\logs\semsearch.log") "$DataDir\logs\semsearch.log"
Write-Host ("`n{0} failure(s)" -f $fail)
exit $fail
