from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from receipt_bench.storage.validation import validate_id


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["local", "hosted"] = "local"
    host: str | None = None
    port: int = Field(default=8000, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = ()
    trust_forwarded_headers: bool | None = None
    require_https: bool | None = None
    storage_root: Path = Path(".receipt-bench")
    repository_root: Path = Path(".")
    owner_scope: str = "public"
    sroie_rights_evidence: str | None = Field(default=None, max_length=512)
    allow_live_execution: bool | None = None
    start_worker: bool = True
    seed_builtin_projects: bool = False
    seed_cord: bool = False
    seed_sroie: bool = False
    request_max_bytes: int = Field(default=256 * 1024, ge=4 * 1024, le=2 * 1024 * 1024)
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    dataset_package_max_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024 * 1024,
        le=250 * 1024 * 1024,
    )
    dataset_package_uncompressed_max_bytes: int = Field(
        default=250 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024,
    )
    gold_max_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024)
    schema_max_bytes: int = Field(default=64 * 1024, ge=1024, le=512 * 1024)
    schema_max_depth: int = Field(default=16, ge=2, le=64)
    schema_max_nodes: int = Field(default=2_000, ge=16, le=20_000)
    image_max_pixels: int = Field(default=40_000_000, ge=1_000, le=100_000_000)
    image_max_dimension: int = Field(default=20_000, ge=100, le=65_535)
    max_samples_per_project: int = Field(default=100, ge=1, le=500)
    sse_poll_interval: float = Field(default=0.1, gt=0.0, le=10.0)
    sse_heartbeat_interval: float = Field(default=15.0, gt=0.0, le=120.0)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().casefold()
        return (
            candidate[1:-1]
            if candidate.startswith("[") and candidate.endswith("]")
            else candidate
        )

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            item.strip().rstrip(".").casefold() for item in value if item.strip()
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_hosts must be unique")
        if any("/" in item or ":" in item for item in normalized):
            raise ValueError("allowed_hosts entries must be hostnames without ports")
        return normalized

    @field_validator("owner_scope")
    @classmethod
    def validate_owner_scope(cls, value: str) -> str:
        return validate_id(value, field="owner_scope")

    @model_validator(mode="after")
    def validate_profile_defaults(self) -> Self:
        host = self.host or ("127.0.0.1" if self.profile == "local" else "0.0.0.0")
        if self.profile == "local" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Local profile host must be loopback")
        if self.profile == "hosted" and host != "0.0.0.0":
            raise ValueError("Hosted profile host must be 0.0.0.0")
        if self.profile == "local" and self.allow_live_execution is True:
            raise ValueError("Local profile cannot enable live execution")
        if self.profile == "hosted" and self.seed_sroie and not (
            self.sroie_rights_evidence and self.sroie_rights_evidence.strip()
        ):
            raise ValueError("SROIE seeding requires non-empty rights evidence")
        if (self.seed_cord or self.seed_sroie) and not self.seed_builtin_projects:
            raise ValueError("Per-project seeding requires built-in project seeding")
        object.__setattr__(self, "host", host)
        object.__setattr__(
            self,
            "trust_forwarded_headers",
            self.profile == "hosted"
            if self.trust_forwarded_headers is None
            else self.trust_forwarded_headers,
        )
        object.__setattr__(
            self,
            "require_https",
            self.profile == "hosted" if self.require_https is None else self.require_https,
        )
        object.__setattr__(
            self,
            "allow_live_execution",
            self.profile == "hosted"
            if self.allow_live_execution is None
            else self.allow_live_execution,
        )
        return self

    @property
    def default_execution_mode(self) -> Literal["mock", "live"]:
        return "live" if self.profile == "hosted" and self.allow_live_execution else "mock"

    @property
    def sroie_available(self) -> bool:
        return self.profile == "local" or bool(
            self.sroie_rights_evidence and self.sroie_rights_evidence.strip()
        )

    @classmethod
    def from_env(cls, profile: Literal["local", "hosted"] | None = None) -> WebSettings:
        selected = profile or _literal_profile(os.getenv("RECEIPT_WEB_PROFILE", "local"))
        allowed = tuple(
            item.strip()
            for item in os.getenv("RECEIPT_WEB_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        )
        if selected == "hosted" and not allowed:
            website_host = os.getenv("WEBSITE_HOSTNAME", "").strip()
            allowed = (website_host,) if website_host else ("localhost", "testserver")
        port_text = os.getenv("PORT", "8000")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError("PORT must be an integer") from exc
        execution_mode = os.getenv(
            "RECEIPT_EXECUTION_MODE",
            "azure" if selected == "hosted" else "mock",
        ).strip().casefold()
        if execution_mode not in {"mock", "azure"}:
            raise ValueError("RECEIPT_EXECUTION_MODE must be 'mock' or 'azure'")
        seed_builtins = _environment_boolean(
            "RECEIPT_SEED_BUILTIN_PROJECTS",
            default=selected == "local",
        )
        return cls(
            profile=selected,
            port=port,
            allowed_hosts=allowed,
            storage_root=Path(os.getenv("RECEIPT_WEB_STORAGE_ROOT", ".receipt-bench")),
            repository_root=Path(os.getenv("RECEIPT_WEB_REPOSITORY_ROOT", ".")),
            sroie_rights_evidence=os.getenv("RECEIPT_WEB_SROIE_RIGHTS_EVIDENCE") or None,
            allow_live_execution=execution_mode == "azure",
            start_worker=_environment_boolean("RECEIPT_WORKER_ENABLED", default=True),
            seed_builtin_projects=seed_builtins,
            seed_cord=_environment_boolean("RECEIPT_SEED_CORD", default=seed_builtins),
            seed_sroie=_environment_boolean(
                "RECEIPT_SEED_SROIE",
                default=seed_builtins and selected == "local",
            ),
        )


def _literal_profile(value: str) -> Literal["local", "hosted"]:
    normalized = value.strip().casefold()
    if normalized not in {"local", "hosted"}:
        raise ValueError("RECEIPT_WEB_PROFILE must be 'local' or 'hosted'")
    return normalized  # type: ignore[return-value]


def _environment_boolean(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


__all__ = ["WebSettings"]
