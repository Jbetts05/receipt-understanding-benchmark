"""Tests for the receipt_bench Typer CLI.

These tests never make real (potentially paid) provider calls or real network
requests: ``run`` is exercised with ``receipt_bench.cli.build_adapters``
monkeypatched to return in-memory ``FakeAdapter`` instances, and ``data
prepare`` is exercised with ``receipt_bench.cli.prepare_dataset`` monkeypatched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

import receipt_bench.cli as cli_module
from receipt_bench.cli import app
from receipt_bench.config import AzureSettings, BenchmarkConfig
from receipt_bench.datasets.errors import DatasetConfigError
from receipt_bench.providers.base import SolutionAdapter
from tests.conftest import FakeAdapter

ROOT = Path(__file__).resolve().parent.parent

_SECRET_VALUE = "super-secret-do-not-print-me"

_AZURE_ENV: dict[str, str] = {
    "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_RESOURCE_GROUP": "rg-receipt-bench-test",
    "AZURE_LOCATION": "swedencentral",
    "AZURE_FOUNDRY_ACCOUNT": "airbtest0001",
    "AZURE_FOUNDRY_PROJECT": "receipt-bench",
    "AZURE_FOUNDRY_API_KEY": _SECRET_VALUE,
    "AZURE_FOUNDRY_OPENAI_ENDPOINT": "https://foundry-openai.example.com",
    "AZURE_FOUNDRY_MODELS_ENDPOINT": "https://foundry-models.example.com",
    "AZURE_ANTHROPIC_ENDPOINT": "https://anthropic.example.com",
    "AZURE_MISTRAL_OCR_ENDPOINT": "https://mistral-ocr.example.com",
    "AZURE_CONTENT_UNDERSTANDING_ENDPOINT": "https://content-understanding.example.com",
    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://document-intelligence.example.com",
    "AZURE_VISION_ENDPOINT": "https://vision.example.com",
}

runner = CliRunner()


def test_demo_command_delegates_to_packaged_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict[str, Any] = {}

    def fake_run_demo_server(
        *,
        host: str | None,
        port: int | None,
        open_browser: bool,
    ) -> None:
        called.update(host=host, port=port, open_browser=open_browser)

    monkeypatch.setattr(cli_module, "run_demo_server", fake_run_demo_server)

    result = runner.invoke(
        app,
        ["demo", "--host", "127.0.0.1", "--port", "8123", "--no-open"],
    )

    assert result.exit_code == 0, result.output
    assert called == {"host": "127.0.0.1", "port": 8123, "open_browser": False}


@pytest.fixture
def azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every required ``AzureSettings`` environment variable to a fake value."""
    for name, value in _AZURE_ENV.items():
        monkeypatch.setenv(name, value)


def _write_config(
    tmp_path: Path,
    *,
    schema_path: Path,
    dataset_manifest: Path,
    output_root: Path | None = None,
    warmup_requests: int = 0,
    solutions: list[dict[str, Any]] | None = None,
    name: str = "config.yaml",
) -> Path:
    """Write a ``BenchmarkConfig``-shaped file (JSON is valid YAML) and return its path."""
    payload: dict[str, Any] = {
        "schema_path": str(schema_path),
        "dataset_manifest": str(dataset_manifest),
        "output_root": str(output_root if output_root is not None else tmp_path / "runs"),
        "warmup_requests": warmup_requests,
        "random_seed": 12345,
        "solutions": (
            solutions
            if solutions is not None
            else [{"id": "solution-a", "kind": "direct", "api": "azure-openai"}]
        ),
    }
    config_path = tmp_path / name
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def _fake_build_adapters(
    config: BenchmarkConfig,
    settings: AzureSettings,
    schema: dict[str, Any],
) -> dict[str, SolutionAdapter]:
    del settings, schema
    return {solution.id: FakeAdapter(solution.id) for solution in config.solutions}


# --------------------------------------------------------------------------- #
# config-check
# --------------------------------------------------------------------------- #


def test_config_check_succeeds_and_never_prints_the_secret(
    tmp_path: Path, manifest_path: Path, azure_env: None
) -> None:
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "All checks passed." in result.output
    assert "manifest samples: 2" in result.output
    assert _SECRET_VALUE not in result.output
    assert "azure_ai_auth_mode:    auto (resolved: key)" in result.output
    assert "azure_ai_credential:   default" in result.output
    assert "azure_foundry_api_key: ****** (present, not printed)" in result.output


def test_config_check_accepts_entra_without_an_api_key(
    tmp_path: Path,
    manifest_path: Path,
    azure_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_AI_AUTH_MODE", "entra")
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY")
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "azure_ai_auth_mode:    entra (resolved: entra)" in result.output
    assert "azure_foundry_api_key: not configured" in result.output


def test_config_check_fails_when_config_file_is_missing(
    tmp_path: Path, azure_env: None
) -> None:
    result = runner.invoke(
        app, ["config-check", "--config", str(tmp_path / "does-not-exist.yaml")]
    )

    assert result.exit_code != 0
    assert "not found" in result.output.lower()
    assert _SECRET_VALUE not in result.output


def test_config_check_fails_when_schema_path_is_missing(
    tmp_path: Path, manifest_path: Path, azure_env: None
) -> None:
    config_path = _write_config(
        tmp_path,
        schema_path=tmp_path / "missing-schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "schema_path does not exist" in result.output


def test_config_check_fails_when_dataset_manifest_is_missing(
    tmp_path: Path, azure_env: None
) -> None:
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=tmp_path / "missing-manifest.jsonl",
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "dataset_manifest does not exist" in result.output


def test_config_check_fails_when_schema_is_not_valid_json(
    tmp_path: Path, manifest_path: Path, azure_env: None
) -> None:
    bad_schema = tmp_path / "bad-schema.json"
    bad_schema.write_text("{not json", encoding="utf-8")
    config_path = _write_config(
        tmp_path, schema_path=bad_schema, dataset_manifest=manifest_path
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "not a valid JSON schema" in result.output


def test_config_check_fails_when_manifest_row_is_malformed(
    tmp_path: Path, manifest_path: Path, azure_env: None
) -> None:
    broken_manifest = tmp_path / "broken-manifest.jsonl"
    broken_manifest.write_text(
        manifest_path.read_text(encoding="utf-8") + "not json at all\n", encoding="utf-8"
    )
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=broken_manifest,
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "manifest" in result.output.lower()


def test_config_check_fails_when_azure_settings_are_missing(
    tmp_path: Path, manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _AZURE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # avoid picking up a real .env from the repo root
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["config-check", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Azure settings are missing or invalid" in result.output
    assert _SECRET_VALUE not in result.output


# --------------------------------------------------------------------------- #
# data prepare
# --------------------------------------------------------------------------- #


def test_data_prepare_delegates_and_reports_the_manifest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    produced_manifest = tmp_path / "data" / "manifests" / "benchmark.jsonl"

    def _fake_prepare_dataset(config_path: Path, *, force: bool = False) -> Path:
        assert config_path == tmp_path / "datasets.yaml"
        assert force is True
        return produced_manifest

    monkeypatch.setattr(cli_module, "prepare_dataset", _fake_prepare_dataset)
    config_path = tmp_path / "datasets.yaml"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app, ["data", "prepare", "--config", str(config_path), "--force"]
    )

    assert result.exit_code == 0, result.output
    assert str(produced_manifest) in result.output


def test_data_prepare_uses_packaged_config_when_override_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    produced_manifest = tmp_path / "benchmark.jsonl"

    def _fake_prepare_dataset(
        config_path: Path | None,
        *,
        force: bool = False,
    ) -> Path:
        assert config_path is None
        assert not force
        return produced_manifest

    monkeypatch.setattr(cli_module, "prepare_dataset", _fake_prepare_dataset)

    result = runner.invoke(app, ["data", "prepare"])

    assert result.exit_code == 0, result.output
    assert str(produced_manifest) in result.output


def test_data_prepare_reports_dataset_config_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_prepare_dataset(config_path: Path, *, force: bool = False) -> Path:
        del config_path, force
        raise DatasetConfigError("bad dataset config")

    monkeypatch.setattr(cli_module, "prepare_dataset", _fake_prepare_dataset)
    config_path = tmp_path / "datasets.yaml"
    config_path.write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["data", "prepare", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Dataset preparation failed" in result.output
    assert "bad dataset config" in result.output


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def test_run_executes_all_pairs_with_fake_adapters(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    output_root = tmp_path / "runs"
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=output_root,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "Total pairs:     2" in result.output
    assert "Completed pairs: 2" in result.output
    assert "Skipped pairs:   0" in result.output

    results_files = list(output_root.glob("*/results.jsonl"))
    assert len(results_files) == 1
    lines = results_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_run_resume_skips_already_complete_pairs(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    output_root = tmp_path / "runs"
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=output_root,
    )

    first = runner.invoke(app, ["run", "--config", str(config_path)])
    assert first.exit_code == 0, first.output
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in first.output.splitlines()
        if line.startswith("Run id:")
    )

    second = runner.invoke(
        app, ["run", "--config", str(config_path), "--resume", run_id]
    )

    assert second.exit_code == 0, second.output
    assert f"Run id:          {run_id}" in second.output
    assert "Completed pairs: 0" in second.output
    assert "Skipped pairs:   2" in second.output


def test_run_limit_restricts_to_first_sample_by_sample_id(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manifest_path has sample-001 and sample-002; --limit 1 must select the
    lowest sample_id deterministically, regardless of manifest file order."""
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    output_root = tmp_path / "runs"
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=output_root,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path), "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "Total pairs:     1" in result.output
    assert "Completed pairs: 1" in result.output
    assert "Skipped pairs:   0" in result.output

    results_files = list(output_root.glob("*/results.jsonl"))
    assert len(results_files) == 1
    lines = results_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["sample_id"] == "sample-001"


def test_run_limit_smoke_then_resume_without_limit_completes_remaining_pairs(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `--limit 1` smoke run can be resumed with the same run id and no
    `--limit` to process the rest of the manifest without repeating work."""
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    output_root = tmp_path / "runs"
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=output_root,
    )

    smoke = runner.invoke(app, ["run", "--config", str(config_path), "--limit", "1"])
    assert smoke.exit_code == 0, smoke.output
    assert "Total pairs:     1" in smoke.output
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in smoke.output.splitlines()
        if line.startswith("Run id:")
    )

    full = runner.invoke(app, ["run", "--config", str(config_path), "--resume", run_id])

    assert full.exit_code == 0, full.output
    assert f"Run id:          {run_id}" in full.output
    assert "Total pairs:     2" in full.output
    assert "Completed pairs: 1" in full.output
    assert "Skipped pairs:   1" in full.output

    results_files = list(output_root.glob("*/results.jsonl"))
    assert len(results_files) == 1
    lines = results_files[0].read_text(encoding="utf-8").strip().splitlines()
    sample_ids = {json.loads(line)["sample_id"] for line in lines}
    assert sample_ids == {"sample-001", "sample-002"}


def test_run_limit_rejects_non_positive_value(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path), "--limit", "0"])

    assert result.exit_code != 0
    assert "limit" in result.output.lower()


def test_run_solution_filter_executes_only_selected_solution(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    output_root = tmp_path / "runs"
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=output_root,
        solutions=[
            {"id": "solution-a", "kind": "direct", "api": "azure-openai"},
            {"id": "solution-b", "kind": "native", "api": "content-understanding"},
        ],
    )

    result = runner.invoke(
        app, ["run", "--config", str(config_path), "--solution", "solution-b"]
    )

    assert result.exit_code == 0, result.output
    assert "Total pairs:     2" in result.output
    results_path = next(output_root.glob("*/results.jsonl"))
    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {row["solution_id"] for row in rows} == {"solution-b"}


def test_run_solution_filter_rejects_unknown_solution(
    tmp_path: Path, manifest_path: Path, azure_env: None
) -> None:
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(
        app, ["run", "--config", str(config_path), "--solution", "missing-solution"]
    )

    assert result.exit_code != 0
    assert "Unknown --solution value" in result.output
    assert "solution-a" in result.output


def test_run_help_documents_limit_option() -> None:
    result = runner.invoke(app, ["run", "--help"], color=False, terminal_width=160)
    help_text = strip_ansi(result.output)

    assert result.exit_code == 0
    assert "--limit" in help_text
    assert "--solution" in help_text
    assert "smoke" in help_text.lower()


def test_run_fails_when_a_configured_solution_has_no_adapter(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _incomplete_build_adapters(
        config: BenchmarkConfig, settings: AzureSettings, schema: dict[str, Any]
    ) -> dict[str, SolutionAdapter]:
        del config, settings, schema
        return {}

    monkeypatch.setattr(cli_module, "build_adapters", _incomplete_build_adapters)
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Run failed" in result.output
    assert "solution-a" in result.output


def test_run_fails_when_azure_settings_are_missing(
    tmp_path: Path, manifest_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _AZURE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Azure settings are missing or invalid" in result.output


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def _run_once(
    tmp_path: Path,
    manifest_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_root: Path,
    warmup_requests: int = 0,
    solutions: list[dict[str, Any]] | None = None,
) -> tuple[Path, str]:
    monkeypatch.setattr(cli_module, "build_adapters", _fake_build_adapters)
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=output_root,
        warmup_requests=warmup_requests,
        solutions=solutions,
    )
    result = runner.invoke(app, ["run", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    run_id = next(
        line.split(":", 1)[1].strip()
        for line in result.output.splitlines()
        if line.startswith("Run id:")
    )
    return config_path, run_id


def test_report_writes_json_csv_and_html_outputs(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path,
        manifest_path,
        monkeypatch,
        output_root=output_root,
    )
    report_dir = tmp_path / "report-out"

    result = runner.invoke(
        app,
        [
            "report",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--output",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (report_dir / "aggregate.json").is_file()
    assert (report_dir / "report.html").is_file()
    csv_files = list(report_dir.glob("*.csv"))
    assert csv_files

    aggregate = json.loads((report_dir / "aggregate.json").read_text(encoding="utf-8"))
    assert aggregate["run_id"] == run_id
    assert "ranking" not in json.dumps(aggregate).lower()


def test_report_rejects_an_incomplete_run_matrix(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path,
        manifest_path,
        monkeypatch,
        output_root=output_root,
    )
    results_path = output_root / run_id / "results.jsonl"
    rows = results_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) > 1
    results_path.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["report", "--config", str(config_path), "--run-id", run_id],
    )

    assert result.exit_code != 0
    assert "complete expected result matrix" in result.output


def test_report_overlay_replaces_matching_pairs_and_records_source_runs(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path,
        manifest_path,
        monkeypatch,
        output_root=output_root,
        warmup_requests=1,
    )
    base_results_path = output_root / run_id / "results.jsonl"
    base_rows = [
        json.loads(line)
        for line in base_results_path.read_text(encoding="utf-8").splitlines()
    ]
    overlay_run_id = "correction-run"
    overlay_dir = output_root / overlay_run_id
    overlay_dir.mkdir(parents=True)
    corrected_row = dict(base_rows[0])
    corrected_row.update(
        {
            "run_id": overlay_run_id,
            "status": "failed",
            "receipt": None,
            "error_type": "ProviderResponseError",
            "error_message": "corrected measurement failed",
            "components": [],
        }
    )
    (overlay_dir / "results.jsonl").write_text(
        json.dumps(corrected_row) + "\n", encoding="utf-8"
    )
    report_dir = tmp_path / "overlay-report"

    result = runner.invoke(
        app,
        [
            "report",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--overlay-run",
            overlay_run_id,
            "--output",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    aggregate = json.loads((report_dir / "aggregate.json").read_text(encoding="utf-8"))
    assert aggregate["run_id"] == f"{run_id}+{overlay_run_id}"
    assert aggregate["solutions"][0]["total_samples"] == 2
    assert aggregate["solutions"][0]["failed"] == 1
    assert aggregate["warmups"][0]["warmup_count"] == 1
    assert f"Source runs: ['{run_id}', '{overlay_run_id}']" in result.output


def test_report_overlay_rejects_pairs_absent_from_base_run(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path, manifest_path, monkeypatch, output_root=output_root
    )
    base_results_path = output_root / run_id / "results.jsonl"
    extra_row = json.loads(
        base_results_path.read_text(encoding="utf-8").splitlines()[0]
    )
    overlay_run_id = "unrelated-run"
    overlay_dir = output_root / overlay_run_id
    overlay_dir.mkdir(parents=True)
    extra_row.update({"run_id": overlay_run_id, "sample_id": "not-in-base"})
    (overlay_dir / "results.jsonl").write_text(
        json.dumps(extra_row) + "\n", encoding="utf-8"
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--overlay-run",
            overlay_run_id,
            "--output",
            str(tmp_path / "overlay-report"),
        ],
    )

    assert result.exit_code != 0
    assert "pairs absent from the base run" in result.output


def test_report_overlay_requires_explicit_output_directory(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path, manifest_path, monkeypatch, output_root=output_root
    )

    result = runner.invoke(
        app,
        [
            "report",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--overlay-run",
            "correction-run",
        ],
    )

    assert result.exit_code != 0
    assert "--output is required" in result.output


def test_report_threads_solution_lifecycle_kind_deployment_and_model(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview solution's BenchmarkConfig identity metadata (lifecycle, kind,
    deployment, model) must reach the aggregate JSON/CSV/HTML so the report can
    display a Preview badge, without affecting ranking or ordering.
    """
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path,
        manifest_path,
        monkeypatch,
        output_root=output_root,
        solutions=[
            {
                "id": "solution-a",
                "kind": "ocr_pipeline",
                "api": "azure-openai",
                "deployment": "receipt-mistral-ocr-4",
                "model": "mistral-ocr-4-0",
                "lifecycle": "preview",
            }
        ],
    )
    report_dir = tmp_path / "report-out"

    result = runner.invoke(
        app,
        [
            "report",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--output",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    aggregate = json.loads((report_dir / "aggregate.json").read_text(encoding="utf-8"))
    solution = next(
        row for row in aggregate["solutions"] if row["solution_id"] == "solution-a"
    )
    assert solution["lifecycle"] == "preview"
    assert solution["kind"] == "ocr_pipeline"
    assert solution["deployment"] == "receipt-mistral-ocr-4"
    assert solution["model"] == "mistral-ocr-4-0"

    solutions_csv = (report_dir / "solutions.csv").read_text(encoding="utf-8")
    assert "preview" in solutions_csv
    assert "mistral-ocr-4-0" in solutions_csv

    html = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "badge-preview" in html
    assert '"lifecycle": "preview"' in html


def test_report_includes_warmup_overhead_when_recorded(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path, manifest_path, monkeypatch, output_root=output_root, warmup_requests=2
    )
    report_dir = tmp_path / "report-out"

    result = runner.invoke(
        app,
        [
            "report",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
            "--output",
            str(report_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    aggregate = json.loads((report_dir / "aggregate.json").read_text(encoding="utf-8"))
    assert aggregate["warmups"]
    assert aggregate["warmups"][0]["solution_id"] == "solution-a"
    assert aggregate["warmups"][0]["warmup_count"] == 2

    warmups_csv = (report_dir / "warmups.csv").read_text(encoding="utf-8")
    assert "solution-a" in warmups_csv

    html = (report_dir / "report.html").read_text(encoding="utf-8")
    assert "warmup" in html.lower()


def test_report_fails_when_warmups_file_is_malformed(
    tmp_path: Path, manifest_path: Path, azure_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = tmp_path / "runs"
    config_path, run_id = _run_once(
        tmp_path, manifest_path, monkeypatch, output_root=output_root, warmup_requests=1
    )
    warmups_path = output_root / run_id / "warmups.jsonl"
    with warmups_path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")

    result = runner.invoke(
        app, ["report", "--config", str(config_path), "--run-id", run_id]
    )

    assert result.exit_code != 0
    assert "warmup" in result.output.lower()


def test_report_fails_when_no_results_exist_for_the_run_id(
    tmp_path: Path, manifest_path: Path, azure_env: None
) -> None:
    config_path = _write_config(
        tmp_path,
        schema_path=ROOT / "schemas" / "expense-receipt.schema.json",
        dataset_manifest=manifest_path,
        output_root=tmp_path / "runs",
    )

    result = runner.invoke(
        app, ["report", "--config", str(config_path), "--run-id", "no-such-run"]
    )

    assert result.exit_code != 0
    assert "No results found" in result.output


# --------------------------------------------------------------------------- #
# top-level smoke test
# --------------------------------------------------------------------------- #


def test_app_shows_help_with_no_args() -> None:
    no_args_result = runner.invoke(app, [])
    help_result = runner.invoke(app, ["--help"])

    assert help_result.exit_code == 0
    for result in (no_args_result, help_result):
        assert "demo" in result.output
        assert "config-check" in result.output
        assert "report" in result.output
