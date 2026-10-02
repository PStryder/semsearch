"""Optional sentence-transformers (PyTorch) provider. Install with the ``st`` extra."""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .base import Kind, l2_normalize


class SentenceTransformersProvider:
    name = "sentence-transformers"

    def __init__(self, model: str, revision: str | None = None, device: str = "cpu", batch_size: int = 32,
                 query_prefix: str = "", document_prefix: str = "", **_):
        from sentence_transformers import SentenceTransformer
        dev = None if device == "auto" else device
        self.model = SentenceTransformer(model, revision=revision, device=dev)
        self.model_id = model
        self.revision = revision or "default"
        self.device = str(self.model.device)
        self.batch_size = batch_size
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.dim = int(self.model.get_sentence_embedding_dimension())

    @property
    def fingerprint(self) -> str:
        return f"st:{self.model_id}:{self.revision}:{self.dim}:st"

    def embed(self, texts: Sequence[str], kind: Kind = "document") -> np.ndarray:
        prefix = self.query_prefix if kind == "query" else self.document_prefix
        texts = [prefix + t for t in texts] if prefix else list(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        emb = self.model.encode(texts, batch_size=self.batch_size, convert_to_numpy=True, normalize_embeddings=False)
        return l2_normalize(emb)

    def close(self) -> None:
        self.model = None
