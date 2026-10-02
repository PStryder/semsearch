"""Inventory source interface: where the list of files and their metadata comes from."""
from __future__ import annotations

from typing import Iterator, Protocol

from ..models import FileEntry


class InventorySource(Protocol):
    name: str

    def covers(self, root: str) -> bool:
        """True if this source can enumerate the given root."""
        ...

    def enumerate(self, root: str) -> Iterator[FileEntry]:
        """Yield every file (not directory) under root, recursively."""
        ...

    def changed_since(self, root: str, since_ts: float) -> Iterator[FileEntry]:
        """Yield files whose indexer gather time / mtime is >= since_ts."""
        ...


class LexicalSource(Protocol):
    """Optional: a source that can rank documents by text relevance (Windows Search)."""

    def freetext(self, query: str, roots: list[str], limit: int) -> list[tuple[str, float]]:
        """Return [(path, rank 0..1000)] ordered by relevance."""
        ...
