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
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from typing import Any

DEFAULT_TEXT_EXTENSIONS = [
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".vtt", ".srt",
    ".py", ".pyi", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".rs", ".go", ".java", ".kt", ".swift", ".rb", ".php", ".lua", ".sql", ".r", ".jl", ".scala",
    ".ps1", ".psm1", ".sh", ".bash", ".zsh", ".bat", ".cmd",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
    ".css", ".scss", ".tex", ".bib", ".gitignore", ".dockerfile",  # dot-names count as their own extension
]
DEFAULT_DOC_EXTENSIONS = [".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".rtf", ".msg", ".eml"]

DEFAULT_EXCLUDES = [
    "**/.git/**", "**/.hg/**", "**/.svn/**",
    "**/node_modules/**", "**/.venv/**", "**/venv/**", "**/__pycache__/**", "**/.mypy_cache/**",
    "**/.pytest_cache/**", "**/.ruff_cache/**", "**/.tox/**", "**/dist/**", "**/build/**",
    "**/.idea/**", "**/.vs/**", "**/bin/**", "**/obj/**", "**/target/**", "**/.cache/**",
    "**/site-packages/**", "**/*.min.js", "**/*.min.css", "**/*.lock", "**/package-lock.json",
    "**/$RECYCLE.BIN/**", "**/System Volume Information/**",
    # semsearch's own evaluation corpus and indexes (copies of the user's files)
    "**/semsearch/eval/corpus/**", "**/semsearch/eval/index_*/**", "**/semsearch/dist/**",
    # credential-looking locations (path globs) ...
    "**/.ssh/**", "**/.aws/**", "**/.azure/**", "**/.gnupg/**", "**/.kube/**", "**/.docker/config.json",
    # ... and credential-looking FILE names (bare patterns match the file name only, never a folder name);
    # the indexer additionally scans content (security.suspected_secret)
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "*.kdbx", "*.ppk",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*secret*", "*credential*", "*password*", "*passwd*",
    "*api_key*", "*api-key*", "*apikey*", "*.netrc", "_netrc", "*.htpasswd",
]


class EmbeddingConfig(BaseModel):
    provider: Literal["onnx", "sentence-transformers", "hashing"] = "onnx"
    model: str = "BAAI/bge-small-en-v1.5"
    revision: str | None = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"  # pinned commit of the default model (same snapshot an unpinned install resolved to)
    # Devices are "cpu", "cuda[:id]", "dml[:id]", "auto", or a logical name defined under
    # `devices` (resolved to the current DirectML ordinal at startup by stable adapter identity).
    # `device` (alias: steady_state_device) embeds documents in steady state; `bulk_device` takes
    # over during a full build or when the queue is deep; `query_device` embeds search queries.
    # "same" means: same as `device`. A named device that is not present falls back to
    # `fallback_device` with a logged warning; the service never refuses to start over a GPU.
    device: str = "cpu"
    bulk_device: str = "same"
    query_device: str = "same"
    fallback_device: str = "cpu"
    devices: dict[str, dict[str, Any]] = Field(default_factory=lambda: {
        # generic selectors that work on most machines; the installer rewrites them with the exact
        # vendor/device/subsystem ids and PCI address of the adapters it finds
        "integrated-gpu": {"integrated": True},
        "discrete-gpu": {"integrated": False},
    })
    bulk_threshold: int = 500  # queue depth (pending jobs) above which bulk_device is used
    cache_dir: Path | None = None  # Hugging Face cache for model files (default: <data_dir>/models)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, v):
        if isinstance(v, dict) and "steady_state_device" in v:
            v = dict(v)
            v.setdefault("device", v.pop("steady_state_device"))
        return v
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
    allowed_hosts: list[str] = Field(default_factory=list)  # extra Host header values accepted (DNS-rebinding guard)
    log_requests: bool = False
    backup_dir: Path | None = None  # where POST /backup may write (default: <data_dir>/backups)
    # On a machine with several interactive users: also require the admin token for search, /document,
    # /status, /stats and /errors (only /health stays open). The token file is readable by the operator
    # account only, so other local accounts cannot read indexed text through the loopback API.
    read_token: bool = False


class IndexingConfig(BaseModel):
    use_windows_search: bool = True
    poll_interval_s: float = 30.0
    reconcile_interval_s: float = 3600.0
    auto_start: bool = True
    max_file_bytes: int = 50 * 1024 * 1024
    max_text_chars: int = 2_000_000
    max_expanded_bytes: int = 512 * 1024 * 1024  # Office containers whose members expand beyond this are not parsed
    extractor_memory_mb: int = 2048       # commit limit for the extractor child process (Windows job object); 0 = none
    follow_reparse_points: bool = False
    max_attempts: int = 3
    extract_timeout_s: float = 120.0
    watch_filesystem: bool = True
    fs_poll_interval_s: float = 600.0     # roots NOT covered by Windows Search are mtime-scanned at most this often
    skip_suspected_secrets: bool = True   # refuse to index text that contains private keys / API tokens
    reconcile_min_fraction: float = 0.5   # skip tombstoning when an enumeration returns fewer than this fraction of known docs
    startup_reconcile_delay_s: float = 600.0  # first full reconcile this long after start (incremental runs immediately)
    low_priority: bool = True             # run the process at below-normal CPU priority (background work)
    secret_scan_allow: list[str] = Field(default_factory=list)  # path globs exempt from secret screening (documented examples etc.)
    ocr_scanned_pdfs: bool = False        # OCR image-only PDFs with the Windows OCR engine (needs the `ocr` extra; slow)
    ocr_max_pages: int = 50
    bulk_yield_gpu_percent: float = 40.0  # do not use the bulk GPU while other processes keep it busier than this
    vacuum_interval_s: float = 86400.0    # reclaim free pages from the index database this often (idle time)


class ServiceConfig(BaseModel):
    name: str = "SemSearch"
    display_name: str = "Semantic Search (semsearch)"
    shutdown_timeout_s: float = 30.0      # bounded: indexer stop + API drain
    startup_timeout_s: float = 180.0      # model load + store open + API bind before SCM gives up
    event_log: bool = True                # mirror WARNING+ and lifecycle events to the Windows Application log
    integrity_check: Literal["quick", "none"] = "quick"  # SQLite quick_check on open (bounded by max_check_mb)
    integrity_check_max_mb: int = 4096


class RetrievalConfig(BaseModel):
    default_mode: Literal["literal", "semantic", "hybrid"] = "hybrid"
    fusion: Literal["convex", "rrf"] = "convex"  # convex = weighted min-max normalized scores; rrf = reciprocal rank fusion
    rrf_k: int = 60
    semantic_weight: float = 0.6
    lexical_weight: float = 0.4
    candidate_chunks: int = 300
    candidate_docs: int = 100
    use_windows_rank: bool = True
    windows_rank_disable_after: int = 20  # consecutive empty FREETEXT answers before it is paused for an hour
    collapse_duplicates: bool = True      # one hit per identical content; other copies listed on it
    excerpt_chars: int = 420
    vector_cache: bool = True
    vector_cache_dtype: Literal["float32", "float16"] = "float32"  # float16 halves RAM at some query latency cost


class Config(BaseModel):
    data_dir: Path = Field(default_factory=lambda: Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "semsearch")
    # sub-locations default under data_dir; the service install sets data_dir=%ProgramData%\SemSearch
    index_dir: Path | None = None   # <data_dir>/index   (semsearch.db + WAL)
    state_dir: Path | None = None   # <data_dir>/state   (admin token, resolved devices, markers)
    log_dir_override: Path | None = Field(default=None, alias="log_dir")  # <data_dir>/logs
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
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    log_level: str = "INFO"
    source_path: Path | None = None  # where the config was loaded from (not persisted)

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _server_alias(cls, v):
        # `server:` is the production spelling of `api:`
        if isinstance(v, dict) and "server" in v:
            v = dict(v)
            srv = v.pop("server") or {}
            api = dict(v.get("api") or {})
            api.update(srv)
            v["api"] = api
        return v

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        if str(v).upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        return str(v).upper()

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
    def index_path(self) -> Path:
        return Path(os.path.expandvars(str(self.index_dir))) if self.index_dir else self.data_dir / "index"

    @property
    def state_path(self) -> Path:
        return Path(os.path.expandvars(str(self.state_dir))) if self.state_dir else self.data_dir / "state"

    @property
    def db_path(self) -> Path:
        return self.index_path / "semsearch.db"

    @property
    def log_dir(self) -> Path:
        return Path(os.path.expandvars(str(self.log_dir_override))) if self.log_dir_override else self.data_dir / "logs"

    @property
    def model_cache_dir(self) -> Path:
        return Path(os.path.expandvars(str(self.embedding.cache_dir))) if self.embedding.cache_dir else self.data_dir / "models"

    @property
    def backup_dir(self) -> Path:
        return Path(os.path.expandvars(str(self.api.backup_dir))) if self.api.backup_dir else self.data_dir / "backups"

    def validate_for_startup(self) -> list[str]:
        """Problems a human must fix before the service can do useful work. Returns messages;
        empty means OK. Soft problems (a missing root) are returned as warnings by the caller."""
        problems: list[str] = []
        if not self.roots:
            problems.append("no roots configured (roots: [...] in semsearch.yaml)")
        for r in self.roots:
            if not os.path.isabs(str(r)):
                problems.append(f"root is not an absolute path: {r}")
        if self.api.port < 1 or self.api.port > 65535:
            problems.append(f"api.port out of range: {self.api.port}")
        if self.api.host not in ("127.0.0.1", "localhost", "::1") and not self.api.allow_non_loopback:
            problems.append(f"api.host {self.api.host!r} is not loopback; set api.allow_non_loopback: true to allow it")
        if self.retrieval.semantic_weight < 0 or self.retrieval.lexical_weight < 0:
            problems.append("retrieval weights must be non-negative")
        if self.embedding.provider == "onnx":
            from .embed.onnx_provider import parse_device
            for role in ("device", "bulk_device", "query_device", "fallback_device"):
                spec = getattr(self.embedding, role)
                if spec == "same":
                    continue
                low = spec.lower()
                if low in ("cpu", "auto") or low.startswith("cuda") or low.startswith("dml"):
                    try:
                        parse_device(spec)
                    except ValueError:
                        problems.append(f"embedding.{role}: malformed device {spec!r}")
                elif spec not in self.embedding.devices:
                    problems.append(f"embedding.{role}: '{spec}' is not cpu/cuda/dml/auto and not defined under embedding.devices")
        return problems

    def all_extensions(self) -> set[str]:
        return {e.lower() for e in (self.text_extensions + self.document_extensions + self.extra_extensions)}

    def normalized_roots(self) -> list[str]:
        from .security import normalize_path
        return [normalize_path(str(r)) for r in self.roots]


class ConfigError(Exception):
    """Malformed or unusable configuration; the message is meant for a human operator."""


def machine_config_path() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SemSearch" / "semsearch.yaml"


def _candidate_paths(explicit: str | os.PathLike | None) -> list[Path]:
    c: list[Path] = []
    if explicit:
        c.append(Path(explicit))
    env = os.environ.get("SEMSEARCH_CONFIG")
    if env:
        c.append(Path(env))
    c.append(Path.cwd() / "semsearch.yaml")
    c.append(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "semsearch" / "semsearch.yaml")
    c.append(machine_config_path())  # the service install
    return c


def load_config(path: str | os.PathLike | None = None) -> Config:
    for p in _candidate_paths(path):
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                raise ConfigError(f"{p}: not valid YAML: {e}") from e
            if not isinstance(data, dict):
                raise ConfigError(f"{p}: top level must be a mapping")
            try:
                cfg = Config.model_validate(data)
            except ValidationError as e:
                lines = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
                raise ConfigError(f"{p}: {len(lines)} invalid setting(s):\n  " + "\n  ".join(lines)) from e
            cfg.source_path = p
            return cfg
        if path is not None and p == Path(path):
            raise ConfigError(f"config file not found: {p}")
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
