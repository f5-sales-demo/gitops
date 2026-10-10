#!/usr/bin/env python3
"""Resolve one branch identity for naming, state, workflows, and cleanup."""

import argparse
import hashlib
import json
import re
import unicodedata

EXCLUDED = (
    "sync/",
    "governance/",
    "renovate/",
    "dependabot/",
    "release/",
    "snapshot/",
    "content-",
    "gh-pages",
)


def resolve(branch: str) -> dict:
    """Hash original UTF-8 bytes so normalization collisions stay isolated."""
    if not branch or "\x00" in branch or "\n" in branch or "\r" in branch:
        message = "branch must be a nonempty single-line name"
        raise ValueError(message)
    normalized = (
        unicodedata.normalize("NFKD", branch).encode("ascii", "ignore").decode().lower()
    )
    slug = (
        re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:24].rstrip("-") or "branch"
    )
    identity = (
        "main"
        if branch == "main"
        else slug + "-" + hashlib.sha256(branch.encode()).hexdigest()[:12]
    )
    name = "gitops" if identity == "main" else "gitops-" + identity
    return {
        "branch": branch,
        "environment_id": identity,
        "hostname": name + ".f5-sales-demo.com",
        "namespace_name": name,
        "resource_name": "gitops",
        "secret_suffix": identity,
        "deployable": not branch.startswith(EXCLUDED),
        "tfvars": {
            "environment_id": identity,
            "base_name": "gitops",
            "base_domain": "f5-sales-demo.com",
            "origin_hostname": "httpbin.org",
        },
    }


def main() -> None:
    """Print the canonical branch environment or nonsecret variables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("branch")
    parser.add_argument("--tfvars", action="store_true")
    args = parser.parse_args()
    result = resolve(args.branch)
    print(json.dumps(result["tfvars"] if args.tfvars else result, sort_keys=True))


if __name__ == "__main__":
    main()
