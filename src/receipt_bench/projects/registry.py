from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from receipt_bench.projects.loading import load_builtin_projects
from receipt_bench.projects.models import Project


class ProjectRegistry:
    """In-process project registry populated from validated project contracts."""

    def __init__(self, projects: Iterable[Project] = ()) -> None:
        self._projects: dict[str, Project] = {}
        for project in projects:
            self.register(project)

    def register(self, project: Project) -> None:
        if project.project_id in self._projects:
            raise ValueError(f"Project already registered: {project.project_id!r}")
        self._projects[project.project_id] = project

    def get(self, project_id: str) -> Project:
        try:
            return self._projects[project_id]
        except KeyError as exc:
            raise KeyError(f"Unknown project: {project_id!r}") from exc

    def list(self) -> tuple[Project, ...]:
        return tuple(self._projects[key] for key in sorted(self._projects))

    @classmethod
    def with_builtins(
        cls,
        *,
        repository_root: Path | None = None,
        definitions_root: Path | None = None,
    ) -> ProjectRegistry:
        return cls(
            load_builtin_projects(
                repository_root=repository_root,
                definitions_root=definitions_root,
            )
        )


__all__ = ["ProjectRegistry"]
