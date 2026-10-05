# Builds a self-contained release directory: dist\SemSearch-<version>\
#   python\                 python-build-standalone CPython (exact version) with semsearch and every
#                           dependency installed from the LOCKED, hash-verified requirement set
#   models\bundled\...      the pinned embedding model as plain files (no network at install time)
#   install.ps1 / uninstall.ps1 / validate.ps1 / *.py   installer and its helpers
#   requirements.lock.txt   what was installed (uv export of uv.lock, with hashes)
#   THIRD-PARTY-NOTICES.txt, sbom.json, model-manifest.json, release-manifest.json, LICENSE, VERSION
# Run from the repo root (unelevated). Requires uv and network (model + interpreter download, once).
param(
  [string]$Python = "3.12.12",      # exact interpreter version: a release must not pick up a newer patch silently
  [string]$Extra = "dml,ocr",       # exactly one of dml | cpu | gpu (one ONNX Runtime build), plus optional ocr
  [string]$OutRoot = "dist",
  [switch]$NoModel,                 # do not bundle the model (the installer then downloads it)
  [switch]$Zip                      # also produce dist\SemSearch-<version>.zip and its .sha256
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$version = (Select-String -Path "src\semsearch\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$out = Join-Path $repo "$OutRoot\SemSearch-$version"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null
$extras = $Extra -split ','
if (($extras | Where-Object { $_ -in @('cpu','dml','gpu') }).Count -ne 1) { throw "Extra must name exactly one ONNX Runtime build (cpu|dml|gpu)" }

"[1/9] building wheel"
uv build --wheel -q
$wheel = Get-ChildItem "dist\semsearch-$version-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "wheel not found" }

"[2/9] exporting the locked dependency set for extras [$Extra] (uv.lock, hashes included)"
$extraArgs = @(); foreach ($e in $extras) { $extraArgs += "--extra"; $extraArgs += $e }
uv export --frozen --no-dev --no-emit-project @extraArgs --format requirements-txt -o "$out\requirements.lock.txt" -q
if ($LASTEXITCODE -ne 0) { throw "uv export failed (is uv.lock up to date? run: uv lock)" }

"[3/9] installing standalone CPython $Python into $out\python"
uv python install $Python --install-dir "$out\python" | Out-Null
$base = (Get-ChildItem "$out\python\cpython-$Python*\python.exe" | Select-Object -First 1).FullName
if (-not $base) { throw "standalone python $Python not found under $out\python" }
$pyDir = Split-Path $base

"[4/9] installing the locked set + semsearch into the runtime (no venv: a venv launcher is a child"
"      process the Service Control Manager treats as foreign)"
# the standalone build carries a PEP 668 marker meant for OS-managed interpreters; this copy is ours
Remove-Item (Join-Path $pyDir "Lib\EXTERNALLY-MANAGED") -ErrorAction SilentlyContinue
# -s: ignore the building user's site-packages (%APPDATA%\Python), or pip reports packages found
# there as satisfied and leaves them out of the runtime (bit us with `tokenizers`)
& $base -s -m ensurepip --default-pip | Out-Null
& $base -s -m pip install -q --no-warn-script-location --require-hashes -r "$out\requirements.lock.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install of the locked requirement set failed" }
& $base -s -m pip install -q --no-warn-script-location --no-deps "$($wheel.FullName)"
if ($LASTEXITCODE -ne 0) { throw "pip install of the semsearch wheel failed" }

"[5/9] pruning the runtime (test suite, IDLE, Tk: not used by the service)"
# pip is needed to build the runtime, never to run it: it would be an installer of code inside
# the service's runtime, and it shows up in the SBOM
foreach ($rel in @("Lib\test", "Lib\idlelib", "Lib\turtledemo", "Lib\tkinter", "tcl", "DLLs\_tkinter.pyd", "DLLs\tcl86t.dll", "DLLs\tk86t.dll",
                   "DLLs\_testcapi.pyd", "DLLs\_testclinic.pyd", "DLLs\_testinternalcapi.pyd", "DLLs\_testbuffer.pyd", "DLLs\_testimportmultiple.pyd",
                   "DLLs\_testmultiphase.pyd", "DLLs\_testsinglephase.pyd", "DLLs\_testconsole.pyd", "Lib\ensurepip")) {
  $p = Join-Path $pyDir $rel
  if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Get-ChildItem (Join-Path $pyDir "Lib\site-packages") -Directory | Where-Object { $_.Name -match '^pip(-[0-9].*\.dist-info)?$' } | Remove-Item -Recurse -Force
Get-ChildItem (Join-Path $pyDir "Scripts") -Filter "pip*.exe" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem $pyDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
# byte-compile once here: %ProgramFiles% is read-only for the service account, so it could not do it later
& $base -s -m compileall -q -j 0 (Join-Path $pyDir "Lib") | Out-Null

"[6/9] verifying the packaged runtime (native dependencies, user site-packages disabled)"
& $base -s "$repo\installer\verify_runtime.py"
if ($LASTEXITCODE -ne 0) { throw "packaged runtime verification failed" }

"[7/9] copying installer scripts, licence and example configuration"
foreach ($f in @("install.ps1", "uninstall.ps1", "validate.ps1", "verify_runtime.py", "relocate_launchers.py", "collect_notices.py", "release_manifest.py", "model_manifest.py", "README.md")) {
  Copy-Item "$repo\installer\$f" "$out\" -Force
}
Copy-Item "$repo\semsearch.example.yaml" "$out\semsearch.example.yaml"
Copy-Item "$repo\LICENSE" "$out\LICENSE"
Set-Content "$out\VERSION" $version -Encoding ascii

if (-not $NoModel) {
  "[8/9] bundling the pinned embedding model"
  & $base -s "$repo\installer\model_manifest.py" $out
  if ($LASTEXITCODE -ne 0) { throw "model bundling failed (network needed once; or build with -NoModel)" }
} else { "[8/9] model not bundled (-NoModel): the installer will download it" }

"[9/9] third-party notices, SBOM and release manifest"
& $base -s "$repo\installer\collect_notices.py" $out
if ($LASTEXITCODE -ne 0) { throw "notice collection failed" }
& $base -s "$repo\installer\release_manifest.py" write $out
if ($LASTEXITCODE -ne 0) { throw "release manifest failed" }
"release staged: $out"

if ($Zip) {
  $zip = Join-Path $repo "$OutRoot\SemSearch-$version.zip"
  if (Test-Path $zip) { Remove-Item -Force $zip }
  "zipping -> $zip"
  Compress-Archive -Path $out -DestinationPath $zip -CompressionLevel Optimal
  $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
  Set-Content "$zip.sha256" "$hash  SemSearch-$version.zip" -Encoding ascii
  "sha256 $hash"
}
