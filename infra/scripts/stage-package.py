from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
STAGE_ROOT = REPOSITORY_ROOT / "infra" / "package"

REQUIRED_FILES = (
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "requirements.lock",
)
REQUIRED_DIRECTORIES = (
    "configs",
    "frontend",
    "schemas",
    "src",
    "data/manifests",
)
OPTIONAL_DIRECTORIES = ("prompts",)
REQUIRED_BUILD_SCRIPT = Path("infra/scripts/oryx-pre-build.sh")
STARTUP_MODULE = Path("src/receipt_bench/web/app.py")
STARTUP_FACTORY = "create_app"
PYTHON_RUNTIME_VERSION = "3.12"
REQUIRED_HOSTED_DEPENDENCIES = frozenset(
    {
        "aiohttp",
        "azure-data-tables",
        "azure-identity",
        "azure-keyvault-secrets",
        "azure-storage-blob",
        "azure-storage-queue",
        "python-multipart",
    }
)

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".receipt-bench",
        ".ruff_cache",
        ".secrets",
        ".venv",
        "__tests__",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "raw",
        "reports",
        "restricted",
        "results",
        "secrets",
        "test",
        "tests",
    }
)
EXCLUDED_FILE_NAMES = frozenset(
    {
        ".coverage",
        "benchmark.jsonl",
        "credentials.json",
        "local.settings.json",
        "secrets.json",
    }
)
EXCLUDED_SECRET_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
EXCLUDED_GENERATED_PREFIXES = (
    Path("frontend/dist"),
    Path("src/receipt_bench/web/static"),
)
EXCLUDED_RESTRICTED_IMAGE_PARTS = frozenset({"raw", "restricted"})
IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})


def _validate_required_layout(stage_root: Path) -> None:
    required_files = [
        *(Path(path) for path in REQUIRED_FILES),
        REQUIRED_BUILD_SCRIPT,
        Path("frontend/package.json"),
        Path("frontend/package-lock.json"),
        Path("src/receipt_bench/__init__.py"),
        Path("src/receipt_bench/demo/static/index.html"),
        Path("src/receipt_bench/web/__init__.py"),
    ]
    missing = [
        path.as_posix() for path in required_files if not (stage_root / path).is_file()
    ]
    missing.extend(
        path for path in REQUIRED_DIRECTORIES if not (stage_root / path).is_dir()
    )
    if missing:
        raise RuntimeError(
            f"Deployment package is incomplete; missing: {', '.join(sorted(missing))}"
        )


def _canonicalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _locked_packages(lock_path: Path) -> set[str]:
    packages: set[str] = set()
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", ".")):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match is not None:
            packages.add(_canonicalize_package_name(match.group(1)))
    return packages


def _is_relative_to(path: Path, prefix: Path) -> bool:
    return path == prefix or prefix in path.parents


def _is_excluded(relative_path: Path, *, is_directory: bool) -> bool:
    lowered_parts = tuple(part.casefold() for part in relative_path.parts)
    name = relative_path.name.casefold()
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in lowered_parts):
        return True
    if any(_is_relative_to(relative_path, prefix) for prefix in EXCLUDED_GENERATED_PREFIXES):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if not is_directory and (
        name in EXCLUDED_FILE_NAMES
        or relative_path.suffix.casefold() in EXCLUDED_SECRET_SUFFIXES
        or ".test." in name
        or ".spec." in name
        or name.startswith("test_")
        or name.endswith("_test.py")
    ):
        return True
    return (
        not is_directory
        and relative_path.suffix.casefold() in IMAGE_SUFFIXES
        and any(part in EXCLUDED_RESTRICTED_IMAGE_PARTS for part in lowered_parts[:-1])
    )


def _copy_directory(repository_root: Path, stage_root: Path, relative_path: str) -> None:
    source = repository_root / relative_path
    if not source.is_dir():
        raise RuntimeError(f"Required package directory is missing: {relative_path}")
    if source.is_symlink():
        raise RuntimeError(f"Refusing to package symlink: {relative_path}")

    destination = stage_root / relative_path
    destination.mkdir(parents=True, exist_ok=True)
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        current_path = Path(current)
        current_relative = current_path.relative_to(repository_root)
        directory_names.sort()
        file_names.sort()

        retained_directories: list[str] = []
        for directory_name in directory_names:
            candidate = current_path / directory_name
            relative_candidate = candidate.relative_to(repository_root)
            if _is_excluded(relative_candidate, is_directory=True):
                continue
            if candidate.is_symlink():
                raise RuntimeError(f"Refusing to package symlink: {relative_candidate}")
            retained_directories.append(directory_name)
        directory_names[:] = retained_directories

        relative_destination = stage_root / current_relative
        relative_destination.mkdir(parents=True, exist_ok=True)
        for file_name in file_names:
            candidate = current_path / file_name
            relative_candidate = candidate.relative_to(repository_root)
            if _is_excluded(relative_candidate, is_directory=False):
                continue
            if candidate.is_symlink():
                raise RuntimeError(f"Refusing to package symlink: {relative_candidate}")
            shutil.copy2(candidate, relative_destination / file_name)


def _validate_hosted_dependencies(stage_root: Path) -> None:
    lock_path = stage_root / "requirements.lock"
    installed = _locked_packages(lock_path)
    missing = sorted(REQUIRED_HOSTED_DEPENDENCIES - installed)
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(
            "Authoritative requirements.lock is missing hosted Azure SDK dependencies: "
            f"{names}. The parent/API track must declare them in pyproject.toml and "
            "regenerate requirements.lock; do not hand-edit the lock."
        )


def _validate_python_runtime(stage_root: Path) -> None:
    pyproject_path = stage_root / "pyproject.toml"
    with pyproject_path.open("rb") as stream:
        pyproject = tomllib.load(stream)
    requires_python = pyproject.get("project", {}).get("requires-python")
    if not isinstance(requires_python, str) or re.search(
        rf"(?:^|,)\s*>=\s*{re.escape(PYTHON_RUNTIME_VERSION)}(?:\D|$)",
        requires_python,
    ) is None:
        raise RuntimeError(
            "Staged pyproject.toml must declare Python >=3.12 to match the "
            "App Service Python 3.12 runtime."
        )


def _validate_startup_target(stage_root: Path) -> None:
    module_path = stage_root / STARTUP_MODULE
    if not module_path.is_file():
        raise RuntimeError(
            "Staged App Service startup module is missing: "
            f"{STARTUP_MODULE.as_posix()}"
        )
    try:
        module = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except SyntaxError as exc:
        raise RuntimeError(
            f"Staged App Service startup module is not valid Python: {exc}"
        ) from exc
    factories = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == STARTUP_FACTORY
    ]
    if not factories:
        raise RuntimeError(
            "Staged App Service startup target is missing: "
            "receipt_bench.web.app:create_app. The parent/API track must provide "
            "a top-level synchronous create_app factory before packaging."
        )
    factory = factories[0]
    positional = [*factory.args.posonlyargs, *factory.args.args]
    required_positional = len(positional) - len(factory.args.defaults)
    required_keyword_only = sum(
        default is None for default in factory.args.kw_defaults
    )
    if required_positional or required_keyword_only:
        raise RuntimeError(
            "Staged App Service startup factory receipt_bench.web.app:create_app "
            "must be callable without arguments for uvicorn --factory."
        )


def _validate_no_forbidden_content(stage_root: Path) -> None:
    violations: list[str] = []
    for candidate in stage_root.rglob("*"):
        relative_candidate = candidate.relative_to(stage_root)
        if candidate.is_symlink() or _is_excluded(
            relative_candidate,
            is_directory=candidate.is_dir(),
        ):
            violations.append(relative_candidate.as_posix())
    if violations:
        raise RuntimeError(
            "Deployment package contains forbidden generated, test, cache, "
            f"credential, or restricted-image paths: {', '.join(sorted(violations))}"
        )


def validate_staged_package(stage_root: Path) -> None:
    _validate_required_layout(stage_root)
    _validate_python_runtime(stage_root)
    _validate_hosted_dependencies(stage_root)
    _validate_startup_target(stage_root)
    _validate_no_forbidden_content(stage_root)


def _populate_stage(repository_root: Path, stage_root: Path) -> None:
    stage_root.mkdir(parents=True)
    (stage_root / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")

    for relative_path in REQUIRED_FILES:
        source = repository_root / relative_path
        if not source.is_file():
            raise RuntimeError(f"Required package file is missing: {relative_path}")
        if source.is_symlink():
            raise RuntimeError(f"Refusing to package symlink: {relative_path}")
        destination = stage_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for relative_path in REQUIRED_DIRECTORIES:
        _copy_directory(repository_root, stage_root, relative_path)

    for relative_path in OPTIONAL_DIRECTORIES:
        if (repository_root / relative_path).is_dir():
            _copy_directory(repository_root, stage_root, relative_path)

    data_license = repository_root / "data" / "LICENSES.md"
    if data_license.is_file():
        if data_license.is_symlink():
            raise RuntimeError("Refusing to package symlink: data/LICENSES.md")
        destination = stage_root / "data" / data_license.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(data_license, destination)

    build_script_source = repository_root / REQUIRED_BUILD_SCRIPT
    if not build_script_source.is_file():
        raise RuntimeError(
            f"Required Oryx build script is missing: {REQUIRED_BUILD_SCRIPT.as_posix()}"
        )
    if build_script_source.is_symlink():
        raise RuntimeError(
            f"Refusing to package symlink: {REQUIRED_BUILD_SCRIPT.as_posix()}"
        )
    build_script_destination = stage_root / REQUIRED_BUILD_SCRIPT
    build_script_destination.parent.mkdir(parents=True, exist_ok=True)
    build_script_destination.write_text(
        build_script_source.read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )


def _remove_directory_contents(root: Path) -> None:
    for candidate in root.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            candidate.unlink()
        else:
            shutil.rmtree(candidate)


def _replace_stage_contents_in_place(
    work_root: Path,
    stage_root: Path,
    backup_root: Path,
) -> None:
    stage_root.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir()

    moved_originals: list[str] = []
    try:
        for candidate in sorted(stage_root.iterdir(), key=lambda path: path.name):
            candidate.replace(backup_root / candidate.name)
            moved_originals.append(candidate.name)
    except OSError:
        for name in reversed(moved_originals):
            (backup_root / name).replace(stage_root / name)
        backup_root.rmdir()
        raise

    try:
        for candidate in sorted(work_root.iterdir(), key=lambda path: path.name):
            candidate.replace(stage_root / candidate.name)
    except OSError as swap_error:
        _remove_directory_contents(stage_root)
        try:
            for candidate in sorted(backup_root.iterdir(), key=lambda path: path.name):
                candidate.replace(stage_root / candidate.name)
        except OSError as rollback_error:
            raise RuntimeError(
                "Deployment package swap and rollback both failed; "
                f"the previous package remains in {backup_root}"
            ) from rollback_error
        backup_root.rmdir()
        raise swap_error

    shutil.rmtree(backup_root)


def stage_package(
    repository_root: Path = REPOSITORY_ROOT,
    stage_root: Path = STAGE_ROOT,
    *,
    dry_run: bool = False,
    in_place: bool = False,
) -> int:
    work_root = stage_root.with_name(f".{stage_root.name}-staging")
    backup_root = stage_root.with_name(f".{stage_root.name}-backup")
    if stage_root.is_symlink() or work_root.is_symlink() or backup_root.is_symlink():
        raise RuntimeError("Refusing to replace a symlinked deployment package directory")
    if stage_root.exists() and not stage_root.is_dir():
        raise RuntimeError("Deployment package path exists but is not a directory")
    if work_root.exists():
        shutil.rmtree(work_root)
    if backup_root.exists():
        if not backup_root.is_dir():
            raise RuntimeError("Deployment package backup exists but is not a directory")
        if stage_root.exists():
            shutil.rmtree(backup_root)
        else:
            backup_root.replace(stage_root)

    try:
        _populate_stage(repository_root, work_root)
        validate_staged_package(work_root)
        file_count = sum(1 for candidate in work_root.rglob("*") if candidate.is_file())
        if dry_run:
            print(
                f"Validated {file_count} deployment files in a clean temporary stage; "
                "infra/package was not changed."
            )
            return file_count

        if in_place:
            _replace_stage_contents_in_place(work_root, stage_root, backup_root)
        else:
            if stage_root.exists():
                stage_root.replace(backup_root)
            try:
                work_root.replace(stage_root)
            except OSError:
                if backup_root.exists() and not stage_root.exists():
                    backup_root.replace(stage_root)
                raise
        if backup_root.exists():
            shutil.rmtree(backup_root)
        print(
            f"Staged and validated {file_count} deployment files "
            "for the App Service Oryx Python build."
        )
        return file_count
    finally:
        if work_root.exists():
            shutil.rmtree(work_root)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create and statically validate a clean App Service source package."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate a temporary stage without replacing infra/package.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the current infra/package without copying source files.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help=(
            "Replace package contents without renaming infra/package. "
            "Use from an azd service hook whose working directory is infra/package."
        ),
    )
    arguments = parser.parse_args()
    if sum((arguments.dry_run, arguments.validate_only, arguments.in_place)) > 1:
        parser.error("--dry-run, --validate-only, and --in-place are mutually exclusive")
    return arguments


def main() -> None:
    arguments = _parse_args()
    try:
        if arguments.validate_only:
            validate_staged_package(STAGE_ROOT)
            file_count = sum(1 for candidate in STAGE_ROOT.rglob("*") if candidate.is_file())
            print(f"Validated {file_count} files in infra/package.")
            return
        stage_package(dry_run=arguments.dry_run, in_place=arguments.in_place)
    except (OSError, RuntimeError, tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise SystemExit(f"stage-package: error: {exc}") from None


if __name__ == "__main__":
    main()
