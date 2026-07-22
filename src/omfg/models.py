from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Source(StrEnum):
    PACMAN = "pacman"
    AUR = "aur"
    FLATPAK = "flatpak"
    UPSTREAM = "upstream"


class PackageKind(StrEnum):
    APPLICATION = "application"
    DEPENDENCY = "dependency"


class Capability(StrEnum):
    SYSTEM = "system"
    DEPS = "deps"
    APPS = "apps"
    FLATPAK = "flatpak"
    FLATHUB = "flathub"
    GIT = "git"
    GITHUB = "github"
    SSH = "ssh"
    CODEX = "codex"
    SHELL = "shell"
    CHECK = "check"


@dataclass(frozen=True, slots=True, order=True)
class Package:
    source: Source
    identifier: str
    name: str
    category: str
    executable: str | None = None
    kind: PackageKind = PackageKind.APPLICATION


@dataclass(frozen=True, slots=True)
class Catalog:
    apps: tuple[Package, ...]
    deps: tuple[Package, ...]
    app_categories: frozenset[str]
    dep_categories: frozenset[str]


@dataclass(frozen=True, slots=True)
class Selection:
    capabilities: frozenset[Capability]
    app_categories: frozenset[str] = frozenset()
    dep_categories: frozenset[str] = frozenset()
    complete: bool = False


@dataclass(frozen=True, slots=True)
class Action:
    capability: Capability
    operation: str
    destructive_risk: bool = False


@dataclass(frozen=True, slots=True)
class Plan:
    selected: tuple[Capability, ...]
    prerequisites: tuple[Capability, ...]
    packages: tuple[Package, ...]
    actions: tuple[Action, ...]
    checks: tuple[str, ...]


@dataclass(slots=True)
class RunOptions:
    dry_run: bool = False
    assume_yes: bool = False
    verbose: bool = False
    keep_temp: bool = False
    home: Path = field(default_factory=Path.home)


@dataclass(slots=True)
class ExecutionSummary:
    requirements: int = 0
    pending_before: int = 0
    installed: int = 0
    components_configured: int = 0
    existing_keys_preserved: int = 0


class WorkflowStage(StrEnum):
    ADMINISTRATOR = "Administrator access"
    SYSTEM = "System update"
    APPLICATIONS = "Applications"
    FLATPAK = "Flatpak"
    GIT = "Git"
    GITHUB = "GitHub"
    SSH = "SSH"
    CODEX = "Codex"
    SHELL = "Shell PATH"
    VERIFICATION = "Verification"


STAGE_ORDER = tuple(WorkflowStage)


@dataclass(slots=True)
class WorkflowProgress:
    selected: tuple[WorkflowStage, ...]
    current: WorkflowStage | None = None
    completed: list[WorkflowStage] = field(default_factory=list)
    codex_profile: str | None = None
    verification_completed: bool = False
    mutation_started: bool = False

    @property
    def remaining(self) -> tuple[WorkflowStage, ...]:
        return tuple(stage for stage in self.selected if stage not in self.completed)

    def begin(self, stage: WorkflowStage) -> None:
        self.current = stage

    def finish(self, stage: WorkflowStage) -> None:
        if stage not in self.completed:
            self.completed.append(stage)
        if stage is WorkflowStage.VERIFICATION:
            self.verification_completed = True
        self.current = None
