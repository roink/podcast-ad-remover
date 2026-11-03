"""Root-only NAS installer for an already loaded, CI-qualified immutable image.

--check is read-only. Installation drains by default, preserves the pause setting,
and shares the existing NAS maintenance lock. No production data enters CI.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tarfile
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


def archive_image_id(archive, release):
    """Resolve a verified export to the config ID shared by Docker and Podman.

    Docker's containerd store may report the OCI index as Id. Follow its
    content-addressed descriptors rather than trusting a tag or revision label.
    Legacy Docker exports are accepted only with an exact configuration ID.
    """
    validate_release(release)
    with tarfile.open(archive) as image:

        def read(name):
            member = image.getmember(name)
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError("Invalid image metadata member")
            return image.extractfile(member).read()

        def blob(digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Invalid image descriptor digest")
            raw = read("blobs/sha256/" + digest.split(":")[1])
            if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("Image descriptor checksum mismatch")
            return json.loads(raw)

        exports = json.loads(read("manifest.json"))
        if len(exports) != 1:
            raise ValueError("Expected one exported image")
        raw = read(exports[0]["Config"])
        config_id = "sha256:" + hashlib.sha256(raw).hexdigest()
        config = json.loads(raw)
        registry_digest = release["image"].split("@")[1]
        # New exports carry the registry index/manifest as content-addressed blobs.
        if "oci-layout" in image.getnames():
            descriptor = blob(registry_digest)
            if "manifests" in descriptor:
                matches = [
                    item
                    for item in descriptor["manifests"]
                    if item.get("platform", {}).get("os") == "linux"
                    and item.get("platform", {}).get("architecture") == "amd64"
                ]
                if len(matches) != 1:
                    raise ValueError("Expected one Linux amd64 image descriptor")
                descriptor = blob(matches[0]["digest"])
            if descriptor["config"]["digest"] != config_id:
                raise ValueError("Registry image does not match exported configuration")
            blob(config_id)
            accepted_ids = {config_id, registry_digest}
        else:
            accepted_ids = {config_id}
        if release["image_id"] not in accepted_ids:
            raise ValueError("Exported image does not match CI image identity")
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("Expected Linux amd64 image configuration")
        labels = config.get("config", {}).get("Labels") or {}
        if labels.get("org.opencontainers.image.revision") != release["revision"]:
            raise ValueError("Exported image does not match CI source revision")
        return config_id


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
