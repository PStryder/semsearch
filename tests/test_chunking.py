from semsearch.chunking import chunk_text, normalize_text
from semsearch.config import ChunkingConfig


def cfg(**kw):
    base = dict(target_chars=200, max_chars=300, overlap_chars=40, min_chars=10, max_chunks_per_doc=50)
    base.update(kw)
    return ChunkingConfig(**base)


def test_empty_text_gives_no_chunks():
    assert chunk_text("", cfg()) == []
    assert chunk_text("   \n\n  ", cfg()) == []


def test_offsets_point_into_normalized_text():
    text = "Para one line.\r\n\r\nPara two is here.\r\n\r\n" + ("Para three. " * 30)
    norm = normalize_text(text)
    chunks = chunk_text(text, cfg())
    assert len(chunks) >= 2
    for ch in chunks:
        body = norm[ch.start:ch.end]
        assert body.strip()
        # the chunk text ends with the body (an overlap prefix may precede it)
        assert ch.text.endswith(body)


def test_long_paragraph_is_split_under_max_chars():
    text = "This is a sentence. " * 100  # 2000 chars, one block
    chunks = chunk_text(text, cfg(target_chars=200, max_chars=300, overlap_chars=0))
    assert len(chunks) > 3
    assert all(len(c.text) <= 300 + 1 for c in chunks)


def test_overlap_carries_tail_of_previous_chunk():
    text = "\n\n".join(f"Paragraph number {i} with some words in it." for i in range(20))
    chunks = chunk_text(text, cfg(target_chars=120, max_chars=200, overlap_chars=30))
    assert len(chunks) > 2
    prev = chunks[0].text
    tail_word = prev.split()[-1]
    assert tail_word in chunks[1].text.split("\n")[0]


def test_headings_start_new_blocks_for_markdown():
    text = "# Title\nintro line\n# Second\nmore\n" * 10
    chunks = chunk_text(text, cfg(target_chars=60, max_chars=100, overlap_chars=0), ".md")
    assert len(chunks) > 1
    assert chunks[0].start == 0


def test_code_splits_on_definitions():
    code = "".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(40))
    chunks = chunk_text(code, cfg(target_chars=150, max_chars=250, overlap_chars=0), ".py")
    assert len(chunks) > 3
    # every chunk begins with a definition because boundaries fall on 'def '
    assert all(c.text.lstrip().startswith("def ") for c in chunks)


def test_max_chunks_per_doc_is_enforced():
    text = "\n\n".join(f"block {i} " + "x" * 100 for i in range(500))
    chunks = chunk_text(text, cfg(max_chunks_per_doc=7))
    assert len(chunks) == 7


def test_ordinals_are_sequential():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(30))
    chunks = chunk_text(text, cfg())
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
