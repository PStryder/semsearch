"""Bundle the embedding model into a release and write model-manifest.json.

    python -s model_manifest.py <stage_dir> [--model REPO] [--revision REV]

Downloads the pinned revision (package default: semsearch.config.EmbeddingConfig) as PLAIN
files into <stage_dir>/models/bundled/<owner--name>/<commit>/ (no Hugging Face cache layout,
no symlinks, so the installer can copy it anywhere) and records repo, commit, every file's
SHA-256 and the licence declared by the model card. The runtime resolves this layout first
(semsearch.embed.onnx_provider.bundled_model_dir), with the same fingerprint a cache snapshot
of that commit would have.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

MIT_FLAGEMBEDDING = """MIT License

Copyright (c) 2022 staoxiao

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
(Source: https://github.com/FlagOpen/FlagEmbedding/blob/master/LICENSE, linked from the
BAAI/bge-small-en-v1.5 model card at the pinned revision, which declares `license: mit`.)
"""

PATTERNS = ["onnx/model.onnx", "tokenizer.json", "tokenizer_config.json", "config.json", "special_tokens_map.json",
            "README.md", "LICENSE", "LICENSE.md", "LICENSE.txt"]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage_dir")
    ap.add_argument("--model")
    ap.add_argument("--revision")
    a = ap.parse_args()
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import HfApi, snapshot_download
    from semsearch.config import EmbeddingConfig
    cfg = EmbeddingConfig()
    model = a.model or cfg.model
    revision = a.revision or cfg.revision
    commit = HfApi().model_info(model, revision=revision).sha
    dest = os.path.join(os.path.abspath(a.stage_dir), "models", "bundled", model.replace("/", "--"), commit)
    os.makedirs(dest, exist_ok=True)
    snapshot_download(model, revision=commit, allow_patterns=PATTERNS, local_dir=dest)
    # local_dir mode leaves a .cache bookkeeping folder behind; the release does not need it
    import shutil
    shutil.rmtree(os.path.join(dest, ".cache"), ignore_errors=True)
    if not os.path.isfile(os.path.join(dest, "onnx", "model.onnx")) or not os.path.isfile(os.path.join(dest, "tokenizer.json")):
        print("model files missing after download", file=sys.stderr)
        return 1
    files = []
    for dp, _, fn in os.walk(dest):
        for f in fn:
            p = os.path.join(dp, f)
            files.append({"path": os.path.relpath(p, dest).replace("\\", "/"), "bytes": os.path.getsize(p), "sha256": sha256(p)})
    files.sort(key=lambda x: x["path"])
    license_id = None
    license_text = ""
    readme = os.path.join(dest, "README.md")
    if os.path.isfile(readme):
        text = open(readme, encoding="utf-8", errors="replace").read()
        # the licence is a key of the YAML front matter, which on model cards can run to many
        # thousands of characters of benchmark tables before it: search the whole front matter
        fm = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---", text, re.S)
        m = re.search(r"^license:\s*([A-Za-z0-9.\-+]+)", fm.group(1) if fm else text[:4000], re.M)
        if m:
            license_id = "MIT" if m.group(1).lower() == "mit" else m.group(1)
    for cand in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        p = os.path.join(dest, cand)
        if os.path.isfile(p):
            license_text = open(p, encoding="utf-8", errors="replace").read()
            break
    if not license_text and license_id == "MIT":
        # the model card declares MIT and links FlagEmbedding's LICENSE; MIT requires the notice
        # itself to travel with copies, so reproduce it in full (not a summary)
        license_text = MIT_FLAGEMBEDDING
    manifest = {
        "repo": model, "revision": commit, "requested_revision": revision,
        "url": f"https://huggingface.co/{model}/tree/{commit}",
        "license": license_id, "license_text": license_text,
        "bundled_path": os.path.relpath(dest, os.path.abspath(a.stage_dir)).replace("\\", "/"),
        "files": files,
        "fingerprint_inputs": {"pooling": cfg.pooling, "max_seq_length": cfg.max_seq_length, "normalize": cfg.normalize,
                               "query_prefix": cfg.query_prefix, "document_prefix": cfg.document_prefix},
    }
    with open(os.path.join(a.stage_dir, "model-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    total = sum(x["bytes"] for x in files)
    print(f"model {model} @ {commit}: {len(files)} files, {total / 1e6:.1f} MB, licence {license_id or 'UNKNOWN (check the model card)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
