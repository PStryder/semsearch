import pytest


def names(res):
    return [h["filename"] for h in res["results"]]


def test_literal_and_terms_with_or_fallback(built):
    r = built.retriever.search("destructive actions", "literal", 5)
    assert names(r)[0] == "agents.md"
    assert r["results"][0]["match_type"] == "lexical"
    # a term that only exists together with another in no document -> OR fallback still returns something
    r2 = built.retriever.search("destructive zebra", "literal", 5)
    assert "agents.md" in names(r2)


def test_literal_glob_is_a_filename_search(built):
    r = built.retriever.search("*.py", "literal", 5)
    assert names(r) == ["blackboard.py"]
    assert r["results"][0]["match_type"] == "filename"
    assert r["glob"] is True


def test_filename_terms_boost(built):
    r = built.retriever.search("blackboard", "literal", 5)
    assert names(r)[0] == "blackboard.py"
    assert r["results"][0]["scores"]["filename"] == 1.0


def test_semantic_mode_ranks_by_cosine(built):
    r = built.retriever.search("GPU memory architecture", "semantic", 3)
    assert names(r)[0] == "gpu.txt"
    top = r["results"][0]
    assert top["match_type"] == "semantic"
    assert top["scores"]["semantic"] is not None and top["scores"]["lexical"] is None
    assert top["score"] == top["scores"]["semantic"]


def test_hybrid_exposes_component_scores_and_why(built):
    r = built.retriever.search("agents destructive actions", "hybrid", 5)
    top = r["results"][0]
    assert top["filename"] == "agents.md"
    assert top["match_type"] == "both"
    sc = top["scores"]
    assert sc["semantic"] is not None and sc["lexical"] is not None
    assert sc["semantic_rank"] == 1 and sc["lexical_rank"] == 1
    assert sc["rrf"] is not None
    assert 0.0 <= top["score"] <= 1.0
    assert any(w.startswith("semantic:") for w in top["why"]) and any(w.startswith("lexical:") for w in top["why"])
    assert top["excerpt"] and "destructive" in top["excerpt"].lower()
    assert top["modified"] and top["file_type"] == "md" and top["size"] > 0


def test_hybrid_rrf_fusion_option(built, cfg):
    cfg.retrieval.fusion = "rrf"
    r = built.retriever.search("agents destructive actions", "hybrid", 5)
    assert names(r)[0] == "agents.md"
    assert abs(r["results"][0]["score"] - r["results"][0]["scores"]["rrf"]) < 1e-4


def test_filters_by_extension_and_root(built, root):
    r = built.retriever.search("agent", "hybrid", 10, extensions=["py"])
    assert names(r) == ["blackboard.py"]
    r = built.retriever.search("agent", "hybrid", 10, roots=[str(root / "sub")])
    assert all(h["path"].lower().startswith(str(root / "sub").lower()) for h in r["results"])
    assert names(r)


def test_empty_and_bad_mode(built):
    assert built.retriever.search("   ", "hybrid", 5)["results"] == []
    with pytest.raises(ValueError):
        built.retriever.search("x", "weird", 5)


def test_excerpt_windows_around_first_term(built):
    r = built.retriever.search("migration locked", "literal", 1)
    ex = r["results"][0]["excerpt"]
    assert "migration" in ex and len(ex) <= built.cfg.retrieval.excerpt_chars + 6


def test_limit_and_timing_fields(built):
    r = built.retriever.search("the", "hybrid", 2)
    assert len(r["results"]) <= 2
    assert "took_ms" in r and "timings" in r and "candidates" in r
