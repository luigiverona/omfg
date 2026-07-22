from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omfg.catalog import load_catalog
from omfg.errors import ValidationError
from tests.helpers import write_manifest

GOOD = '[[package]]\nname="Git"\nidentifier="git"\nsource="pacman"\n'


class CatalogTests(unittest.TestCase):
    def test_load_and_duplicate_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(root, "apps", "development", GOOD)
            write_manifest(root, "deps", "runtime", GOOD)
            catalog = load_catalog(root)
            self.assertEqual(len(catalog.apps), 1)
            self.assertEqual(len(catalog.deps), 1)

    def test_unsafe_identifier_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(
                root,
                "apps",
                "development",
                GOOD.replace('identifier="git"', 'identifier="git;bad"'),
            )
            write_manifest(root, "deps", "runtime", GOOD)
            with self.assertRaises(ValidationError):
                load_catalog(root)

    def test_unknown_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            write_manifest(root, "apps", "development", GOOD + 'surprise="x"\n')
            write_manifest(root, "deps", "runtime", GOOD)
            with self.assertRaises(ValidationError):
                load_catalog(root)
