"""PDF text via pypdf (pure Python). Scanned PDFs without a text layer yield 'empty'."""
from __future__ import annotations

import logging

from ..models import ExtractResult

log = logging.getLogger(__name__)


class PdfExtractor:
    name = "pypdf"

    def __init__(self, max_pages: int = 2000, max_chars: int = 2_000_000):
        self.max_pages = max_pages
        self.max_chars = max_chars

    def supports(self, extension: str) -> bool:
        return extension.lower() == ".pdf"

    def extract(self, path: str, extension: str) -> ExtractResult:
        import pypdf
        parts: list[str] = []
        total = 0
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f, strict=False)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    return ExtractResult("", "error", self.name, error="encrypted PDF")
            n = len(reader.pages)
            for i, page in enumerate(reader.pages):
                if i >= self.max_pages or total >= self.max_chars:
                    break
                try:
                    t = page.extract_text() or ""
                except Exception as e:  # pypdf can fail per page on odd fonts
                    log.debug("pypdf page %d of %s failed: %s", i, path, e)
                    t = ""
                if t.strip():
                    parts.append(t)
                    total += len(t)
        text = "\n\n".join(parts)
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name, meta={"pages": n})
