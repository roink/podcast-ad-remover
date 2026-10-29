"""Merge only bot-proposed upstream PRs after a successful exact-head NAS build."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

FORK = "roink/podcast-ad-remover"


def gh(*args):
    return subprocess.check_output(["gh", *args, "--repo", FORK], text=True).strip()


def eligible(run, pr):
    return (
        run["conclusion"] == "success"
        and run["head_repository"]["full_name"] == FORK
        and run["path"] == ".github/workflows/nas-ci.yml"
        and run["head_branch"].startswith("update/upstream-")
        and pr["author"]["login"] in {"github-actions[bot]", "app/github-actions"}
        and not pr["isDraft"]
        and pr["state"] == "OPEN"
        and pr["baseRefName"] == "nas"
        and pr["headRefName"] == run["head_branch"]
        and pr["headRefOid"] == run["head_sha"]
    )


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    run = event["workflow_run"]
    if not run["head_branch"].startswith("update/upstream-"):
        print("Not an automatic upstream proposal.")
        return
    prs = json.loads(
        gh(
            "pr",
            "list",
            "--base",
            "nas",
            "--head",
            run["head_branch"],
            "--json",
            "number,author,isDraft,state,baseRefName,headRefName,headRefOid",
        )
    )
    for pr in prs:
        if eligible(run, pr):
            # No admin bypass or squash: GitHub enforces the required checks,
            # current base and exact head. Stale/conflicting PRs remain pending.
            gh("pr", "merge", str(pr["number"]), "--merge", "--match-head-commit", run["head_sha"])
            # Bot merges do not emit ordinary CI triggers; explicitly build nas.
            gh("workflow", "run", "nas-ci.yml", "--ref", "nas")
            print("Merged upstream PR", pr["number"], "and dispatched the NAS build.")


if __name__ == "__main__":
    main()
