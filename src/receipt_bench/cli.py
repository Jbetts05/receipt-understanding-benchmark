"""Typer CLI for receipt-bench: config validation, dataset prep, run, and report."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from receipt_bench.config import AzureSettings, BenchmarkConfig
from receipt_bench.datasets.errors import (
    DatasetConfigError,
    DatasetFetchError,
    DatasetParsingError,
)
from receipt_bench.datasets.prepare import prepare_dataset
from receipt_bench.evaluation.aggregate import SolutionMetadata, build_report
from receipt_bench.evaluation.errors import (
    ManifestError,
    PricingConfigError,
    ResultsIOError,
    RunConfigurationError,
    WarmupIOError,
)
from receipt_bench.evaluation.manifest import load_manifest
from receipt_bench.evaluation.pricing import load_pricing_snapshot
from receipt_bench.evaluation.results_io import read_results
from receipt_bench.evaluation.run_plan import load_run_plan
from receipt_bench.evaluation.runner import ImageIntegrityError, run_benchmark
from receipt_bench.evaluation.scoring import score_output
from receipt_bench.evaluation.warmups import WarmupRecord, read_warmups
from receipt_bench.models import EvaluatedOutput
from receipt_bench.prompting import load_json_schema
from receipt_bench.providers.base import AdapterConfigurationError
from receipt_bench.providers.registry import build_adapters
from receipt_bench.reporting.html_report import write_html_report
from receipt_bench.reporting.outputs import write_csv_reports, write_json_report
from receipt_bench.web.cli import ProductLaunchError
from receipt_bench.web.cli import run_server as run_demo_server

app = typer.Typer(
    name="receipt-bench",
    help="Deterministic benchmark orchestration for receipt-understanding solutions.",
    no_args_is_help=True,
)
data_app = typer.Typer(help="Dataset preparation commands.")
app.add_typer(data_app, name="data")


def _fail(message: str) -> NoReturn:
    """Print an error to stderr and exit with a nonzero status."""
    typer.secho(message, err=True, fg=typer.colors.RED)
    raise typer.Exit(code=1)


def _load_config(config_path: Path) -> BenchmarkConfig:
    try:
        return BenchmarkConfig.load(config_path)
    except FileNotFoundError as exc:
        _fail(str(exc))
    except ValidationError as exc:
        _fail(f"Invalid benchmark config {config_path}:\n{exc}")


def _load_azure_settings() -> AzureSettings:
    try:
        return AzureSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        _fail(
            "Azure settings are missing or invalid. Checked environment variables and "
            f".env file:\n{exc}"
        )


ConfigOption = Annotated[Path, typer.Option("--config", help="Benchmark config YAML.")]


@app.command("demo")
def demo(
    host: Annotated[
        str | None,
        typer.Option(
            "--host",
            help="Loopback bind address (defaults to RECEIPT_DEMO_HOST or 127.0.0.1).",
        ),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="Local port (defaults to RECEIPT_DEMO_PORT or 8000).",
        ),
    ] = None,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the local demo URL in the default browser."),
    ] = True,
) -> None:
    """Launch the packaged web demo; defaults to offline deterministic mock mode."""
    try:
        run_demo_server(host=host, port=port, open_browser=open_browser)
    except ProductLaunchError as exc:
        _fail(str(exc))


@app.command("config-check")
def config_check(config: ConfigOption) -> None:
    """Validate the benchmark config and Azure settings without making any calls.

    Never prints secret values: the Azure Foundry API key is reported as present or
    missing only, never echoed.
    """
    benchmark_config = _load_config(config)
    typer.echo(f"Benchmark config OK: {config}")
    typer.echo(f"  schema_path:      {benchmark_config.schema_path}")
    typer.echo(f"  dataset_manifest: {benchmark_config.dataset_manifest}")
    typer.echo(f"  output_root:      {benchmark_config.output_root}")
    typer.echo(f"  concurrency:      {benchmark_config.concurrency}")
    typer.echo(f"  warmup_requests:  {benchmark_config.warmup_requests}")
    typer.echo(f"  random_seed:      {benchmark_config.random_seed}")
    typer.echo(f"  solutions:        {[solution.id for solution in benchmark_config.solutions]}")

    if not benchmark_config.schema_path.is_file():
        _fail(f"schema_path does not exist: {benchmark_config.schema_path}")
    if not benchmark_config.dataset_manifest.is_file():
        _fail(f"dataset_manifest does not exist: {benchmark_config.dataset_manifest}")

    try:
        load_json_schema(benchmark_config.schema_path)
    except (OSError, TypeError, ValueError) as exc:
        _fail(f"schema_path is not a valid JSON schema: {exc}")

    try:
        manifest = load_manifest(benchmark_config.dataset_manifest)
    except ManifestError as exc:
        _fail(str(exc))
    typer.echo(f"  manifest samples: {len(manifest)}")

    settings = _load_azure_settings()
    typer.echo("Azure settings OK:")
    typer.echo(f"  azure_subscription_id: {settings.azure_subscription_id}")
    typer.echo(f"  azure_resource_group:  {settings.azure_resource_group}")
    typer.echo(f"  azure_location:        {settings.azure_location}")
    typer.echo(f"  azure_foundry_account: {settings.azure_foundry_account}")
    typer.echo(f"  azure_foundry_project: {settings.azure_foundry_project}")
    typer.echo(
        "  azure_ai_auth_mode:    "
        f"{settings.azure_ai_auth_mode} (resolved: {settings.resolved_auth_mode})"
    )
    typer.echo(f"  azure_ai_credential:   {settings.azure_ai_credential_kind}")
    typer.echo(
        "  azure_foundry_api_key: "
        + (
            "****** (present, not printed)"
            if settings.azure_foundry_api_key is not None
            else "not configured"
        )
    )
    typer.echo(f"  azure_foundry_openai_endpoint: {settings.azure_foundry_openai_endpoint}")
    typer.echo(f"  azure_foundry_models_endpoint: {settings.azure_foundry_models_endpoint}")
    typer.echo(f"  azure_anthropic_endpoint: {settings.azure_anthropic_endpoint}")
    typer.echo(f"  azure_mistral_ocr_endpoint: {settings.azure_mistral_ocr_endpoint}")
    typer.echo(
        f"  azure_content_understanding_endpoint: {settings.azure_content_understanding_endpoint}"
    )
    typer.echo(
        f"  azure_document_intelligence_endpoint: {settings.azure_document_intelligence_endpoint}"
    )
    typer.echo(f"  azure_vision_endpoint: {settings.azure_vision_endpoint}")
    typer.echo("All checks passed.")


@data_app.command("prepare")
def data_prepare(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Optional dataset preparation YAML; defaults to the packaged pinned config.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-fetch and overwrite even if already downloaded."),
    ] = False,
) -> None:
    """Prepare the receipt dataset manifest and images (delegates to the datasets package)."""
    try:
        manifest_path = prepare_dataset(config, force=force)
    except FileNotFoundError as exc:
        _fail(str(exc))
    except (DatasetConfigError, DatasetFetchError, DatasetParsingError) as exc:
        _fail(f"Dataset preparation failed: {exc}")
    typer.echo(f"Dataset manifest written to: {manifest_path}")


@app.command("run")
def run(
    config: ConfigOption,
    resume: Annotated[
        str | None,
        typer.Option("--resume", help="Existing run id to resume instead of starting a new run."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help=(
                "Run only the first N samples (sorted by sample_id) for a required smoke "
                "phase before the full run. The slice is deterministic: sorting happens "
                "before the limit is applied, so `--limit 1` always selects the same "
                "sample. A smoke run's --resume run id can later be re-run without "
                "--limit to process the remaining (sample_id, solution_id) pairs; "
                "already-completed pairs are skipped, not repeated."
            ),
        ),
    ] = None,
    solution_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--solution",
            help="Run only the named solution; repeat the option to select multiple solutions.",
        ),
    ] = None,
) -> None:
    """Run the benchmark: every configured solution against every manifest sample.

    Makes real (potentially paid) provider calls through adapters built by
    ``receipt_bench.providers.registry.build_adapters``.
    """
    benchmark_config = _load_config(config)
    selected_ids = set(solution_ids or [])
    if selected_ids:
        configured_ids = {solution.id for solution in benchmark_config.solutions}
        unknown_ids = sorted(selected_ids - configured_ids)
        if unknown_ids:
            _fail(
                "Unknown --solution value(s): "
                f"{unknown_ids}. Configured solutions: {sorted(configured_ids)}"
            )

    try:
        schema = load_json_schema(benchmark_config.schema_path)
    except (OSError, TypeError, ValueError) as exc:
        _fail(f"Failed to load schema: {exc}")

    try:
        manifest = load_manifest(benchmark_config.dataset_manifest)
    except ManifestError as exc:
        _fail(str(exc))
    planned_sample_ids = tuple(sample.sample_id for sample in manifest)

    if limit is not None:
        # Sort before slicing so the smoke subset is deterministic and matches the
        # sample_id order run_benchmark itself uses, independent of manifest file order.
        manifest = sorted(manifest, key=lambda sample: sample.sample_id)[:limit]

    settings = _load_azure_settings()

    try:
        adapters = build_adapters(benchmark_config, settings, schema)
    except AdapterConfigurationError as exc:
        _fail(f"Failed to build provider adapters: {exc}")

    if selected_ids:
        selected_solutions = [
            solution for solution in benchmark_config.solutions if solution.id in selected_ids
        ]
        benchmark_config = benchmark_config.model_copy(update={"solutions": selected_solutions})
        adapters = {solution.id: adapters[solution.id] for solution in selected_solutions}

    try:
        summary = run_benchmark(
            config=benchmark_config,
            manifest_path=benchmark_config.dataset_manifest,
            manifest=manifest,
            planned_sample_ids=planned_sample_ids,
            adapters=adapters,
            run_id=resume,
        )
    except (RunConfigurationError, ImageIntegrityError, FileNotFoundError) as exc:
        _fail(f"Run failed: {exc}")

    typer.echo(f"Run id:          {summary.run_id}")
    typer.echo(f"Total pairs:     {summary.total_pairs}")
    typer.echo(f"Completed pairs: {summary.completed_pairs}")
    typer.echo(f"Skipped pairs:   {summary.skipped_pairs}")
    typer.echo(f"Results path:    {summary.results_path}")


@app.command("report")
def report(
    config: ConfigOption,
    run_id: Annotated[str, typer.Option("--run-id", help="Run id to score and report on.")],
    overlay_run_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--overlay-run",
            help=(
                "Later run whose (sample, solution) pairs replace matching base-run pairs; "
                "repeat to apply multiple overlays in order."
            ),
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", help="Directory to write report outputs (defaults to the run directory)."
        ),
    ] = None,
    pricing: Annotated[
        Path,
        typer.Option(
            "--pricing",
            help=(
                "Optional pricing snapshot YAML (solution/component rate card) used to "
                "estimate component costs during aggregation. Missing file is not an error; "
                "a present-but-invalid file is."
            ),
        ),
    ] = Path("configs/pricing.yaml"),
) -> None:
    """Score a completed run's results deterministically and write JSON/CSV/HTML reports."""
    benchmark_config = _load_config(config)
    if overlay_run_ids and output is None:
        _fail("--output is required when --overlay-run is used")

    try:
        schema = load_json_schema(benchmark_config.schema_path)
    except (OSError, TypeError, ValueError) as exc:
        _fail(f"Failed to load schema: {exc}")

    try:
        manifest = load_manifest(benchmark_config.dataset_manifest)
    except ManifestError as exc:
        _fail(str(exc))
    samples_by_id = {sample.sample_id: sample for sample in manifest}

    source_run_ids = [run_id, *(overlay_run_ids or [])]
    results_path = benchmark_config.output_root / run_id / "results.jsonl"
    try:
        results = read_results(results_path)
    except ResultsIOError as exc:
        _fail(str(exc))
    if not results:
        _fail(f"No results found for run {run_id!r} at {results_path}")

    results_by_pair = {(result.sample_id, result.solution_id): result for result in results}
    if len(results_by_pair) != len(results):
        _fail(f"Base run {run_id!r} contains duplicate (sample_id, solution_id) pairs")
    base_pairs = set(results_by_pair)
    run_plan_path = benchmark_config.output_root / run_id / "run-plan.json"
    if run_plan_path.is_file():
        try:
            plan = load_run_plan(run_plan_path)
        except RunConfigurationError as exc:
            _fail(str(exc))
        if plan.run_id != run_id:
            _fail(
                f"Run plan ID {plan.run_id!r} does not match requested run {run_id!r}"
            )
        expected_pairs = plan.expected_pairs
    else:
        observed_samples = {sample_id for sample_id, _ in base_pairs}
        observed_solutions = {solution_id for _, solution_id in base_pairs}
        expected_pairs = {
            (sample_id, solution_id)
            for sample_id in observed_samples
            for solution_id in observed_solutions
        }
    missing_pairs = sorted(expected_pairs - base_pairs)
    unexpected_pairs = sorted(base_pairs - expected_pairs)
    if missing_pairs or unexpected_pairs:
        _fail(
            f"Base run {run_id!r} does not contain its complete expected result matrix; "
            f"missing={missing_pairs[:5]}, unexpected={unexpected_pairs[:5]}"
        )

    overlay_warmups_by_solution: dict[str, list[WarmupRecord]] = {}
    for overlay_run_id in overlay_run_ids or []:
        overlay_results_path = (
            benchmark_config.output_root / overlay_run_id / "results.jsonl"
        )
        try:
            overlay_results = read_results(overlay_results_path)
        except ResultsIOError as exc:
            _fail(str(exc))
        if not overlay_results:
            _fail(
                f"No results found for overlay run {overlay_run_id!r} "
                f"at {overlay_results_path}"
            )
        overlay_pairs = {
            (result.sample_id, result.solution_id): result for result in overlay_results
        }
        if len(overlay_pairs) != len(overlay_results):
            _fail(
                f"Overlay run {overlay_run_id!r} contains duplicate "
                "(sample_id, solution_id) pairs"
            )
        unexpected_pairs = sorted(set(overlay_pairs) - base_pairs)
        if unexpected_pairs:
            _fail(
                f"Overlay run {overlay_run_id!r} contains pairs absent from the base run: "
                f"{unexpected_pairs[:5]}"
            )
        results_by_pair.update(overlay_pairs)
        overlay_warmups_path = (
            benchmark_config.output_root / overlay_run_id / "warmups.jsonl"
        )
        try:
            current_overlay_warmups = read_warmups(overlay_warmups_path)
        except WarmupIOError as exc:
            _fail(str(exc))
        for solution_id in {result.solution_id for result in overlay_results}:
            replacement_warmups = [
                warmup
                for warmup in current_overlay_warmups
                if warmup.solution_id == solution_id
            ]
            if replacement_warmups:
                overlay_warmups_by_solution[solution_id] = replacement_warmups

    results = [
        results_by_pair[pair]
        for pair in sorted(results_by_pair, key=lambda pair: (pair[0], pair[1]))
    ]

    try:
        pricing_snapshot = load_pricing_snapshot(pricing)
    except PricingConfigError as exc:
        _fail(str(exc))

    warmups_path = benchmark_config.output_root / run_id / "warmups.jsonl"
    try:
        warmups = read_warmups(warmups_path)
    except WarmupIOError as exc:
        _fail(str(exc))
    replaced_warmup_solution_ids = set(overlay_warmups_by_solution)
    if replaced_warmup_solution_ids:
        warmups = [
            warmup
            for warmup in warmups
            if warmup.solution_id not in replaced_warmup_solution_ids
        ]
        for solution_id in sorted(overlay_warmups_by_solution):
            warmups.extend(overlay_warmups_by_solution[solution_id])

    evaluated: list[EvaluatedOutput] = []
    for solution_output in results:
        sample = samples_by_id.get(solution_output.sample_id)
        if sample is None:
            _fail(
                f"Result references unknown sample_id {solution_output.sample_id!r} "
                "not present in the configured dataset manifest"
            )
        evaluated.append(score_output(sample, solution_output, schema))

    normalizer_models = {
        solution.id: solution.normalizer
        for solution in benchmark_config.solutions
        if solution.normalizer is not None
    }
    solution_metadata = {
        solution.id: SolutionMetadata(
            kind=solution.kind,
            lifecycle=solution.lifecycle,
            deployment=solution.deployment,
            model=solution.model,
        )
        for solution in benchmark_config.solutions
    }
    report_run_id = "+".join(source_run_ids)
    aggregate_report = build_report(
        report_run_id,
        evaluated,
        samples_by_id,
        pricing=pricing_snapshot,
        normalizer_models=normalizer_models,
        warmups=warmups,
        solution_metadata=solution_metadata,
    )

    report_dir = output if output is not None else benchmark_config.output_root / run_id / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    json_path = write_json_report(aggregate_report, report_dir / "aggregate.json")
    csv_paths = write_csv_reports(aggregate_report, report_dir)
    html_path = write_html_report(aggregate_report, report_dir / "report.html")

    typer.echo(f"Source runs: {source_run_ids}")
    typer.echo(f"JSON report: {json_path}")
    for name, path in csv_paths.items():
        typer.echo(f"CSV ({name}): {path}")
    typer.echo(f"HTML report: {html_path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
