"""Embedding provider interface.

The rest of the system only depends on this protocol. A provider is identified by its
``fingerprint`` (provider:model:revision:dim:pooling); vectors from different fingerprints
are never compared.
"""
from __future__ import annotations

from typing import Literal, Protocol, Sequence

import numpy as np

Kind = Literal["document", "query"]


class EmbeddingProvider(Protocol):
    name: str
    model_id: str
    dim: int
    device: str

    @property
    def fingerprint(self) -> str: ...

    def embed(self, texts: Sequence[str], kind: Kind = "document") -> np.ndarray:
        """Return float32 array (n, dim), L2-normalized rows."""
        ...

    def close(self) -> None: ...


def l2_normalize(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return a / n
