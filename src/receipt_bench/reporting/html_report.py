from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from receipt_bench.evaluation.aggregate import AggregateReport, SolutionAggregate

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_NAME = "report.html.jinja"


def _json_default(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        result = isoformat()
        return str(result)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _format_percentage(value: float) -> str:
    return f"{value * 100:.1f}%"


def _format_seconds(value_ms: float) -> str:
    value_seconds = value_ms / 1_000
    digits = 2 if value_seconds < 10 else 1
    return f"{value_seconds:.{digits}f} s"


def _format_usd(value: float) -> str:
    absolute = abs(value)
    digits = 2 if value == 0 or absolute >= 1 else 3 if absolute >= 0.01 else 4
    return f"${value:.{digits}f}"


def _highlight(
    solutions: Sequence[SolutionAggregate],
    *,
    metric: str,
    title: str,
    description: str,
    value_for: Callable[[SolutionAggregate], float | None],
    display: Callable[[float], str],
    highest: bool,
) -> dict[str, Any]:
    measured: list[tuple[SolutionAggregate, float]] = []
    for solution in solutions:
        value = value_for(solution)
        if value is not None and math.isfinite(value):
            measured.append((solution, value))

    if not measured:
        return {
            "metric": metric,
            "title": title,
            "description": description,
            "value": None,
            "display": "Unavailable",
            "winners": [],
        }

    best_value = (max if highest else min)(value for _, value in measured)
    winners = sorted(
        (
            solution.solution_id
            for solution, value in measured
            if value == best_value
        ),
        key=lambda value: (value.casefold(), value),
    )
    return {
        "metric": metric,
        "title": title,
        "description": description,
        "value": best_value,
        "display": display(best_value),
        "winners": winners,
    }


def _highlight_cards(solutions: Sequence[SolutionAggregate]) -> list[dict[str, Any]]:
    return [
        _highlight(
            solutions,
            metric="overall_quality_score",
            title="Highest measured quality",
            description="Mean receipt-level field F1 on this benchmark.",
            value_for=lambda solution: solution.overall_quality_score,
            display=_format_percentage,
            highest=True,
        ),
        _highlight(
            solutions,
            metric="overall_latency.p50_ms",
            title="Fastest median response",
            description="Lowest measured median end-to-end time on this benchmark.",
            value_for=lambda solution: solution.overall_latency.p50_ms,
            display=_format_seconds,
            highest=False,
        ),
        _highlight(
            solutions,
            metric="priced_cost_mean",
            title="Lowest known cost/receipt",
            description="Lowest priced mean; unavailable prices are excluded.",
            value_for=lambda solution: solution.priced_cost_mean,
            display=_format_usd,
            highest=False,
        ),
        _highlight(
            solutions,
            metric="valid_output_rate",
            title="Most reliable",
            description="Highest valid-output rate on this benchmark.",
            value_for=lambda solution: solution.valid_output_rate,
            display=_format_percentage,
            highest=True,
        ),
    ]


def render_html_report(report: AggregateReport) -> str:
    """Render the self-contained static HTML report for one aggregate report.

    The template embeds all data as an inline JSON payload and renders tables and
    distributions with inline JavaScript/CSS only; no network assets are referenced.
    """
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)
    template = env.get_template(_TEMPLATE_NAME)
    payload = asdict(report)
    data_json = json.dumps(payload, ensure_ascii=False, default=_json_default)
    return template.render(
        run_id=report.run_id,
        generated_at=report.generated_at.isoformat(),
        data_json=data_json,
        solution_count=len(report.solutions),
        receipt_count=max((solution.total_samples for solution in report.solutions), default=0),
        field_count=len(report.fields),
        highlight_cards=_highlight_cards(report.solutions),
        pricing_source=report.pricing_source,
        pricing_currency=report.pricing_currency,
        pricing_captured_at=(
            report.pricing_captured_at.isoformat()
            if report.pricing_captured_at is not None
            else None
        ),
    )


def write_html_report(report: AggregateReport, path: Path) -> Path:
    """Render and write the static HTML report to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html_report(report), encoding="utf-8")
    return path


__all__ = ["render_html_report", "write_html_report"]
