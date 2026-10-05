"""Ordered extractor chain per extension.

Order of preference:
  text-like   -> TextExtractor
  .pdf        -> pypdf, then Windows IFilter (Windows.Data.Pdf drops word spacing)
  .docx/.pptx/.xlsx -> Windows IFilter (same output the indexer would have produced), then the
                  pure-Python fallback
  .doc/.xls/.ppt/.rtf/.htm(l) -> Windows IFilter only
The first extractor that returns non-empty text wins; statuses from earlier attempts are
kept in ``meta["attempts"]`` for diagnostics.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time

from ..config import Config
from ..models import ExtractResult
from .base import Extractor

log = logging.getLogger(__name__)


class ExtractorRegistry:
    def __init__(self, chains: dict[str, list[Extractor]], default_chain: list[Extractor] | None = None):
        self.chains = {k.lower(): v for k, v in chains.items()}
        self.default_chain = default_chain or []

    def extractors_for(self, extension: str) -> list[Extractor]:
        ext = extension.lower()
        chain = self.chains.get(ext)
        if chain is not None:
            return chain
        return [e for e in self.default_chain if e.supports(ext)]

    def supported_extensions(self) -> set[str]:
        return set(self.chains)

    def extract(self, path: str, extension: str | None = None) -> ExtractResult:
        ext = (extension or os.path.splitext(path)[1]).lower()
        if not os.path.isfile(path):
            return ExtractResult("", "missing", "none", error="file not found")
        chain = self.extractors_for(ext)
        if not chain:
            return ExtractResult("", "unsupported", "none", error=f"no extractor for {ext or '(no extension)'}")
        attempts: list[dict] = []
        last: ExtractResult | None = None
        for ex in chain:
            if not ex.supports(ext):
                continue
            t0 = time.perf_counter()
            try:
                res = ex.extract(path, ext)
            except FileNotFoundError:
                return ExtractResult("", "missing", ex.name, error="file not found")
            except PermissionError as e:
                return ExtractResult("", "denied", ex.name, error=str(e))
            except Exception as e:  # extractor bug or corrupt file: record and try the next one
                res = ExtractResult("", "error", ex.name, error=f"{type(e).__name__}: {e}"[:500])
            attempts.append({"extractor": ex.name, "status": res.status, "ms": round((time.perf_counter() - t0) * 1000), "error": res.error})
            if res.ok:
                res.meta["attempts"] = attempts
                res.text = clean_text(res.text)
                return res
            last = res
        if last is None:
            return ExtractResult("", "unsupported", "none", error=f"no extractor for {ext}")
        last.meta["attempts"] = attempts
        return last


def clean_text(s: str) -> str:
    """Extracted text must be valid Unicode: PDF parsers can emit lone surrogates (broken
    CMaps), which cannot be encoded as UTF-8 and make the tokenizer reject the whole batch
    ("TextEncodeInput must be Union[...]"; seen on a 125-page PDF). Replace them, and drop C0
    control characters other than tab, newline, carriage return and form feed: they carry
    terminal escape sequences (a file could clear or retitle the user's console through the
    CLI's excerpts) and inflate the JSON reply from the extractor child six-fold."""
    if not s:
        return s
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        # lone surrogates -> U+FFFD (a UTF-16 round trip keeps every valid character intact)
        s = s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return _C0.sub("", s)


_C0 = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def build_default_registry(cfg: Config) -> ExtractorRegistry:
    from .office import DocxExtractor, PptxExtractor, XlsxExtractor
    from .pdf import PdfExtractor
    from .text import TextExtractor

    text_exts = set(e.lower() for e in cfg.text_extensions) | set(e.lower() for e in cfg.extra_extensions)
    text = TextExtractor(text_exts, max_chars=cfg.indexing.max_text_chars)
    pdf = PdfExtractor(max_chars=cfg.indexing.max_text_chars)
    mx = cfg.indexing.max_text_chars
    expanded = cfg.indexing.max_expanded_bytes
    docx, pptx, xlsx = DocxExtractor(max_chars=mx, max_expanded=expanded), PptxExtractor(max_chars=mx, max_expanded=expanded), XlsxExtractor(max_chars=mx, max_expanded=expanded)
    ifilter = None
    if sys.platform == "win32":
        try:
            from .ifilter import IFilterExtractor
            ifilter = IFilterExtractor(max_chars=cfg.indexing.max_text_chars)
        except Exception as e:  # comtypes missing etc.
            log.warning("IFilter extractor unavailable: %s", e)

    chains: dict[str, list[Extractor]] = {}
    for e in text_exts:
        chains[e] = [text]
    chains[".pdf"] = [pdf] + ([ifilter] if ifilter else [])
    if cfg.indexing.ocr_scanned_pdfs and sys.platform == "win32":
        from .ocr import WindowsOcrPdfExtractor, available as ocr_available
        if ocr_available():
            chains[".pdf"].append(WindowsOcrPdfExtractor(max_pages=cfg.indexing.ocr_max_pages, max_chars=cfg.indexing.max_text_chars))
        else:
            log.warning("indexing.ocr_scanned_pdfs is on but the pywinrt OCR packages are missing (install the `ocr` extra)")
    chains[".docx"] = ([ifilter] if ifilter else []) + [docx]
    chains[".pptx"] = ([ifilter] if ifilter else []) + [pptx]
    chains[".xlsx"] = ([ifilter] if ifilter else []) + [xlsx]
    chains[".xlsm"] = [xlsx]
    for e in (".doc", ".xls", ".ppt", ".rtf", ".msg", ".eml", ".odt"):
        if ifilter:
            chains[e] = [ifilter]
    # html is text-like for us but the IFilter strips tags nicely; prefer IFilter then text
    for e in (".html", ".htm"):
        chains[e] = ([ifilter] if ifilter else []) + [text]
    for e in cfg.document_extensions:
        if e.lower() not in chains and ifilter:
            chains[e.lower()] = [ifilter]
    return ExtractorRegistry(chains, default_chain=[text] + ([ifilter] if ifilter else []))
