"""Install successful NAS fork builds from public release assets, without credentials.

Installed on the NAS with nas_install.py beside it. A durable pending record
pauses new jobs while current work finishes; no transcription is interrupted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import urllib.error
import urllib.request

try:
    from . import nas_install as installer
except ImportError:
    import nas_install as installer

REPOSITORY = "roink/podcast-ad-remover"
API = f"https://api.github.com/repos/{REPOSITORY}"
STATE = Path("/tank/services/compose/updates/podcast-fork")


def api(path):
    request = urllib.request.Request(
        API + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "nas-podcast-updater"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def write(path, data):
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def qualified(run, release):
    installer.validate_release(release)
    if (
        run["conclusion"] != "success"
        or run["head_branch"] != "nas"
        or run["event"] == "pull_request"
        or run["path"] != ".github/workflows/nas-ci.yml"
        or run["head_sha"] != release["revision"]
        or run["repository"]["full_name"] != REPOSITORY
        or run["id"] != release["ci_run_id"]
        or run["run_attempt"] != release["ci_run_attempt"]
    ):
        raise ValueError("Build does not match a successful NAS workflow")
    for key in ("archive_sha256", "source_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", release.get(key, "")):
            raise ValueError("Missing build file checksum")


def newest():
    runs = api("/actions/workflows/nas-ci.yml/runs?branch=nas&status=success&per_page=10")[
        "workflow_runs"
    ]
    for run in sorted(runs, key=lambda r: (r["run_number"], r["run_attempt"]), reverse=True):
        tag = f"nas-build-{run['id']}-{run['run_attempt']}"
        try:
            release = api("/releases/tags/" + tag)
        except urllib.error.HTTPError as error:
            if error.code == 404:  # Older pre-automation builds have no release.
                continue
            raise
        assets = {a["name"]: a["browser_download_url"] for a in release["assets"]}
        if all(
            name in assets for name in ("nas-release.json", "nas-image.tar.gz", "nas-source.tar.gz")
        ):
            return run, assets
    return None


def download(url, path, limit):
    prefix = f"https://github.com/{REPOSITORY}/releases/download/"
    if not url.startswith(prefix):
        raise ValueError("Unexpected build download location")
    request = urllib.request.Request(url, headers={"User-Agent": "nas-podcast-updater"})
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, path.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("Build file exceeds size limit")
            output.write(chunk)


def checksum(path, expected):
    with path.open("rb") as stream:
        # Ubuntu's host Python may predate hashlib.file_digest.
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("Downloaded build checksum mismatch")


def extract_source(archive, destination):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Unsafe source archive member")
        if sum(m.size for m in members) > 100 * 1024**2:
            raise ValueError("Source archive exceeds size limit")
        source.extractall(destination, members=members, filter="data")


def current_image():
    return installer.run(
        "podman", "inspect", "nas-podcast-ad-remover", "--format", "{{.Image}}"
    ).removeprefix("sha256:")


def finish(pending):
    installer.pause(pending["previous_pause"])
    write(STATE / "installed.json", {**pending["release"], "run_number": pending["run_number"]})
    (STATE / "pending.json").unlink()
    print("Installed NAS fork revision", pending["release"]["revision"], flush=True)


def apply_pending(pending):
    # Crash recovery: a completed install may have happened before state was saved.
    if current_image() == pending["release"]["image_id"].removeprefix("sha256:"):
        installer.run("/usr/local/sbin/nas-container-readiness", "podcast-ad-remover")
        finish(pending)
        return
    installer.pause(True)
    if installer.status()["running_jobs"]:
        print(
            "Waiting for current episode; new jobs remain paused until the next check.", flush=True
        )
        return
    installer.install(Path(pending["staging"]), 0)
    finish(pending)


def poll(check=False):
    if (STATE / "blocked.json").exists():
        raise RuntimeError(
            "Prior installation failed; review podcast-fork/blocked.json and its recovery point"
        )
    if (STATE / "pending.json").exists():
        pending = json.loads((STATE / "pending.json").read_text())
        if check:
            print(
                json.dumps(
                    {"pending_revision": pending["release"]["revision"], **installer.status()}
                )
            )
        else:
            try:
                apply_pending(pending)
            except Exception as error:
                write(STATE / "blocked.json", {"error": str(error), "pending": pending})
                raise
        return
    candidate = newest()
    if candidate is None:
        print("No qualified downloadable NAS build available.")
        return
    run, assets = candidate
    installed = (
        json.loads((STATE / "installed.json").read_text())
        if (STATE / "installed.json").exists()
        else {}
    )
    if (run["run_number"], run["run_attempt"]) <= (
        installed.get("run_number", 0),
        installed.get("ci_run_attempt", 0),
    ):
        print("Already running the newest qualified build.")
        return
    if check:
        print(
            json.dumps(
                {"available_run": run["id"], "revision": run["head_sha"], **installer.status()}
            )
        )
        return
    staging = STATE / f"build-{run['id']}-{run['run_attempt']}"
    staging.mkdir(mode=0o700, exist_ok=True)
    download(assets["nas-release.json"], staging / "nas-release.json", 64 * 1024)
    release = json.loads((staging / "nas-release.json").read_text())
    qualified(run, release)
    if current_image() == release["image_id"].removeprefix("sha256:"):
        write(STATE / "installed.json", {**release, "run_number": run["run_number"]})
        print("Current image already matches this qualified build.")
        return
    # Everything needed for installation is downloaded and verified before pausing.
    for name, key, limit in [
        ("nas-image.tar.gz", "archive_sha256", 2 * 1024**3),
        ("nas-source.tar.gz", "source_sha256", 30 * 1024**2),
    ]:
        path = staging / name
        download(assets[name], path, limit)
        checksum(path, release[key])
    source = staging / "source"
    if source.exists():
        # Only remove a previously extracted, fixed staging subdirectory. Never /data.
        shutil.rmtree(source)
    extract_source(staging / "nas-source.tar.gz", source)
    installer.run("podman", "load", "--input", str(staging / "nas-image.tar.gz"), timeout=1800)
    info = json.loads(installer.run("podman", "image", "inspect", release["image_id"]))[0]
    if info["Labels"].get("org.opencontainers.image.revision") != release["revision"]:
        raise ValueError("Loaded image source identity does not match")
    pending = {
        "release": release,
        "run_number": run["run_number"],
        "staging": str(staging),
        "previous_pause": installer.status()["processing_paused"],
    }
    write(STATE / "pending.json", pending)
    try:
        apply_pending(pending)
    except Exception as error:
        write(STATE / "blocked.json", {"error": str(error), "pending": pending})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root on the NAS")
    installer.run("/usr/local/sbin/nas-storage-ready")
    if installer.run("zpool", "list", "-H", "-o", "health", "tank") != "ONLINE":
        raise RuntimeError("Pool must be ONLINE")
    if args.check:
        poll(check=True)
        return
    os.umask(0o077)
    with Path("/run/nas-container-maintenance.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (installer.ROOT / "updates/blocked.json").exists() or (
            installer.ROOT / "updates/failed-podcast-ad-remover"
        ).exists():
            raise RuntimeError("Existing NAS maintenance failure needs review")
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        poll()


if __name__ == "__main__":
    main()
