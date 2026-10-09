#!/usr/bin/env python3
"""Exercise commit reconciliation and private recovery without cloud writes."""

# unittest keeps recovery checks dependency-free.
# pylint: disable=protected-access
# ruff: noqa: PT009, PT027, SLF001
import copy
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deployment


def source_archive() -> bytes:
    """Build a minimal immutable GitHub configuration archive."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in {
            "repo/terraform/versions.tf": b"terraform {}",
            "repo/terraform/.terraform.lock.hcl": b"locked",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def record_call(calls: list, args: list) -> int:
    """Record an operation and emulate a plan with changes."""
    calls.append(args)
    return 2 if args[0] == "plan" else 0


class DeploymentTests(unittest.TestCase):
    """Verify stale commits, private recovery and competing writers."""

    def test_superseded_plan_never_applies(self) -> None:
        """Verify superseded plan never applies."""
        calls = []
        stores = []
        lease = mock.Mock()
        with (
            mock.patch.object(
                deployment,
                "latest",
                side_effect=[
                    "a" * 40,
                    "b" * 40,
                    "b" * 40,
                    "b" * 40,
                    "b" * 40,
                    "b" * 40,
                ],
            ),
            mock.patch.object(deployment, "github", return_value=source_archive()),
            mock.patch.object(deployment, "initialize"),
            mock.patch.object(
                deployment,
                "terraform",
                side_effect=lambda _directory, args, **_kwargs: record_call(
                    calls, args
                ),
            ),
            mock.patch.object(
                deployment,
                "store",
                side_effect=lambda _name, _archive, receipt: stores.append(
                    copy.deepcopy(receipt)
                ),
            ),
        ):
            deployment.reconcile("feature/demo", lease)
        self.assertEqual(sum(args[0] == "plan" for args in calls), 2)
        self.assertEqual(sum(args[0] == "apply" for args in calls), 1)
        self.assertTrue(all(receipt["commit"] == "b" * 40 for receipt in stores))
        lease.check.assert_called_once()

    def test_new_commit_after_apply_reconciles(self) -> None:
        """Verify new commit after apply reconciles."""
        commits = ["a" * 40] * 3 + ["b" * 40] * 5
        calls = []
        with (
            mock.patch.object(deployment, "latest", side_effect=commits),
            mock.patch.object(deployment, "github", return_value=source_archive()),
            mock.patch.object(deployment, "initialize"),
            mock.patch.object(
                deployment,
                "terraform",
                side_effect=lambda _directory, args, **_kwargs: record_call(
                    calls, args
                ),
            ),
            mock.patch.object(deployment, "store"),
        ):
            deployment.reconcile("feature/demo", mock.Mock())
        self.assertEqual(sum(args[0] == "apply" for args in calls), 2)

    def test_main_and_recreated_branch_cleanup_are_protected(self) -> None:
        """Verify main and recreated branch cleanup are protected."""
        with self.assertRaises(RuntimeError):
            deployment.cleanup("main", mock.Mock())
        with (
            mock.patch.object(deployment, "latest", return_value="a" * 40),
            mock.patch.object(deployment, "get_secret") as secret,
        ):
            deployment.cleanup("feature/demo", mock.Mock())
        secret.assert_not_called()

    def test_archive_round_trip_preserves_exact_configuration(self) -> None:
        """Verify archive round trip preserves exact configuration."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            deployment.extract_configuration(source_archive(), source)
            (source / "environment.auto.tfvars.json").write_text(
                json.dumps({"environment_id": "main"})
            )
            (source / "ignored.plan").write_bytes(b"private plan")
            archive = deployment.recovery_archive(source)
            target = root / "restored"
            target.mkdir()
            deployment.restore(archive, target)
            self.assertEqual(
                (source / ".terraform.lock.hcl").read_bytes(),
                (target / ".terraform.lock.hcl").read_bytes(),
            )
            self.assertFalse((target / "ignored.plan").exists())

    def test_competing_transaction_holder_cannot_acquire(self) -> None:
        """Verify competing transaction holder cannot acquire."""
        lease = deployment.EnvironmentLease("main")
        current = {
            "spec": {
                "holderIdentity": "other-writer",
                "renewTime": deployment.datetime.now(deployment.UTC).isoformat(),
                "leaseDurationSeconds": 120,
            }
        }
        with mock.patch.object(deployment, "kubectl", return_value=current) as command:
            self.assertFalse(lease._write(acquire=True))
        self.assertEqual(command.call_count, 1)


if __name__ == "__main__":
    unittest.main()
