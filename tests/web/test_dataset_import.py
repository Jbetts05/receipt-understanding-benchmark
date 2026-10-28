from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from receipt_bench.web.dataset_package import (
    DatasetPackageError,
    parse_labeled_dataset_package,
)
from receipt_bench.web.service import ProductError
from receipt_bench.web.settings import WebSettings

from .conftest import PNG_BYTES, TEST_SCHEMA


def _package(
    *,
    name: str = "Imported receipts",
    include_answer: bool = True,
    include_ocr: bool = False,
    image_path: str = "images/receipt-001.png",
    extra_files: dict[str, bytes] | None = None,
) -> bytes:
    sample: dict[str, Any] = {
        "id": "receipt-001",
        "title": "Receipt one",
        "image": image_path,
        "answer": "answers/receipt-001.json",
    }
    if include_ocr:
        sample["ocr_text"] = "ocr/receipt-001.txt"
    manifest = {
        "format_version": "1.0",
        "name": name,
        "description": "Complete labelled package",
        "schema": "schema.json",
        "prompt": "Extract the labelled receipt fields and return strict JSON.",
        "samples": [sample],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dataset.json", json.dumps(manifest))
        archive.writestr("schema.json", json.dumps(TEST_SCHEMA))
        archive.writestr(image_path, PNG_BYTES)
        if include_answer:
            archive.writestr(
                "answers/receipt-001.json",
                json.dumps({"merchant": "Contoso", "total": 12.5}),
            )
        if include_ocr:
            archive.writestr("ocr/receipt-001.txt", "CONTOSO\nTOTAL 12.50\n")
        for path, value in (extra_files or {}).items():
            archive.writestr(path, value)
    return output.getvalue()


def _import(
    client: TestClient,
    package: bytes,
    *,
    filename: str = "dataset.zip",
) -> Response:
    return client.post(
        "/api/projects/import-labeled",
        files={"package": (filename, package, "application/zip")},
    )


def _two_sample_package(*, partial_ocr: bool = False) -> bytes:
    samples: list[dict[str, str]] = [
        {
            "id": "receipt-001",
            "image": "images/receipt-001.png",
            "answer": "answers/receipt-001.json",
            "ocr_text": "ocr/receipt-001.txt",
        },
        {
            "id": "receipt-002",
            "image": "images/receipt-002.png",
            "answer": "answers/receipt-002.json",
        },
    ]
    if not partial_ocr:
        samples[1]["ocr_text"] = "ocr/receipt-002.txt"
    manifest = {
        "format_version": "1.0",
        "name": "Two receipt package",
        "schema": "schema.json",
        "prompt": "Extract the labelled fields.",
        "samples": samples,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dataset.json", json.dumps(manifest))
        archive.writestr("schema.json", json.dumps(TEST_SCHEMA))
        for index in (1, 2):
            archive.writestr(f"images/receipt-00{index}.png", PNG_BYTES)
            archive.writestr(
                f"answers/receipt-00{index}.json",
                json.dumps({"merchant": f"Merchant {index}", "total": float(index)}),
            )
        archive.writestr("ocr/receipt-001.txt", "MERCHANT 1\nTOTAL 1.00")
        if not partial_ocr:
            archive.writestr("ocr/receipt-002.txt", "MERCHANT 2\nTOTAL 2.00")
    return output.getvalue()


def test_import_complete_labelled_dataset_freezes_revision(
    client: TestClient,
) -> None:
    response = _import(client, _package())

    assert response.status_code == 201, response.text
    project = response.json()
    assert project["import_mode"] == "labeled_package"
    assert project["dataset_state"] == "frozen"
    assert len(project["dataset_revision_hash"]) == 64
    assert project["sample_count"] == 1
    assert project["gold_sample_count"] == 1
    assert project["scored_sample_count"] == 1
    assert project["has_ocr_references"] is False
    assert len(project["selected_solution_ids"]) == 22

    declarations = {
        item["solution_id"]: item for item in project["compatibility"]
    }
    assert declarations["gpt-5.6-luna"]["compatibility"] == "full"
    assert declarations["content-understanding-custom"]["compatibility"] == "full"
    assert (
        declarations["content-understanding-prebuilt-receipt"]["compatibility"]
        == "partial_reference"
    )
    assert (
        declarations["azure-vision-read-text"]["compatibility"]
        == "partial_reference"
    )

    samples = client.get(f"/api/projects/{project['id']}/samples").json()
    assert [sample["id"] for sample in samples] == ["receipt-001"]
    assert samples[0]["gold"] == {"merchant": "Contoso", "total": 12.5}

    frozen = client.post(
        f"/api/projects/{project['id']}/samples",
        content=PNG_BYTES,
        headers={
            "Content-Type": "image/png",
            "X-Filename-B64": "cmVjZWlwdC0wMDIucG5n",
        },
    )
    assert frozen.status_code == 409
    assert frozen.json()["code"] == "dataset_frozen"


def test_imported_ocr_references_enable_standalone_ocr_scoring(
    client: TestClient,
) -> None:
    response = _import(
        client,
        _package(name="Imported OCR receipts", include_ocr=True),
    )

    assert response.status_code == 201, response.text
    project = response.json()
    assert project["has_ocr_references"] is True
    declaration = next(
        item
        for item in project["compatibility"]
        if item["solution_id"] == "azure-vision-read-text"
    )
    assert declaration["compatibility"] == "full"
    assert declaration["comparable_quality"] is True


def test_import_rejects_missing_unexpected_and_unsafe_files_before_creation(
    client: TestClient,
) -> None:
    initial_ids = {project["id"] for project in client.get("/api/projects").json()}

    missing = _import(client, _package(include_answer=False))
    assert missing.status_code == 422
    assert missing.json()["code"] == "dataset_files_missing"

    unexpected = _import(
        client,
        _package(extra_files={"notes.txt": b"not declared"}),
    )
    assert unexpected.status_code == 422
    assert unexpected.json()["code"] == "dataset_files_unexpected"

    unsafe = _import(
        client,
        _package(image_path="../receipt.png"),
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["code"] in {
        "invalid_dataset_manifest",
        "unsafe_dataset_package",
    }

    final_ids = {project["id"] for project in client.get("/api/projects").json()}
    assert final_ids == initial_ids


def test_import_rejects_answer_that_does_not_match_schema(
    client: TestClient,
) -> None:
    package = _package()
    source = zipfile.ZipFile(io.BytesIO(package))
    output = io.BytesIO()
    with source, zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for info in source.infolist():
            value = source.read(info.filename)
            if info.filename == "answers/receipt-001.json":
                value = json.dumps({"merchant": "Contoso"}).encode()
            archive.writestr(info.filename, value)

    response = _import(client, output.getvalue())

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_gold"
    assert "does not match" in response.json()["detail"]


def test_import_rejects_duplicate_receipt_images(client: TestClient) -> None:
    response = _import(client, _two_sample_package())

    assert response.status_code == 422
    assert response.json()["code"] == "duplicate_sample_image"


def test_import_rejects_partial_ocr_reference_sets(client: TestClient) -> None:
    response = _import(client, _two_sample_package(partial_ocr=True))

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_dataset_manifest"


def test_dataset_package_enforces_compressed_and_expanded_limits(
    tmp_path: Path,
) -> None:
    settings = WebSettings(
        storage_root=tmp_path,
        dataset_package_max_bytes=1024 * 1024,
        dataset_package_uncompressed_max_bytes=1024 * 1024,
    )
    with pytest.raises(DatasetPackageError) as compressed:
        parse_labeled_dataset_package(b"x" * (1024 * 1024 + 1), settings)
    assert compressed.value.code == "dataset_package_too_large"
    assert compressed.value.status_code == 413

    expanded = io.BytesIO()
    with zipfile.ZipFile(
        expanded,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("oversized.txt", b"x" * (1024 * 1024 + 1))
    with pytest.raises(DatasetPackageError) as uncompressed:
        parse_labeled_dataset_package(expanded.getvalue(), settings)
    assert uncompressed.value.code == "dataset_package_too_large"
    assert uncompressed.value.status_code == 413


def test_failed_persistence_rolls_back_partial_import(
    client: TestClient,
    local_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_ids = {project["id"] for project in client.get("/api/projects").json()}

    async def fail_upload(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ProductError(500, "Injected persistence failure", code="injected_failure")

    monkeypatch.setattr(local_app.state.product_service, "upload_sample", fail_upload)
    response = _import(client, _package(name="Rollback receipt package"))

    assert response.status_code == 500
    assert response.json()["code"] == "injected_failure"
    final_ids = {project["id"] for project in client.get("/api/projects").json()}
    assert final_ids == initial_ids
