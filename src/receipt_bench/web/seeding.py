from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx

from receipt_bench.projects.models import ArtifactReference, Project
from receipt_bench.projects.registry import ProjectRegistry
from receipt_bench.storage.paths import (
    project_prompt,
    project_record,
    project_schema,
    sample_gold,
    sample_image,
)
from receipt_bench.storage.validation import (
    JsonObject,
    content_sha256,
    require_json_object,
    strict_json_bytes,
    strict_json_loads,
    validate_id,
)
from receipt_bench.web.repositories import RepositoryBundle
from receipt_bench.web.runtime import _ocr_artifact_path
from receipt_bench.web.security import validate_image
from receipt_bench.web.service import validate_gold
from receipt_bench.web.settings import WebSettings

_SEED_LEASE_SECONDS = 60
_SEED_RENEW_SECONDS = 20
_SEED_CONCURRENCY = 8


@dataclass(frozen=True)
class _SeedSample:
    image: bytes
    gold: JsonObject
    ocr_text: str


class BuiltInProjectSeeder:
    """Seed immutable built-in artifacts under a durable cross-instance lease."""

    def __init__(
        self,
        *,
        repositories: RepositoryBundle,
        registry: ProjectRegistry,
        settings: WebSettings,
        source_loader: Callable[[Project], dict[str, _SeedSample]] | None = None,
    ) -> None:
        self._repositories = repositories
        self._registry = registry
        self._settings = settings
        self._source_loader = source_loader or self._load_sources

    async def __call__(self) -> None:
        if not self._settings.seed_builtin_projects:
            return
        project_ids = [
            project_id
            for project_id, enabled in (
                ("cord-v2", self._settings.seed_cord),
                ("sroie", self._settings.seed_sroie),
            )
            if enabled
        ]
        for project_id in project_ids:
            await self._seed_project(self._registry.get(project_id))

    async def _seed_project(self, project: Project) -> None:
        seed_id = _seed_id(project)
        coordinator = self._repositories.seeds
        if coordinator is None:
            raise RuntimeError(
                "Built-in project seeding requires a durable seed coordinator"
            )
        owner = validate_id(f"web-seed-{secrets.token_hex(8)}", field="seed_owner")
        while True:
            if await coordinator.is_complete(seed_id):
                return
            lease = await coordinator.acquire(
                seed_id,
                owner=owner,
                lease_seconds=_SEED_LEASE_SECONDS,
            )
            if lease is not None:
                break
            await asyncio.sleep(1)

        current_lease = [lease]
        stop_renewal = asyncio.Event()

        async def renew() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        stop_renewal.wait(),
                        timeout=_SEED_RENEW_SECONDS,
                    )
                    return
                except TimeoutError:
                    current_lease[0] = await coordinator.renew(
                        current_lease[0],
                        lease_seconds=current_lease[0].lease_seconds,
                    )

        renewal = asyncio.create_task(renew())
        try:
            sources = await asyncio.to_thread(self._source_loader, project)
            await self._persist_project(project, sources)
            stop_renewal.set()
            await renewal
            await coordinator.complete(
                current_lease[0],
                {
                    "project_id": project.project_id,
                    "source_revision": project.source.revision,
                    "schema_hash": project.project_schema.content_hash,
                    "sample_count": len(project.samples),
                },
            )
        except BaseException:
            stop_renewal.set()
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            with suppress(Exception):
                await coordinator.release(current_lease[0])
            raise

    async def _persist_project(
        self,
        project: Project,
        sources: dict[str, _SeedSample],
    ) -> None:
        await self._repositories.artifacts.create_json(
            project_schema(project.project_id),
            project.project_schema.document,
        )
        await self._repositories.artifacts.create_json(
            project_prompt(project.project_id, project.prompts[0].version),
            {
                "project_id": project.project_id,
                "prompts": [
                    prompt.model_dump(mode="json") for prompt in project.prompts
                ],
            },
        )
        await self._repositories.artifacts.create_json(
            project_record(project.project_id),
            {
                "project_id": project.project_id,
                "source": project.source.model_dump(mode="json"),
                "schema_hash": project.project_schema.content_hash,
                "sample_ids": [sample.sample_id for sample in project.samples],
            },
        )
        semaphore = asyncio.Semaphore(_SEED_CONCURRENCY)

        async def persist_sample(sample_id: str) -> None:
            sample = project.sample(sample_id)
            source = sources.get(sample.sample_id)
            if source is None:
                raise RuntimeError(
                    f"Built-in seed source is missing sample {sample.sample_id!r}"
                )
            validate_gold(project.project_schema.document, source.gold)
            if len(source.image) > self._settings.upload_max_bytes:
                raise RuntimeError("Built-in seed image exceeds the configured size limit")
            media_type = validate_image(
                source.image,
                self._settings,
                sample.image.media_type,
            )
            gold_bytes = strict_json_bytes(source.gold)
            if len(gold_bytes) > self._settings.gold_max_bytes:
                raise RuntimeError("Built-in seed gold exceeds the configured size limit")
            ocr_bytes = source.ocr_text.encode("utf-8")
            if len(ocr_bytes) > self._settings.upload_max_bytes:
                raise RuntimeError("Built-in seed OCR text exceeds the configured size limit")
            async with semaphore:
                await self._repositories.artifacts.create_bytes(
                    sample_image(project.project_id, sample.sample_id),
                    source.image,
                    content_type=media_type,
                    metadata={"source_revision": project.source.revision},
                )
                await self._repositories.artifacts.create_json(
                    sample_gold(project.project_id, sample.sample_id),
                    source.gold,
                    metadata={"source_revision": project.source.revision},
                )
                await self._repositories.artifacts.create_bytes(
                    _ocr_artifact_path(project.project_id, sample.sample_id),
                    ocr_bytes,
                    content_type="text/plain; charset=utf-8",
                    metadata={"source_revision": project.source.revision},
                )

        await asyncio.gather(
            *(persist_sample(sample.sample_id) for sample in project.samples)
        )

    def _load_sources(self, project: Project) -> dict[str, _SeedSample]:
        local = _load_local_sources(project, self._settings.repository_root)
        if local is not None:
            return local
        if project.project_id == "cord-v2":
            return _download_cord(
                project,
                max_image_bytes=self._settings.upload_max_bytes,
            )
        if project.project_id == "sroie":
            return _download_sroie(
                project,
                max_image_bytes=self._settings.upload_max_bytes,
            )
        raise RuntimeError(f"No built-in seed source for {project.project_id!r}")


def _seed_id(project: Project) -> str:
    prompt_hashes = "\n".join(prompt.content_hash for prompt in project.prompts)
    sample_contract = "\n".join(
        "|".join(
            (
                sample.sample_id,
                sample.scoring_role,
                sample.image.sha256 or "",
                sample.gold.sha256 or "",
                sample.ocr_reference.sha256
                if sample.ocr_reference is not None
                and sample.ocr_reference.sha256 is not None
                else "",
            )
        )
        for sample in project.samples
    )
    digest = hashlib.sha256(
        (
            f"{project.project_id}\n{project.source.revision}\n"
            f"{project.project_schema.content_hash}\n{prompt_hashes}\n{sample_contract}"
        ).encode()
    ).hexdigest()[:20]
    return validate_id(f"builtin-{project.project_id}-{digest}", field="seed_id")


def _load_local_sources(
    project: Project,
    repository_root: Path,
) -> dict[str, _SeedSample] | None:
    paths = [
        repository_root / reference.path
        for sample in project.samples
        for reference in (sample.image, sample.gold, sample.ocr_reference)
        if reference is not None
    ]
    if not paths or not all(path.is_file() for path in paths):
        return None
    result: dict[str, _SeedSample] = {}
    for sample in project.samples:
        assert sample.ocr_reference is not None
        image = _read_verified_source(repository_root, sample.image)
        gold_bytes = _read_verified_source(repository_root, sample.gold)
        ocr_bytes = _read_verified_source(repository_root, sample.ocr_reference)
        result[sample.sample_id] = _SeedSample(
            image=image,
            gold=_gold_document(project, gold_bytes),
            ocr_text=_ocr_document(project.project_id, ocr_bytes),
        )
    return result


def _read_verified_source(root: Path, reference: ArtifactReference) -> bytes:
    data = (root / reference.path).read_bytes()
    if reference.sha256 is not None and content_sha256(data) != reference.sha256:
        raise RuntimeError(f"Built-in source checksum mismatch for {reference.path!r}")
    return data


def _gold_document(project: Project, data: bytes) -> JsonObject:
    from receipt_bench.projects.normalization import normalize_answer

    document = require_json_object(strict_json_loads(data), field="built-in gold")
    if project.project_id == "cord-v2":
        answer = require_json_object(document.get("gt_parse"), field="CORD gt_parse")
    elif project.project_id == "sroie":
        required = ("company", "address", "date", "total")
        missing = [key for key in required if key not in document]
        if missing:
            raise RuntimeError(f"SROIE built-in gold is missing {missing}")
        answer = require_json_object(
            {key: document[key] for key in required},
            field="SROIE gold",
        )
    else:
        answer = document
    return normalize_answer(project.normalization_profile, answer)


def _ocr_document(project_id: str, data: bytes) -> str:
    if project_id == "cord-v2":
        document = require_json_object(strict_json_loads(data), field="CORD OCR source")
        lines = document.get("valid_line")
        if not isinstance(lines, list):
            raise RuntimeError("CORD OCR source has no valid_line array")
        rendered: list[str] = []
        for line in lines:
            if not isinstance(line, dict):
                raise RuntimeError("CORD OCR source has a malformed line")
            raw_words = line.get("words")
            if not isinstance(raw_words, list):
                raise RuntimeError("CORD OCR source has a malformed words array")
            words: list[str] = []
            for word in raw_words:
                if not isinstance(word, dict):
                    continue
                text = word.get("text")
                if isinstance(text, str) and text.strip():
                    words.append(text.strip())
            if words:
                rendered.append(" ".join(words))
        if not rendered:
            raise RuntimeError("CORD OCR source is empty")
        return "\n".join(rendered)
    rows = csv.reader(io.StringIO(data.decode("utf-8-sig")))
    rendered = []
    for row in rows:
        if len(row) < 9:
            raise RuntimeError("SROIE OCR source has a malformed row")
        text = ",".join(row[8:]).strip()
        if text:
            rendered.append(text)
    if not rendered:
        raise RuntimeError("SROIE OCR source is empty")
    return "\n".join(rendered)


def _download_cord(
    project: Project,
    *,
    max_image_bytes: int,
) -> dict[str, _SeedSample]:
    from receipt_bench.datasets.config import DatasetsConfig
    from receipt_bench.datasets.cord import (
        extract_ground_truth_text,
        extract_image_url,
        fetch_cord_row,
        verify_cord_revision,
    )
    from receipt_bench.datasets.http import DEFAULT_TIMEOUT, USER_AGENT

    source = DatasetsConfig.load_builtin().cord
    if source.revision != project.source.revision:
        raise RuntimeError("CORD seed configuration does not match the project revision")
    result: dict[str, _SeedSample] = {}
    with httpx.Client(follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        verify_cord_revision(client, source)

        def load_sample(sample_id: str) -> tuple[str, _SeedSample]:
            sample = project.sample(sample_id)
            assert sample.source_index is not None
            row = fetch_cord_row(client, source, sample.source_index)
            image = _download(
                client,
                extract_image_url(row, index=sample.source_index),
                timeout=DEFAULT_TIMEOUT,
                max_bytes=max_image_bytes,
            )
            gold_bytes = extract_ground_truth_text(
                row,
                index=sample.source_index,
            ).encode("utf-8")
            _verify_optional_hash(sample.image, image)
            _verify_optional_hash(sample.gold, gold_bytes)
            assert sample.ocr_reference is not None
            _verify_optional_hash(sample.ocr_reference, gold_bytes)
            return (
                sample.sample_id,
                _SeedSample(
                    image=image,
                    gold=_gold_document(project, gold_bytes),
                    ocr_text=_ocr_document(project.project_id, gold_bytes),
                ),
            )
        with ThreadPoolExecutor(max_workers=_SEED_CONCURRENCY) as executor:
            for sample_id, seeded in executor.map(
                load_sample,
                (sample.sample_id for sample in project.samples),
            ):
                result[sample_id] = seeded
    return result


def _download_sroie(
    project: Project,
    *,
    max_image_bytes: int,
) -> dict[str, _SeedSample]:
    from receipt_bench.datasets.config import DatasetsConfig
    from receipt_bench.datasets.http import DEFAULT_TIMEOUT, USER_AGENT
    from receipt_bench.datasets.sroie import sroie_urls

    source = DatasetsConfig.load_builtin().sroie
    if source.commit != project.source.revision:
        raise RuntimeError("SROIE seed configuration does not match the project revision")
    result: dict[str, _SeedSample] = {}
    with httpx.Client(follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:

        def load_sample(sample_id: str) -> tuple[str, _SeedSample]:
            sample = project.sample(sample_id)
            urls = sroie_urls(source, sample.source_id)
            image = _download(
                client,
                urls["img"],
                timeout=DEFAULT_TIMEOUT,
                max_bytes=max_image_bytes,
            )
            gold_bytes = _download(
                client,
                urls["key"],
                timeout=DEFAULT_TIMEOUT,
                max_bytes=1024 * 1024,
            )
            ocr_bytes = _download(
                client,
                urls["box"],
                timeout=DEFAULT_TIMEOUT,
                max_bytes=1024 * 1024,
            )
            _verify_optional_hash(sample.image, image)
            _verify_optional_hash(sample.gold, gold_bytes)
            assert sample.ocr_reference is not None
            _verify_optional_hash(sample.ocr_reference, ocr_bytes)
            return (
                sample.sample_id,
                _SeedSample(
                    image=image,
                    gold=_gold_document(project, gold_bytes),
                    ocr_text=_ocr_document(project.project_id, ocr_bytes),
                ),
            )
        with ThreadPoolExecutor(max_workers=_SEED_CONCURRENCY) as executor:
            for sample_id, seeded in executor.map(
                load_sample,
                (sample.sample_id for sample in project.samples),
            ):
                result[sample_id] = seeded
    return result


def _download(
    client: httpx.Client,
    url: str,
    *,
    timeout: httpx.Timeout,
    max_bytes: int,
) -> bytes:
    with client.stream("GET", url, timeout=timeout) as response:
        if response.status_code != 200:
            raise RuntimeError("Built-in source returned a non-success status")
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > max_bytes:
                    raise RuntimeError("Built-in source exceeds the configured size limit")
            except ValueError as exc:
                raise RuntimeError("Built-in source has an invalid content length") from exc
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > max_bytes:
                raise RuntimeError("Built-in source exceeds the configured size limit")
        return bytes(chunks)


def _verify_optional_hash(reference: ArtifactReference, data: bytes) -> None:
    if reference.sha256 is not None and content_sha256(data) != reference.sha256:
        raise RuntimeError(f"Built-in source checksum mismatch for {reference.path!r}")


__all__ = ["BuiltInProjectSeeder"]
