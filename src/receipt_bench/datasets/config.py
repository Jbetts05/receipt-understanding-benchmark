from __future__ import annotations

import random
import re
from importlib.resources import files
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from receipt_bench.datasets.errors import DatasetConfigError

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class DatasetConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SroieSourceConfig(DatasetConfigModel):
    """Pinned source configuration for the SROIE mirror.

    Selects the deterministic, zero-padded ids ``000`` through ``{count - 1:0{id_width}}``
    (80 ids -> ``000``-``079`` by default) from a single pinned commit of the
    ``zzzDavid/ICDAR-2019-SROIE`` mirror.

    Provenance note: the pinned mirror's ``data/`` folder is the labeled
    trainval-style data (626 files) redistributed by the mirror maintainer,
    *not* the official ICDAR 2019 SROIE 400-image held-out test split (which
    was never publicly released with ground truth). ``split`` therefore
    documents this as ``"trainval"``, never ``"test"``: these samples may
    overlap with data used to train or evaluate other public OCR/LLM models,
    so any benchmark run against them carries a real train/eval leakage risk
    that must not be misrepresented as an untouched held-out test set.
    """

    repo: str = "zzzDavid/ICDAR-2019-SROIE"
    commit: str
    count: int = Field(default=80, ge=1)
    id_width: int = Field(default=3, ge=1)
    url_template: str = "https://raw.githubusercontent.com/{repo}/{commit}/data/{kind}/{sample_id}.{ext}"
    license_note: str
    split: str = "trainval"

    @field_validator("commit")
    @classmethod
    def _validate_commit(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError(f"commit must be a 40-character hex SHA, got {value!r}")
        return value


class CordSourceConfig(DatasetConfigModel):
    """Pinned source configuration for CORD v2 (20 of the 100 test-split rows).

    ``selected_indices`` is committed here (rather than recomputed at prepare
    time) so the exact sample is auditable in version control, but it is
    validated on load to be exactly
    ``sorted(random.Random(selection_seed).sample(range(total_rows), sample_count))``.
    """

    dataset: str = "naver-clova-ix/cord-v2"
    revision: str
    split: str = "test"
    license: str = "CC-BY-4.0"
    total_rows: int = Field(default=100, ge=1)
    sample_count: int = Field(default=20, ge=1)
    selection_seed: int = 20260828
    selected_indices: list[int]
    rows_api_template: str

    @field_validator("revision")
    @classmethod
    def _validate_revision(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError(f"revision must be a 40-character hex SHA, got {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_selected_indices(self) -> Self:
        if len(self.selected_indices) != self.sample_count:
            raise ValueError(
                f"selected_indices must contain exactly {self.sample_count} entries, "
                f"got {len(self.selected_indices)}"
            )
        if len(set(self.selected_indices)) != len(self.selected_indices):
            raise ValueError("selected_indices must not contain duplicates")
        if list(self.selected_indices) != sorted(self.selected_indices):
            raise ValueError("selected_indices must be sorted ascending")
        if any(index < 0 or index >= self.total_rows for index in self.selected_indices):
            raise ValueError(f"selected_indices must fall within range(0, {self.total_rows})")
        expected = sorted(
            random.Random(self.selection_seed).sample(range(self.total_rows), self.sample_count)
        )
        if list(self.selected_indices) != expected:
            raise ValueError(
                "selected_indices do not match the deterministic selection "
                f"sorted(random.Random({self.selection_seed}).sample("
                f"range({self.total_rows}), {self.sample_count})) = {expected}; "
                f"got {self.selected_indices}"
            )
        return self


class DatasetsConfig(DatasetConfigModel):
    """Top-level schema for ``configs/datasets.yaml``: the approved dataset mix."""

    manifest_path: Path = Path("data/manifests/benchmark.jsonl")
    images_root: Path = Path("data/raw/images")
    sroie: SroieSourceConfig
    cord: CordSourceConfig

    @classmethod
    def _load_text(cls, text: str, *, source: str) -> DatasetsConfig:
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise DatasetConfigError(
                f"Datasets config {source} must contain a YAML mapping at the top level, "
                f"got {type(raw).__name__}"
            )
        try:
            return cls.model_validate(raw)
        except ValueError as exc:
            raise DatasetConfigError(f"Invalid datasets config {source}: {exc}") from exc

    @classmethod
    def load(cls, path: Path) -> DatasetsConfig:
        """Load and validate a dataset acquisition configuration file."""

        if not path.is_file():
            raise FileNotFoundError(f"Datasets config not found: {path}")
        return cls._load_text(path.read_text(encoding="utf-8"), source=str(path))

    @classmethod
    def load_builtin(cls) -> DatasetsConfig:
        """Load the pinned acquisition config packaged with installed wheels."""

        resource = files("receipt_bench.datasets").joinpath(
            "resources",
            "datasets.yaml",
        )
        return cls._load_text(
            resource.read_text(encoding="utf-8"),
            source="receipt_bench.datasets/resources/datasets.yaml",
        )


__all__ = ["CordSourceConfig", "DatasetConfigError", "DatasetsConfig", "SroieSourceConfig"]
