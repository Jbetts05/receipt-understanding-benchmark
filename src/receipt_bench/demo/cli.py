from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import typer
import uvicorn
from pydantic import ValidationError

from receipt_bench.demo.settings import DemoSettings
from receipt_bench.web.settings import WebSettings


class DemoLaunchError(RuntimeError):
    """Raised when local demo prerequisites or safety settings are invalid."""


def _local_url(host: str, port: int) -> str:
    browser_host = host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def _schedule_browser_open(url: str) -> None:
    timer = threading.Timer(0.75, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
    settings: DemoSettings | None = None,
) -> None:
    """Start the cohesive local demo without requiring a frontend toolchain."""
    try:
        resolved = settings or DemoSettings()
        overrides: dict[str, object] = {}
        if host is not None:
            overrides["host"] = host
        if port is not None:
            overrides["port"] = port
        if overrides:
            resolved = DemoSettings.model_validate({**resolved.model_dump(), **overrides})
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]["msg"]
        raise DemoLaunchError(f"Invalid demo settings: {first_error}") from exc

    index_path = Path(resolved.static_dir) / "index.html"
    if not index_path.is_file():
        raise DemoLaunchError(
            "The packaged demo frontend is missing. Reinstall the project from a clean checkout."
        )
    if resolved.mode == "azure":
        raise DemoLaunchError(
            "Azure mode is safety-locked in the local module entry point. "
            "Deploy the hosted Receipt Bench for live execution."
        )

    url = _local_url(resolved.host, resolved.port)
    typer.echo("Starting Receipt Bench in offline mock mode.")
    typer.echo(f"Open {url}")
    if open_browser:
        _schedule_browser_open(url)

    from receipt_bench.web.app import create_local_app

    uvicorn.run(
        create_local_app(
            WebSettings(
                profile="local",
                host=resolved.host,
                port=resolved.port,
            )
        ),
        host=resolved.host,
        port=resolved.port,
        reload=False,
        access_log=False,
        log_level="warning",
    )


def main() -> None:
    run_server()


__all__ = ["DemoLaunchError", "main", "run_server"]
