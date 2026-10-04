"""ONNX Runtime embedding provider (default).

Loads a Hugging Face transformer exported to ONNX (``onnx/model.onnx``) plus its
``tokenizer.json``. The model is fetched once into the HF cache when ``allow_download`` is
true; afterwards everything runs offline. A local directory containing ``model.onnx`` (or
``onnx/model.onnx``) and ``tokenizer.json`` is also accepted as ``model``.

Devices (strings): ``cpu``, ``cuda[:n]`` (onnxruntime-gpu), ``dml[:n]`` (onnxruntime-directml,
any DirectX 12 adapter including integrated GPUs), ``auto`` (first available accelerator).
Three roles can use different devices and share one weight file:

* ``device``       steady-state document embedding
* ``bulk_device``  document embedding while ``set_bulk_mode(True)`` (full builds, deep queues)
* ``query_device`` query embedding (small batches: latency matters, not throughput)

Sessions are created lazily per distinct device. The fingerprint does not include the device:
vectors computed on any of them are interchangeable (fp32 on every provider).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Sequence

import numpy as np

from .base import Kind, l2_normalize

log = logging.getLogger(__name__)


def _files_digest(*paths: Path) -> str:
    import hashlib
    h = hashlib.blake2b(digest_size=8)
    for p in paths:
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
    return h.hexdigest()


def bundled_model_dir(model: str, revision: str | None, cache_dir: str | os.PathLike | None = None) -> Path | None:
    """A release can ship the model as plain files under <cache>/bundled/<owner--name>/<commit>/
    (no Hugging Face cache layout, no symlinks, no network). Returns that snapshot directory
    when present; its name is the commit, so the fingerprint equals a downloaded snapshot's."""
    base = Path(cache_dir or os.environ.get("HF_HOME") or "")
    if not str(base):
        return None
    d = base / "bundled" / model.replace("/", "--")
    if not d.is_dir():
        return None
    if revision:
        snap = d / revision
        return snap if (snap / "tokenizer.json").is_file() else None
    snaps = [s for s in d.iterdir() if s.is_dir() and (s / "tokenizer.json").is_file()]
    return snaps[0] if len(snaps) == 1 else None


def _resolve_model_files(model: str, revision: str | None, allow_download: bool, cache_dir: str | os.PathLike | None = None) -> tuple[Path, Path, str]:
    """Return (model.onnx, tokenizer.json, resolved_revision)."""
    p = Path(os.path.expandvars(os.path.expanduser(model)))
    bundled = None if p.is_dir() else bundled_model_dir(model, revision, cache_dir)
    if bundled is not None:
        onnx = bundled / "onnx" / "model.onnx"
        if not onnx.is_file():
            onnx = bundled / "model.onnx"
        if onnx.is_file():
            return onnx, bundled / "tokenizer.json", bundled.name
    if p.is_dir():
        onnx = p / "model.onnx"
        if not onnx.is_file():
            onnx = p / "onnx" / "model.onnx"
        tok = p / "tokenizer.json"
        if not onnx.is_file() or not tok.is_file():
            raise FileNotFoundError(f"model dir {p} must contain model.onnx (or onnx/model.onnx) and tokenizer.json")
        # a local directory has no revision: hash the files so replacing the weights or the
        # tokenizer in place changes the fingerprint (and triggers a re-embed) like a new revision
        return onnx, tok, "local-" + _files_digest(onnx, tok)
    from huggingface_hub import snapshot_download
    # README and LICENSE ride along: the model card carries the licence terms that must be kept with a bundled copy
    patterns = ["onnx/model.onnx", "tokenizer.json", "tokenizer_config.json", "config.json", "special_tokens_map.json",
                "README.md", "LICENSE", "LICENSE.md", "LICENSE.txt"]
    try:
        snap = snapshot_download(model, revision=revision, allow_patterns=patterns, local_files_only=True)
    except Exception:
        if not allow_download:
            raise RuntimeError(f"model {model} is not cached and allow_download is false")
        log.info("downloading embedding model %s (one-time)", model)
        snap = snapshot_download(model, revision=revision, allow_patterns=patterns)
    snap = Path(snap)
    onnx = snap / "onnx" / "model.onnx"
    if not onnx.is_file():
        onnx = snap / "model.onnx"
    tok = snap / "tokenizer.json"
    if not onnx.is_file():
        raise FileNotFoundError(f"{model}: no onnx/model.onnx in repo snapshot {snap}")
    if not tok.is_file():
        raise FileNotFoundError(f"{model}: no tokenizer.json in repo snapshot {snap}")
    return onnx, tok, snap.name


def parse_device(spec: str) -> tuple[str, int]:
    """'dml:1' -> ('dml', 1); 'cpu' -> ('cpu', 0)."""
    s = (spec or "cpu").strip().lower()
    if ":" in s:
        kind, _, idx = s.partition(":")
        return kind, int(idx or 0)
    return s, 0


def resolve_device(spec: str, available: list[str]) -> tuple[str, int]:
    """Map a requested device to one the installed runtime can provide, warning on fallback."""
    kind, idx = parse_device(spec)
    if kind == "auto":
        if "CUDAExecutionProvider" in available:
            return "cuda", idx
        if "DmlExecutionProvider" in available:
            return "dml", idx
        return "cpu", 0
    if kind == "cuda" and "CUDAExecutionProvider" not in available:
        log.warning("device=%s requested but CUDAExecutionProvider is not available (install the `gpu` extra); using CPU", spec)
        return "cpu", 0
    if kind == "dml" and "DmlExecutionProvider" not in available:
        log.warning("device=%s requested but DmlExecutionProvider is not available (install the `dml` extra); using CPU", spec)
        return "cpu", 0
    if kind not in ("cpu", "cuda", "dml"):
        raise ValueError(f"unknown device {spec!r}; use cpu, cuda[:n], dml[:n] or auto")
    return kind, idx


class OnnxProvider:
    name = "onnx"

    def __init__(self, model: str, revision: str | None = None, device: str = "cpu", batch_size: int = 32,
                 max_seq_length: int = 512, pooling: str = "cls", normalize: bool = True,
                 query_prefix: str = "", document_prefix: str = "", allow_download: bool = True, threads: int = 0,
                 bulk_device: str = "same", query_device: str = "same"):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._ort = ort
        self.model_id = model
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length
        self.pooling = pooling
        self.normalize = normalize
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.threads = threads
        self.onnx_path, tok_path, self.revision = _resolve_model_files(model, revision, allow_download)

        self.tokenizer = Tokenizer.from_file(str(tok_path))
        self.tokenizer.enable_truncation(max_seq_length)
        self.tokenizer.enable_padding(pad_id=self._pad_id(), pad_token="[PAD]")

        avail = ort.get_available_providers()
        self.devices = {
            "steady": resolve_device(device, avail),
            "bulk": resolve_device(device if bulk_device == "same" else bulk_device, avail),
            "query": resolve_device(device if query_device == "same" else query_device, avail),
        }
        self._sessions: dict[tuple[str, int], object] = {}
        self._lock = threading.Lock()
        # DirectML forbids concurrent Run() calls on one session (CPU sessions are thread-safe
        # and queries benefit from running in parallel there); one execution lock per GPU session
        self._run_locks: dict[tuple[str, int], threading.Lock] = {}
        self.bulk_mode = False
        first = self._session(self.devices["steady"])
        self.input_names = {i.name for i in first.get_inputs()}
        out_shape = first.get_outputs()[0].shape
        self.dim = int(out_shape[-1]) if isinstance(out_shape[-1], int) else int(self.embed(["probe"]).shape[1])
        log.info("onnx embedding model %s rev=%s dim=%d pooling=%s devices=%s", model, self.revision, self.dim, pooling, self.device_summary())

    # ---- devices ----
    @property
    def device(self) -> str:
        return self._fmt(self.devices["bulk" if self.bulk_mode else "steady"])

    def device_summary(self) -> dict[str, str]:
        return {role: self._fmt(d) for role, d in self.devices.items()}

    @staticmethod
    def _fmt(d: tuple[str, int]) -> str:
        return "cpu" if d[0] == "cpu" else f"{d[0]}:{d[1]}"

    def set_bulk_mode(self, on: bool) -> None:
        if on != self.bulk_mode:
            self.bulk_mode = on
            log.info("embedding documents on %s (%s)", self.device, "bulk" if on else "steady state")

    def _session(self, dev: tuple[str, int]):
        with self._lock:
            s = self._sessions.get(dev)
            if s is not None:
                return s
            ort = self._ort
            so = ort.SessionOptions()
            if self.threads > 0:
                so.intra_op_num_threads = self.threads
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            kind, idx = dev
            providers: list = ["CPUExecutionProvider"]
            if kind == "cuda":
                providers = [("CUDAExecutionProvider", {"device_id": idx}), "CPUExecutionProvider"]
            elif kind == "dml":
                providers = [("DmlExecutionProvider", {"device_id": idx}), "CPUExecutionProvider"]
                so.enable_mem_pattern = False  # required by the DirectML provider
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            s = ort.InferenceSession(str(self.onnx_path), sess_options=so, providers=providers)
            self._sessions[dev] = s
            return s

    def _run_lock_for(self, dev: tuple[str, int]) -> threading.Lock | None:
        """Serialize Run() on GPU sessions (DirectML requires it; one lock per session, so the
        steady, bulk and query roles only contend when they resolve to the same adapter)."""
        if dev[0] == "cpu":
            return None
        with self._lock:
            lk = self._run_locks.get(dev)
            if lk is None:
                lk = self._run_locks[dev] = threading.Lock()
            return lk

    def _pad_id(self) -> int:
        try:
            pid = self.tokenizer.token_to_id("[PAD]")
            return pid if pid is not None else 0
        except Exception:
            return 0

    @property
    def fingerprint(self) -> str:
        """Everything that changes the vectors: model, revision, dim, pooling, sequence length,
        normalization and the query/document prefixes (hashed). Two configurations with
        different fingerprints never share an index."""
        import hashlib
        pre = hashlib.blake2b(f"{self.query_prefix}\x00{self.document_prefix}".encode("utf-8"), digest_size=4).hexdigest()
        return (f"onnx:{self.model_id}:{self.revision}:{getattr(self, 'dim', '?')}:{self.pooling}"
                f":L{self.max_seq_length}:{'norm' if self.normalize else 'raw'}:p{pre}")

    # ---- inference ----
    def _run(self, session, texts: Sequence[str]) -> np.ndarray:
        enc = self.tokenizer.encode_batch(list(texts))
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        out = session.run(None, feeds)[0]  # (n, seq, dim) last_hidden_state
        if out.ndim == 2:
            emb = out
        elif self.pooling == "cls":
            emb = out[:, 0, :]
        else:
            m = mask[..., None].astype(np.float32)
            emb = (out * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
        return emb.astype(np.float32)

    def embed(self, texts: Sequence[str], kind: Kind = "document") -> np.ndarray:
        prefix = self.query_prefix if kind == "query" else self.document_prefix
        texts = [prefix + t for t in texts] if prefix else list(texts)
        if not texts:
            return np.zeros((0, getattr(self, "dim", 0)), dtype=np.float32)
        role = "query" if kind == "query" else ("bulk" if self.bulk_mode else "steady")
        dev = self.devices[role]
        session = self._session(dev)
        chunks = []
        run_lock = self._run_lock_for(dev)
        for i in range(0, len(texts), self.batch_size):
            if run_lock is None:
                chunks.append(self._run(session, texts[i:i + self.batch_size]))
            else:
                with run_lock:
                    chunks.append(self._run(session, texts[i:i + self.batch_size]))
        emb = np.concatenate(chunks, axis=0)
        return l2_normalize(emb) if self.normalize else emb

    def close(self) -> None:
        with self._lock:
            self._sessions.clear()
