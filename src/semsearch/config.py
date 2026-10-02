"""Configuration model and loader.

Resolution order for the config file:
  1. explicit path passed to load_config()
  2. $SEMSEARCH_CONFIG
  3. ./semsearch.yaml
  4. %LOCALAPPDATA%/semsearch/semsearch.yaml
If none exists, defaults are used (no roots -> nothing is indexed until configured).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_TEXT_EXTENSIONS = [
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".vtt", ".srt",
    ".py", ".pyi", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".rs", ".go", ".java", ".kt", ".swift", ".rb", ".php", ".lua", ".sql", ".r", ".jl", ".scala",
    ".ps1", ".psm1", ".sh", ".bash", ".zsh", ".bat", ".cmd",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
    ".css", ".scss", ".tex", ".bib", ".env.example", ".gitignore", ".dockerfile",
]
DEFAULT_DOC_EXTENSIONS = [".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".rtf"]

DEFAULT_EXCLUDES = [
    "**/.git/**", "**/.hg/**", "**/.svn/**",
    "**/node_modules/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**", "**/.mypy_cache/**",
    "**/.pytest_cache/**", "**/.ruff_cache/**", "**/.tox/**", "**/dist/**", "**/build/**",
    "**/.idea/**", "**/.vs/**", "**/bin/**", "**/obj/**", "**/target/**", "**/.cache/**",
    "**/site-packages/**", "**/*.min.js", "**/*.min.css", "**/*.lock", "**/package-lock.json",
    "**/$RECYCLE.BIN/**", "**/System Volume Information/**",
]


class EmbeddingConfig(BaseModel):
    provider: Literal["onnx", "sentence-transformers", "hashing"] = "onnx"
    model: str = "BAAI/bge-small-en-v1.5"
    revision: str | None = None
    # Devices are "cpu", "cuda[:id]", "dml[:id]" or "auto". `device` embeds documents in steady
    # state; `bulk_device` takes over during a full build or when the queue is deep (so a fast
    # GPU can do the first pass while an idle integrated GPU handles the trickle afterwards);
    # `query_device` embeds search queries (latency matters more than throughput there).
    # "same" means: same as `device`.
    device: str = "cpu"
    bulk_device: str = "same"
    query_device: str = "same"
    bulk_threshold: int = 500  # queue depth (pending jobs) above which bulk_device is used
    batch_size: int = 32
    max_seq_length: int = 512
    pooling: Literal["cls", "mean"] = "cls"
    normalize: bool = True
    query_prefix: str = "Represent this sentence for searching relevant passages: "
    document_prefix: str = ""
    allow_download: bool = True
    threads: int = 0  # 0 = onnxruntime default
    on_model_change: Literal["reembed", "refuse"] = "reembed"


class ChunkingConfig(BaseModel):
    target_chars: int = 1400
    max_chars: int = 2200
    overlap_chars: int = 180
    min_chars: int = 40
    max_chunks_per_doc: int = 400


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    allow_non_loopback: bool = False
    log_requests: bool = False


class IndexingConfig(BaseModel):
    use_windows_search: bool = True
    poll_interval_s: float = 30.0
    reconcile_interval_s: float = 3600.0
    auto_start: bool = True
    max_file_bytes: int = 50 * 1024 * 1024
    max_text_chars: int = 2_000_000
    follow_reparse_points: bool = False
    max_attempts: int = 3
    extract_timeout_s: float = 120.0
    watch_filesystem: bool = True


class RetrievalConfig(BaseModel):
    default_mode: Literal["literal", "semantic", "hybrid"] = "hybrid"
    fusion: Literal["convex", "rrf"] = "convex"  # convex = weighted min-max normalized scores; rrf = reciprocal rank fusion
    rrf_k: int = 60
    semantic_weight: float = 0.6
    lexical_weight: float = 0.4
    candidate_chunks: int = 300
    candidate_docs: int = 100
    use_windows_rank: bool = True
    excerpt_chars: int = 420
    vector_cache: bool = True


class Config(BaseModel):
    data_dir: Path = Field(default_factory=lambda: Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "semsearch")
    roots: list[Path] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    text_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_TEXT_EXTENSIONS))
    document_extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_DOC_EXTENSIONS))
    extra_extensions: list[str] = Field(default_factory=list)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    log_level: str = "INFO"
    source_path: Path | None = None  # where the config was loaded from (not persisted)

    @field_validator("roots", mode="before")
    @classmethod
    def _expand_roots(cls, v):
        # return strings: pydantic's JSON-mode path validation rejects Path objects from a before-validator
        return [os.path.expandvars(os.path.expanduser(str(r))) for r in (v or [])]

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand_data_dir(cls, v):
        return os.path.expandvars(os.path.expanduser(str(v)))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "semsearch.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def all_extensions(self) -> set[str]:
        return {e.lower() for e in (self.text_extensions + self.document_extensions + self.extra_extensions)}

    def normalized_roots(self) -> list[str]:
        from .security import normalize_path
        return [normalize_path(str(r)) for r in self.roots]


def _candidate_paths(explicit: str | os.PathLike | None) -> list[Path]:
    c: list[Path] = []
    if explicit:
        c.append(Path(explicit))
    env = os.environ.get("SEMSEARCH_CONFIG")
    if env:
        c.append(Path(env))
    c.append(Path.cwd() / "semsearch.yaml")
    c.append(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "semsearch" / "semsearch.yaml")
    return c


def load_config(path: str | os.PathLike | None = None) -> Config:
    for p in _candidate_paths(path):
        if p.is_file():
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = Config.model_validate(data)
            cfg.source_path = p
            return cfg
        if path is not None and p == Path(path):
            raise FileNotFoundError(f"config file not found: {p}")
    return Config()


def example_yaml() -> str:
    return """# semsearch configuration
data_dir: "%LOCALAPPDATA%/semsearch"
roots:
  - "F:/HexyLab"
# excludes: glob patterns matched against the full path (forward slashes)
# excludes: ["**/.git/**", "**/node_modules/**"]
embedding:
  provider: onnx            # onnx | sentence-transformers | hashing (tests only)
  model: BAAI/bge-small-en-v1.5
  device: cpu               # cpu | cuda | auto
  batch_size: 32
api:
  host: 127.0.0.1
  port: 8765
indexing:
  use_windows_search: true
  poll_interval_s: 30
  reconcile_interval_s: 3600
  max_file_bytes: 52428800
retrieval:
  default_mode: hybrid
"""
