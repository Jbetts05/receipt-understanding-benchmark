from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from receipt_bench.datasets.config import CordSourceConfig, DatasetsConfig, SroieSourceConfig
from receipt_bench.datasets.errors import DatasetConfigError

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs" / "datasets.yaml"


def test_committed_datasets_config_loads_and_validates() -> None:
    config = DatasetsConfig.load(CONFIG_PATH)

    assert config.sroie.repo == "zzzDavid/ICDAR-2019-SROIE"
    assert config.sroie.commit == "27be4271b251c256f695acbade9a801bffe85994"
    assert config.sroie.count == 80
    assert config.sroie.split == "trainval"

    assert config.cord.dataset == "naver-clova-ix/cord-v2"
    assert config.cord.revision == "7f0115a4b758a71d6473b8d085751692da2fef98"
    assert config.cord.total_rows == 100
    assert config.cord.sample_count == 20
    assert config.cord.selection_seed == 20260828
    assert config.cord.selected_indices == [
        3,
        11,
        15,
        26,
        28,
        34,
        35,
        42,
        46,
        47,
        49,
        58,
        63,
        68,
        72,
        73,
        74,
        80,
        85,
        98,
    ]
    assert config.cord.selected_indices == sorted(config.cord.selected_indices)
    assert len(set(config.cord.selected_indices)) == 20


def test_packaged_datasets_config_matches_committed_source_pins() -> None:
    committed = DatasetsConfig.load(CONFIG_PATH)
    packaged = DatasetsConfig.load_builtin()

    assert packaged.sroie.repo == committed.sroie.repo
    assert packaged.sroie.commit == committed.sroie.commit
    assert packaged.sroie.count == committed.sroie.count
    assert packaged.cord.dataset == committed.cord.dataset
    assert packaged.cord.revision == committed.cord.revision
    assert packaged.cord.selected_indices == committed.cord.selected_indices


def test_load_missing_config_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        DatasetsConfig.load(tmp_path / "does-not-exist.yaml")


def test_load_non_mapping_config_raises_dataset_config_error(tmp_path: Path) -> None:
    path = tmp_path / "datasets.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(DatasetConfigError):
        DatasetsConfig.load(path)


def _base_config_dict() -> dict[str, object]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_tampered_selected_indices_are_rejected(tmp_path: Path) -> None:
    raw = _base_config_dict()
    cord = dict(raw["cord"])  # type: ignore[arg-type]
    indices = list(cord["selected_indices"])
    # Swap one selected index for a nearby one not in the original set, keeping the
    # same count/uniqueness/sortedness but breaking the deterministic-selection match.
    replacement = next(i for i in range(100) if i not in indices)
    indices[0] = replacement
    cord["selected_indices"] = sorted(indices)
    raw["cord"] = cord

    path = tmp_path / "datasets.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(DatasetConfigError):
        DatasetsConfig.load(path)


def test_unsorted_selected_indices_are_rejected() -> None:
    raw = _base_config_dict()
    cord = dict(raw["cord"])  # type: ignore[arg-type]
    cord["selected_indices"] = list(reversed(cord["selected_indices"]))
    raw["cord"] = cord

    with pytest.raises(ValueError, match="sorted ascending"):
        DatasetsConfig.model_validate(raw)


def test_duplicate_selected_indices_are_rejected() -> None:
    raw = _base_config_dict()
    cord = dict(raw["cord"])  # type: ignore[arg-type]
    indices = list(cord["selected_indices"])
    indices[1] = indices[0]
    cord["selected_indices"] = indices
    raw["cord"] = cord

    with pytest.raises(DatasetConfigError):
        DatasetsConfig.load(_write_tmp(raw))


def _write_tmp(raw: dict[str, object]) -> Path:
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as handle:
        yaml.safe_dump(raw, handle)
        name = handle.name
    return Path(name)


def test_out_of_range_selected_index_is_rejected() -> None:
    raw = _base_config_dict()
    cord = dict(raw["cord"])  # type: ignore[arg-type]
    indices = list(cord["selected_indices"])
    indices[0] = 100
    cord["selected_indices"] = sorted(indices)
    raw["cord"] = cord

    with pytest.raises(DatasetConfigError):
        DatasetsConfig.load(_write_tmp(raw))


def test_invalid_commit_sha_rejected() -> None:
    with pytest.raises(ValueError, match="40-character hex SHA"):
        SroieSourceConfig(
            commit="not-a-sha",
            license_note="n/a",
        )


def test_invalid_revision_sha_rejected() -> None:
    with pytest.raises(ValueError, match="40-character hex SHA"):
        CordSourceConfig(
            revision="not-a-sha",
            selected_indices=sorted(range(20)),
            rows_api_template="https://example.invalid/{offset}",
        )


def test_sroie_source_config_default_split_is_trainval_not_test() -> None:
    """The mirror's data/ folder is trainval-style data, never the test split."""
    source = SroieSourceConfig(
        commit="27be4271b251c256f695acbade9a801bffe85994",
        license_note="n/a",
    )
    assert source.split == "trainval"
    assert source.split != "test"
