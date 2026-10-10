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
    return (
        2 if args[0] == "plan" and any(arg.startswith("-out=") for arg in args) else 0
    )


class TrafficTests(unittest.TestCase):
    """Require semantic forwarding proof and bounded retries."""

    def test_three_consecutive_checks_reset_after_failure(self) -> None:
        """A transient failure resets the success streak."""
        with (
            mock.patch.object(
                deployment,
                "traffic_check",
                side_effect=[None, ValueError(), None, None, None],
            ) as check,
            mock.patch.object(deployment.time, "sleep") as sleep,
        ):
            result = deployment.verify_traffic("gitops.f5-sales-demo.com", mock.Mock())
        self.assertEqual(check.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertIn("verified_at", result)

    def test_verification_expires_without_printing_response(self) -> None:
        """An unreachable endpoint cannot qualify a deployment."""
        with (
            mock.patch.object(
                deployment, "traffic_check", side_effect=ValueError("private response")
            ),
            mock.patch.object(
                deployment.time, "monotonic", side_effect=[0, 0, 601, 601]
            ),
            mock.patch.object(deployment.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "Traffic verification expired"),
        ):
            deployment.verify_traffic("gitops.f5-sales-demo.com", mock.Mock())

    def test_httpbin_requires_both_markers_and_origin(self) -> None:
        """Status 200 or a wrong origin cannot satisfy the gate."""
        with (
            mock.patch.object(deployment, "resolve_hostname"),
            mock.patch.object(
                deployment.uuid, "uuid4", return_value=mock.Mock(hex="marker")
            ),
            mock.patch.object(
                deployment,
                "request_json",
                side_effect=[
                    {
                        "args": {"gitops_marker": "marker"},
                        "url": "https://httpbin.org/get?gitops_marker=marker",
                        "headers": {"Host": "httpbin.org"},
                    },
                    {
                        "json": {"gitops_marker": "marker"},
                        "url": "https://httpbin.org/anything",
                        "headers": {"Host": "httpbin.org"},
                    },
                ],
            ),
        ):
            deployment.traffic_check("gitops.f5-sales-demo.com", 100)
        with (
            mock.patch.object(deployment, "resolve_hostname"),
            mock.patch.object(
                deployment,
                "request_json",
                return_value={"args": {}, "url": "https://wrong.example/get"},
            ),
            self.assertRaises(ValueError),
        ):
            deployment.traffic_check("gitops.f5-sales-demo.com", 100)


class DeploymentTests(unittest.TestCase):
    """Verify stale commits, private recovery and competing writers."""

    def setUp(self) -> None:
        """Keep reconcile tests independent of live traffic."""
        patch = mock.patch.object(
            deployment,
            "verify_traffic",
            return_value={"verified_at": "2026-10-09T00:00:00Z"},
        )
        patch.start()
        self.addCleanup(patch.stop)
        summary = mock.patch.object(deployment, "plan_summary", return_value=[])
        summary.start()
        self.addCleanup(summary.stop)

    def test_superseded_plan_never_applies(self) -> None:
        """Verify superseded plan never applies."""
        calls: list[list[str]] = []
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
        self.assertEqual(sum(args[0] == "plan" for args in calls), 3)
        self.assertEqual(sum(args[0] == "apply" for args in calls), 1)
        self.assertTrue(all(receipt["commit"] == "b" * 40 for receipt in stores))
        lease.check.assert_called_once()

    def test_new_commit_after_apply_reconciles(self) -> None:
        """Verify new commit after apply reconciles."""
        commits = ["a" * 40] * 3 + ["b" * 40] * 7
        calls: list[list[str]] = []
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

    def test_failed_verification_retains_applied_recovery(self) -> None:
        """Traffic expiry must leave the exact applied configuration available."""
        stores = []
        environment = deployment.resolve("feature/demo")
        with (
            mock.patch.object(
                deployment, "verify_traffic", side_effect=RuntimeError("expired")
            ),
            mock.patch.object(
                deployment,
                "store",
                side_effect=lambda _name, _archive, receipt: stores.append(
                    copy.deepcopy(receipt)
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "expired"),
        ):
            deployment.verify_deployment(
                Path("unused"),
                environment,
                b"archive",
                receipt={"commit": "a" * 40},
                lease=mock.Mock(),
            )
        self.assertEqual(stores[-1]["status"], "applied")
        self.assertEqual(stores[-1]["commit"], "a" * 40)

    def test_legacy_cleanup_restores_stored_object_names(self) -> None:
        """Cleanup uses original configuration despite the new resolver names."""
        environment = deployment.resolve("feature/demo")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "environment.auto.tfvars.json").write_text(
                json.dumps(environment["tfvars"])
            )
            legacy = (
                b'resource "xcsh_namespace" "environment" { name = "gitops-legacy" }'
            )
            (directory / "main.tf").write_bytes(legacy)
            archive = deployment.recovery_archive(directory)
        receipt = {
            "branch": environment["branch"],
            "environment_id": environment["environment_id"],
            "repository": deployment.REPOSITORY,
            "configuration_sha256": deployment.hashlib.sha256(archive).hexdigest(),
        }
        secret = {
            "data": {
                "configuration.tgz": deployment.base64.b64encode(archive).decode(),
                "receipt.json": deployment.base64.b64encode(
                    json.dumps(receipt).encode()
                ).decode(),
            }
        }
        restored = []
        with (
            mock.patch.object(deployment, "latest", return_value=None),
            mock.patch.object(deployment, "get_secret", return_value=secret),
            mock.patch.object(
                deployment,
                "initialize",
                side_effect=lambda root, _env: restored.append(
                    (root / "main.tf").read_bytes()
                ),
            ),
            mock.patch.object(deployment, "terraform", return_value=0),
            mock.patch.object(deployment, "store"),
        ):
            deployment.cleanup(environment["branch"], mock.Mock())
        self.assertEqual(restored, [legacy])

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
