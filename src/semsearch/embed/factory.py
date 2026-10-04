from __future__ import annotations

from ..config import EmbeddingConfig
from .base import EmbeddingProvider


def create_provider(cfg: EmbeddingConfig) -> EmbeddingProvider:
    if cfg.provider == "hashing":
        from .hashing import HashingProvider
        return HashingProvider()
    if cfg.provider == "onnx":
        from .onnx_provider import OnnxProvider
        return OnnxProvider(model=cfg.model, revision=cfg.revision, device=cfg.device, batch_size=cfg.batch_size,
                            max_seq_length=cfg.max_seq_length, pooling=cfg.pooling, normalize=cfg.normalize,
                            query_prefix=cfg.query_prefix, document_prefix=cfg.document_prefix,
                            allow_download=cfg.allow_download, threads=cfg.threads,
                            bulk_device=cfg.bulk_device, query_device=cfg.query_device)
    if cfg.provider == "sentence-transformers":
        from .st_provider import SentenceTransformersProvider
        return SentenceTransformersProvider(model=cfg.model, revision=cfg.revision, device=cfg.device,
                                            batch_size=cfg.batch_size, query_prefix=cfg.query_prefix,
                                            document_prefix=cfg.document_prefix, allow_download=cfg.allow_download,
                                            max_seq_length=cfg.max_seq_length)
    raise ValueError(f"unknown embedding provider {cfg.provider}")
