"""Live tests against this machine's Windows Search index. Skipped when unavailable."""
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def win():
    from semsearch.inventory.windows_search import WindowsSearchInventory
    w = WindowsSearchInventory([REPO], ["**/.venv/**", "**/.git/**"])
    if not w.ping():
        pytest.skip("Windows Search OLE DB not available")
    if not w.covers(REPO):
        pytest.skip("repository directory is not in the Windows Search index")
    return w


def test_url_roundtrip():
    from semsearch.inventory.windows_search import path_to_url, url_to_path
    p = r"F:\Some Dir\file name.md"
    assert path_to_url(p) == "file:F:/Some Dir/file name.md"
    assert url_to_path(path_to_url(p)) == p


def test_lookup_and_enumerate_repo(win):
    me = os.path.join(REPO, "README.md")  # a long-lived file: fresh test files may not be gathered yet
    deadline = time.time() + 30
    fe = None
    while time.time() < deadline:
        fe = win.lookup(me)
        if fe:
            break
        time.sleep(1)
    if fe is None:
        pytest.skip("README.md not (yet) in SystemIndex; indexer busy")
    assert fe.path.lower() == me.lower() and fe.size and fe.mtime and fe.win_entry_id
    files = list(win.enumerate(REPO))
    paths = {f.path.lower() for f in files}
    assert me.lower() in paths
    assert not any(".venv" in p for p in paths)
    assert all(not f.is_dir for f in files)


def test_changed_since_and_freetext(win):
    recent = list(win.changed_since(REPO, time.time() - 7 * 86400))
    assert isinstance(recent, list)
    hits = win.freetext("semantic search sidecar", [REPO], limit=5)
    assert isinstance(hits, list)
    for p, rank in hits:
        assert os.path.isabs(p) and 0 <= rank <= 1000


def test_catalog_status():
    from semsearch.inventory.catalog import catalog_status
    s = catalog_status()
    if not s.get("available"):
        pytest.skip(s.get("error"))
    assert s["items"] > 0 and s["status"]
