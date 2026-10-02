import json
import os
import sys

import pytest

from semsearch.app_state import AppState
from semsearch.config import Config

IS_WIN = sys.platform == "win32"


def write(path, text, mode="w", encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode, encoding=encoding if "b" not in mode else None) as f:
        f.write(text)
    return path


SAMPLE_FILES = {
    "agents.md": "# Agent safety\n\nAutonomous agents must not be allowed to perform destructive actions such as deleting user files. Every irreversible operation needs a human receipt.\n\nAudit logs record every receipt.\n",
    "gpu.txt": "GPU memory architecture: VRAM, L2 cache, and the memory hierarchy of modern accelerators. Bandwidth matters more than capacity for inference.\n",
    "sub/blackboard.py": "class Blackboard:\n    '''Tracks which agent has read which entry (read tracking design).'''\n    def __init__(self):\n        self.reads = {}\n\n    def mark_read(self, agent, key):\n        self.reads.setdefault(key, set()).add(agent)\n",
    "notes.json": json.dumps({"topic": "quarterly budget", "owner": "finance"}),
    "sub/deep/report.yaml": "title: incident report\nsummary: the deploy failed because the database migration locked the users table\n",
}


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "root"
    for rel, text in SAMPLE_FILES.items():
        write(str(r / rel), text)
    with open(r / "bin.dat", "wb") as f:
        f.write(b"\x00\x01\x02" * 100)
    return r


@pytest.fixture
def cfg(root, tmp_path):
    c = Config(roots=[str(root)], data_dir=str(tmp_path / "data"), extra_extensions=[".dat"])
    c.embedding.provider = "hashing"
    c.indexing.use_windows_search = False
    c.indexing.watch_filesystem = False
    c.indexing.poll_interval_s = 0.2
    return c


@pytest.fixture
def app(cfg):
    st = AppState(cfg, start_indexer=False, isolate_extractors=False)
    yield st
    st.close()


def drain(st, limit=10000):
    """Process every queued job in the foreground; return list of (op, path, result)."""
    out = []
    for _ in range(limit):
        job = st.store.next_job()
        if job is None:
            break
        out.append((job["op"], job["path"], st.indexer.process_job(job)))
    return out


@pytest.fixture
def built(app):
    app.indexer.full_build()
    drain(app)
    return app
