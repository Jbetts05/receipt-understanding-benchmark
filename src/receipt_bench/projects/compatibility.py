from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal

from receipt_bench.models import CanonicalReceipt
from receipt_bench.projects.models import JsonObject, JsonValue, SolutionCompatibility

NativeMapping = Callable[[CanonicalReceipt], JsonObject]

DIRECT_SOLUTION_IDS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.4-mini",
    "gpt-5.2",
    "grok-4.6",
    "mistral-large-3",
)
OCR_NORMALIZER_SOLUTION_IDS = (
    "mistral-document-ai-2512",
    "mistral-ocr-4.0",
    "azure-vision-read",
    "content-understanding-read",
    "content-understanding-layout",
)
OCR_TEXT_SOLUTION_IDS = (
    "mistral-document-ai-2512-text",
    "mistral-ocr-4.0-text",
    "azure-vision-read-text",
    "content-understanding-read-text",
    "content-understanding-layout-text",
)
NATIVE_CUSTOM_SOLUTION_ID = "content-understanding-custom"
NATIVE_PREBUILT_SOLUTION_IDS = (
    "content-understanding-prebuilt-receipt",
    "document-intelligence-receipt",
)
NATIVE_NORMALIZER_SOLUTION_IDS = (
    "content-understanding-prebuilt-receipt-luna",
    "document-intelligence-receipt-luna",
)

SROIE_NATIVE_MAPPING_IDS = frozenset(
    {
        "content-understanding-sroie-native-v1",
        "content-understanding-prebuilt-to-sroie-v1",
        "document-intelligence-prebuilt-to-sroie-v1",
    }
)
CORD_NATIVE_MAPPING_IDS = frozenset({"content-understanding-cord-native-v1"})
CORD_PREBUILT_MAPPING_IDS = frozenset(
    {
        "content-understanding-prebuilt-to-cord-v1",
        "document-intelligence-prebuilt-to-cord-v1",
    }
)
DYNAMIC_CONTENT_UNDERSTANDING_MAPPING_ID = (
    "content-understanding-dynamic-schema-v1"
)
CANONICAL_NATIVE_OUTPUT_MAPPING_ID = "canonical-receipt-native-output-v1"


def map_canonical_receipt_to_sroie(receipt: CanonicalReceipt) -> JsonObject:
    """Deterministically project native/prebuilt canonical fields into SROIE."""

    raw_date = receipt.transaction.date
    rendered_date = raw_date.isoformat() if isinstance(raw_date, date) else (raw_date or "")
    raw_total = receipt.amounts.total
    rendered_total = (
        format(Decimal(str(raw_total)).normalize(), "f") if raw_total is not None else ""
    )
    return {
        "company": receipt.merchant.name or "",
        "address": receipt.merchant.address or "",
        "date": rendered_date,
        "total": rendered_total,
    }


def _number_text(value: float | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).normalize(), "f")


def map_canonical_receipt_to_cord(receipt: CanonicalReceipt) -> JsonObject:
    """Project only genuinely available prebuilt receipt fields into CORD."""

    menu: list[JsonValue] = []
    for item in receipt.line_items or []:
        mapped: JsonObject = {"nm": item.description or item.product_code or ""}
        for key, value in (
            ("cnt", _number_text(item.quantity)),
            ("price", _number_text(item.total_price)),
            ("unitprice", _number_text(item.unit_price)),
            ("num", item.product_code),
        ):
            if value is not None:
                mapped[key] = value
        menu.append(mapped)

    sub_total: JsonObject = {}
    for key, value in (
        ("subtotal_price", _number_text(receipt.amounts.subtotal)),
        ("discount_price", _number_text(receipt.amounts.discount)),
        ("tax_price", _number_text(receipt.amounts.tax)),
        ("service_price", _number_text(receipt.amounts.tip)),
    ):
        if value is not None:
            sub_total[key] = value

    total: JsonObject = {
        "total_price": _number_text(receipt.amounts.total) or "",
    }
    for payment in receipt.payment_methods or []:
        method = (payment.method or "").casefold()
        amount = _number_text(payment.amount)
        if amount is None:
            continue
        if "cash" in method and "cashprice" not in total:
            total["cashprice"] = amount
        elif (
            any(value in method for value in ("card", "credit", "debit"))
            and "creditcardprice" not in total
        ):
            total["creditcardprice"] = amount

    result: JsonObject = {"menu": menu, "total": total}
    if sub_total:
        result["sub_total"] = sub_total
    return result


def map_canonical_receipt_to_native_output(
    receipt: CanonicalReceipt,
) -> JsonObject:
    return receipt.model_dump(mode="json")


_DETERMINISTIC_MAPPINGS: Mapping[str, NativeMapping] = {
    mapping_id: map_canonical_receipt_to_sroie for mapping_id in SROIE_NATIVE_MAPPING_IDS
}
_DETERMINISTIC_MAPPINGS = {
    **_DETERMINISTIC_MAPPINGS,
    **{
        mapping_id: map_canonical_receipt_to_cord
        for mapping_id in CORD_PREBUILT_MAPPING_IDS
    },
    CANONICAL_NATIVE_OUTPUT_MAPPING_ID: map_canonical_receipt_to_native_output,
}
REGISTERED_DETERMINISTIC_MAPPING_IDS = (
    frozenset(_DETERMINISTIC_MAPPINGS)
    | CORD_NATIVE_MAPPING_IDS
    | {DYNAMIC_CONTENT_UNDERSTANDING_MAPPING_ID}
)


def resolve_native_mapping(mapping_id: str) -> NativeMapping:
    try:
        return _DETERMINISTIC_MAPPINGS[mapping_id]
    except KeyError as exc:
        raise KeyError(f"Unknown native mapping: {mapping_id!r}") from exc


def resolve_sroie_native_mapping(mapping_id: str) -> NativeMapping:
    return resolve_native_mapping(mapping_id)


def default_custom_compatibility(
    *,
    native_mappings: Mapping[str, str] | None = None,
) -> tuple[SolutionCompatibility, ...]:
    """Declare custom-project capability without pretending prebuilt coverage."""

    mappings = {
        NATIVE_CUSTOM_SOLUTION_ID: DYNAMIC_CONTENT_UNDERSTANDING_MAPPING_ID,
        **{
            solution_id: CANONICAL_NATIVE_OUTPUT_MAPPING_ID
            for solution_id in NATIVE_PREBUILT_SOLUTION_IDS
        },
        **(native_mappings or {}),
    }
    declarations = [
        SolutionCompatibility(
            solution_id=solution_id,
            solution_kind="direct-multimodal",
            level="full",
            comparable_quality=True,
            reason="Direct multimodal extraction is constrained by the uploaded project schema.",
        )
        for solution_id in DIRECT_SOLUTION_IDS
    ]
    declarations.extend(
        SolutionCompatibility(
            solution_id=solution_id,
            solution_kind="ocr-normalizer",
            level="full",
            comparable_quality=True,
            reason="OCR text is normalized by a model constrained by the uploaded project schema.",
        )
        for solution_id in OCR_NORMALIZER_SOLUTION_IDS
    )
    declarations.extend(
        SolutionCompatibility(
            solution_id=solution_id,
            solution_kind="native-normalizer",
            level="full",
            comparable_quality=True,
            reason=(
                "Native receipt extraction is normalized by GPT-5.6 Luna into "
                "the uploaded project schema."
            ),
        )
        for solution_id in NATIVE_NORMALIZER_SOLUTION_IDS
    )
    declarations.extend(
        SolutionCompatibility(
            solution_id=solution_id,
            solution_kind="ocr-text",
            level="unsupported",
            comparable_quality=False,
            reason="No OCR reference transcription is available for this custom project.",
        )
        for solution_id in OCR_TEXT_SOLUTION_IDS
    )
    for solution_id in (NATIVE_CUSTOM_SOLUTION_ID, *NATIVE_PREBUILT_SOLUTION_IDS):
        mapping_id = mappings.get(solution_id)
        declarations.append(
            SolutionCompatibility(
                solution_id=solution_id,
                solution_kind=(
                    "native-custom"
                    if solution_id == NATIVE_CUSTOM_SOLUTION_ID
                    else "native-prebuilt"
                ),
                level="full" if mapping_id is not None else "unsupported",
                comparable_quality=mapping_id is not None,
                deterministic_mapping_id=mapping_id,
                reason=(
                    "A registered deterministic mapping covers the complete uploaded schema."
                    if mapping_id is not None
                    else "No deterministic mapping is registered for the uploaded schema."
                ),
            )
        )
    return tuple(declarations)


__all__ = [
    "CANONICAL_NATIVE_OUTPUT_MAPPING_ID",
    "CORD_NATIVE_MAPPING_IDS",
    "CORD_PREBUILT_MAPPING_IDS",
    "DIRECT_SOLUTION_IDS",
    "DYNAMIC_CONTENT_UNDERSTANDING_MAPPING_ID",
    "NATIVE_CUSTOM_SOLUTION_ID",
    "NATIVE_NORMALIZER_SOLUTION_IDS",
    "NATIVE_PREBUILT_SOLUTION_IDS",
    "OCR_NORMALIZER_SOLUTION_IDS",
    "OCR_TEXT_SOLUTION_IDS",
    "REGISTERED_DETERMINISTIC_MAPPING_IDS",
    "SROIE_NATIVE_MAPPING_IDS",
    "default_custom_compatibility",
    "map_canonical_receipt_to_cord",
    "map_canonical_receipt_to_native_output",
    "map_canonical_receipt_to_sroie",
    "resolve_native_mapping",
    "resolve_sroie_native_mapping",
]
