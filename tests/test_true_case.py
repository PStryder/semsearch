from conftest import drain
from semsearch.security import normalize_path, true_case_path


def test_results_show_on_disk_casing_even_when_job_key_is_folded(built, root):
    p = root / "sub" / "MixedCase_Notes.md"
    p.write_text("# Mixed\n\nA note about case preservation in result paths.\n", encoding="utf-8")
    built.store.enqueue(normalize_path(str(p)), "index", 1)  # the folded key is what the queue holds
    drain(built)
    hit = built.retriever.search("case preservation", "literal", 1)["results"][0]
    assert hit["path"].endswith("MixedCase_Notes.md")
    assert hit["filename"] == "MixedCase_Notes.md"


def test_true_case_path_falls_back_for_missing_files(tmp_path):
    missing = str(tmp_path / "Nope.txt").lower()
    assert true_case_path(missing).lower() == missing
