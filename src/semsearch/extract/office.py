"""Pure-Python Office Open XML fallbacks (used when no IFilter is registered or it fails).

Every extractor here is bounded twice: the ZIP container's declared uncompressed size is
checked before anything is parsed (a 2 MB .docx can hold gigabytes of XML), and the text is
capped at `max_chars` while it is collected, not after.
"""
from __future__ import annotations

import zipfile

from ..models import ExtractResult

DEFAULT_MAX_EXPANDED = 512 * 1024 * 1024


def zip_expanded_size(path: str) -> int:
    """Sum of the declared uncompressed member sizes (what the parser would have to hold)."""
    with zipfile.ZipFile(path) as z:
        return sum(max(0, i.file_size) for i in z.infolist())


def check_container(path: str, max_expanded: int, name: str) -> ExtractResult | None:
    try:
        size = zip_expanded_size(path)
    except (zipfile.BadZipFile, OSError) as e:
        return ExtractResult("", "error", name, error=f"not a readable Office container: {e}"[:300])
    if size > max_expanded:
        return ExtractResult("", "too_large", name, error=f"container expands to {size // (1024 * 1024)} MB (limit {max_expanded // (1024 * 1024)} MB)")
    return None


class _Collector:
    """Accumulates text parts up to a character budget; says when to stop."""

    def __init__(self, max_chars: int):
        self.max_chars = max_chars
        self.parts: list[str] = []
        self.total = 0
        self.truncated = False

    def add(self, s: str) -> bool:
        if self.total >= self.max_chars:
            self.truncated = True
            return False
        if self.total + len(s) > self.max_chars:
            s = s[: self.max_chars - self.total]
            self.truncated = True
        self.parts.append(s)
        self.total += len(s)
        return not self.truncated

    def text(self, sep: str = "\n\n") -> str:
        return sep.join(self.parts)[: self.max_chars]


class DocxExtractor:
    name = "python-docx"

    def __init__(self, max_chars: int = 2_000_000, max_expanded: int = DEFAULT_MAX_EXPANDED):
        self.max_chars = max_chars
        self.max_expanded = max_expanded

    def supports(self, extension: str) -> bool:
        return extension.lower() == ".docx"

    def extract(self, path: str, extension: str) -> ExtractResult:
        bad = check_container(path, self.max_expanded, self.name)
        if bad:
            return bad
        import docx
        d = docx.Document(path)
        col = _Collector(self.max_chars)
        for p in d.paragraphs:
            if p.text and p.text.strip() and not col.add(p.text):
                break
        if not col.truncated:
            for t in d.tables:
                for row in t.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells) and not col.add(" | ".join(cells)):
                        break
                if col.truncated:
                    break
        text = col.text()
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name, meta={"truncated": True} if col.truncated else {})


class PptxExtractor:
    name = "python-pptx"

    def __init__(self, max_chars: int = 2_000_000, max_expanded: int = DEFAULT_MAX_EXPANDED):
        self.max_chars = max_chars
        self.max_expanded = max_expanded

    def supports(self, extension: str) -> bool:
        return extension.lower() == ".pptx"

    def extract(self, path: str, extension: str) -> ExtractResult:
        bad = check_container(path, self.max_expanded, self.name)
        if bad:
            return bad
        from pptx import Presentation
        prs = Presentation(path)
        col = _Collector(self.max_chars)
        n_slides = 0
        for i, slide in enumerate(prs.slides, 1):
            n_slides = i
            lines = [f"# Slide {i}"]
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        t = "".join(r.text for r in p.runs).strip()
                        if t:
                            lines.append(t)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            lines.append(" | ".join(cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                nt = slide.notes_slide.notes_text_frame.text.strip()
                if nt:
                    lines.append("Notes: " + nt)
            if len(lines) > 1 and not col.add("\n".join(lines)):
                break
        text = col.text()
        meta = {"slides": len(prs.slides)}
        if col.truncated:
            meta["truncated"] = True
            meta["slides_read"] = n_slides
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name, meta=meta)


class XlsxExtractor:
    name = "openpyxl"

    def __init__(self, max_rows_per_sheet: int = 5000, max_chars: int = 1_000_000, max_expanded: int = DEFAULT_MAX_EXPANDED):
        self.max_rows = max_rows_per_sheet
        self.max_chars = max_chars
        self.max_expanded = max_expanded

    def supports(self, extension: str) -> bool:
        return extension.lower() in (".xlsx", ".xlsm")

    def extract(self, path: str, extension: str) -> ExtractResult:
        bad = check_container(path, self.max_expanded, self.name)
        if bad:
            return bad
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts: list[str] = []
        total = 0
        try:
            for ws in wb.worksheets:
                lines = [f"# Sheet: {ws.title}"]
                for r_i, row in enumerate(ws.iter_rows(values_only=True)):
                    if r_i >= self.max_rows or total > self.max_chars:
                        break
                    vals = [str(v).strip() for v in row if v is not None and str(v).strip()]
                    if vals:
                        ln = " | ".join(vals)
                        lines.append(ln)
                        total += len(ln)
                if len(lines) > 1:
                    parts.append("\n".join(lines))
        finally:
            wb.close()
        text = "\n\n".join(parts)
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name)
