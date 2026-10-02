"""Shared data types."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ExtractStatus = Literal["ok", "empty", "unsupported", "error", "too_large", "binary", "missing", "denied", "skipped"]


@dataclass(slots=True)
class FileEntry:
    """A file as seen by an inventory source (Windows Search or a filesystem crawl)."""
    path: str                     # display path, backslashes
    size: int | None = None
    mtime: float | None = None    # POSIX seconds
    is_dir: bool = False
    source: str = "fs"            # "windows_search" | "fs"
    win_entry_id: int | None = None
    gather_time: float | None = None
    extension: str | None = None


@dataclass(slots=True)
class ExtractResult:
    text: str
    status: ExtractStatus
    method: str
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.text.strip())


@dataclass(slots=True)
class Chunk:
    ordinal: int
    start: int
    end: int
    text: str
    embed_text: str | None = None  # text actually embedded (title header + text); None = text

    @property
    def for_embedding(self) -> str:
        return self.embed_text if self.embed_text is not None else self.text


@dataclass(slots=True)
class ScoreComponents:
    semantic: float | None = None      # cosine similarity of best chunk (0..1)
    lexical: float | None = None       # normalized BM25 (0..1 within result set)
    filename: float | None = None      # 1.0 if filename matched
    windows_rank: float | None = None  # Windows Search System.Search.Rank / 1000 if available
    semantic_rank: int | None = None
    lexical_rank: int | None = None
    rrf: float | None = None


@dataclass(slots=True)
class SearchHit:
    path: str
    filename: str
    score: float
    match_type: str              # semantic | lexical | both | filename
    excerpt: str
    chunk_ordinal: int | None
    modified: float | None
    file_type: str
    size: int | None
    components: ScoreComponents
    why: list[str]
    doc_id: int

    def to_dict(self) -> dict[str, Any]:
        import datetime as _dt
        return {
            "path": self.path,
            "filename": self.filename,
            "score": round(self.score, 4),
            "match_type": self.match_type,
            "excerpt": self.excerpt,
            "chunk_ordinal": self.chunk_ordinal,
            "modified": _dt.datetime.fromtimestamp(self.modified).isoformat(timespec="seconds") if self.modified else None,
            "modified_ts": self.modified,
            "file_type": self.file_type,
            "size": self.size,
            "scores": {
                "semantic": None if self.components.semantic is None else round(self.components.semantic, 4),
                "lexical": None if self.components.lexical is None else round(self.components.lexical, 4),
                "filename": self.components.filename,
                "windows_rank": self.components.windows_rank,
                "semantic_rank": self.components.semantic_rank,
                "lexical_rank": self.components.lexical_rank,
                "rrf": None if self.components.rrf is None else round(self.components.rrf, 5),
            },
            "why": self.why,
            "doc_id": self.doc_id,
        }
