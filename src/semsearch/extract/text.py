"""Plain-text family: source code, Markdown, JSON/YAML/XML, logs, CSV.

Decoding order: BOM sniff (UTF-8/UTF-16/UTF-32), then strict UTF-8, then cp1252 with
replacement. Files with NUL bytes in the first 8 KB are classified as binary.
"""
from __future__ import annotations

import codecs

from ..models import ExtractResult

_BOMS = [
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
]


def decode_bytes(data: bytes) -> tuple[str, str]:
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data.decode(enc, errors="replace"), enc
    head = data[:8192]
    if b"\x00" in head:
        # UTF-16 without BOM: one byte lane is mostly NUL, the other mostly not (ASCII text)
        even, odd = head[0::2], head[1::2]
        if len(odd) >= 4:
            if odd.count(0) > 0.6 * len(odd) and even.count(0) < 0.1 * len(even):
                return data.decode("utf-16-le", errors="replace"), "utf-16-le"
            if even.count(0) > 0.6 * len(even) and odd.count(0) < 0.1 * len(odd):
                return data.decode("utf-16-be", errors="replace"), "utf-16-be"
        raise ValueError("binary")
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


class TextExtractor:
    name = "text"

    def __init__(self, extensions: set[str], max_chars: int = 2_000_000):
        self.extensions = {e.lower() for e in extensions}
        self.max_chars = max_chars

    def supports(self, extension: str) -> bool:
        return extension.lower() in self.extensions

    def extract(self, path: str, extension: str) -> ExtractResult:
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return ExtractResult("", "empty", self.name)
        try:
            text, enc = decode_bytes(data)
        except ValueError:
            return ExtractResult("", "binary", self.name, error="NUL bytes present; not text")
        if len(text) > self.max_chars:
            text = text[: self.max_chars]
        return ExtractResult(text, "ok" if text.strip() else "empty", self.name, meta={"encoding": enc})
