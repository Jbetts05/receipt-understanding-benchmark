from __future__ import annotations

import pytest

from receipt_bench.projects.loading import load_builtin_project
from receipt_bench.projects.models import Project


@pytest.fixture(scope="session")
def sroie_project() -> Project:
    return load_builtin_project("sroie")


@pytest.fixture(scope="session")
def cord_project() -> Project:
    return load_builtin_project("cord-v2")
