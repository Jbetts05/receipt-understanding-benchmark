from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date as Date
from datetime import datetime
from datetime import time as Time
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NonFiniteNumberError(ValueError):
    """Raised when a model or JSON-like payload contains NaN or infinity."""


def ensure_finite_numbers(value: Any) -> None:
    """Reject non-finite numbers recursively, including values hidden in ``Any`` fields."""

    def visit(item: Any, path: str, seen: set[int]) -> None:
        if isinstance(item, float):
            if not math.isfinite(item):
                raise NonFiniteNumberError(f"Non-finite numeric value at {path}")
            return
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise NonFiniteNumberError(f"Non-finite numeric value at {path}")
            return
        if isinstance(item, BaseModel):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for name, child in item.__dict__.items():
                visit(child, f"{path}.{name}", seen)
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for key, child in item.items():
                visit(key, f"{path}.<key>", seen)
                visit(child, f"{path}.{key}", seen)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]", seen)

    visit(value, "$", set())


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="after")
    def reject_nested_non_finite_numbers(self) -> Self:
        ensure_finite_numbers(self)
        return self


class Merchant(StrictModel):
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    tax_id: str | None = None


class Transaction(StrictModel):
    date: Date | str | None = None
    time: Time | str | None = None
    receipt_number: str | None = None
    currency: str | None = None
    country: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def parse_valid_date_but_preserve_other_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return Date.fromisoformat(value)
        except ValueError:
            return value

    @field_validator("time", mode="before")
    @classmethod
    def parse_valid_time_but_preserve_other_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return Time.fromisoformat(value)
        except ValueError:
            return value


class Amounts(StrictModel):
    subtotal: float | None = None
    discount: float | None = None
    tax: float | None = None
    tip: float | None = None
    total: float | None = None


class PaymentMethod(StrictModel):
    method: str | None = None
    amount: float | None = None
    card_last4: str | None = None


class LineItem(StrictModel):
    description: str | None = None
    product_code: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    discount: float | None = None
    tax: float | None = None
    total_price: float | None = None


class CanonicalReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    merchant: Merchant = Field(default_factory=Merchant)
    transaction: Transaction = Field(default_factory=Transaction)
    amounts: Amounts = Field(default_factory=Amounts)
    payment_methods: list[PaymentMethod] | None = None
    receipt_type: str | None = None
    line_items: list[LineItem] | None = None


class ReceiptSample(StrictModel):
    sample_id: str
    dataset: str
    split: str
    image_path: str
    image_sha256: str
    source_language: str | None = None
    ground_truth: CanonicalReceipt
    labeled_fields: list[str]


class Usage(StrictModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    pages: int | None = None
    contextualization_tokens: int | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class ComponentMeasurement(StrictModel):
    component: str
    latency_ms: float
    usage: Usage = Field(default_factory=Usage)
    estimated_cost: float | None = None


class SolutionOutput(StrictModel):
    run_id: str
    sample_id: str
    solution_id: str
    started_at: datetime
    completed_at: datetime
    status: Literal["succeeded", "failed", "invalid"]
    receipt: CanonicalReceipt | None = None
    raw_artifact: str
    error_type: str | None = None
    error_message: str | None = None
    attempts: int = 1
    components: list[ComponentMeasurement] = Field(default_factory=list)


class FieldScore(StrictModel):
    field: str
    labeled: bool
    exact_match: bool | None = None
    normalized_similarity: float | None = None
    expected: Any = None
    actual: Any = None


class EvaluatedOutput(StrictModel):
    output: SolutionOutput
    schema_valid: bool
    fields: list[FieldScore]
    arithmetic_consistent: bool | None = None
