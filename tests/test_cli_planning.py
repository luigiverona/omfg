from __future__ import annotations

import argparse
import unittest

from omfg.catalog import load_catalog
from omfg.cli import parser, selection_from_args
from omfg.models import Capability, Source
from omfg.planning import build_plan


class CliPlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def parse(self, *args: str) -> argparse.Namespace:
        return parser().parse_args(args)

    def selection(self, *args: str):
        return selection_from_args(
            self.parse(*args), self.catalog.app_categories, self.catalog.dep_categories
        )

    def test_no_flags_select_complete_workflow(self) -> None:
        selected = self.selection()
        self.assertTrue(selected.complete)
        self.assertEqual(selected.capabilities, frozenset(Capability))

    def test_restricted_flag(self) -> None:
        plan = build_plan(self.selection("--github"), self.catalog)
        self.assertEqual(plan.selected, (Capability.GITHUB,))
        self.assertIn(Capability.DEPS, plan.prerequisites)
        self.assertNotIn(Capability.CODEX, plan.prerequisites)
        self.assertEqual(
            {package.identifier for package in plan.packages},
            {"git", "github-cli", "openssh"},
        )

    def test_codex_includes_only_runtime_and_official_artifact(self) -> None:
        plan = build_plan(self.selection("--codex"), self.catalog)
        self.assertEqual(
            {package.identifier for package in plan.packages},
            {"codex", "curl"},
        )

    def test_repeatable_categories(self) -> None:
        selection = self.selection("--app", "browser", "--app", "media", "--dep", "aur")
        self.assertEqual(selection.app_categories, frozenset({"browser", "media"}))
        self.assertEqual(selection.dep_categories, frozenset({"aur"}))

    def test_unknown_category(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown application category"):
            self.selection("--app", "soundcloud")

    def test_deterministic_deduplicated_plan(self) -> None:
        first = build_plan(self.selection(), self.catalog)
        second = build_plan(self.selection(), self.catalog)
        self.assertEqual(first, second)
        identities = [(p.source, p.identifier) for p in first.packages]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(identities, sorted(identities, key=lambda p: (p[0].value, p[1])))

    def test_browser_only_has_no_unrelated_app(self) -> None:
        plan = build_plan(self.selection("--app", "browser"), self.catalog)
        app_ids = {p.identifier for p in plan.packages if p.source is Source.AUR}
        self.assertIn("librewolf-bin", app_ids)
        self.assertNotIn("mullvad-vpn-bin", app_ids)

    def test_vpn_uses_one_official_requirement(self) -> None:
        plan = build_plan(self.selection("--app", "vpn"), self.catalog)
        applications = [package for package in plan.packages if package.category == "vpn"]
        self.assertEqual(
            [(package.source, package.identifier) for package in applications],
            [(Source.PACMAN, "mullvad-vpn")],
        )
        self.assertNotIn("mullvad-vpn-daemon", {package.identifier for package in plan.packages})

    def test_game_resolves_flatpak_and_flathub(self) -> None:
        plan = build_plan(self.selection("--app", "game"), self.catalog)
        self.assertIn(Capability.FLATPAK, plan.prerequisites)
        self.assertIn(Capability.FLATHUB, plan.prerequisites)
        self.assertIn("org.vinegarhq.Sober", {package.identifier for package in plan.packages})

    def test_development_app_resolves_codex_flow(self) -> None:
        plan = build_plan(self.selection("--app", "development"), self.catalog)
        self.assertIn(Capability.CODEX, plan.prerequisites)

    def test_check_has_complete_verification_inventory(self) -> None:
        plan = build_plan(self.selection("--check"), self.catalog)
        self.assertEqual(len(plan.packages), 15)
