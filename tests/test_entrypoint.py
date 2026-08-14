"""The console wrapper reports invalid configuration without a traceback."""

from nlab_pipeline import entrypoint


def test_entrypoint_maps_configuration_import_failure(monkeypatch, capsys):
    def fail_cli_import(*_args, **_kwargs):
        raise ValueError("bad environment")

    monkeypatch.setattr(entrypoint, "import_module", fail_cli_import)
    assert entrypoint.main() == 4
    captured = capsys.readouterr()
    assert "配置加载失败" in captured.err
