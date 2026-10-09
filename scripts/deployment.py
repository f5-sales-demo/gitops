#!/usr/bin/env python3
"""Reconcile an environment and retain private configuration for recovery."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from branch_environment import resolve

REPOSITORY = "f5-sales-demo/gitops"
STATE_NAMESPACE = "gitops-terraform-state"
NOT_FOUND = 404
SECRET_BUDGET = 700_000
PLAN_CHANGED = 2
KUBECTL = shutil.which("kubectl") or "/usr/bin/kubectl"
TERRAFORM = shutil.which("terraform") or "/usr/local/bin/terraform"


def github(path: str, *, binary: bool = False) -> dict | bytes | None:
    """Read a fixed GitHub repository endpoint without exposing credentials."""
    request = urllib.request.Request(
        "https://api.github.com/repos/" + REPOSITORY + "/" + path,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        # Request and redirect targets are GitHub HTTPS endpoints.
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            data = response.read()
    except urllib.error.HTTPError as error:
        if error.code == NOT_FOUND:
            return None
        raise RuntimeError(
            "GitHub request failed with HTTP " + str(error.code)
        ) from None
    return data if binary else json.loads(data)


def latest(branch: str) -> str | None:
    """Resolve the current branch head, treating a missing branch as deleted."""
    result = github("git/ref/heads/" + urllib.parse.quote(branch, safe=""))
    return None if result is None else result["object"]["sha"]


def kubectl(
    args: list[str], *, data: bytes | None = None, allow_missing: bool = False
) -> dict | None:
    """Execute a namespaced Kubernetes operation with private payloads."""
    result = subprocess.run(  # noqa: S603
        [KUBECTL, "-n", STATE_NAMESPACE, *args],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # Never print kubectl payloads or provider diagnostics into public job logs.
        raise RuntimeError("Kubernetes operation failed: " + args[0])
    if not result.stdout.strip() and allow_missing:
        return None
    return json.loads(result.stdout) if result.stdout.strip() else None


def get_secret(name: str) -> dict | None:
    """Read a recovery Secret without displaying its data."""
    return kubectl(
        ["get", "secret", name, "--ignore-not-found", "-o", "json"], allow_missing=True
    )


def store(name: str, archive: bytes, receipt: dict) -> None:
    """Replace or create a versioned recovery Secret."""
    encoded_receipt = json.dumps(receipt, sort_keys=True).encode()
    if len(archive) + len(encoded_receipt) > SECRET_BUDGET:
        message = "Recovery configuration exceeds the Kubernetes Secret size budget"
        raise RuntimeError(message)
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": STATE_NAMESPACE,
            "labels": {
                "app.kubernetes.io/part-of": "gitops",
                "gitops-environment": receipt["environment_id"],
            },
        },
        "type": "Opaque",
        "data": {
            "configuration.tgz": base64.b64encode(archive).decode(),
            "receipt.json": base64.b64encode(encoded_receipt).decode(),
        },
    }
    existing = get_secret(name)
    if existing:
        secret["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    kubectl(
        ["replace" if existing else "create", "-f", "-", "-o", "json"],
        data=json.dumps(secret).encode(),
    )


def extract_configuration(source: bytes, target: Path) -> None:
    """Extract regular Terraform files only; no symlinks or traversal."""
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        members = archive.getmembers()
        prefix = members[0].name.split("/")[0] + "/terraform/"
        for member in members:
            if not member.name.startswith(prefix) or not member.isfile():
                continue
            relative = Path(member.name[len(prefix) :])
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or any(
                    part.startswith(".terraform") and part != ".terraform.lock.hcl"
                    for part in relative.parts
                )
            ):
                message = "Unsafe Terraform archive member"
                raise RuntimeError(message)
            if relative.suffix not in {".tf", ".hcl"} or relative.name.endswith(
                ".tfvars"
            ):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.extractfile(member).read())
    if (
        not (target / ".terraform.lock.hcl").is_file()
        or not (target / "versions.tf").is_file()
    ):
        message = "Deployment requires a committed lock file and configuration"
        raise RuntimeError(message)


def recovery_archive(directory: Path) -> bytes:
    """Archive exact configuration, lock file and generated variables."""
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if (
                path.is_file()
                and ".terraform" not in path.relative_to(directory).parts
                and (
                    path.suffix in {".tf", ".hcl"}
                    or path.name == "environment.auto.tfvars.json"
                )
            ):
                archive.add(
                    path, arcname=str(path.relative_to(directory)), recursive=False
                )
    return result.getvalue()


def restore(archive: bytes, directory: Path) -> None:
    """Restore only regular relative files from a private recovery archive."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source.getmembers():
            relative = Path(member.name)
            if not member.isfile() or relative.is_absolute() or ".." in relative.parts:
                message = "Invalid recovery archive"
                raise RuntimeError(message)
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.extractfile(member).read())


def terraform(
    directory: Path, args: list[str], *, expected: tuple[int, ...] = (0,)
) -> int:
    """Run Terraform with diagnostics retained only in private local storage."""
    with (directory.parent / "terraform-private.log").open("ab") as log:
        result = subprocess.run(  # noqa: S603
            [TERRAFORM, "-chdir=" + str(directory), *args, "-no-color"],
            stdout=log,
            stderr=log,
            check=False,
        )
    if result.returncode not in expected:
        raise RuntimeError(
            "Terraform "
            + args[0]
            + " failed; private recovery configuration is retained"
        )
    return result.returncode


def initialize(directory: Path, environment: dict) -> None:
    """Initialize native in-cluster backend and the committed provider lock."""
    cache_root = os.environ.get("TF_PLUGIN_CACHE_ROOT")
    if cache_root:
        identity = hashlib.sha256(
            (directory / ".terraform.lock.hcl").read_bytes()
        ).hexdigest()
        cache = Path(cache_root) / identity
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["TF_PLUGIN_CACHE_DIR"] = str(cache)
    terraform(
        directory,
        [
            "init",
            "-input=false",
            "-reconfigure",
            "-lockfile=readonly",
            "-backend-config=namespace=" + STATE_NAMESPACE,
            "-backend-config=secret_suffix=" + environment["secret_suffix"],
            "-backend-config=in_cluster_config=true",
        ],
    )


def reconcile(branch: str, lease: EnvironmentLease) -> None:
    """Replan superseded commits and reconcile newer work after each apply."""
    environment = resolve(branch)
    if not environment["deployable"]:
        print("Excluded automation branch")
        return
    secret_name = "gitops-config-" + environment["environment_id"]
    # GitHub concurrency bounds queueing. This loop owns reconciliation ordering.
    while True:
        commit = latest(branch)
        if commit is None:
            print("Branch deleted; trusted cleanup will reconcile its state")
            return
        with tempfile.TemporaryDirectory(prefix="gitops-") as temporary:
            Path(temporary).chmod(0o700)
            directory = Path(temporary) / "terraform"
            directory.mkdir()
            source = github("tarball/" + commit, binary=True)
            if source is None:
                message = "Commit configuration is unavailable"
                raise RuntimeError(message)
            extract_configuration(source, directory)
            variables = directory / "environment.auto.tfvars.json"
            variables.write_text(
                json.dumps(environment["tfvars"], sort_keys=True) + "\n"
            )
            initialize(directory, environment)
            terraform(directory, ["validate"])
            plan = directory.parent / "saved.plan"
            code = terraform(
                directory,
                [
                    "plan",
                    "-input=false",
                    "-lock-timeout=10m",
                    "-detailed-exitcode",
                    "-out=" + str(plan),
                ],
                expected=(0, 2),
            )
            if latest(branch) != commit:
                print("Superseded plan discarded")
                continue
            archive = recovery_archive(directory)
            receipt = {
                "repository": REPOSITORY,
                "branch": branch,
                "environment_id": environment["environment_id"],
                "commit": commit,
                "configuration_sha256": hashlib.sha256(archive).hexdigest(),
                "lock_sha256": hashlib.sha256(
                    (directory / ".terraform.lock.hcl").read_bytes()
                ).hexdigest(),
                "status": "applying",
                "run_id": os.environ.get("GITHUB_RUN_ID", "manual"),
            }
            # Keep prior receipts and the complete newest configuration before mutation.
            store(secret_name + "-" + commit[:12], archive, receipt)
            store(secret_name, archive, receipt)
            # Recheck after the recovery write as well as after planning.
            if latest(branch) != commit:
                print("Superseded configuration discarded before apply")
                continue
            lease.check()
            if code == PLAN_CHANGED:
                terraform(
                    directory, ["apply", "-input=false", "-lock-timeout=10m", str(plan)]
                )
            receipt["status"] = "applied"
            store(secret_name, archive, receipt)
            print(
                json.dumps(
                    {
                        "environment_id": environment["environment_id"],
                        "commit": commit,
                        "hostname": environment["hostname"],
                        "status": "applied",
                    },
                    sort_keys=True,
                )
            )
            if latest(branch) == commit:
                return
            print("New branch commit detected after apply; reconciling")


def cleanup(branch: str, lease: EnvironmentLease) -> None:
    """Destroy a deleted preview using its isolated recovery configuration."""
    environment = resolve(branch)
    if branch == "main":
        message = "Main cleanup is prohibited"
        raise RuntimeError(message)
    if not environment["deployable"]:
        return
    if latest(branch) is not None:
        print("Branch exists; cleanup skipped")
        return
    name = "gitops-config-" + environment["environment_id"]
    secret = get_secret(name)
    if secret is None:
        print("No recovery configuration; no resources were deployed")
        return
    receipt = json.loads(base64.b64decode(secret["data"]["receipt.json"]))
    archive = base64.b64decode(secret["data"]["configuration.tgz"])
    if (
        receipt["branch"] != branch
        or receipt["environment_id"] != environment["environment_id"]
        or receipt["repository"] != REPOSITORY
        or receipt["configuration_sha256"] != hashlib.sha256(archive).hexdigest()
    ):
        message = "Recovery identity does not match this branch"
        raise RuntimeError(message)
    with tempfile.TemporaryDirectory(prefix="gitops-cleanup-") as temporary:
        Path(temporary).chmod(0o700)
        directory = Path(temporary) / "terraform"
        directory.mkdir()
        restore(archive, directory)
        if (
            json.loads((directory / "environment.auto.tfvars.json").read_text())
            != environment["tfvars"]
        ):
            message = "Recovery variables do not match this branch"
            raise RuntimeError(message)
        initialize(directory, environment)
        plan = directory.parent / "destroy.plan"
        terraform(
            directory,
            [
                "plan",
                "-destroy",
                "-input=false",
                "-lock-timeout=10m",
                "-out=" + str(plan),
            ],
        )
        if latest(branch) is not None:
            print("Branch recreated; cleanup skipped")
            return
        lease.check()
        terraform(directory, ["apply", "-input=false", "-lock-timeout=10m", str(plan)])
        receipt["status"] = "destroyed"
        store(name, archive, receipt)
    print(
        json.dumps(
            {
                "environment_id": environment["environment_id"],
                "status": "destroyed",
                "empty_state_retained": True,
            }
        )
    )


class EnvironmentLease:
    """Coordinate whole reconcile transactions across independent workflow runs."""

    def __init__(self, environment_id: str) -> None:
        """Prepare a unique holder and renewal lifecycle."""
        self.name = "gitops-deployment-" + environment_id
        self.holder = os.environ.get("GITHUB_RUN_ID", "manual") + "-" + uuid.uuid4().hex
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None
        self.mutex = threading.Lock()

    def _write(self, *, acquire: bool = False) -> bool:
        with self.mutex:
            return self._write_unlocked(acquire=acquire)

    def _write_unlocked(self, *, acquire: bool = False) -> bool:
        current = kubectl(
            ["get", "lease", self.name, "--ignore-not-found", "-o", "json"],
            allow_missing=True,
        )
        now = datetime.now(UTC)
        if current and current["spec"].get("holderIdentity") not in {
            None,
            "",
            self.holder,
        }:
            renewed = datetime.fromisoformat(current["spec"]["renewTime"])
            if not acquire or now < renewed + timedelta(
                seconds=current["spec"]["leaseDurationSeconds"]
            ):
                return False
        lease = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": self.name, "namespace": STATE_NAMESPACE},
            "spec": {
                "holderIdentity": self.holder,
                "leaseDurationSeconds": 120,
                "renewTime": now.isoformat().replace("+00:00", "Z"),
            },
        }
        if current:
            lease["metadata"]["resourceVersion"] = current["metadata"][
                "resourceVersion"
            ]
        try:
            kubectl(
                ["replace" if current else "create", "-f", "-", "-o", "json"],
                data=json.dumps(lease).encode(),
            )
        except RuntimeError:
            return False
        return True

    def check(self) -> None:
        """Stop before mutation if transaction lease ownership was lost."""
        if self.lost.is_set() or not self._write():
            message = "Environment transaction Lease was lost"
            raise RuntimeError(message)

    def _renew(self) -> None:
        while not self.stop.wait(20):
            try:
                if not self._write():
                    self.lost.set()
                    return
            except RuntimeError:
                self.lost.set()
                return

    def __enter__(self) -> EnvironmentLease:
        """Acquire ownership and start renewing the transaction Lease."""
        deadline = time.monotonic() + 600
        while not self._write(acquire=True):
            if time.monotonic() >= deadline:
                message = "Environment transaction Lease acquisition timed out"
                raise RuntimeError(message)
            time.sleep(5)
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_arguments: object) -> None:
        """Release only the Lease still owned by this transaction."""
        self.stop.set()
        self.thread.join()
        current = kubectl(
            ["get", "lease", self.name, "--ignore-not-found", "-o", "json"],
            allow_missing=True,
        )
        if current and current["spec"].get("holderIdentity") == self.holder:
            current["spec"]["holderIdentity"] = ""
            kubectl(
                ["replace", "-f", "-", "-o", "json"], data=json.dumps(current).encode()
            )


def main() -> None:
    """Select the authorized deployment operation and validate repository identity."""
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["deploy", "cleanup"])
    parser.add_argument("branch")
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY", REPOSITORY) != REPOSITORY:
        message = "Repository identity mismatch"
        raise SystemExit(message)
    with EnvironmentLease(resolve(args.branch)["environment_id"]) as lease:
        if args.operation == "cleanup":
            cleanup(args.branch, lease)
        else:
            reconcile(args.branch, lease)


if __name__ == "__main__":
    main()
