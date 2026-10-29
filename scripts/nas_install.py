"""Root-only NAS installer for an already loaded, CI-qualified immutable image.

--check is read-only. Installation drains by default, preserves the pause setting,
and shares the existing NAS maintenance lock. No production data enters CI.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

ROOT = Path("/tank/services/compose")
DB = Path("/tank/services/appdata/podcast-ad-remover/db/podcasts.db")
CURRENT = "localhost/nas-apps/podcast-ad-remover:current"
SERVICE = "podcast-ad-remover.service"


class Deferred(RuntimeError):
    """Current work must finish before installation can proceed."""


def run(*args, timeout=900):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def status():
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True) as conn:
        return {
            "processing_paused": bool(
                conn.execute("SELECT processing_paused FROM app_settings WHERE id=1").fetchone()[0]
            ),
            "running_jobs": conn.execute(
                "SELECT count(*) FROM jobs WHERE status='running'"
            ).fetchone()[0],
        }


def pause(value):
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE app_settings SET processing_paused=? WHERE id=1", (int(value),))


def validate_release(release):
    if release.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{40}", release.get("revision", "")):
        raise ValueError("Invalid release revision/schema")
    if not re.fullmatch(
        r"ghcr\.io/roink/podcast-ad-remover@sha256:[0-9a-f]{64}", release.get("image", "")
    ):
        raise ValueError("Expected a digest-pinned fork image")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", release.get("image_id", "")):
        raise ValueError("Invalid image identity")


def install(staging, wait_seconds, restart_job=False):
    release = json.loads((staging / "nas-release.json").read_text())
    validate_release(release)
    revision = release["revision"]
    context = ROOT / "build/podcast-ad-remover" / revision
    if context.exists():
        raise RuntimeError(
            "Source context already exists; inspect it before redeploying this revision"
        )
    info = json.loads(run("podman", "image", "inspect", release["image_id"]))[0]
    assert info["Id"].removeprefix("sha256:") == release["image_id"].removeprefix("sha256:")
    assert info["Labels"]["org.opencontainers.image.revision"] == revision
    assert (staging / "source/Dockerfile").is_file()
    before = status()
    assert run("systemctl", "is-active", SERVICE) == "active"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = ROOT / "deployments" / ("podcast-nas-" + stamp)
    record.mkdir(mode=0o700, parents=True)
    previous_image = run("podman", "inspect", "nas-podcast-ad-remover", "--format", "{{.Image}}")
    previous_context = str((ROOT / "build/podcast-ad-remover/current").resolve())
    rollback = "localhost/nas-rollback/podcast-ad-remover:before-" + stamp.lower()
    (record / "deployment.json").write_text(
        json.dumps(
            {
                **release,
                "previous_image": previous_image,
                "previous_context": previous_context,
                "rollback_image": rollback,
                "previous_status": before,
            },
            indent=2,
        )
        + "\n"
    )
    stopped = False
    pause(True)
    try:
        deadline = time.monotonic() + wait_seconds
        while status()["running_jobs"]:
            if restart_job:
                break
            if time.monotonic() >= deadline:
                raise Deferred(
                    "Job still running; installation deferred and original pause setting restored"
                )
            time.sleep(2)
        run("podman", "tag", previous_image, rollback)
        # Treat even a failed stop as a stopped/uncertain service: keep paused.
        stopped = True
        run("systemctl", "stop", SERVICE)
        with sqlite3.connect(DB) as conn:
            with sqlite3.connect(record / "podcasts-before.db") as backup:
                conn.backup(backup)
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for dataset in ("tank/services/appdata", "tank/services/compose"):
            run("zfs", "snapshot", dataset + "@before-podcast-nas-" + stamp)
        if status()["running_jobs"]:
            run(
                "podman",
                "run",
                "--rm",
                "--network=none",
                "--memory=512m",
                "--env-file",
                str(ROOT / "private/podcast-ad-remover.env"),
                "--volume",
                str(DB.parent.parent) + ":/data",
                "--entrypoint",
                "python",
                rollback,
                "-c",
                "from app.infra.repository import EpisodeRepository; EpisodeRepository().requeue_stuck()",
            )
        (staging / "source").rename(context)
        link = context.parent / "current-next"
        link.symlink_to(context.name, target_is_directory=True)
        link.replace(context.parent / "current")
        run("podman", "tag", release["image_id"], CURRENT)
        run("systemctl", "start", SERVICE)
        run("/usr/local/sbin/nas-container-readiness", "podcast-ad-remover")
        actual = run("podman", "inspect", "nas-podcast-ad-remover", "--format", "{{.Image}}")
        assert actual.removeprefix("sha256:") == release["image_id"].removeprefix("sha256:")
        pause(before["processing_paused"])
        (record / "success").write_text("Healthy; previous pause setting restored.\n")
        print(json.dumps({"recovery": str(record), "revision": revision, **status()}))
    except Exception:
        if not stopped:
            pause(before["processing_paused"])
        else:
            (record / "needs-review").write_text(
                "Keep processing paused; review the matching image/database recovery point.\n"
            )
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument(
        "--restart-job",
        action="store_true",
        help="Explicitly restart unfinished work through outgoing app recovery",
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root on the NAS")
    run("/usr/local/sbin/nas-storage-ready")
    if run("zpool", "list", "-H", "-o", "health", "tank") != "ONLINE":
        raise RuntimeError("Pool must be ONLINE")
    if args.check:
        print(json.dumps({"service": run("systemctl", "is-active", SERVICE), **status()}))
        return
    if args.staging is None or args.wait_seconds < 0:
        parser.error("--staging and a nonnegative wait time are required")
    os.umask(0o077)
    with Path("/run/nas-container-maintenance.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (ROOT / "updates/blocked.json").exists() or (
            ROOT / "updates/failed-podcast-ad-remover"
        ).exists():
            raise RuntimeError("Existing maintenance failure needs review")
        install(args.staging.resolve(), args.wait_seconds, args.restart_job)


if __name__ == "__main__":
    main()
