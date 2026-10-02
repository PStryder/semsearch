"""OCR for image-only PDFs using the Windows OCR engine (Windows.Media.Ocr) through the
WinRT projection, with pages rendered by Windows.Data.Pdf. No third-party OCR runtime.

Requires the `ocr` extra (pywinrt packages). Enabled by `indexing.ocr_scanned_pdfs`; it runs
only when the text extractors produced nothing, and is bounded by `indexing.ocr_max_pages`.
Languages: whatever OCR language packs Windows has installed (Settings > Time & language).
"""
from __future__ import annotations

import asyncio
import logging

from ..models import ExtractResult

log = logging.getLogger(__name__)


def available() -> bool:
    try:
        import winrt.windows.data.pdf  # noqa: F401
        import winrt.windows.media.ocr  # noqa: F401
        import winrt.windows.graphics.imaging  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


async def _ocr_pdf(path: str, max_pages: int) -> tuple[str, int, int]:
    from winrt.windows.data.pdf import PdfDocument
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage import FileAccessMode, StorageFile
    from winrt.windows.storage.streams import InMemoryRandomAccessStream

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("no OCR language pack installed in Windows")
    file = await StorageFile.get_file_from_path_async(path)
    stream = await file.open_async(FileAccessMode.READ)
    doc = await PdfDocument.load_from_stream_async(stream)
    n = min(int(doc.page_count), max_pages)
    parts: list[str] = []
    for i in range(n):
        page = doc.get_page(i)
        mem = InMemoryRandomAccessStream()
        await page.render_to_stream_async(mem)
        decoder = await BitmapDecoder.create_async(mem)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        text = "\n".join(line.text for line in result.lines)
        if text.strip():
            parts.append(text)
        page.close()
        mem.close()
    stream.close()
    return "\n\n".join(parts), n, int(doc.page_count)


class WindowsOcrPdfExtractor:
    name = "windows-ocr"

    def __init__(self, max_pages: int = 50, max_chars: int = 2_000_000):
        self.max_pages = max_pages
        self.max_chars = max_chars

    def supports(self, extension: str) -> bool:
        return extension.lower() == ".pdf" and available()

    def extract(self, path: str, extension: str) -> ExtractResult:
        if not available():
            return ExtractResult("", "unsupported", self.name, error="pywinrt OCR packages not installed (ocr extra)")
        try:
            text, done, total = asyncio.run(_ocr_pdf(path, self.max_pages))
        except OSError as e:
            code = getattr(e, "winerror", None)
            if code in (-2147023573, 0x8007052B):  # ERROR_WRONG_PASSWORD from Windows.Data.Pdf
                return ExtractResult("", "error", self.name, error="password-protected PDF (cannot be read without the password)")
            return ExtractResult("", "error", self.name, error=f"OCR failed: {type(e).__name__}: {e}"[:400])
        except Exception as e:  # noqa: BLE001
            return ExtractResult("", "error", self.name, error=f"OCR failed: {type(e).__name__}: {e}"[:400])
        text = text[: self.max_chars]
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name, meta={"pages_ocr": done, "pages": total})
