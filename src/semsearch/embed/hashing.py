"""Deterministic, dependency-free provider for tests.

Maps word tokens into hashed buckets (a bag-of-words sketch). It has no semantics beyond
lexical overlap but is fast, stable, and exercises every code path that handles vectors.
"""
from __future__ import annotations

import hashlib
import re
from typing import Sequence

import numpy as np

from .base import Kind, l2_normalize

_TOKEN = re.compile(r"[a-z0-9]+")


class HashingProvider:
    name = "hashing"

    def __init__(self, dim: int = 128):
        self.model_id = f"hashing-{dim}"
        self.dim = dim
        self.device = "cpu"

    @property
    def fingerprint(self) -> str:
        return f"hashing:{self.model_id}:none:{self.dim}:bow"

    def embed(self, texts: Sequence[str], kind: Kind = "document") -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in _TOKEN.findall(t.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
                out[i, h % self.dim] += 1.0 if (h >> 63) == 0 else -1.0
        return l2_normalize(out)

    def close(self) -> None:
        pass
