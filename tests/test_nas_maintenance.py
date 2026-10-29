"""Synthetic Git histories and disposable DBs for fork maintenance gates."""

import json
import sqlite3
import subprocess

import pytest

from scripts import nas_install as installer
from scripts import nas_release as release
from scripts import nas_upstream as updater

SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64
MANIFEST = {
    "schema": 1,
    "revision": SHA,
    "image_id": IMAGE,
    "image": "ghcr.io/roink/podcast-ad-remover@sha256:" + "c" * 64,
}


def test_release_requires_successful_matching_fork_build():
    metadata = {
        "conclusion": "success",
        "head_branch": "nas",
        "head_sha": SHA,
        "event": "push",
        "path": ".github/workflows/nas-ci.yml",
    }
    release.qualified_run(metadata, MANIFEST)
    for key, value in [
        ("conclusion", "failure"),
        ("head_branch", "main"),
        ("head_sha", "d" * 40),
        ("event", "pull_request"),
        ("path", ".github/workflows/other.yml"),
    ]:
        with pytest.raises(ValueError):
            release.qualified_run({**metadata, key: value}, MANIFEST)
    for key, value in [
        ("image", "ghcr.io/roink/podcast-ad-remover:latest"),
        ("image", "ghcr.io/elsewhere/image@sha256:" + "c" * 64),
        ("revision", "../bad"),
        ("image_id", "not-an-id"),
    ]:
        with pytest.raises(ValueError):
            installer.validate_release({**MANIFEST, key: value})


@pytest.fixture
def git_history(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    work = tmp_path / "work"

    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    git(upstream, "config", "user.name", "Test")
    git(upstream, "config", "user.email", "test@example.invalid")
    (upstream / "base").write_text("base\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "Base")
    subprocess.run(["git", "clone", "-q", str(upstream), str(work)], check=True)
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "switch", "-qc", "nas")
    (work / "custom").write_text("keep NAS customization\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "NAS patch")
    git(work, "push", "-q", "origin", "nas")
    monkeypatch.setattr(updater, "ROOT", work)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/fork")
    original = updater.git

    def redirect(*args, **kwargs):
        if args[:3] == ("fetch", "--no-tags", "https://github.com/owner/upstream.git"):
            args = ("fetch", "--no-tags", str(upstream), *args[3:])
        return original(*args, **kwargs)

    monkeypatch.setattr(updater, "git", redirect)
    calls = []

    def gh(repository, *args):
        assert repository == "owner/fork"
        calls.append(args)
        return "[]" if args[:2] == ("pr", "list") else "https://example.invalid/pr/1"

    monkeypatch.setattr(updater, "gh", gh)
    return upstream, work, git, calls


@pytest.mark.parametrize("conflict", [False, True])
def test_proposals_keep_patch_and_write_only_fork(git_history, conflict):
    upstream, work, git, calls = git_history
    target = "custom" if conflict else "upstream-new"
    (upstream / target).write_text("upstream content\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "Production update")
    revision = git(upstream, "rev-parse", "HEAD")
    updater.propose(
        {
            "fork_repository": "owner/fork",
            "upstream_repository": "owner/upstream",
            "upstream_branch": "main",
        }
    )
    assert git(work, "show", "origin/nas:custom") == "keep NAS customization"
    assert git(work, "merge-base", "--is-ancestor", revision, "HEAD") == ""
    create = next(c for c in calls if c[:2] == ("pr", "create"))
    assert create[create.index("--base") + 1] == "nas"
    assert ("--draft" in create) is conflict
    if conflict:
        assert git(work, "rev-parse", "HEAD") == revision
        assert not any(c[:2] == ("workflow", "run") for c in calls)
    else:
        assert (work / "custom").read_text() == "keep NAS customization\n"
        assert any(c[:2] == ("workflow", "run") for c in calls)


def test_cannot_propose_to_upstream():
    with pytest.raises(RuntimeError):
        updater.propose({"fork_repository": "owner/same", "upstream_repository": "owner/same"})


@pytest.fixture
def staged_release(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    db = tmp_path / "podcasts.db"
    monkeypatch.setattr(installer, "DB", db)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE app_settings(id INTEGER,processing_paused INTEGER); INSERT INTO app_settings VALUES(1,0); CREATE TABLE jobs(status TEXT);"
        )
    staging = tmp_path / "staging"
    (staging / "source").mkdir(parents=True)
    (staging / "source/Dockerfile").write_text("synthetic source")
    (staging / "nas-release.json").write_text(json.dumps(MANIFEST))
    (tmp_path / "build/podcast-ad-remover").mkdir(parents=True)
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("podman", "image", "inspect"):
            return json.dumps([{"Id": IMAGE, "Labels": {"org.opencontainers.image.revision": SHA}}])
        if args[:2] == ("systemctl", "is-active"):
            return "active"
        if args[:2] == ("podman", "inspect"):
            return IMAGE
        return ""

    monkeypatch.setattr(installer, "run", run)
    return staging, db, calls


def test_busy_job_defers_without_stopping_and_restores_pause(staged_release):
    staging, db, calls = staged_release
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO jobs VALUES('running')")
    with pytest.raises(RuntimeError, match="deferred"):
        installer.install(staging, 0)
    assert not installer.status()["processing_paused"]
    assert ("systemctl", "stop", installer.SERVICE) not in calls


def test_install_backup_and_restored_enabled_state(staged_release):
    staging, db, calls = staged_release
    installer.install(staging, 0)
    assert not installer.status()["processing_paused"]
    assert ("systemctl", "stop", installer.SERVICE) in calls
    assert ("systemctl", "start", installer.SERVICE) in calls
    backups = list(db.parent.glob("deployments/*/podcasts-before.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (db.parent / "build/podcast-ad-remover/current").resolve().name == SHA


def test_failed_start_keeps_processing_paused(staged_release, monkeypatch):
    staging, db, calls = staged_release
    original = installer.run

    def failing(*args, **kwargs):
        if args[:2] == ("systemctl", "start"):
            raise RuntimeError("Synthetic start failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(installer, "run", failing)
    with pytest.raises(RuntimeError, match="Synthetic"):
        installer.install(staging, 0)
    assert installer.status()["processing_paused"]
    assert list(db.parent.glob("deployments/*/needs-review"))
