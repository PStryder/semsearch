"""The document title (first heading or humanized filename) is embedded with every chunk."""
from conftest import drain, write
from semsearch.indexer import derive_title
from semsearch.store.db import text_hash


def test_derive_title_prefers_heading_then_filename():
    assert derive_title("# Agent Safety Rules\n\nbody", r"F:\x\agents.md") == "Agent Safety Rules (agents)"
    assert derive_title("no heading here", r"F:\x\capacity_planning-notes.md") == "capacity planning notes"
    assert derive_title("", r"F:\proj\pcdc\README.md") == "pcdc README"
    assert derive_title("# pcdc readme\n", r"F:\proj\pcdc\README.md") == "pcdc README"  # heading equals the name -> no duplicate


def test_semantic_search_finds_file_by_its_name_only(built, root):
    # body deliberately shares no words with the query; only the filename does
    p = root / "capacity_planning_notes.md"
    write(str(p), "We expect to need twice the servers next quarter and should order early.\n")
    built.indexer.index_path(str(p))
    drain(built)
    chunks = built.store.chunks_for_doc(built.store.get_document(str(p).lower())["id"])
    assert chunks[0]["text_hash"] == text_hash("capacity planning notes\n\n" + chunks[0]["text"])
    assert chunks[0]["text_hash"] != text_hash(chunks[0]["text"])
    r = built.retriever.search("capacity planning", "semantic", 3)
    assert r["results"][0]["filename"] == "capacity_planning_notes.md"
    assert "capacity" not in r["results"][0]["excerpt"].lower()  # excerpt is the stored text, not the header


def test_exact_filename_match_wins_in_hybrid(built, root):
    r = built.retriever.search("blackboard", "hybrid", 3)
    assert r["results"][0]["filename"] == "blackboard.py"
    assert r["results"][0]["score"] >= 0.9
