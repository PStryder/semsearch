"""Structure-aware text chunking.

Strategy:
  * normalize line endings; split into blocks on blank lines, Markdown headings, and
    (for code) on top-level definition boundaries
  * greedily pack blocks up to ``target_chars``; a block longer than ``max_chars`` is split
    on sentence boundaries, then on whitespace
  * each chunk carries ``overlap_chars`` of the previous chunk's tail so that a sentence cut
    at the boundary is still retrievable
  * character offsets into the normalized text are kept for excerpts
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ChunkingConfig
from .models import Chunk

_HEADING = re.compile(r"^(#{1,6}\s|={3,}\s*$|-{3,}\s*$)")
_CODE_DEF = re.compile(r"^(def |class |function |fn |pub fn |func |export |public |private |static |#region|///|/\*\*)")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
_WS = re.compile(r"[ \t]+")

CODE_EXTS = {".py", ".pyi", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".cs", ".c", ".h", ".cpp", ".hpp", ".cc", ".rs", ".go",
             ".java", ".kt", ".swift", ".rb", ".php", ".lua", ".sql", ".r", ".jl", ".scala", ".ps1", ".psm1", ".sh",
             ".bash", ".zsh", ".bat", ".cmd", ".css", ".scss"}


def normalize_text(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    t = re.sub(r"(?m)^[ \t\f\v]+$", "", t)   # whitespace-only lines are block gaps, never content
    t = re.sub(r"\n{4,}", "\n\n\n", t)
    return t


@dataclass(slots=True)
class _Block:
    start: int
    end: int
    text: str


def _blocks(text: str, is_code: bool) -> list[_Block]:
    blocks: list[_Block] = []
    pos = 0
    cur_start = 0
    cur: list[str] = []
    lines = text.split("\n")
    for ln in lines:
        ln_start = pos
        pos += len(ln) + 1
        boundary = False
        if not ln.strip():
            boundary = True
        elif not is_code and _HEADING.match(ln):
            boundary = True
        elif is_code and _CODE_DEF.match(ln) and cur:
            boundary = True
        if boundary and cur:
            btxt = "\n".join(cur)
            blocks.append(_Block(cur_start, cur_start + len(btxt), btxt))
            cur = []
        if not cur:
            cur_start = ln_start
        if ln.strip() or (is_code and cur):
            cur.append(ln)
        elif not cur:
            cur_start = pos
    if cur:
        btxt = "\n".join(cur)
        blocks.append(_Block(cur_start, cur_start + len(btxt), btxt))
    return blocks


def _split_long(b: _Block, max_chars: int) -> list[_Block]:
    if len(b.text) <= max_chars:
        return [b]
    out: list[_Block] = []
    pieces = _SENT.split(b.text)
    buf = ""
    buf_start = b.start
    off = 0
    for piece in pieces:
        idx = b.text.find(piece, off)
        if idx < 0:
            idx = off
        off = idx + len(piece)
        # walk an oversized piece with an index: re-slicing the remainder after every cut copied
        # megabytes per cut, which made a 14 MB JSON (one "sentence") take 7 s and 50 MB 78 s
        p0 = 0
        while len(piece) - p0 > max_chars:
            cut = piece.rfind(" ", p0, p0 + max_chars) - p0
            if cut < max_chars // 2:
                cut = max_chars
            if buf:
                out.append(_Block(buf_start, buf_start + len(buf), buf))
                buf = ""
            out.append(_Block(b.start + idx, b.start + idx + cut, piece[p0:p0 + cut]))
            p0 += cut
            idx += cut
        if p0:
            piece = piece[p0:]
        if buf and len(buf) + 1 + len(piece) > max_chars:
            out.append(_Block(buf_start, buf_start + len(buf), buf))
            buf = ""
        if not buf:
            buf_start = b.start + idx
            buf = piece
        else:
            buf = buf + " " + piece
    if buf:
        out.append(_Block(buf_start, buf_start + len(buf), buf))
    return out


def chunk_text(text: str, cfg: ChunkingConfig, extension: str | None = None) -> list[Chunk]:
    text = normalize_text(text)
    if not text.strip():
        return []
    is_code = (extension or "").lower() in CODE_EXTS
    blocks: list[_Block] = []
    for b in _blocks(text, is_code):
        blocks.extend(_split_long(b, cfg.max_chars))

    chunks: list[Chunk] = []
    cur: list[_Block] = []
    cur_len = 0

    def flush():
        nonlocal cur, cur_len
        if not cur:
            return
        start = cur[0].start
        end = cur[-1].end
        body = text[start:end]
        if len(body.strip()) >= cfg.min_chars or not chunks:
            prev_tail = ""
            if chunks and cfg.overlap_chars > 0:
                prev = chunks[-1].text
                tail = prev[-cfg.overlap_chars:]
                sp = tail.find(" ")
                prev_tail = tail[sp + 1:] if sp >= 0 else tail
                if prev_tail.strip():
                    body = prev_tail.strip() + "\n" + body
            chunks.append(Chunk(len(chunks), start, end, body))
        elif chunks:
            # too small: merge into previous chunk
            last = chunks[-1]
            chunks[-1] = Chunk(last.ordinal, last.start, end, last.text + "\n" + body)
        cur = []
        cur_len = 0

    for b in blocks:
        if cur and cur_len + len(b.text) + 2 > cfg.target_chars:
            flush()
        cur.append(b)
        cur_len += len(b.text) + 2
        if len(chunks) >= cfg.max_chunks_per_doc:
            break
    flush()
    return chunks[: cfg.max_chunks_per_doc]
