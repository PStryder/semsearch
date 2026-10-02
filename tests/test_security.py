import os
import subprocess
import sys

import pytest

from semsearch.security import PathRejected, check_indexable, is_excluded, is_within, normalize_path, root_for, walk_safe

WIN = sys.platform == "win32"


def test_normalize_path_forms(tmp_path):
    p = str(tmp_path / "A" / "b.TXT")
    assert normalize_path(p) == normalize_path(p.upper())
    assert normalize_path("\\\\?\\" + p) == normalize_path(p)
    assert normalize_path("file:" + p.replace("\\", "/")) == normalize_path(p)
    assert not normalize_path(str(tmp_path) + "\\").endswith("\\")


def test_is_within_does_not_match_sibling_prefix(tmp_path):
    root = str(tmp_path / "HexyLab")
    os.makedirs(root)
    os.makedirs(str(tmp_path / "HexyLab2"))
    assert is_within(os.path.join(root, "x.txt"), [root])
    assert is_within(root, [root])
    assert not is_within(str(tmp_path / "HexyLab2" / "x.txt"), [root])
    assert not is_within(str(tmp_path / "x.txt"), [root])
    assert root_for(os.path.join(root, "a", "b.txt"), [root, str(tmp_path)]) == normalize_path(root)


def test_is_within_rejects_dotdot_escape(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    assert not is_within(os.path.join(root, "..", "secret.txt"), [root])


def test_excludes_match_directories_and_globs():
    pats = ["**/.git/**", "**/node_modules/**", "**/*.min.js"]
    assert is_excluded(r"F:\proj\.git\config", pats)
    assert is_excluded(r"F:\proj\a\node_modules\b\c.js", pats)
    assert is_excluded(r"F:\proj\dist\app.min.js", pats)
    assert not is_excluded(r"F:\proj\src\app.js", pats)
    assert not is_excluded(r"F:\proj\gitlab\readme.md", pats)


def test_check_indexable_rejects_outside_root_and_dirs(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "a.txt"
    f.write_text("hi")
    (tmp_path / "outside.txt").write_text("no")
    info = check_indexable(str(f), [str(root)])
    assert info.size == 2 and not info.is_dir
    with pytest.raises(PathRejected):
        check_indexable(str(tmp_path / "outside.txt"), [str(root)])
    with pytest.raises(PathRejected):
        check_indexable(str(root), [str(root)])
    with pytest.raises(FileNotFoundError):
        check_indexable(str(root / "missing.txt"), [str(root)])


@pytest.mark.skipif(not WIN, reason="junctions are Windows-only")
def test_junction_is_not_followed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "secret.txt").write_text("secret")
    link = root / "jump"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"mklink failed: {r.stderr or r.stdout}")
    (root / "ok.txt").write_text("fine")
    found = [p for p, _ in walk_safe(str(root), [str(root)], [])]
    assert any(p.endswith("ok.txt") for p in found)
    assert not any("secret.txt" in p for p in found)
    with pytest.raises(PathRejected):
        check_indexable(str(link / "secret.txt"), [str(root)])
    with pytest.raises(PathRejected):
        check_indexable(str(link), [str(root)])


@pytest.mark.skipif(not WIN, reason="symlinks are Windows-only here")
def test_file_symlink_is_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("secret")
    link = root / "link.txt"
    try:
        os.symlink(str(target), str(link))
    except OSError:
        pytest.skip("symlink creation requires developer mode or elevation")
    with pytest.raises(PathRejected):
        check_indexable(str(link), [str(root)])
    found = [p for p, _ in walk_safe(str(root), [str(root)], [])]
    assert found == []
