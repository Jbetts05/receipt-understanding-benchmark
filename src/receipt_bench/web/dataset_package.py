from __future__ import annotations

import hashlib
import io
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from receipt_bench.storage.validation import (
    JsonObject,
    StrictJsonError,
    require_json_object,
    strict_json_bytes,
    strict_json_loads,
    validate_id,
)
from receipt_bench.web.settings import WebSettings

DATASET_MANIFEST_PATH = "dataset.json"
MAX_DATASET_MANIFEST_BYTES = 256 * 1024
MAX_OCR_REFERENCE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1_024


class DatasetPackageError(ValueError):
    def __init__(self, detail: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


class _PackageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _package_path(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 512
        or "\\" in normalized
        or "\x00" in normalized
        or normalized.startswith("/")
    ):
        raise ValueError(f"{field} must be a bounded relative POSIX path")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or str(path) != normalized
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field} must be a normalized relative POSIX path")
    return normalized


class LabeledDatasetSampleManifest(_PackageModel):
    id: str
    image: str
    answer: str
    ocr_text: str | None = None
    title: str | None = Field(default=None, max_length=200)

    @field_validator("id")
    @classmethod
    def validate_sample_id(cls, value: str) -> str:
        return validate_id(value, field="sample_id")

    @field_validator("image", "answer", "ocr_text")
    @classmethod
    def validate_paths(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "path")
        return _package_path(value, field=str(field_name))


class LabeledDatasetManifest(_PackageModel):
    format_version: Literal["1.0"]
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=2_000)
    schema_path: str = Field(alias="schema")
    prompt: str = Field(min_length=1, max_length=20_000)
    selected_solution_ids: list[str] = Field(default_factory=list, max_length=32)
    samples: list[LabeledDatasetSampleManifest] = Field(min_length=1, max_length=500)

    @field_validator("name", "prompt")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped

    @field_validator("schema_path")
    @classmethod
    def validate_schema_path(cls, value: str) -> str:
        return _package_path(value, field="schema")

    @field_validator("selected_solution_ids")
    @classmethod
    def validate_solution_ids(cls, value: list[str]) -> list[str]:
        validated = [
            validate_id(item, field="solution_id")
            for item in value
        ]
        if len(set(validated)) != len(validated):
            raise ValueError("selected_solution_ids must be unique")
        return validated

    @model_validator(mode="after")
    def validate_samples(self) -> Self:
        sample_ids = [sample.id for sample in self.samples]
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("sample IDs must be unique")
        ocr_count = sum(sample.ocr_text is not None for sample in self.samples)
        if ocr_count not in {0, len(self.samples)}:
            raise ValueError(
                "OCR references must be supplied for every sample or omitted entirely"
            )
        return self


@dataclass(frozen=True)
class LabeledDatasetSample:
    sample_id: str
    title: str
    filename: str
    image: bytes
    answer: JsonObject
    ocr_text: str | None
    image_sha256: str
    answer_sha256: str
    ocr_sha256: str | None


@dataclass(frozen=True)
class LabeledDatasetPackage:
    manifest: LabeledDatasetManifest
    schema: JsonObject
    samples: tuple[LabeledDatasetSample, ...]
    archive_sha256: str
    revision_sha256: str

    @property
    def has_ocr_references(self) -> bool:
        return bool(self.samples) and all(sample.ocr_text is not None for sample in self.samples)


def _read_entry(
    archive: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    *,
    max_bytes: int,
) -> bytes:
    if entry.file_size > max_bytes:
        raise DatasetPackageError(
            f"{entry.filename} exceeds the {max_bytes}-byte limit",
            code="package_entry_too_large",
            status_code=413,
        )
    try:
        with archive.open(entry, "r") as stream:
            value = stream.read(max_bytes + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise DatasetPackageError(
            f"Could not read {entry.filename} from the dataset package",
            code="invalid_dataset_package",
        ) from exc
    if len(value) > max_bytes:
        raise DatasetPackageError(
            f"{entry.filename} exceeds the {max_bytes}-byte limit",
            code="package_entry_too_large",
            status_code=413,
        )
    return value


def _archive_entries(
    archive: zipfile.ZipFile,
    settings: WebSettings,
) -> dict[str, zipfile.ZipInfo]:
    entries: dict[str, zipfile.ZipInfo] = {}
    casefolded: set[str] = set()
    total_size = 0
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise DatasetPackageError(
            "Dataset package contains too many archive entries",
            code="dataset_package_too_complex",
        )
    for info in infos:
        if info.is_dir():
            continue
        try:
            name = _package_path(info.filename, field="archive entry")
        except ValueError as exc:
            raise DatasetPackageError(
                f"Dataset package contains an unsafe path: {info.filename}",
                code="unsafe_dataset_package",
            ) from exc
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or info.flag_bits & 0x1:
            raise DatasetPackageError(
                f"Dataset package entry is not a regular unencrypted file: {name}",
                code="unsafe_dataset_package",
            )
        folded = name.casefold()
        if name in entries or folded in casefolded:
            raise DatasetPackageError(
                f"Dataset package contains a duplicate path: {name}",
                code="duplicate_package_path",
            )
        total_size += info.file_size
        if total_size > settings.dataset_package_uncompressed_max_bytes:
            raise DatasetPackageError(
                "Dataset package expands beyond the configured uncompressed limit",
                code="dataset_package_too_large",
                status_code=413,
            )
        entries[name] = info
        casefolded.add(folded)
    return entries


def parse_labeled_dataset_package(
    data: bytes,
    settings: WebSettings,
) -> LabeledDatasetPackage:
    if not data:
        raise DatasetPackageError(
            "Dataset package is empty",
            code="invalid_dataset_package",
        )
    if len(data) > settings.dataset_package_max_bytes:
        raise DatasetPackageError(
            f"Dataset package exceeds the {settings.dataset_package_max_bytes}-byte limit",
            code="dataset_package_too_large",
            status_code=413,
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), mode="r")
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise DatasetPackageError(
            "Dataset package must be a valid ZIP archive",
            code="invalid_dataset_package",
        ) from exc

    with archive:
        entries = _archive_entries(archive, settings)
        manifest_entry = entries.get(DATASET_MANIFEST_PATH)
        if manifest_entry is None:
            raise DatasetPackageError(
                f"Dataset package must contain {DATASET_MANIFEST_PATH} at its root",
                code="dataset_manifest_missing",
            )
        try:
            manifest_value = strict_json_loads(
                _read_entry(
                    archive,
                    manifest_entry,
                    max_bytes=MAX_DATASET_MANIFEST_BYTES,
                ),
                max_bytes=MAX_DATASET_MANIFEST_BYTES,
            )
            manifest = LabeledDatasetManifest.model_validate(manifest_value)
        except (StrictJsonError, ValidationError, ValueError) as exc:
            raise DatasetPackageError(
                "dataset.json does not match the labelled-dataset manifest contract",
                code="invalid_dataset_manifest",
            ) from exc
        if len(manifest.samples) > settings.max_samples_per_project:
            raise DatasetPackageError(
                f"Dataset contains more than {settings.max_samples_per_project} samples",
                code="sample_limit",
            )

        referenced = {DATASET_MANIFEST_PATH, manifest.schema_path}
        for sample in manifest.samples:
            referenced.update({sample.image, sample.answer})
            if sample.ocr_text is not None:
                referenced.add(sample.ocr_text)
        missing = sorted(referenced - set(entries))
        if missing:
            raise DatasetPackageError(
                "Dataset package is missing referenced files: " + ", ".join(missing[:10]),
                code="dataset_files_missing",
            )
        unexpected = sorted(set(entries) - referenced)
        if unexpected:
            raise DatasetPackageError(
                "Dataset package contains files not declared in dataset.json: "
                + ", ".join(unexpected[:10]),
                code="dataset_files_unexpected",
            )

        try:
            schema = require_json_object(
                strict_json_loads(
                    _read_entry(
                        archive,
                        entries[manifest.schema_path],
                        max_bytes=settings.schema_max_bytes,
                    ),
                    max_bytes=settings.schema_max_bytes,
                ),
                field="schema",
            )
        except StrictJsonError as exc:
            raise DatasetPackageError(
                "The declared schema file must contain a strict JSON object",
                code="invalid_schema",
            ) from exc

        samples: list[LabeledDatasetSample] = []
        image_hashes: set[str] = set()
        for declared in manifest.samples:
            image = _read_entry(
                archive,
                entries[declared.image],
                max_bytes=settings.upload_max_bytes,
            )
            image_sha256 = hashlib.sha256(image).hexdigest()
            if image_sha256 in image_hashes:
                raise DatasetPackageError(
                    f"Dataset package contains a duplicate receipt image: {declared.image}",
                    code="duplicate_sample_image",
                )
            image_hashes.add(image_sha256)
            answer_bytes = _read_entry(
                archive,
                entries[declared.answer],
                max_bytes=settings.gold_max_bytes,
            )
            try:
                answer = require_json_object(
                    strict_json_loads(
                        answer_bytes,
                        max_bytes=settings.gold_max_bytes,
                    ),
                    field=f"answer for {declared.id}",
                )
            except StrictJsonError as exc:
                raise DatasetPackageError(
                    f"{declared.answer} must contain a strict JSON object",
                    code="invalid_gold",
                ) from exc
            ocr_text: str | None = None
            ocr_sha256: str | None = None
            if declared.ocr_text is not None:
                ocr_bytes = _read_entry(
                    archive,
                    entries[declared.ocr_text],
                    max_bytes=MAX_OCR_REFERENCE_BYTES,
                )
                try:
                    ocr_text = ocr_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DatasetPackageError(
                        f"{declared.ocr_text} must be UTF-8 text",
                        code="invalid_ocr_reference",
                    ) from exc
                if not ocr_text.strip():
                    raise DatasetPackageError(
                        f"{declared.ocr_text} cannot be empty",
                        code="invalid_ocr_reference",
                    )
                ocr_sha256 = hashlib.sha256(ocr_bytes).hexdigest()
            samples.append(
                LabeledDatasetSample(
                    sample_id=declared.id,
                    title=declared.title or declared.id,
                    filename=PurePosixPath(declared.image).name,
                    image=image,
                    answer=answer,
                    ocr_text=ocr_text,
                    image_sha256=image_sha256,
                    answer_sha256=hashlib.sha256(answer_bytes).hexdigest(),
                    ocr_sha256=ocr_sha256,
                )
            )

    revision_material = "\n".join(
        [
            hashlib.sha256(
                strict_json_bytes(manifest.model_dump(mode="json", by_alias=True))
            ).hexdigest(),
            hashlib.sha256(strict_json_bytes(schema)).hexdigest(),
            *(
                ":".join(
                    filter(
                        None,
                        (
                            sample.sample_id,
                            sample.image_sha256,
                            sample.answer_sha256,
                            sample.ocr_sha256,
                        ),
                    )
                )
                for sample in samples
            ),
        ]
    ).encode()
    return LabeledDatasetPackage(
        manifest=manifest,
        schema=schema,
        samples=tuple(samples),
        archive_sha256=hashlib.sha256(data).hexdigest(),
        revision_sha256=hashlib.sha256(revision_material).hexdigest(),
    )


__all__ = [
    "DATASET_MANIFEST_PATH",
    "DatasetPackageError",
    "LabeledDatasetManifest",
    "LabeledDatasetPackage",
    "LabeledDatasetSample",
    "parse_labeled_dataset_package",
]
