from __future__ import annotations

import argparse
from pathlib import Path

from omfg import __version__
from omfg.catalog import load_catalog
from omfg.errors import OmfgError
from omfg.models import Capability, RunOptions, Selection
from omfg.planning import build_plan
from omfg.ui import Terminal
from omfg.workflow import Workflow

WORKFLOW_FLAGS = tuple(c for c in Capability if c is not Capability.SHELL)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="omfg", description="Set up an Arch Linux workstation")
    for capability in WORKFLOW_FLAGS:
        if capability in {Capability.APPS, Capability.DEPS}:
            result.add_argument(
                f"--{capability.value}", action="store_true", help=f"select all {capability.value}"
            )
        else:
            result.add_argument(
                f"--{capability.value}", action="store_true", help=f"select {capability.value}"
            )
    result.add_argument(
        "--app",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="select an application category (repeatable)",
    )
    result.add_argument(
        "--dep",
        action="append",
        default=[],
        metavar="CATEGORY",
        help="select a dependency category (repeatable)",
    )
    result.add_argument("--dry-run", action="store_true", help="plan without mutations")
    result.add_argument(
        "--yes", action="store_true", help="approve normal non-destructive confirmations"
    )
    result.add_argument("--verbose", action="store_true", help="show detailed operations")
    result.add_argument("--keep-temp", action="store_true", help="preserve temporary workspace")
    result.add_argument("--version", action="version", version=f"Omfg {__version__}")
    return result


def selection_from_args(
    args: argparse.Namespace, app_categories: frozenset[str], dep_categories: frozenset[str]
) -> Selection:
    unknown_apps = sorted(set(args.app) - app_categories)
    unknown_deps = sorted(set(args.dep) - dep_categories)
    if unknown_apps:
        raise ValueError(
            f"unknown application category: {', '.join(unknown_apps)}; choose from {', '.join(sorted(app_categories))}"
        )
    if unknown_deps:
        raise ValueError(
            f"unknown dependency category: {', '.join(unknown_deps)}; choose from {', '.join(sorted(dep_categories))}"
        )
    selected = {c for c in WORKFLOW_FLAGS if getattr(args, c.value)}
    if args.app:
        selected.add(Capability.APPS)
    if args.dep:
        selected.add(Capability.DEPS)
    complete = not selected
    if complete:
        selected = set(Capability)
    return Selection(frozenset(selected), frozenset(args.app), frozenset(args.dep), complete)


def main(argv: list[str] | None = None) -> int:
    terminal = Terminal()
    try:
        args = parser().parse_args(argv)
        catalog = load_catalog()
        selection = selection_from_args(args, catalog.app_categories, catalog.dep_categories)
        plan = build_plan(selection, catalog)
        if not args.dry_run:
            terminal.output(f"Omfg {__version__}")
        options = RunOptions(args.dry_run, args.yes, args.verbose, args.keep_temp, Path.home())
        return Workflow(plan, options, terminal).run()
    except ValueError as exc:
        parser().error(str(exc))
    except OmfgError as exc:
        terminal.error(exc.component, exc.operation, exc.reason, exc.log_path, exc.packages)
        return exc.exit_code
    except KeyboardInterrupt:
        terminal.output("Interrupted; no further changes will be made.")
        return 130
    except Exception as exc:
        terminal.error("setup", "complete workstation setup", str(exc))
        return 1
    return 2
