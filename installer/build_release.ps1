# Builds a self-contained release directory: dist\SemSearch-<version>\
#   python\      python-build-standalone CPython (relocatable) with semsearch[dml] and all
#                dependencies installed into its own site-packages (no venv layer)
#   installer\   install.ps1 / uninstall.ps1 / validate.ps1
#   VERSION
# Run from the repo root (unelevated). Requires uv.
param(
  [string]$Python = "3.12",
  [string]$Extra = "dml",            # dml | cpu | gpu  (exactly one ONNX Runtime build)
  [string]$OutRoot = "dist"
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$version = (Select-String -Path "src\semsearch\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$out = Join-Path $repo "$OutRoot\SemSearch-$version"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null

"[1/5] building wheel"
uv build --wheel -q
$wheel = Get-ChildItem "dist\semsearch-$version-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "wheel not found" }

"[2/5] installing standalone CPython $Python into $out\python"
uv python install $Python --install-dir "$out\python" | Out-Null
$base = (Get-ChildItem "$out\python\cpython-$Python*\python.exe" | Select-Object -First 1).FullName
if (-not $base) { throw "standalone python not found under $out\python" }

"[3/5] installing semsearch[$Extra] directly into the runtime (no venv: a venv's python.exe is a launcher that"
"      spawns a child interpreter, which the Service Control Manager treats as a foreign process)"
# the standalone build carries a PEP 668 marker meant for OS-managed interpreters; this copy is ours
Remove-Item (Join-Path (Split-Path $base) "Lib\EXTERNALLY-MANAGED") -ErrorAction SilentlyContinue
# -s: ignore the building user's site-packages (%APPDATA%\Python); otherwise pip reports packages
# found there as "already satisfied" and leaves them out of the runtime (bit us with `tokenizers`)
& $base -s -m ensurepip --default-pip | Out-Null
& $base -s -m pip install -q --no-warn-script-location "$($wheel.FullName)[$Extra]"
if ($LASTEXITCODE -ne 0) { throw "pip install into the runtime failed" }

"[4/5] verifying the packaged runtime (native dependencies, user site-packages disabled)"
& $base -s "$repo\installer\verify_runtime.py"
if ($LASTEXITCODE -ne 0) { throw "packaged runtime verification failed" }

"[5/5] copying installer scripts"
Copy-Item "$repo\installer\install.ps1", "$repo\installer\uninstall.ps1", "$repo\installer\validate.ps1", "$repo\installer\verify_runtime.py", "$repo\installer\README.md" "$out\" -Force
Copy-Item "$repo\semsearch.example.yaml" "$out\semsearch.example.yaml"
Set-Content "$out\VERSION" $version -Encoding ascii
"release staged: $out"
