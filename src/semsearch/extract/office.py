"""Pure-Python Office Open XML fallbacks (used when no IFilter is registered or it fails)."""
from __future__ import annotations

from ..models import ExtractResult


class DocxExtractor:
    name = "python-docx"

    def supports(self, extension: str) -> bool:
        return extension.lower() == ".docx"

    def extract(self, path: str, extension: str) -> ExtractResult:
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        text = "\n\n".join(parts)
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name)


class PptxExtractor:
    name = "python-pptx"

    def supports(self, extension: str) -> bool:
        return extension.lower() == ".pptx"

    def extract(self, path: str, extension: str) -> ExtractResult:
        from pptx import Presentation
        prs = Presentation(path)
        parts: list[str] = []
        for i, slide in enumerate(prs.slides, 1):
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
            if len(lines) > 1:
                parts.append("\n".join(lines))
        text = "\n\n".join(parts)
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name, meta={"slides": len(prs.slides)})


class XlsxExtractor:
    name = "openpyxl"

    def __init__(self, max_rows_per_sheet: int = 5000, max_chars: int = 1_000_000):
        self.max_rows = max_rows_per_sheet
        self.max_chars = max_chars

    def supports(self, extension: str) -> bool:
        return extension.lower() in (".xlsx", ".xlsm")

    def extract(self, path: str, extension: str) -> ExtractResult:
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
