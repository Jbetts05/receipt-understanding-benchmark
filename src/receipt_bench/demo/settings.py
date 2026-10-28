from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class DemoSettings(BaseSettings):
    """Server-only demo settings.

    Mock mode is the fail-safe default. Real provider execution requires both
    ``mode=azure`` and the independent ``allow_live`` opt-in.
    """

    model_config = SettingsConfigDict(
        env_prefix="RECEIPT_DEMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: Literal["mock", "azure"] = "mock"
    allow_live: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    max_parallel: int = Field(default=6, ge=1, le=30)
    provider_concurrency: int = Field(default=2, ge=1, le=10)
    solution_concurrency: int = Field(default=1, ge=1, le=5)
    max_active_runs: int = Field(default=2, ge=1, le=10)
    mock_delay_scale: float = Field(default=1.0, ge=0.0, le=10.0)
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    upload_max_items: int = Field(default=20, ge=1, le=100)
    upload_max_total_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1024,
        le=250 * 1024 * 1024,
    )
    upload_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    upload_cleanup_interval_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    run_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    max_retained_runs: int = Field(default=20, ge=1, le=100)
    benchmark_config_path: Path | None = None
    schema_path: Path | None = None
    pricing_path: Path | None = None
    asset_manifest_path: Path = Path(__file__).resolve().parent / "assets" / "manifest.json"
    static_dir: Path = Path(__file__).resolve().parent / "static"

    @field_validator("host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        candidate = value.strip()
        address = (
            candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
        )
        normalized = address.casefold()
        if normalized == "localhost":
            return "localhost"
        if normalized in {"127.0.0.1", "::1"}:
            return normalized
        raise ValueError("Demo host must be localhost, 127.0.0.1, or ::1")

    @property
    def live_execution_enabled(self) -> bool:
        return self.mode == "azure" and self.allow_live


__all__ = ["PROJECT_ROOT", "DemoSettings"]
