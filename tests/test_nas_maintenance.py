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


def test_automerge_only_tested_bot_upstream_proposal():
    from scripts.nas_automerge import eligible

    run = {
        "conclusion": "success",
        "head_repository": {"full_name": "roink/podcast-ad-remover"},
        "path": ".github/workflows/nas-ci.yml",
        "head_branch": "update/upstream-123",
        "head_sha": SHA,
    }
    pr = {
        "author": {"login": "github-actions[bot]"},
        "isDraft": False,
        "state": "OPEN",
        "baseRefName": "nas",
        "headRefName": run["head_branch"],
        "headRefOid": SHA,
    }
    assert eligible(run, pr)
    for key, value in [
        ("conclusion", "failure"),
        ("head_sha", "c" * 40),
        ("head_branch", "feature/something"),
        ("head_repository", {"full_name": "untrusted/fork"}),
    ]:
        assert not eligible({**run, key: value}, pr)
    for key, value in [
        ("isDraft", True),
        ("author", {"login": "human"}),
        ("state", "CLOSED"),
        ("baseRefName", "main"),
    ]:
        assert not eligible(run, {**pr, key: value})


def test_public_build_qualification_and_checksums(tmp_path):
    from scripts import nas_poll as poll

    run = {
        "conclusion": "success",
        "head_branch": "nas",
        "event": "push",
        "head_sha": SHA,
        "path": ".github/workflows/nas-ci.yml",
        "repository": {"full_name": poll.REPOSITORY},
        "id": 123,
        "run_attempt": 1,
    }
    manifest = {
        **MANIFEST,
        "ci_run_id": 123,
        "ci_run_attempt": 1,
        "archive_sha256": "d" * 64,
        "source_sha256": "e" * 64,
    }
    poll.qualified(run, manifest)
    for key, value in [
        ("conclusion", "failure"),
        ("head_branch", "update/upstream-123"),
        ("head_sha", "c" * 40),
        ("id", 124),
        ("run_attempt", 2),
    ]:
        with pytest.raises(ValueError):
            poll.qualified({**run, key: value}, manifest)
    data = tmp_path / "download"
    data.write_text("corrupt build")
    with pytest.raises(ValueError, match="checksum"):
        poll.checksum(data, "d" * 64)


def test_source_extraction_rejects_traversal_and_links(tmp_path):
    import io
    import tarfile
    from scripts import nas_poll as poll

    for name, kind in [("../outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)]:
        archive = tmp_path / "source.tar"
        with tarfile.open(archive, "w") as tar:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.linkname = "/etc"
            tar.addfile(entry, io.BytesIO())
        with pytest.raises(ValueError, match="Unsafe"):
            poll.extract_source(archive, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


@pytest.fixture
def pending_poll(tmp_path, monkeypatch):
    from scripts import nas_poll as poll

    monkeypatch.setattr(poll, "STATE", tmp_path)
    pending = {
        "release": MANIFEST,
        "run_number": 1,
        "staging": str(tmp_path / "staging"),
        "previous_pause": False,
    }
    poll.write(tmp_path / "pending.json", pending)
    monkeypatch.setattr(poll, "current_image", lambda: "old-image")
    monkeypatch.setattr(installer, "status", lambda: {"running_jobs": 1, "processing_paused": True})
    pauses = []
    installs = []
    monkeypatch.setattr(installer, "pause", pauses.append)
    monkeypatch.setattr(installer, "install", lambda *args: installs.append(args))
    monkeypatch.setattr(installer, "run", lambda *args: "")
    return poll, pending, pauses, installs


def test_poll_drain_persists_then_restores_original_state(pending_poll, monkeypatch):
    poll, pending, pauses, installs = pending_poll
    poll.poll()
    assert pauses == [True]
    assert installs == []
    assert (poll.STATE / "pending.json").exists()
    monkeypatch.setattr(installer, "status", lambda: {"running_jobs": 0, "processing_paused": True})
    poll.poll()
    assert len(installs) == 1
    assert pauses[-1] is False
    assert not (poll.STATE / "pending.json").exists()
    assert json.loads((poll.STATE / "installed.json").read_text())["revision"] == SHA


def test_poll_install_failure_blocks_further_attempts(pending_poll, monkeypatch):
    poll, pending, pauses, installs = pending_poll
    monkeypatch.setattr(installer, "status", lambda: {"running_jobs": 0, "processing_paused": True})

    def failure(*args):
        raise RuntimeError("Synthetic migration/start failure")

    monkeypatch.setattr(installer, "install", failure)
    with pytest.raises(RuntimeError, match="Synthetic"):
        poll.poll()
    assert (poll.STATE / "blocked.json").exists()
    assert pauses[-1] is True
    with pytest.raises(RuntimeError, match="Prior installation failed"):
        poll.poll()


def test_poll_crash_after_install_resumes_without_reinstalling(pending_poll, monkeypatch):
    poll, pending, pauses, installs = pending_poll
    monkeypatch.setattr(poll, "current_image", lambda: IMAGE.removeprefix("sha256:"))
    poll.poll()
    assert installs == []
    assert pauses == [False]
    assert (poll.STATE / "installed.json").exists()
