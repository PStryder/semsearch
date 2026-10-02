"""Direct filesystem crawl, used for roots that Windows Search does not index."""
from __future__ import annotations

import os
from typing import Iterator

from ..models import FileEntry
from ..security import walk_safe


class FilesystemInventory:
    name = "fs"

    def __init__(self, roots: list[str], excludes: list[str], follow_reparse: bool = False):
        self.roots = roots
        self.excludes = excludes
        self.follow_reparse = follow_reparse

    def covers(self, root: str) -> bool:
        return os.path.isdir(root)

    def enumerate(self, root: str) -> Iterator[FileEntry]:
        for path, entry in walk_safe(root, self.roots, self.excludes, self.follow_reparse):
            try:
                st = entry.stat(follow_symlinks=self.follow_reparse)
            except OSError:
                continue
            yield FileEntry(path=path, size=st.st_size, mtime=st.st_mtime, source=self.name,
                            extension=os.path.splitext(path)[1].lower())

    def changed_since(self, root: str, since_ts: float) -> Iterator[FileEntry]:
        for fe in self.enumerate(root):
            if fe.mtime is not None and fe.mtime >= since_ts:
                yield fe
