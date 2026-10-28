from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import receipt_bench.demo.cli as demo_cli
from receipt_bench.demo.cli import DemoLaunchError, run_server
from receipt_bench.demo.settings import DemoSettings


def test_settings_default_to_mock_without_an_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RECEIPT_DEMO_MODE", raising=False)
    monkeypatch.delenv("RECEIPT_DEMO_ALLOW_LIVE", raising=False)

    settings = DemoSettings(_env_file=None)

    assert settings.mode == "mock"
    assert settings.live_execution_enabled is False
    assert settings.static_dir.joinpath("index.html").is_file()


def test_native_server_prints_one_url_and_uses_product_app(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    def fake_uvicorn_run(app: object, **kwargs: Any) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(demo_cli.uvicorn, "run", fake_uvicorn_run)
    monkeypatch.setattr(
        demo_cli,
        "_schedule_browser_open",
        lambda _: pytest.fail("browser should not open with --no-open"),
    )

    run_server(
        open_browser=False,
        settings=DemoSettings(
            mode="mock",
            allow_live=False,
            host="127.0.0.1",
            port=8124,
        ),
    )

    output = capsys.readouterr().out
    assert output.count("http://127.0.0.1:8124") == 1
    assert "Receipt Bench in offline mock mode" in output
    assert captured["kwargs"]["host"] == "127.0.0.1"
    assert captured["kwargs"]["port"] == 8124
    assert captured["app"].state.web_settings.profile == "local"


def test_native_server_refuses_safety_locked_azure_mode() -> None:
    with pytest.raises(DemoLaunchError, match="safety-locked"):
        run_server(
            open_browser=False,
            settings=DemoSettings(mode="azure", allow_live=False),
        )


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "127.0.0.2", "192.168.1.10", "demo.local"],
)
def test_settings_refuse_non_loopback_bind_addresses(host: str) -> None:
    with pytest.raises(ValidationError, match=r"localhost, 127\.0\.0\.1, or ::1"):
        DemoSettings(host=host)


def test_native_server_validates_command_line_host_override() -> None:
    with pytest.raises(DemoLaunchError, match=r"localhost, 127\.0\.0\.1, or ::1"):
        run_server(
            host="0.0.0.0",
            open_browser=False,
            settings=DemoSettings(),
        )
