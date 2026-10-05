"""Optional sentence-transformers (PyTorch) provider. Install with the ``st`` extra."""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .base import Kind, l2_normalize


class SentenceTransformersProvider:
    name = "sentence-transformers"

    def __init__(self, model: str, revision: str | None = None, device: str = "cpu", batch_size: int = 32,
                 query_prefix: str = "", document_prefix: str = "", allow_download: bool = True,
                 max_seq_length: int | None = None, **_):
        from sentence_transformers import SentenceTransformer
        dev = None if device == "auto" else device
        self.model = SentenceTransformer(model, revision=revision, device=dev, local_files_only=not allow_download)
        self.model_id = model
        self.revision = self._resolved_revision(model, revision)
        self.device = str(self.model.device)
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        if max_seq_length:
            self.model.max_seq_length = max_seq_length
        self.max_seq_length = int(getattr(self.model, "max_seq_length", 0) or 0)
        self.dim = int(self.model.get_sentence_embedding_dimension())

    @staticmethod
    def _resolved_revision(model: str, revision: str | None) -> str:
        """The commit the cached snapshot came from (so 'main' moving changes the fingerprint),
        or a hash of a local directory's weights."""
        import os
        if os.path.isdir(model):
            import hashlib
            h = hashlib.blake2b(digest_size=8)
            # every file's CONTENT (recursively, in a stable order): names + sizes would miss
            # replaced weights of equal size
            for dp, dn, fn in os.walk(model):
                dn.sort()
                for name in sorted(fn):
                    p = os.path.join(dp, name)
                    h.update(os.path.relpath(p, model).replace("\\", "/").encode("utf-8"))
                    with open(p, "rb") as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            h.update(block)
            return "local-" + h.hexdigest()
        try:
            from huggingface_hub import snapshot_download
            snap = snapshot_download(model, revision=revision, local_files_only=True)
            return os.path.basename(snap)
        except Exception:  # noqa: BLE001
            return revision or "default"

    @property
    def fingerprint(self) -> str:
        import hashlib
        pre = hashlib.blake2b(f"{self.query_prefix}\x00{self.document_prefix}".encode("utf-8"), digest_size=4).hexdigest()
        return f"st:{self.model_id}:{self.revision}:{self.dim}:st:L{self.max_seq_length}:norm:p{pre}"

    def embed(self, texts: Sequence[str], kind: Kind = "document") -> np.ndarray:
        prefix = self.query_prefix if kind == "query" else self.document_prefix
        texts = [prefix + t for t in texts] if prefix else list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        emb = self.model.encode(texts, batch_size=self.batch_size, convert_to_numpy=True, normalize_embeddings=False)
        return l2_normalize(emb)

    def close(self) -> None:
        self.model = None
