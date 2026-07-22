from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Source(StrEnum):
    PACMAN = "pacman"
    AUR = "aur"
    FLATPAK = "flatpak"
    UPSTREAM = "upstream"


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
