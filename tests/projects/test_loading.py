from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from receipt_bench.models import (
    Amounts,
    CanonicalReceipt,
    LineItem,
    Merchant,
    PaymentMethod,
    Transaction,
)
from receipt_bench.projects.compatibility import (
    DIRECT_SOLUTION_IDS,
    NATIVE_CUSTOM_SOLUTION_ID,
    NATIVE_NORMALIZER_SOLUTION_IDS,
    NATIVE_PREBUILT_SOLUTION_IDS,
    OCR_NORMALIZER_SOLUTION_IDS,
    OCR_TEXT_SOLUTION_IDS,
    REGISTERED_DETERMINISTIC_MAPPING_IDS,
    default_custom_compatibility,
    map_canonical_receipt_to_cord,
    resolve_native_mapping,
    resolve_sroie_native_mapping,
)
from receipt_bench.projects.loading import BuiltInProjectSpec, load_builtin_projects
from receipt_bench.projects.models import Project, canonical_sha256
from receipt_bench.projects.registry import ProjectRegistry

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_ROOT = files("receipt_bench.projects").joinpath("resources")


def test_fixed_sample_ids_and_non_scored_exclusions(
    sroie_project: Project, cord_project: Project
) -> None:
    assert tuple(sample.sample_id for sample in sroie_project.samples) == tuple(
        f"sroie-{index:03d}" for index in range(80)
    )
    assert tuple(sample.sample_id for sample in cord_project.samples) == tuple(
        f"cord-{index:03d}"
        for index in (3, 11, 15, 26, 28, 34, 35, 42, 46, 47, 49, 58, 63, 68, 72, 73, 74, 80, 85, 98)
    )
    assert tuple(sample.sample_id for sample in sroie_project.few_shot_samples) == (
        "sroie-012",
        "sroie-016",
    )
    assert tuple(sample.sample_id for sample in cord_project.few_shot_samples) == (
        "cord-028",
        "cord-046",
    )
    assert sroie_project.excluded_sample_ids == (
        "sroie-015",
        "sroie-018",
    )
    assert cord_project.excluded_samples == ()
    assert len(sroie_project.scored_samples) == 76
    assert len(cord_project.scored_samples) == 18
    assert cord_project.sample("cord-047").scoring_role == "scored"
    assert cord_project.sample("cord-074").scoring_role == "scored"

    sroie_example_ids = {sample.sample_id for sample in sroie_project.few_shot_samples}
    sroie_excluded_ids = {
        sample.sample_id for sample in sroie_project.excluded_samples
    }
    for duplicate_group in (
        {"sroie-012", "sroie-015"},
        {"sroie-016", "sroie-018"},
    ):
        assert len(sroie_example_ids & duplicate_group) == 1
        assert len(sroie_excluded_ids & duplicate_group) == 1
        assert min(duplicate_group) in sroie_example_ids
        assert not duplicate_group & set(sroie_project.scored_sample_ids)
        pair = tuple(sroie_project.sample(sample_id) for sample_id in duplicate_group)
        assert pair[0].image.sha256 == pair[1].image.sha256

    for project in (sroie_project, cord_project):
        for sample in project.few_shot_samples:
            assert sample.few_shot_reason
            assert sample.image.sha256
            assert sample.gold.sha256
            assert sample.ocr_reference is not None
            assert sample.ocr_reference.sha256
    for sample in sroie_project.excluded_samples:
        assert sample.exclusion_reason
        assert sample.few_shot_reason is None
        assert sample.image.sha256
        assert sample.gold.sha256
        assert sample.ocr_reference is not None
        assert sample.ocr_reference.sha256


def test_sroie_scored_images_have_unique_hashes(sroie_project: Project) -> None:
    image_paths = [ROOT / sample.image.path for sample in sroie_project.scored_samples]
    if not all(path.is_file() for path in image_paths):
        pytest.skip("Pinned SROIE image cache is not available")

    image_hashes = [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in image_paths
    ]
    assert len(image_hashes) == 76
    assert len(set(image_hashes)) == 76


def test_prompt_versions_have_stable_semantic_hashes(
    sroie_project: Project, cord_project: Project
) -> None:
    for project in (sroie_project, cord_project):
        assert {prompt.mode for prompt in project.prompts} == {
            "direct-multimodal",
            "ocr-normalizer",
        }
        for prompt in project.prompts:
            payload = prompt.model_dump(mode="json", exclude={"content_hash"})
            assert canonical_sha256(payload) == prompt.content_hash
            assert prompt.version == "1.0.0"
            assert prompt.example_sample_ids == tuple(
                sample.sample_id for sample in project.few_shot_samples
            )


def test_compatibility_is_explicit_and_excludes_partial_native_quality(
    sroie_project: Project, cord_project: Project
) -> None:
    for project in (sroie_project, cord_project):
        for solution_id in (
            *DIRECT_SOLUTION_IDS,
            *NATIVE_NORMALIZER_SOLUTION_IDS,
            *OCR_NORMALIZER_SOLUTION_IDS,
            *OCR_TEXT_SOLUTION_IDS,
        ):
            declaration = project.solution_compatibility(solution_id)
            assert declaration.level == "full"
            assert declaration.comparable_quality

    for solution_id in NATIVE_PREBUILT_SOLUTION_IDS:
        assert sroie_project.solution_compatibility(solution_id).level == "full"
        cord_declaration = cord_project.solution_compatibility(solution_id)
        assert cord_declaration.level == "full"
        assert cord_declaration.comparable_quality
        assert cord_declaration.deterministic_mapping_id is not None

    cord_custom = cord_project.solution_compatibility("content-understanding-custom")
    assert cord_custom.level == "full"
    assert cord_custom.comparable_quality
    assert cord_custom.deterministic_mapping_id == "content-understanding-cord-native-v1"

    declared_mapping_ids = {
        declaration.deterministic_mapping_id
        for project in (sroie_project, cord_project)
        for declaration in project.compatibility
        if declaration.deterministic_mapping_id is not None
    }
    assert declared_mapping_ids <= REGISTERED_DETERMINISTIC_MAPPING_IDS

    custom = {item.solution_id: item for item in default_custom_compatibility()}
    assert all(custom[item].level == "full" for item in DIRECT_SOLUTION_IDS)
    assert all(custom[item].level == "full" for item in OCR_NORMALIZER_SOLUTION_IDS)
    assert all(custom[item].level == "full" for item in NATIVE_NORMALIZER_SOLUTION_IDS)
    assert all(custom[item].level == "unsupported" for item in OCR_TEXT_SOLUTION_IDS)
    assert custom[NATIVE_CUSTOM_SOLUTION_ID].level == "full"
    assert all(custom[item].level == "full" for item in NATIVE_PREBUILT_SOLUTION_IDS)
    assert all(
        custom[item].deterministic_mapping_id is not None
        for item in (NATIVE_CUSTOM_SOLUTION_ID, *NATIVE_PREBUILT_SOLUTION_IDS)
    )


def test_declared_sroie_native_mapping_covers_exact_schema(
    sroie_project: Project,
) -> None:
    declaration = sroie_project.solution_compatibility("document-intelligence-receipt")
    assert declaration.deterministic_mapping_id is not None
    mapper = resolve_sroie_native_mapping(declaration.deterministic_mapping_id)

    answer = mapper(
        CanonicalReceipt(
            merchant=Merchant(name="Example", address="1 Road"),
            transaction=Transaction(date="2018-12-25"),
            amounts=Amounts(total=9.0),
        )
    )
    assert answer == {
        "company": "Example",
        "address": "1 Road",
        "date": "2018-12-25",
        "total": "9",
    }


def test_declared_cord_prebuilt_mapping_scores_available_fields_and_omits_gaps() -> None:
    receipt = CanonicalReceipt(
        amounts=Amounts(subtotal=10_000, tax=1_000, total=11_000),
        payment_methods=[PaymentMethod(method="cash", amount=20_000)],
        line_items=[
            LineItem(
                description="Nasi",
                quantity=2,
                unit_price=5_000,
                total_price=10_000,
            )
        ],
    )

    expected = {
        "menu": [
            {
                "nm": "Nasi",
                "cnt": "2",
                "price": "10000",
                "unitprice": "5000",
            }
        ],
        "sub_total": {
            "subtotal_price": "10000",
            "tax_price": "1000",
        },
        "total": {
            "total_price": "11000",
            "cashprice": "20000",
        },
    }
    assert map_canonical_receipt_to_cord(receipt) == expected
    assert (
        resolve_native_mapping("document-intelligence-prebuilt-to-cord-v1")(
            receipt
        )
        == expected
    )


def test_project_registry_loads_builtins_without_azure() -> None:
    registry = ProjectRegistry.with_builtins()

    assert tuple(project.project_id for project in registry.list()) == ("cord-v2", "sroie")
    assert registry.get("sroie").source.redistribution == "gated-no-redistribution"
    assert registry.get("cord-v2").source.license == "CC-BY-4.0"
    with pytest.raises(KeyError, match="Unknown project"):
        registry.get("missing")


def test_builtin_loader_order_is_stable() -> None:
    projects = load_builtin_projects()
    assert tuple(project.project_id for project in projects) == ("sroie", "cord-v2")


def test_builtin_loader_uses_package_resources_outside_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    projects = load_builtin_projects(
        repository_root=tmp_path,
        definitions_root=tmp_path,
    )

    assert tuple(project.project_id for project in projects) == ("sroie", "cord-v2")
    assert all(project.project_schema.document for project in projects)
    assert all(project.prompts for project in projects)


@pytest.mark.parametrize(
    ("source_path", "resource_parts"),
    [
        ("schemas/sroie.schema.json", ("schemas", "sroie.schema.json")),
        ("schemas/cord.schema.json", ("schemas", "cord.schema.json")),
        (
            "prompts/sroie/direct-multimodal.v1.json",
            ("prompts", "sroie", "direct-multimodal.v1.json"),
        ),
        (
            "prompts/sroie/ocr-normalizer.v1.json",
            ("prompts", "sroie", "ocr-normalizer.v1.json"),
        ),
        (
            "prompts/cord/direct-multimodal.v1.json",
            ("prompts", "cord", "direct-multimodal.v1.json"),
        ),
        (
            "prompts/cord/ocr-normalizer.v1.json",
            ("prompts", "cord", "ocr-normalizer.v1.json"),
        ),
    ],
)
def test_repository_contract_sources_match_packaged_resources(
    source_path: str,
    resource_parts: tuple[str, ...],
) -> None:
    assert (ROOT / source_path).read_bytes() == RESOURCE_ROOT.joinpath(
        *resource_parts
    ).read_bytes()


def test_project_spec_rejects_unregistered_deterministic_mapping() -> None:
    payload = json.loads(
        RESOURCE_ROOT.joinpath(
            "definitions", "sroie.project.json"
        ).read_text(encoding="utf-8")
    )
    payload["compatibility_groups"][0]["deterministic_mapping_id"] = "missing-mapping"

    with pytest.raises(ValidationError, match="Unregistered deterministic mapping"):
        BuiltInProjectSpec.model_validate(payload)
