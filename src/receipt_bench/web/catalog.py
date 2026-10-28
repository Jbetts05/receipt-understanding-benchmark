from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Literal

from receipt_bench.storage.validation import validate_id
from receipt_bench.web.models import Compatibility, SolutionView


@dataclass(frozen=True)
class SolutionDefinition:
    id: str
    label: str
    short_label: str
    order: int
    kind: Literal[
        "direct",
        "native",
        "native_pipeline",
        "ocr_pipeline",
        "ocr_text",
    ]
    provider: str
    api: str
    lifecycle: Literal["ga", "preview"] = "ga"
    model: str | None = None
    normalizer: str | None = None
    internal_models: tuple[str, ...] = ()
    requires_project_analyzer: bool = False

    def __post_init__(self) -> None:
        validate_id(self.id, field="solution_id")
        validate_id(self.api, field="provider_key")
        if self.order < 0:
            raise ValueError("Solution order cannot be negative")

    @property
    def provider_keys(self) -> tuple[str, ...]:
        keys = [self.api]
        if self.normalizer:
            keys.append(self.normalizer)
        return tuple(dict.fromkeys(keys))


class SolutionCatalog:
    def __init__(self, definitions: tuple[SolutionDefinition, ...]) -> None:
        ordered = tuple(sorted(definitions, key=lambda item: (item.order, item.id)))
        if len({item.id for item in ordered}) != len(ordered):
            raise ValueError("Solution IDs must be unique")
        if len({item.order for item in ordered}) != len(ordered):
            raise ValueError("Solution orders must be unique")
        self._ordered = ordered
        self._by_id = {item.id: item for item in ordered}

    def list(self) -> tuple[SolutionDefinition, ...]:
        return self._ordered

    def get(self, solution_id: str) -> SolutionDefinition:
        try:
            return self._by_id[solution_id]
        except KeyError as exc:
            raise KeyError(f"Unknown solution: {solution_id!r}") from exc

    def view(
        self,
        *,
        compatibility: dict[str, dict[str, tuple[Compatibility, bool]]],
    ) -> builtins.list[SolutionView]:
        return [
            SolutionView(
                id=item.id,
                label=item.label,
                short_label=item.short_label,
                order=item.order,
                kind=item.kind,
                provider=item.provider,
                lifecycle=item.lifecycle,
                model=item.model,
                normalizer=item.normalizer,
                internal_models=list(item.internal_models),
                requires_project_analyzer=item.requires_project_analyzer,
                compatibility={
                    project_id: declaration[item.id][0]
                    for project_id, declaration in sorted(compatibility.items())
                    if item.id in declaration
                },
                comparable_quality={
                    project_id: declaration[item.id][1]
                    for project_id, declaration in sorted(compatibility.items())
                    if item.id in declaration
                },
            )
            for item in self._ordered
        ]


DEFAULT_SOLUTIONS = (
    SolutionDefinition(
        "gpt-5.6-sol", "GPT-5.6 Sol", "Sol", 0, "direct", "Microsoft Foundry",
        "azure-openai", model="gpt-5.6-sol",
    ),
    SolutionDefinition(
        "gpt-5.6-terra", "GPT-5.6 Terra", "Terra", 1, "direct", "Microsoft Foundry",
        "azure-openai", model="gpt-5.6-terra",
    ),
    SolutionDefinition(
        "gpt-5.6-luna", "GPT-5.6 Luna", "Luna", 2, "direct", "Microsoft Foundry",
        "azure-openai", model="gpt-5.6-luna",
    ),
    SolutionDefinition(
        "gpt-5.4-mini", "GPT-5.4 Mini", "5.4 Mini", 3, "direct", "Microsoft Foundry",
        "azure-openai", model="gpt-5.4-mini",
    ),
    SolutionDefinition(
        "gpt-5.2", "GPT-5.2", "5.2", 4, "direct", "Microsoft Foundry",
        "azure-openai", model="gpt-5.2",
    ),
    SolutionDefinition(
        "grok-4.6", "Grok 4.6", "Grok", 5, "direct", "Microsoft Foundry",
        "foundry-openai", lifecycle="preview", model="grok-4.6",
    ),
    SolutionDefinition(
        "mistral-large-3", "Mistral Large 3", "Mistral", 6, "direct",
        "Microsoft Foundry", "foundry-openai", model="Mistral-Large-3",
    ),
    SolutionDefinition(
        "content-understanding-custom",
        "Content Understanding schema-configured (no training)",
        "CU schema",
        7,
        "native", "Azure AI", "content-understanding",
        internal_models=("gpt-5.4-mini",),
        requires_project_analyzer=True,
    ),
    SolutionDefinition(
        "content-understanding-prebuilt-receipt", "Content Understanding receipt",
        "CU receipt", 8, "native", "Azure AI", "content-understanding",
        internal_models=("gpt-5.2", "text-embedding-3-large"),
    ),
    SolutionDefinition(
        "document-intelligence-receipt", "Document Intelligence receipt", "DI receipt", 9,
        "native", "Azure AI", "document-intelligence", model="prebuilt-receipt",
    ),
    SolutionDefinition(
        "mistral-document-ai-2512",
        "Mistral Document AI + GPT-5.6 Luna",
        "Doc AI + Luna",
        10,
        "ocr_pipeline", "Mistral", "mistral-ocr", model="mistral-document-ai-2512",
        normalizer="gpt-5.6-luna",
    ),
    SolutionDefinition(
        "mistral-ocr-4.0",
        "Mistral OCR 4.0 + GPT-5.6 Luna",
        "OCR 4 + Luna",
        11,
        "ocr_pipeline", "Mistral",
        "mistral-ocr", lifecycle="preview", model="mistral-ocr-4-0",
        normalizer="gpt-5.6-luna",
    ),
    SolutionDefinition(
        "azure-vision-read",
        "Azure Vision Read + GPT-5.6 Luna",
        "Vision + Luna",
        12,
        "ocr_pipeline",
        "Azure AI", "azure-vision", normalizer="gpt-5.6-luna",
    ),
    SolutionDefinition(
        "content-understanding-read",
        "Content Understanding Read + GPT-5.6 Luna",
        "CU Read + Luna",
        13,
        "ocr_pipeline", "Azure AI", "content-understanding", normalizer="gpt-5.6-luna",
    ),
    SolutionDefinition(
        "content-understanding-layout",
        "Content Understanding Layout + GPT-5.6 Luna",
        "CU Layout + Luna",
        14,
        "ocr_pipeline", "Azure AI", "content-understanding", normalizer="gpt-5.6-luna",
    ),
    SolutionDefinition(
        "mistral-document-ai-2512-text", "Mistral Document AI OCR", "Doc AI OCR", 15,
        "ocr_text", "Mistral", "mistral-ocr", model="mistral-document-ai-2512",
    ),
    SolutionDefinition(
        "mistral-ocr-4.0-text", "Mistral OCR 4.0 standalone", "OCR 4 text", 16,
        "ocr_text", "Mistral", "mistral-ocr", lifecycle="preview",
        model="mistral-ocr-4-0",
    ),
    SolutionDefinition(
        "azure-vision-read-text", "Azure Vision Read standalone", "Vision text", 17,
        "ocr_text", "Azure AI", "azure-vision",
    ),
    SolutionDefinition(
        "content-understanding-read-text", "Content Understanding Read standalone",
        "CU Read text", 18, "ocr_text", "Azure AI", "content-understanding",
    ),
    SolutionDefinition(
        "content-understanding-layout-text", "Content Understanding Layout standalone",
        "CU Layout text", 19, "ocr_text", "Azure AI", "content-understanding",
    ),
    SolutionDefinition(
        "content-understanding-prebuilt-receipt-luna",
        "Content Understanding receipt + GPT-5.6 Luna", "CU receipt + Luna", 20,
        "native_pipeline", "Azure AI", "content-understanding",
        normalizer="gpt-5.6-luna",
        internal_models=("gpt-5.2", "text-embedding-3-large"),
    ),
    SolutionDefinition(
        "document-intelligence-receipt-luna",
        "Document Intelligence receipt + GPT-5.6 Luna", "DI receipt + Luna", 21,
        "native_pipeline", "Azure AI", "document-intelligence",
        model="prebuilt-receipt", normalizer="gpt-5.6-luna",
    ),
)


def default_solution_catalog() -> SolutionCatalog:
    return SolutionCatalog(DEFAULT_SOLUTIONS)


__all__ = [
    "DEFAULT_SOLUTIONS",
    "SolutionCatalog",
    "SolutionDefinition",
    "default_solution_catalog",
]
