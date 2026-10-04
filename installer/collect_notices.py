"""Collect third-party notices and a machine-readable component inventory (SBOM) for a staged
runtime. Run with the STAGED interpreter (python -s collect_notices.py <out_dir>).

Writes:
  THIRD-PARTY-NOTICES.txt   every distribution's name, version, declared licence, and the full
                            text of each licence/notice file shipped inside it, plus the
                            interpreter's own licence and any extra notice files found under
                            site-packages (DirectML, ONNX Runtime third-party notices, ...)
  sbom.json                 CycloneDX 1.5 JSON: one component per distribution (purl, version,
                            licence expression, file hashes of the dist-info RECORD) and one for
                            the interpreter and the embedding model (from model-manifest.json
                            when present)
Nothing here is legal advice: it is the inventory a human reviews.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from importlib import metadata

NOTICE_RE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS|ThirdPartyNotices|Third[-_ ]?Party)", re.I)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def license_of(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    lic = md.get("License")
    if lic and len(lic) < 120 and "\n" not in lic:
        return lic
    for c in md.get_all("Classifier") or []:
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return lic.splitlines()[0].strip() if lic else "UNKNOWN"


def notice_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    out = []
    base = dist.locate_file("")
    for f in dist.files or []:
        p = str(f)
        if ".dist-info" in p and (NOTICE_RE.search(os.path.basename(p)) or "/licenses/" in p.replace("\\", "/")):
            full = os.path.join(str(base), p)
            if os.path.isfile(full):
                out.append((p, full))
    return out


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    prefix = os.path.dirname(os.path.abspath(sys.executable))
    site = next((p for p in sys.path if p.lower().endswith("site-packages")), None)
    comps = []
    lines = [f"THIRD-PARTY NOTICES for SemSearch (runtime at build time: {prefix})",
             f"Generated {datetime.now(timezone.utc).isoformat()}", ""]
    # interpreter
    py_lic = os.path.join(prefix, "LICENSE.txt")
    lines += ["=" * 78, f"CPython {sys.version.split()[0]} (python-build-standalone)", "License: PSF-2.0 and bundled component licences", "=" * 78]
    if os.path.isfile(py_lic):
        lines += [open(py_lic, encoding="utf-8", errors="replace").read(), ""]
    comps.append({"type": "platform", "name": "cpython", "version": sys.version.split()[0],
                  "licenses": [{"license": {"id": "PSF-2.0"}}],
                  "description": "python-build-standalone; see LICENSE.txt in the runtime for bundled OpenSSL/libffi/etc."})
    seen = set()
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        ver = dist.version
        lic = license_of(dist)
        home = dist.metadata.get("Home-page") or ""
        for u in dist.metadata.get_all("Project-URL") or []:
            if u.lower().startswith(("homepage", "source", "repository")):
                home = home or u.split(",", 1)[-1].strip()
        lines += ["=" * 78, f"{name} {ver}", f"License: {lic}", f"URL: {home}" if home else "", "=" * 78]
        files = notice_files(dist)
        if not files:
            lines.append("(no licence file shipped in the distribution; licence per metadata above)")
        for rel, full in files:
            lines += [f"--- {rel} ---", open(full, encoding="utf-8", errors="replace").read().rstrip(), ""]
        lines.append("")
        comp = {"type": "library", "name": name, "version": ver,
                "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{ver}",
                "licenses": [{"license": {"name": lic}}] if lic != "UNKNOWN" else [],
                "properties": [{"name": "semsearch:notice-files", "value": ";".join(r for r, _ in files)}]}
        comps.append(comp)
    # loose notice files outside dist-info (DirectML.dll terms inside onnxruntime, adodbapi, etc.)
    extra = []
    if site:
        for dp, dn, fn in os.walk(site):
            if ".dist-info" in dp:
                continue
            for f in fn:
                if NOTICE_RE.search(f) or f.lower() in ("privacy.md",):
                    extra.append(os.path.join(dp, f))
    if extra:
        lines += ["=" * 78, "ADDITIONAL NOTICE FILES FOUND IN THE RUNTIME (outside dist-info)", "=" * 78]
        for p in sorted(extra):
            lines += [f"--- {os.path.relpath(p, site)} ---", open(p, encoding="utf-8", errors="replace").read().rstrip(), ""]
    # native binaries that ride inside a wheel under terms the wheel does not carry as a file
    known_native = [
        ("onnxruntime/capi/DirectML.dll", "Microsoft DirectML redistributable",
         "Shipped inside the onnxruntime-directml wheel. Governed by the Microsoft DirectML licence (the LICENSE.txt of the "
         "Microsoft.AI.DirectML NuGet package, https://www.nuget.org/packages/Microsoft.AI.DirectML), which permits "
         "redistribution with an application; it is NOT MIT and is not the ONNX Runtime licence. Review that text before "
         "a public release; it is not reproduced here because the wheel does not include it."),
    ]
    found_native = []
    if site:
        for rel, title, note in known_native:
            p = os.path.join(site, rel.replace("/", os.sep))
            if os.path.isfile(p):
                found_native.append((rel, title, note, sha256(p), os.path.getsize(p)))
    if found_native:
        lines += ["=" * 78, "NATIVE BINARIES WITH SEPARATE TERMS (no licence file inside the wheel)", "=" * 78]
        for rel, title, note, digest, size in found_native:
            lines += [f"--- {rel} ({title}; {size} bytes; sha256 {digest}) ---", note, ""]
            comps.append({"type": "library", "name": title, "version": "bundled",
                          "hashes": [{"alg": "SHA-256", "content": digest}],
                          "licenses": [{"license": {"name": "Microsoft DirectML licence (see notes)"}}],
                          "properties": [{"name": "semsearch:path", "value": rel}, {"name": "semsearch:review-required", "value": "true"}]})
    # model
    mm = os.path.join(out_dir, "model-manifest.json")
    if os.path.isfile(mm):
        m = json.load(open(mm, encoding="utf-8"))
        lines += ["=" * 78, f"Embedding model {m['repo']} @ {m['revision']}", f"License: {m.get('license', 'see model card')}",
                  f"Source: {m.get('url', '')}", "=" * 78, m.get("license_text", "") or "(licence per model card; README.md is bundled with the model files)", ""]
        comps.append({"type": "machine-learning-model", "name": m["repo"], "version": m["revision"],
                      "licenses": [{"license": {"id": m["license"]}}] if m.get("license") else [],
                      "hashes": [{"alg": "SHA-256", "content": f["sha256"]} for f in m.get("files", [])],
                      "externalReferences": [{"type": "distribution", "url": m.get("url", "")}]})
    with open(os.path.join(out_dir, "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    bom = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "serialNumber": f"urn:uuid:{uuid.uuid4()}",
           "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(), "tools": [{"name": "semsearch collect_notices.py"}],
                        "component": {"type": "application", "name": "semsearch", "version": metadata.version("semsearch")}},
           "components": comps}
    with open(os.path.join(out_dir, "sbom.json"), "w", encoding="utf-8") as f:
        json.dump(bom, f, indent=1)
    unknown = [c["name"] for c in comps if c["type"] == "library" and not c["licenses"]]
    print(f"notices: {len(comps)} components; licence unknown from metadata for: {unknown or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
