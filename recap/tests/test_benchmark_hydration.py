import importlib.util
from pathlib import Path
from types import SimpleNamespace


def test_benchmark_runs_flat_pipeline_on_disposable_database(
    tmp_path, monkeypatch, capsys
):
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_hydration.py"
    spec = importlib.util.spec_from_file_location("benchmark_hydration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(
            db=str(tmp_path / "benchmark.db"), limit=2, repeats=1
        ),
    )
    module.main()
    output = capsys.readouterr().out
    assert output.count("cold_materialization=") == 5
    assert "database_collection=" in output
    assert "json_encoding=" in output
    assert "cached_materialization=" in output
    assert "roots=0" not in output
    module.main()
    from recap import RecapClient

    with RecapClient.from_sqlite(tmp_path / "benchmark.db") as client, client.namespace("benchmark") as scoped:
        assert len(scoped.list_namespaces()) == 1


def test_documented_direct_backend_example_runs():
    path = Path(__file__).resolve().parents[2] / "docs" / "how-to" / "performance.qmd"
    section = path.read_text().split("This disposable example", 1)[1]
    code = section.split("```python\n", 1)[1].split("```", 1)[0]
    exec(compile(code, str(path), "exec"), {})
