from __future__ import annotations

import json
import random
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from receipt_bench.datasets.config import DatasetsConfig
from receipt_bench.datasets.prepare import prepare_dataset
from receipt_bench.evaluation.manifest import load_manifest

_SROIE_COMMIT = "27be4271b251c256f695acbade9a801bffe85994"
_CORD_REVISION = "7f0115a4b758a71d6473b8d085751692da2fef98"
_CORD_SEED = 20260828
_CORD_TOTAL_ROWS = 100


def _cord_indices(count: int) -> list[int]:
    return sorted(random.Random(_CORD_SEED).sample(range(_CORD_TOTAL_ROWS), count))


def _write_config(tmp_path: Path, *, sroie_count: int, cord_indices: list[int]) -> Path:
    config_path = tmp_path / "datasets.yaml"
    indices_yaml = ", ".join(str(i) for i in cord_indices)
    config_path.write_text(
        f"""
manifest_path: {(tmp_path / "manifests" / "benchmark.jsonl").as_posix()}
images_root: {(tmp_path / "images").as_posix()}
sroie:
  repo: zzzDavid/ICDAR-2019-SROIE
  commit: {_SROIE_COMMIT}
  count: {sroie_count}
  id_width: 3
  url_template: "https://raw.githubusercontent.com/{{repo}}/{{commit}}/data/{{kind}}/{{sample_id}}.{{ext}}"
  license_note: "test"
cord:
  dataset: naver-clova-ix/cord-v2
  revision: {_CORD_REVISION}
  split: test
  license: CC-BY-4.0
  total_rows: {_CORD_TOTAL_ROWS}
  sample_count: {len(cord_indices)}
  selection_seed: {_CORD_SEED}
  selected_indices: [{indices_yaml}]
  rows_api_template: "https://datasets-server.huggingface.co/rows?dataset=naver-clova-ix%2Fcord-v2&config=default&split=test&offset={{offset}}&length=1"
""",
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def mocked_environment(tmp_path: Path) -> Iterator[tuple[Path, list[int]]]:
    cord_indices = _cord_indices(2)
    config_path = _write_config(tmp_path, sroie_count=2, cord_indices=cord_indices)
    DatasetsConfig.load(config_path)  # sanity-check the config parses before mocking network calls

    with respx.mock(assert_all_called=False) as mock:
        for source_id in ("000", "001"):
            mock.get(
                f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{_SROIE_COMMIT}/data/img/{source_id}.jpg"
            ).mock(return_value=httpx.Response(200, content=f"sroie-image-{source_id}".encode()))
            mock.get(
                f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{_SROIE_COMMIT}/data/key/{source_id}.json"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "company": f"Store {source_id}",
                        "address": "1 Main St",
                        "date": "01/02/2020",
                        "total": "10.00",
                    },
                )
            )
            mock.get(
                f"https://raw.githubusercontent.com/zzzDavid/ICDAR-2019-SROIE/{_SROIE_COMMIT}/data/box/{source_id}.csv"
            ).mock(return_value=httpx.Response(200, content=b"box-bytes"))

        mock.get("https://huggingface.co/api/datasets/naver-clova-ix/cord-v2").mock(
            return_value=httpx.Response(200, json={"sha": _CORD_REVISION})
        )

        for index in cord_indices:
            row_url = (
                "https://datasets-server.huggingface.co/rows"
                f"?dataset=naver-clova-ix%2Fcord-v2&config=default&split=test&offset={index}&length=1"
            )
            image_url = f"https://img.example/signed/{index}.jpg"
            ground_truth = json.dumps(
                {
                    "gt_parse": {
                        "total": {"total_price": "15.000"},
                        "menu": {"nm": "Item", "price": "15.000"},
                    }
                }
            )
            mock.get(row_url).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "rows": [
                            {
                                "row_idx": index,
                                "row": {"image": {"src": image_url}, "ground_truth": ground_truth},
                            }
                        ]
                    },
                )
            )
            mock.get(image_url).mock(
                return_value=httpx.Response(200, content=f"cord-image-{index}".encode())
            )

        yield config_path, cord_indices


def test_prepare_dataset_end_to_end(mocked_environment: tuple[Path, list[int]]) -> None:
    config_path, cord_indices = mocked_environment

    manifest_path = prepare_dataset(config_path)

    assert manifest_path.name == "benchmark.jsonl"
    samples = load_manifest(manifest_path)

    expected_ids = sorted(
        ["sroie-000", "sroie-001"] + [f"cord-{index:03d}" for index in cord_indices]
    )
    assert [sample.sample_id for sample in samples] == expected_ids

    sroie_samples = [s for s in samples if s.dataset == "sroie"]
    cord_samples = [s for s in samples if s.dataset == "cord-v2"]
    assert len(sroie_samples) == 2
    assert len(cord_samples) == 2

    for sample in sroie_samples:
        assert sample.source_language == "en"
        assert sample.ground_truth.merchant.name is not None
        resolved = (manifest_path.parent / sample.image_path).resolve()
        assert resolved.is_file()

    for sample in cord_samples:
        assert sample.source_language == "id"
        assert sample.ground_truth.amounts.total == 15000.0
        resolved = (manifest_path.parent / sample.image_path).resolve()
        assert resolved.is_file()


def test_prepare_dataset_is_idempotent_on_second_run(
    mocked_environment: tuple[Path, list[int]],
) -> None:
    config_path, _ = mocked_environment

    manifest_path = prepare_dataset(config_path)
    first_samples = load_manifest(manifest_path)

    manifest_path_again = prepare_dataset(config_path)
    second_samples = load_manifest(manifest_path_again)

    assert manifest_path == manifest_path_again
    assert [s.sample_id for s in first_samples] == [s.sample_id for s in second_samples]
    assert [s.image_sha256 for s in first_samples] == [s.image_sha256 for s in second_samples]


def test_prepare_dataset_manifest_image_paths_are_portable(
    mocked_environment: tuple[Path, list[int]],
) -> None:
    """``image_path`` must serialize with ``/`` even though ``manifest_path``
    and ``images_root`` are siblings, which requires ``../`` traversal -- the
    exact shape that previously produced OS-native backslashes on Windows.
    """
    config_path, _ = mocked_environment

    manifest_path = prepare_dataset(config_path)

    raw_text = manifest_path.read_text(encoding="utf-8")
    assert "\\" not in raw_text

    for sample in load_manifest(manifest_path):
        assert "\\" not in sample.image_path
        assert sample.image_path.startswith("../images/")
