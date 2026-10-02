import pytest
from fastapi.testclient import TestClient

from conftest import drain
from semsearch.api import create_app


@pytest.fixture
def client(built, cfg):
    app = create_app(cfg, state=built)
    with TestClient(app, base_url="http://127.0.0.1", headers={"x-semsearch-token": built.admin_token}) as c:
        yield c


def test_maintenance_endpoints_require_admin_token(built, cfg, root):
    app = create_app(cfg, state=built)
    with TestClient(app, base_url="http://127.0.0.1") as c:  # no token
        assert c.post("/search", json={"query": "gpu"}).status_code == 200
        assert c.get("/status").status_code == 200 and c.get("/stats").status_code == 200
        for path, body in [("/index/path", {"path": str(root)}), ("/remove/path", {"path": str(root)}), ("/reindex", {"full": True}),
                           ("/indexer/pause", None), ("/indexer/resume", None), ("/indexer/retry-failed", None),
                           ("/indexer/incremental", None), ("/indexer/reconcile", None)]:
            r = c.post(path, json=body) if body is not None else c.post(path)
            assert r.status_code == 403, path
        assert c.post("/indexer/pause", headers={"x-semsearch-token": "wrong" * 16}).status_code == 403
        assert c.post("/indexer/pause", headers={"x-semsearch-token": built.admin_token}).status_code == 200
        c.post("/indexer/resume", headers={"x-semsearch-token": built.admin_token})
    assert len(built.admin_token) == 64 and (cfg.state_path / "admin.token").read_text() == built.admin_token


def test_health(client):
    r = client.get("/health").json()
    assert r["ok"] and r["documents"] == 6 and r["embedding"].startswith("hashing")


def test_search_post_and_get(client):
    r = client.post("/search", json={"query": "destructive actions by agents", "mode": "hybrid", "limit": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "hybrid" and body["results"][0]["filename"] == "agents.md"
    assert set(body["results"][0]) >= {"path", "filename", "score", "match_type", "excerpt", "modified", "file_type", "scores", "why"}
    g = client.get("/search", params={"q": "*.py", "mode": "literal"}).json()
    assert g["results"][0]["filename"] == "blackboard.py"
    assert client.post("/search", json={"query": "x", "mode": "nope"}).status_code == 422
    assert client.post("/search", json={"query": "x", "limit": 0}).status_code == 422


def test_search_default_mode_is_hybrid(client):
    assert client.post("/search", json={"query": "gpu"}).json()["mode"] == "hybrid"


def test_status_and_stats(client):
    s = client.get("/status").json()
    assert "indexer" in s and "windows_search" in s and "store" in s
    assert s["indexer"]["queue"] == {"pending": 0, "running": 0, "failed": 0}
    st = client.get("/stats").json()
    assert st["documents"] == 6 and st["vectors"] == st["chunks"]
    assert "indexer" in st and "throughput" in st


def test_index_path_rejects_outside_roots(client, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    r = client.post("/index/path", json={"path": str(outside)})
    assert r.status_code == 403


def test_index_remove_and_document(client, built, root):
    p = root / "sub" / "new.md"
    p.write_text("# New\n\nfresh content about caching strategies\n", encoding="utf-8")
    r = client.post("/index/path", json={"path": str(p)}).json()
    assert r == {"enqueued": 1, "kind": "file"}
    drain(built)
    d = client.get("/document", params={"path": str(p), "chunks": "true"}).json()
    assert d["extract_status"] == "ok" and len(d["chunks"]) >= 1
    r = client.post("/remove/path", json={"path": str(p)}).json()
    assert r == {"removed": 1}
    assert client.get("/document", params={"path": str(p)}).status_code == 404
    r = client.post("/index/path", json={"path": str(root / "sub")}).json()
    assert r["kind"] == "directory" and r["enqueued"] >= 2


def test_reindex_validation_and_controls(client):
    assert client.post("/reindex", json={}).status_code == 400
    assert client.post("/reindex", json={"full": True}).json()["full_build"] == "requested"
    assert client.post("/indexer/pause").json() == {"paused": True}
    assert client.post("/indexer/resume").json() == {"paused": False}
    assert "requeued" in client.post("/indexer/retry-failed").json()
    assert "enqueued" in client.post("/indexer/incremental").json()
    assert "removed" in client.post("/indexer/reconcile").json()
    assert "errors" in client.get("/errors").json()
