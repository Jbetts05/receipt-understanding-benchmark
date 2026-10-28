from __future__ import annotations

import threading
import webbrowser

import typer
import uvicorn
from pydantic import ValidationError

from receipt_bench.web.settings import WebSettings


class ProductLaunchError(RuntimeError):
    """Raised when the local receipt laboratory cannot start safely."""


def _local_url(host: str, port: int) -> str:
    browser_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
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
    settings: WebSettings | None = None,
) -> None:
    """Start the packaged receipt laboratory in deterministic local mock mode."""
    try:
        resolved = settings or WebSettings.from_env("local")
        updates: dict[str, object] = {}
        if host is not None:
            updates["host"] = host
        if port is not None:
            updates["port"] = port
        if updates:
            resolved = resolved.model_copy(update=updates)
            resolved = WebSettings.model_validate(resolved.model_dump())
    except (ValidationError, ValueError) as exc:
        message = (
            exc.errors(include_url=False)[0]["msg"]
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise ProductLaunchError(f"Invalid local product settings: {message}") from exc

    from receipt_bench.web.app import create_local_app

    url = _local_url(resolved.host or "127.0.0.1", resolved.port)
    typer.echo("Starting Receipt Bench in offline mock mode.")
    typer.echo(f"Open {url}")
    if open_browser:
        _schedule_browser_open(url)
    uvicorn.run(
        create_local_app(resolved),
        host=resolved.host or "127.0.0.1",
        port=resolved.port,
        reload=False,
        access_log=False,
        log_level="warning",
    )


__all__ = ["ProductLaunchError", "run_server"]
