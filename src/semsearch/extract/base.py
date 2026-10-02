from __future__ import annotations

from typing import Protocol

from ..models import ExtractResult


class Extractor(Protocol):
    name: str

    def supports(self, extension: str) -> bool: ...

    def extract(self, path: str, extension: str) -> ExtractResult: ...
