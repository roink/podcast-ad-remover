# Maintaining the NAS fork

`roink/podcast-ad-remover` maintains its customizations on **nas**, its default
branch. `upstream` is `jdcb4/podcast-ad-remover`; `origin` is this fork. Never open
PRs against upstream as part of this workflow. Upstream's `dev`/`main`, Docker Hub
and Pages release procedures remain upstream documentation, not fork operations.

## What we carry

| Customization | Main implementation | Regression coverage |
| --- | --- | --- |
| Per-show EN/DE/Automatic language and Moonshine Small | `moonshine_transcription.py`, `moonshine_worker.py`, models/repository/UI | `test_language_sound_events.py` |
| YAMNet evidence with process isolation | `sound_events.py`, `yamnet_worker.py`, timeline/processor | `test_language_sound_events.py` |
| Previously missed ad/promo excerpts per show | subscription field, prompt snapshot, settings UI/API | `test_missed_ad_examples.py` |

Keep future features in focused commits/modules with regression tests. Do not
rewrite already deployed commits merely to split historical work. Check schema
migrations for collisions on every upstream merge; use descriptive NAS-prefixed
migration IDs for future fork-only migrations. Existing deployed IDs stay intact.
If upstream implements equivalent functionality later, deliberately reconcile it
and preserve stored settings rather than layering two competing implementations.

## Normal changes

Use Python 3.11 and Node 24, with development dependencies installed:

```bash
python -m pip install -r requirements-dev.txt
npm ci
git fetch origin
git switch nas
git pull --ff-only origin nas
git switch -c feature/my-change
npm run verify
uvx ruff==0.17.3 check
uvx ruff==0.17.3 format --check
```

Commit, push the feature branch to **origin**, and open a PR explicitly targeting
`roink/podcast-ad-remover:nas`. Use merge commits so deployed history and upstream
ancestry are retained. The NAS candidate workflow verifies code and builds/tests
the complete image for PRs. No PR build publishes or deploys an image.

## Upstream proposals

`nas/maintenance.json` records the fork, upstream production branch (`main`) and
fork GHCR image repository. The weekly **Propose upstream production update**
workflow fetches that branch and skips revisions already contained in `nas`.
It creates a `update/upstream-<sha>` branch and a PR **inside this fork**. This tracks
upstream's production branch, not its development branch; upstream's version and
migration changes still require review. It does not automatically merge updates.

A clean merge is committed on the proposal branch and CI is explicitly dispatched
because GitHub-token-created PRs/pushes do not trigger ordinary workflows. A
conflicting merge produces a draft PR pointing at the exact upstream commit;
GitHub shows conflicts with `nas`. No NAS changes are thrown away. Resolve the
conflicts on that proposal branch, preserving both histories, then push and mark
ready. Human pushes/PR events trigger normal CI. Closed proposals are not reopened
automatically. An interrupted push without a PR requires manual inspection.

The workflow can also be started manually:

```bash
gh workflow run nas-upstream.yml --repo roink/podcast-ad-remover --ref nas
```

Review migrations/backups, dependency/model changes and the custom-feature tests.
Run synthetic/local model qualification when inference paths change. CI does not
measure real episode accuracy or NAS peak RAM. Merge only after checks pass.

## Image identity and CI

A successful **NAS candidate** run on `nas` verifies Python, tests, CSS and audits,
builds the Dockerfile, and runs the offline container smoke check with disposable
data and no network. It publishes only
`ghcr.io/roink/podcast-ad-remover:nas-<full-commit>` and uploads a small
`nas-release` artifact containing `nas-release.json` (90-day retention).
The manifest records the exact source commit, image ID and registry digest.
Deployment pulls the **digest**, checks source/image identities and requires a
successful matching NAS workflow run. No upstream Docker Hub tags or Pages are
published. Source commits alone do not guarantee bit-identical rebuilds; deploy
the tested image, and rerun CI deliberately for refreshed build dependencies.

GitHub Packages initially may be private. The deployment helper tries anonymous
pull, then uses your workstation's `gh` credential in a temporary Docker config.
That credential needs package read access if the package is private. No registry
credential or production secret is sent to GitHub CI or installed on the NAS.
The workflow's short-lived GitHub token publishes images. Build publication is
automatic; installation is an explicit operator action.

## Deploy from the workstation

Requires Git, GitHub CLI authenticated to the fork, Docker, SSH and passwordless
sudo on the NAS. Host/user is supplied explicitly and is not committed as config.
The NAS needs its existing Podman/systemd/ZFS/storage-readiness tooling and private
per-service environment file. Work from an up-to-date checkout of `nas`.

```bash
python scripts/nas_release.py --host USER@NAS --check
gh run list --repo roink/podcast-ad-remover --workflow nas-ci.yml --branch nas
python scripts/nas_release.py --host USER@NAS --run SUCCESSFUL_RUN_ID
```

`--check` reads service/pool/pause/job status only. Installation downloads the
small CI manifest, validates the successful run/source, pulls the exact image,
loads it on the NAS, and stages a `git archive` of that source revision. Live
`/data`, private environments and registry credentials are never in the archive.

The installer shares `/run/nas-container-maintenance.lock` with existing guarded
maintenance, checks storage/pool and failure markers, saves the current pause
setting, pauses new jobs and waits up to 300 seconds for current work. If the
job doesn't finish, it restores the original pause setting and defers. Use
`--wait-seconds N` to wait longer. `--restart-job` deliberately stops unfinished
work and recovers it with the outgoing application's routine; transcription may
repeat. Use it only when restarting current work is intended.

After stopping the service, installation takes a WAL-safe database backup and
ZFS recovery snapshots, retains a dated outgoing image tag, updates the versioned
source context and local current image, starts the service and verifies readiness
and image identity. It restores the original pause setting only on success.
Failure after stopping leaves processing paused and a `needs-review` marker in
the deployment recovery directory. It does not automatically overwrite a database
that may already have migrated. Retrying the same source-context path requires
inspection rather than overwriting it. Staging/recovery copies remain on the NAS.

Existing published episodes and frozen queued snapshots are preserved. The NAS
continues to run the reviewed local image with `Pull=never`; scheduled source
updates do not contact it. Podcast monthly rebuilding is disabled in the NAS
maintenance policy (`rebuild: false`) so it cannot replace a tested image with an
untested rebuild. Other services retain their existing schedules.

## Recovery

`/tank/services/compose/deployments/podcast-nas-<timestamp>/deployment.json`
records the previous image, previous source context and previous pause state.
The directory contains `podcasts-before.db`; snapshots are named
`@before-podcast-nas-<timestamp>`. The outgoing image has a dated rollback tag.

Stop the podcast service, preserve any work since the attempted update, and
restore matching **podcast-specific** database/application files and the outgoing
image/source-context link. Account for SQLite WAL/SHM files when restoring a stopped
database. Never roll back an entire shared dataset over unrelated running services.
Start and check readiness before restoring the recorded pause state. Keep the
recovery point until real episode acceptance has passed.
