"""Propose production-branch merges inside this fork; never write to upstream."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=check)


def gh(repository, *args):
    return subprocess.check_output(["gh", *args, "--repo", repository], cwd=ROOT, text=True).strip()


def propose(config):
    fork = config["fork_repository"]
    upstream = config["upstream_repository"]
    if fork == upstream or os.environ.get("GITHUB_REPOSITORY", fork) != fork:
        raise RuntimeError("Update proposals may run only in the configured fork")
    if git("status", "--porcelain").stdout.strip():
        raise RuntimeError("Use a clean disposable checkout")
    git("fetch", "--no-tags", "origin", "nas")
    git("fetch", "--no-tags", f"https://github.com/{upstream}.git", config["upstream_branch"])
    revision = git("rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
    if git("merge-base", "--is-ancestor", revision, "origin/nas", check=False).returncode == 0:
        print("The fork already contains upstream production.")
        return
    branch = "update/upstream-" + revision[:12]
    existing = json.loads(
        gh(
            fork,
            "pr",
            "list",
            "--state",
            "all",
            "--base",
            "nas",
            "--head",
            branch,
            "--json",
            "url,state",
        )
    )
    if existing:
        print("Existing proposal (closed proposals are not reopened):", existing[0]["url"])
        return
    # No force pushes: a prior interrupted attempt is preserved for inspection.
    if git("ls-remote", "--heads", "origin", branch).stdout.strip():
        raise RuntimeError(f"Remote branch {branch} already exists without a PR; inspect it")
    git("switch", "-c", branch, "origin/nas")
    merged = git("merge", "--no-ff", "--no-edit", revision, check=False)
    conflict = merged.returncode != 0
    if conflict:
        files = git("diff", "--name-only", "--diff-filter=U").stdout.strip()
        git("merge", "--abort")
        if not files:
            raise RuntimeError("Merge failed without conflicts: " + merged.stderr)
        # Point the proposal at upstream's exact commit. GitHub exposes the merge
        # conflicts against nas; no fake resolution or discarded NAS patches.
        git("switch", "--detach", revision)
        git("branch", "-f", branch, revision)
        git("switch", branch)
    git("push", "origin", f"HEAD:refs/heads/{branch}")
    body = (
        f"Merge `{upstream}` production branch `{config['upstream_branch']}` at `{revision}` "
        "into the NAS fork.\n\nKeep Moonshine/languages, YAMNet evidence and missed examples. "
        "Review database migrations and dependency changes. Nothing deploys to the NAS automatically.\n"
    )
    if conflict:
        body += (
            "\nThis draft has unresolved merge conflicts. Resolve them on this branch before marking ready:\n\n"
            + files
            + "\n"
        )
    path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "nas-upstream-pr.md"
    path.write_text(body)
    args = [
        "pr",
        "create",
        "--base",
        "nas",
        "--head",
        branch,
        "--title",
        f"Merge upstream production {revision[:12]}",
        "--body-file",
        str(path),
    ]
    if conflict:
        args.append("--draft")
    url = gh(fork, *args)
    print(url)
    if not conflict:
        # GITHUB_TOKEN-created pushes/PRs don't trigger ordinary CI. Explicit
        # workflow_dispatch does; verify the merged proposal before review.
        gh(fork, "workflow", "run", "nas-ci.yml", "--ref", branch)


def main():
    propose(json.loads((ROOT / "nas/maintenance.json").read_text()))


if __name__ == "__main__":
    main()
