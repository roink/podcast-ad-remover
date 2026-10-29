"""Deploy the exact image from a successful NAS CI run, using workstation tools.

Requires gh, Docker, Git and SSH with passwordless sudo on the destination.
No registry credential is installed on the NAS. --check reads live status only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

try:
    from .nas_install import validate_release
except ImportError:
    from nas_install import validate_release

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, text=True, **kwargs).strip()


def qualified_run(metadata, release):
    validate_release(release)
    if (
        metadata["conclusion"] != "success"
        or metadata["head_branch"] != "nas"
        or metadata["head_sha"] != release["revision"]
        or metadata["event"] == "pull_request"
        or metadata["path"] != ".github/workflows/nas-ci.yml"
    ):
        raise ValueError(
            "Expected a successful NAS candidate run on nas matching the release revision"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SSH destination, e.g. user@nas-address")
    parser.add_argument("--run", type=int, help="Successful NAS candidate workflow run ID")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--restart-job", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", args.host) or args.host.startswith("-"):
        parser.error("Invalid SSH destination")
    if args.wait_seconds < 0:
        parser.error("Wait time must be nonnegative")
    config = json.loads((ROOT / "nas/maintenance.json").read_text())
    fork = config["fork_repository"]
    # A read-only status check streams the reviewed installer; no staging needed.
    if args.check:
        subprocess.run(
            ["ssh", args.host, "sudo python3 - --check"],
            input=(ROOT / "scripts/nas_install.py").read_bytes(),
            check=True,
        )
        return
    if args.run is None:
        parser.error("--run is required for installation")
    metadata = json.loads(run("gh", "api", f"repos/{fork}/actions/runs/{args.run}"))
    with tempfile.TemporaryDirectory(prefix="par-nas-release-") as directory:
        temporary = Path(directory)
        run(
            "gh",
            "run",
            "download",
            str(args.run),
            "--repo",
            fork,
            "--name",
            "nas-release",
            "--dir",
            directory,
        )
        release = json.loads((temporary / "nas-release.json").read_text())
        qualified_run(metadata, release)
        run("git", "fetch", "--no-tags", "origin", "nas")
        run("git", "merge-base", "--is-ancestor", release["revision"], "origin/nas")
        # Pull by digest. If private, use the workstation's gh credential only in
        # a temporary Docker config; never print it or transfer it to the NAS.
        docker_env = {**os.environ, "DOCKER_CONFIG": str(temporary / "docker")}
        pull = subprocess.run(
            ["docker", "pull", release["image"]], env=docker_env, capture_output=True
        )
        if pull.returncode:
            token = run("gh", "auth", "token")
            subprocess.run(
                [
                    "docker",
                    "login",
                    "ghcr.io",
                    "-u",
                    run("gh", "api", "user", "--jq", ".login"),
                    "--password-stdin",
                ],
                input=token,
                text=True,
                env=docker_env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            run("docker", "pull", release["image"], env=docker_env)
        info = json.loads(run("docker", "image", "inspect", release["image"], env=docker_env))[0]
        if (
            info["Id"] != release["image_id"]
            or info["Config"]["Labels"]["org.opencontainers.image.revision"] != release["revision"]
        ):
            raise RuntimeError("Pulled image does not match CI source/image identity")
        with subprocess.Popen(
            ["docker", "save", release["image"]], stdout=subprocess.PIPE
        ) as export:
            subprocess.run(["ssh", args.host, "sudo podman load"], stdin=export.stdout, check=True)
            export.stdout.close()
            if export.wait():
                raise RuntimeError("Image export failed")
        staging = run(
            "ssh",
            args.host,
            "sudo mktemp -d /tank/services/compose/deployments/podcast-stage-XXXXXXXX",
        )
        quoted = shlex.quote(staging)
        run("ssh", args.host, f"sudo mkdir {quoted}/source")
        with subprocess.Popen(
            ["git", "archive", release["revision"]], cwd=ROOT, stdout=subprocess.PIPE
        ) as archive:
            subprocess.run(
                ["ssh", args.host, f"sudo tar -xf - -C {quoted}/source"],
                stdin=archive.stdout,
                check=True,
            )
            archive.stdout.close()
            if archive.wait():
                raise RuntimeError("Source archive failed")
        subprocess.run(
            ["ssh", args.host, f"sudo tee {quoted}/nas-release.json >/dev/null"],
            input=json.dumps(release).encode(),
            check=True,
        )
        command = f"sudo python3 - --staging {quoted} --wait-seconds {args.wait_seconds}"
        if args.restart_job:
            command += " --restart-job"
        subprocess.run(
            ["ssh", args.host, command],
            input=(ROOT / "scripts/nas_install.py").read_bytes(),
            check=True,
        )


if __name__ == "__main__":
    main()
