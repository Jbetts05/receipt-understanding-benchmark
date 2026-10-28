from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from receipt_bench.evaluation.pricing import ComponentRate, PricingSnapshot
from receipt_bench.models import CanonicalReceipt, ComponentMeasurement, Usage
from receipt_bench.projects.models import (
    ArtifactReference,
    DatasetSource,
    JsonObject,
    Project,
    ProjectSchema,
    PromptVersion,
    Sample,
    SolutionCompatibility,
    canonical_sha256,
)
from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.providers.base import (
    AdapterResponse,
    InvalidSolutionOutput,
    SolutionAdapter,
)
from receipt_bench.providers.ocr_pipeline import OcrExtraction
from receipt_bench.providers.project_adapters import (
    ContentUnderstandingCustomProjectAdapter,
    NativeNormalizerProjectAdapter,
    NativeProjectAdapter,
    OcrProjectAdapter,
    OcrTextProjectAdapter,
    OpenAIProjectAdapter,
    ProjectAdapter,
    ProjectExecutionContext,
    ProjectRawResponse,
    RuntimePromptExample,
)
from receipt_bench.providers.project_executor import (
    ProjectPairExecutor,
    ResolvedPairContext,
)
from receipt_bench.storage.models import TaskRecord
from tests.providers.conftest import FakeOpenAIClient, make_chat_completion

ROOT = Path(__file__).resolve().parents[2]


def _sroie_answer(name: str = "Contoso") -> JsonObject:
    return {
        "company": name,
        "address": "1 Main St",
        "date": "2026-08-29",
        "total": "12.00",
    }


def _context(solution_id: str, mode: str) -> ProjectExecutionContext:
    project = ProjectRegistry.with_builtins(repository_root=ROOT).get("sroie")
    prompt = project.prompt(mode)  # type: ignore[arg-type]
    examples = tuple(
        RuntimePromptExample(
            sample_id=sample_id,
            answer=_sroie_answer(f"Example {index}"),
            image_bytes=b"example-image",
            media_type="image/png",
            ocr_text=f"EXAMPLE {index} TOTAL 12.00",
        )
        for index, sample_id in enumerate(prompt.example_sample_ids)
    )
    return ProjectExecutionContext(
        project=project,
        compatibility=project.solution_compatibility(solution_id),
        prompt=prompt,
        examples=examples,
    )


def _cord_context(solution_id: str) -> ProjectExecutionContext:
    project = ProjectRegistry.with_builtins(repository_root=ROOT).get("cord-v2")
    prompt = project.prompt("direct-multimodal")
    examples = tuple(
        RuntimePromptExample(
            sample_id=sample_id,
            answer={"menu": [], "total": {"total_price": "0"}},
            image_bytes=b"example-image",
            media_type="image/png",
        )
        for sample_id in prompt.example_sample_ids
    )
    return ProjectExecutionContext(
        project=project,
        compatibility=project.solution_compatibility(solution_id),
        prompt=prompt,
        examples=examples,
    )


def test_direct_project_adapter_sends_examples_and_parses_native_answer() -> None:
    client = FakeOpenAIClient(
        [make_chat_completion('{"company":"Target","address":"A","date":"D","total":"9"}')]
    )
    adapter = OpenAIProjectAdapter(
        "gpt-5.4-mini",
        client=client,  # type: ignore[arg-type]
        deployment="deployment",
        strict_schema=True,
        token_limit_param="max_completion_tokens",
        reasoning_effort=None,
    )
    context = _context("gpt-5.4-mini", "direct-multimodal")

    response = adapter.execute_raw(context, b"target-image", "image/png")
    answer = adapter.parse_raw(context, response.raw)

    assert answer["company"] == "Target"
    messages = client.completions.calls[0]["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert response.components[0].usage.total_tokens == 150


class _FakeExtractor:
    def extract_text(self, image_bytes: bytes, media_type: str) -> OcrExtraction:
        assert image_bytes == b"target-image"
        assert media_type == "image/png"
        return OcrExtraction(
            text="TARGET OCR",
            raw_response={"text": "TARGET OCR"},
            measurement=ComponentMeasurement(
                component="ocr",
                latency_ms=5.0,
                usage=Usage(pages=1),
            ),
        )


def test_ocr_project_adapter_preserves_both_pipeline_stages() -> None:
    answer_text = '{"company":"OCR","address":"","date":"","total":"1"}'
    client = FakeOpenAIClient([make_chat_completion(answer_text)])
    adapter = OcrProjectAdapter(
        "azure-vision-read",
        extractor=_FakeExtractor(),
        normalizer_client=client,  # type: ignore[arg-type]
        normalizer_deployment="normalizer",
        reasoning_effort="low",
    )
    context = _context("azure-vision-read", "ocr-normalizer")

    response = adapter.execute_raw(context, b"target-image", "image/png")
    answer = adapter.parse_raw(context, response.raw)

    assert answer["company"] == "OCR"
    assert [component.component for component in response.components] == ["ocr", "normalize"]
    assert response.raw["ocr_text"] == "TARGET OCR"
    assert "TARGET OCR" in client.completions.calls[0]["messages"][-1]["content"]


def test_standalone_ocr_project_adapter_records_text_metrics() -> None:
    context = _context("azure-vision-read-text", "ocr-normalizer")
    adapter = OcrTextProjectAdapter(
        "azure-vision-read-text",
        extractor=_FakeExtractor(),
    )
    executor = ProjectPairExecutor(
        adapters={"azure-vision-read-text": adapter},
        context_resolver=_Resolver(
            context,
            ocr_reference="TARGET OCR TOTAL 1",
        ),
    )
    now = datetime.now(UTC)
    task = TaskRecord(
        project_id="sroie",
        run_id="run-ocr-text",
        sample_id=context.project.scored_sample_ids[0],
        solution_id="azure-vision-read-text",
        image_artifact_path="projects/sroie/samples/image.png",
        media_type="image/png",
        created_at=now,
        updated_at=now,
    )

    raw = asyncio.run(executor.execute(task, b"target-image", "image/png"))
    parsed = asyncio.run(executor.parse(task, raw.raw))

    assert raw.raw["ocr_text"] == "TARGET OCR"  # type: ignore[index]
    assert parsed.parsed == {"text": "TARGET OCR"}
    assert isinstance(parsed.comparison, dict)
    assert parsed.comparison["metric_family"] == "ocr_text"
    assert parsed.comparison["reference_token_recall"] == 0.5
    assert [item["component"] for item in raw.telemetry["components"]] == ["ocr"]  # type: ignore[index]


def test_ocr_project_adapter_retains_ocr_evidence_when_normalizer_is_invalid() -> None:
    client = FakeOpenAIClient([make_chat_completion(None, has_choices=False)])
    adapter = OcrProjectAdapter(
        "azure-vision-read",
        extractor=_FakeExtractor(),
        normalizer_client=client,  # type: ignore[arg-type]
        normalizer_deployment="normalizer",
        reasoning_effort=None,
    )
    context = _context("azure-vision-read", "ocr-normalizer")

    with pytest.raises(InvalidSolutionOutput) as exc_info:
        adapter.execute_raw(context, b"target-image", "image/png")

    error = exc_info.value
    assert error.raw_response["ocr_text"] == "TARGET OCR"
    assert error.raw_response["ocr_response"] == {"text": "TARGET OCR"}
    assert [component.component for component in error.components] == [
        "ocr",
        "normalize",
    ]


class _NativeAdapter(SolutionAdapter):
    def execute(self, image_bytes: bytes, media_type: str) -> AdapterResponse:
        del image_bytes, media_type
        return AdapterResponse(
            receipt=CanonicalReceipt.model_validate(
                {
                    "merchant": {"name": "Native", "address": "Address"},
                    "transaction": {"date": "2026-08-29"},
                    "amounts": {"total": 3.5},
                }
            ),
            raw_response={"native": True},
            components=[],
        )


def test_native_project_adapter_applies_registered_sroie_mapping() -> None:
    context = _context("document-intelligence-receipt", "direct-multimodal")
    adapter = NativeProjectAdapter(
        "document-intelligence-receipt",
        adapter=_NativeAdapter("document-intelligence-receipt"),
    )

    response = adapter.execute_raw(context, b"target", "image/png")
    answer = adapter.parse_raw(context, response.raw)

    assert answer == {
        "company": "Native",
        "address": "Address",
        "date": "2026-08-29",
        "total": "3.5",
    }


def test_native_normalizer_preserves_service_and_luna_stages() -> None:
    client = FakeOpenAIClient(
        [
            make_chat_completion(
                '{"company":"Native","address":"Address",'
                '"date":"2026-08-29","total":"3.5"}'
            )
        ]
    )
    context = _context(
        "document-intelligence-receipt-luna",
        "ocr-normalizer",
    )
    adapter = NativeNormalizerProjectAdapter(
        "document-intelligence-receipt-luna",
        adapter=_NativeAdapter("document-intelligence-receipt-luna"),
        normalizer_client=client,  # type: ignore[arg-type]
        normalizer_deployment="receipt-gpt-56-luna",
        reasoning_effort="low",
    )

    response = adapter.execute_raw(context, b"target", "image/png")
    answer = adapter.parse_raw(context, response.raw)

    assert answer["company"] == "Native"
    assert response.raw["kind"] == "native-pipeline"
    assert [component.component for component in response.components] == [
        "normalize"
    ]
    assert client.completions.calls[0]["model"] == "receipt-gpt-56-luna"
    assert "Native" in client.completions.calls[0]["messages"][-1]["content"]


def test_content_understanding_custom_routes_cord_to_exact_schema_analyzer() -> None:
    class FakeContentUnderstandingClient:
        def __init__(self) -> None:
            self.analyzer_ids: list[str] = []

        def analyze(
            self,
            analyzer_id: str,
            image_bytes: bytes,
            media_type: str,
        ) -> dict[str, object]:
            self.analyzer_ids.append(analyzer_id)
            assert image_bytes == b"target"
            assert media_type == "image/png"
            return {
                "status": "Succeeded",
                "result": {
                    "contents": [
                        {
                            "fields": {
                                "menu": {
                                    "type": "array",
                                    "valueArray": [
                                        {
                                            "type": "object",
                                            "valueObject": {
                                                "nm": {
                                                    "type": "string",
                                                    "valueString": "Nasi",
                                                },
                                                "cnt": {
                                                    "type": "string",
                                                    "valueString": "1",
                                                },
                                                "price": {
                                                    "type": "string",
                                                    "valueString": "10.000",
                                                },
                                                "sub": {
                                                    "type": "array",
                                                    "valueArray": [
                                                        {
                                                            "type": "object",
                                                            "valueObject": {
                                                                "nm": {
                                                                    "type": "string",
                                                                    "valueString": "Extra",
                                                                },
                                                                "price": {
                                                                    "type": "string",
                                                                    "valueString": "2.000",
                                                                },
                                                            },
                                                        }
                                                    ],
                                                },
                                            },
                                        }
                                    ],
                                },
                                "total": {
                                    "type": "object",
                                    "valueObject": {
                                        "total_price": {
                                            "type": "string",
                                            "valueString": "12.000",
                                        },
                                        "cashprice": {
                                            "type": "array",
                                            "valueArray": [
                                                {
                                                    "type": "string",
                                                    "valueString": "20.000",
                                                }
                                            ],
                                        },
                                    },
                                },
                            }
                        }
                    ]
                },
                "usage": {
                    "documentPagesBasic": 1,
                    "tokens": {
                        "gpt-5.6-luna-input": 100,
                        "gpt-5.6-luna-output": 40,
                    },
                },
            }

    client = FakeContentUnderstandingClient()
    adapter = ContentUnderstandingCustomProjectAdapter(
        "content-understanding-custom",
        client=client,  # type: ignore[arg-type]
        analyzer_ids={
            "sroie": "expense_receipt_v1",
            "cord-v2": "cord_receipt_v1",
        },
    )
    context = _cord_context("content-understanding-custom")

    response = adapter.execute_raw(context, b"target", "image/png")
    answer = adapter.parse_raw(context, response.raw)

    assert client.analyzer_ids == ["cord_receipt_v1"]
    assert response.raw["analyzer_id"] == "cord_receipt_v1"
    assert answer == {
        "menu": [
            {
                "nm": "Nasi",
                "cnt": "1",
                "price": "10.000",
                "sub": {"nm": "Extra", "price": "2.000"},
            }
        ],
        "total": {
            "total_price": "12.000",
            "cashprice": "20.000",
        },
    }
    assert [component.component for component in response.components] == [
        "native_extraction",
        "model:gpt-5.6-luna",
    ]


def test_content_understanding_custom_lazily_configures_uploaded_schema() -> None:
    schema_document: JsonObject = {
        "type": "object",
        "additionalProperties": False,
        "required": ["vendor", "total"],
        "properties": {
            "vendor": {"type": "string"},
            "total": {"type": "number"},
        },
    }
    schema_hash = canonical_sha256(schema_document)
    schema = ProjectSchema(
        schema_id="custom-schema",
        version="1.0.0",
        content_hash=schema_hash,
        source=ArtifactReference(
            kind="schema",
            path="projects/custom/schema.json",
        ),
        document=schema_document,
    )
    prompt_payload = {
        "prompt_id": "custom-prompt",
        "project_id": "custom",
        "schema_id": "custom-schema",
        "mode": "direct-multimodal",
        "version": "1.0.0",
        "system_instruction": "Extract the custom fields.",
        "user_template": "Use this schema: {schema_json}",
        "example_sample_ids": [],
    }
    prompt = PromptVersion(
        **prompt_payload,
        content_hash=canonical_sha256(prompt_payload),
    )
    compatibility = SolutionCompatibility(
        solution_id="content-understanding-custom",
        solution_kind="native-custom",
        level="full",
        comparable_quality=True,
        deterministic_mapping_id="content-understanding-dynamic-schema-v1",
        reason="Dynamic analyzer.",
    )
    project = Project(
        project_id="custom",
        name="Custom",
        description="Custom project",
        built_in=False,
        source=DatasetSource(
            dataset_id="custom",
            revision=schema_hash,
            split="user-supplied",
            source_url="urn:test",
            license="Owner managed",
            attribution="Owner",
            redistribution="gated-no-redistribution",
        ),
        normalization_profile="exact-v1",
        project_schema=schema,
        prompts=(prompt,),
        samples=(
            Sample(
                project_id="custom",
                sample_id="sample-1",
                source_id="sample-1",
                source_index=0,
                ordinal=0,
                split="user-supplied",
                language="und",
                image=ArtifactReference(
                    kind="image",
                    path="projects/custom/samples/sample-1/image",
                ),
                gold=ArtifactReference(
                    kind="gold",
                    path="projects/custom/samples/sample-1/gold.json",
                ),
            ),
        ),
        compatibility=(compatibility,),
    )

    class FakeDynamicClient:
        def __init__(self) -> None:
            self.ensured: list[tuple[str, JsonObject, str]] = []

        def ensure_analyzer(
            self,
            analyzer_id: str,
            definition: JsonObject,
            *,
            schema_hash: str,
        ) -> None:
            self.ensured.append((analyzer_id, definition, schema_hash))

        def analyze(
            self,
            analyzer_id: str,
            image_bytes: bytes,
            media_type: str,
        ) -> dict[str, object]:
            assert analyzer_id == f"custom_{schema_hash[:24]}"
            assert image_bytes == b"target"
            assert media_type == "image/png"
            return {
                "status": "Succeeded",
                "result": {
                    "contents": [
                        {
                            "fields": {
                                "vendor": {
                                    "type": "string",
                                    "valueString": "Contoso",
                                },
                                "total": {
                                    "type": "number",
                                    "valueNumber": 12.5,
                                },
                            }
                        }
                    ]
                },
            }

    client = FakeDynamicClient()
    adapter = ContentUnderstandingCustomProjectAdapter(
        "content-understanding-custom",
        client=client,  # type: ignore[arg-type]
        analyzer_ids={
            "sroie": "expense_receipt_v1",
            "cord-v2": "cord_receipt_v1",
        },
    )
    context = ProjectExecutionContext(
        project=project,
        compatibility=compatibility,
        prompt=prompt,
        examples=(),
    )

    first = adapter.execute_raw(context, b"target", "image/png")
    second = adapter.execute_raw(context, b"target", "image/png")

    assert adapter.parse_raw(context, first.raw) == {
        "vendor": "Contoso",
        "total": 12.5,
    }
    assert adapter.parse_raw(context, second.raw) == {
        "vendor": "Contoso",
        "total": 12.5,
    }
    assert len(client.ensured) == 1
    analyzer_id, definition, ensured_hash = client.ensured[0]
    assert analyzer_id == f"custom_{schema_hash[:24]}"
    assert ensured_hash == schema_hash
    assert definition["fieldSchema"] is not None


class _Resolver:
    def __init__(
        self,
        context: ProjectExecutionContext,
        *,
        ocr_reference: str | None = None,
    ) -> None:
        self.context = context
        self.ocr_reference = ocr_reference

    def resolve(self, task: TaskRecord) -> ResolvedPairContext:
        del task
        return ResolvedPairContext(
            execution=self.context,
            gold=_sroie_answer("Target"),
            ocr_reference=self.ocr_reference,
        )


def test_worker_bridge_returns_project_comparison() -> None:
    answer_text = (
        '{"company":"Target","address":"1 Main St",'
        '"date":"2026-08-29","total":"12.00"}'
    )
    client = FakeOpenAIClient(
        [make_chat_completion(answer_text)]
    )
    adapter = OpenAIProjectAdapter(
        "gpt-5.4-mini",
        client=client,  # type: ignore[arg-type]
        deployment="deployment",
        strict_schema=True,
        token_limit_param="max_completion_tokens",
        reasoning_effort=None,
    )
    context = _context("gpt-5.4-mini", "direct-multimodal")
    executor = ProjectPairExecutor(
        adapters={"gpt-5.4-mini": adapter},
        context_resolver=_Resolver(context),
        pricing=PricingSnapshot(
            currency="USD",
            captured_at=datetime.now(UTC),
            region="swedencentral",
            source="test",
            rates={
                "gpt-5.4-mini": ComponentRate(
                    input_per_million_tokens=1.0,
                    output_per_million_tokens=2.0,
                )
            },
        ),
    )
    now = datetime.now(UTC)
    task = TaskRecord(
        project_id="sroie",
        run_id="run-1",
        sample_id=context.project.scored_sample_ids[0],
        solution_id="gpt-5.4-mini",
        image_artifact_path="projects/sroie/samples/image.png",
        media_type="image/png",
        created_at=now,
        updated_at=now,
    )

    import asyncio

    raw = asyncio.run(executor.execute(task, b"target-image", "image/png"))
    parsed = asyncio.run(executor.parse(task, raw.raw))

    assert parsed.parsed["company"] == "Target"  # type: ignore[index]
    assert isinstance(parsed.comparison, dict)
    assert parsed.comparison["exact"] is True
    assert raw.telemetry["estimated_cost_usd"] == pytest.approx(0.0002)
    assert raw.telemetry["components"][0]["estimated_cost_usd"] == pytest.approx(
        0.0002
    )


def test_worker_bridge_keeps_provider_thread_fenced_during_cancellation() -> None:
    class BlockingAdapter(ProjectAdapter):
        def __init__(self) -> None:
            super().__init__("gpt-5.4-mini")
            self.entered = threading.Event()
            self.release = threading.Event()

        def execute_raw(
            self,
            context: ProjectExecutionContext,
            image_bytes: bytes,
            media_type: str,
        ) -> ProjectRawResponse:
            del context, image_bytes, media_type
            self.entered.set()
            assert self.release.wait(timeout=5)
            return ProjectRawResponse(raw=_sroie_answer("Late"), components=())

        def parse_raw(
            self,
            context: ProjectExecutionContext,
            raw: JsonObject,
        ) -> JsonObject:
            del context
            return raw

    async def scenario() -> None:
        context = _context("gpt-5.4-mini", "direct-multimodal")
        adapter = BlockingAdapter()
        executor = ProjectPairExecutor(
            adapters={"gpt-5.4-mini": adapter},
            context_resolver=_Resolver(context),
        )
        now = datetime.now(UTC)
        task = TaskRecord(
            project_id="sroie",
            run_id="run-1",
            sample_id=context.project.scored_sample_ids[0],
            solution_id="gpt-5.4-mini",
            image_artifact_path="projects/sroie/samples/image.png",
            media_type="image/png",
            created_at=now,
            updated_at=now,
        )
        call = asyncio.create_task(executor.execute(task, b"image", "image/png"))
        assert await asyncio.to_thread(adapter.entered.wait, 5)

        call.cancel()
        await asyncio.sleep(0.02)
        call.cancel()
        await asyncio.sleep(0.02)
        assert not call.done()

        adapter.release.set()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())
