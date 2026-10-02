import json

import yaml

from semsearch.cli import main


def write_cfg(tmp_path, root):
    cfg = {"roots": [str(root)], "data_dir": str(tmp_path / "data"), "extra_extensions": [".dat"],
           "embedding": {"provider": "hashing"},
           "indexing": {"use_windows_search": False, "watch_filesystem": False}}
    p = tmp_path / "semsearch.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(p)


def test_init_config_prints_yaml(capsys):
    assert main(["--init-config"]) == 0
    out = capsys.readouterr().out
    assert yaml.safe_load(out)["api"]["port"] == 8765


def test_local_build_then_query_and_status(tmp_path, root, capsys):
    cfg = write_cfg(tmp_path, root)
    assert main(["--config", cfg, "--build"]) == 0
    out = capsys.readouterr().out
    assert '"indexed": 5' in out
    assert main(["--config", cfg, "--local", "--json", "--literal", "destructive actions"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["results"][0]["filename"] == "agents.md" and res["mode"] == "literal"
    assert main(["--config", cfg, "--local", "--semantic", "GPU memory", "-n", "2", "-v"]) == 0
    out = capsys.readouterr().out
    assert "gpu.txt" in out and "semantic:" in out
    assert main(["--config", cfg, "--local", "--status"]) == 0
    assert "Indexer:" in capsys.readouterr().out
    assert main(["--config", cfg, "--local", "--stats"]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 6


def test_http_mode_reports_unreachable_server(tmp_path, root, capsys):
    cfg = write_cfg(tmp_path, root)
    rc = main(["--config", cfg, "--url", "http://127.0.0.1:1", "hello"])
    assert rc == 3
    assert "cannot reach" in capsys.readouterr().err
