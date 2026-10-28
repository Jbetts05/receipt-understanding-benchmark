from __future__ import annotations

import json
from pathlib import Path

from receipt_bench.config import BenchmarkConfig
from receipt_bench.evaluation.pricing import (
    load_pricing_snapshot,
    validate_pricing_coverage,
)

ROOT = Path(__file__).resolve().parents[1]


def test_every_configured_deployment_is_provisioned() -> None:
    config = BenchmarkConfig.load(ROOT / "configs" / "solutions.yaml")
    expected = {
        solution.deployment
        for solution in config.solutions
        if solution.deployment is not None
    }
    deployments = json.loads(
        (ROOT / "configs" / "azure-deployments.json").read_text(encoding="utf-8")
    )
    provisioned = {deployment["deploymentName"] for deployment in deployments}

    assert len(expected) == 9
    assert len(provisioned) == 10
    assert expected < provisioned
    assert "receipt-embedding-3-large" in provisioned
    assert {deployment["capacity"] for deployment in deployments} == {1}


def test_all_runtime_endpoints_are_documented() -> None:
    env_names = {
        line.partition("=")[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert {
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_OPENAI_ENDPOINT",
        "AZURE_FOUNDRY_MODELS_ENDPOINT",
        "AZURE_ANTHROPIC_ENDPOINT",
        "AZURE_MISTRAL_OCR_ENDPOINT",
        "AZURE_CONTENT_UNDERSTANDING_ENDPOINT",
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
        "AZURE_VISION_ENDPOINT",
    } <= env_names


def test_preview_solutions_are_labeled() -> None:
    config = BenchmarkConfig.load(ROOT / "configs" / "solutions.yaml")
    preview_ids = {
        solution.id for solution in config.solutions if solution.lifecycle == "preview"
    }

    assert preview_ids == {
        "grok-4.6",
        "mistral-ocr-4.0",
        "mistral-ocr-4.0-text",
    }


def test_every_runtime_solution_and_assistance_model_has_pricing() -> None:
    config = BenchmarkConfig.load(ROOT / "configs" / "solutions.yaml")
    snapshot = load_pricing_snapshot(ROOT / "configs" / "pricing.yaml")
    required = {solution.id for solution in config.solutions}
    required.update(
        normalizer
        for solution in config.solutions
        if (normalizer := solution.normalizer) is not None
    )
    required.update({"gpt-5.2", "gpt-5.4-mini", "text-embedding-3-large"})

    validate_pricing_coverage(snapshot, required)
    assert snapshot is not None
    grok = snapshot.rates["grok-4.6"]
    assert grok.input_per_million_tokens == 2.0
    assert grok.output_per_million_tokens == 6.0
