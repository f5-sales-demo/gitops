#!/usr/bin/env python3
"""Naming isolation tests against branch inputs that normalize identically."""

import unittest

# unittest keeps these checks dependency-free on contributor workstations.
# ruff: noqa: PT009, PT027
from branch_environment import resolve


class BranchTests(unittest.TestCase):
    """Verify branch normalization and resource isolation."""

    def test_main_and_exclusions(self) -> None:
        """Check main and exclusions behavior."""
        self.assertEqual(resolve("main")["hostname"], "gitops.f5-sales-demo.com")
        for branch in [
            "sync/files",
            "renovate/python",
            "dependabot/npm/x",
            "release/v1.0.0",
            "content-20261009",
            "gh-pages",
        ]:
            self.assertFalse(resolve(branch)["deployable"])
        self.assertTrue(resolve("feature/demo")["deployable"])

    def test_normalization_collisions(self) -> None:
        """Check normalization collisions behavior."""
        branches = [
            "a/b",
            "a-b",
            "a_b",
            "A-B",
            "a.b",
            "démo",
            "demo",
            "部署",
            "🚀",
            "a" * 100 + "b",
            "a" * 100 + "c",
        ]
        names = [resolve(branch)["resource_name"] for branch in branches]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertLessEqual(len(name), 44)
            self.assertRegex(name, r"^gitops-[a-z0-9][a-z0-9-]*-[0-9a-f]{12}$")

    def test_determinism_and_invalid_input(self) -> None:
        """Check determinism and invalid input behavior."""
        self.assertEqual(resolve("feature/日本語"), resolve("feature/日本語"))
        for branch in ["", "x\ny", "x\x00y"]:
            with self.assertRaises(ValueError):
                resolve(branch)


if __name__ == "__main__":
    unittest.main()
