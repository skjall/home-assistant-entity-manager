# Contributing

Thanks for your interest in improving the **Home Assistant Entity Manager** add-on!
This document describes the branching model, how to set up a development environment,
and the conventions we follow.

> Contributions, bug reports and ideas are very welcome.

## Branching model (GitHub Flow)

| Branch | Purpose |
|---|---|
| `main` | The single long-lived branch. Always deployable; protected (CI must pass). |
| `feature/*`, `fix/*`, `chore/*` | Your working branches, created from `main`. |

**Flow:** branch off `main` → PR into **`main`** (Conventional Commit title) → CI green →
**enable auto-merge** → squash-merge. Auto-merge keeps the branch up to date and merges
once CI is green, so no manual rebasing when `main` moves.

**Releases** are automated by [release-please](https://github.com/googleapis/release-please):
it maintains a "release PR" that bumps `version` in `config.json` and updates
`CHANGELOG.md` from the merged commits. Merging that PR tags `v<version>` and publishes the
GitHub release. See [Versioning](#versioning) and [Releasing](#releasing-maintainers).

> Open your Pull Request against **`main`**. There is no separate integration or release branch.

## Getting started

### Prerequisites

- Node.js (for the frontend build) and `npm`
- Docker (to build/run the add-on)
- Python 3.12+ (for linting locally)

### Setup

```bash
git clone https://github.com/Skjall/home-assistant-entity-manager.git
cd home-assistant-entity-manager
git checkout main
git checkout -b feature/my-change

npm ci          # install frontend dependencies
npm run build   # build CSS/JS + copy assets
```

### Build & run the add-on locally

```bash
docker build --build-arg BUILD_FROM="ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.20" -t local/entity_manager .
docker run --rm -it -p 5000:5000 \
  -e HA_URL="http://your-ha-instance:8123" \
  -e HA_TOKEN="your-long-lived-token" \
  local/entity_manager
```

### Frontend changes

The CSS is built with Tailwind, which **purges unused classes**. After adding or
changing utility classes in `templates/` or `src/`, rebuild:

```bash
npm run build:css
```

## Working on several changes in parallel

If you work on multiple independent changes at the same time (for example several
Claude Code sessions), give each one its own **git worktree** and branch so they
never collide in the main checkout and each lands in a separate branch:

```bash
scripts/feature-worktree.sh dashboard-export
#  -> ../home-assistant-entity-manager-dashboard-export
#     branch: feature/dashboard-export (based on origin/main)

cd ../home-assistant-entity-manager-dashboard-export
# ... make changes, commit ...
git push -u origin feature/dashboard-export      # open a PR against main
```

Each worktree is an independent working directory on its own branch, so parallel
work stays isolated — no patching changes back and forth between checkouts. Test a
worktree in isolation (`./deploy.sh` from inside it), and remove it once its PR is
merged:

```bash
git worktree remove ../home-assistant-entity-manager-dashboard-export
```

## Code style & conventions

Run these before opening a PR (the CI enforces them):

```bash
flake8 . --exclude=venv*,legacy,node_modules
black --check --line-length 120 --target-version py312 .
isort --check-only --profile black --line-length 120 .
pytest
```

Or install the **pre-commit hook**, which runs the same checks (black/isort/flake8 + pytest)
automatically before every commit:

```bash
pip install pre-commit && pip install -r requirements-test.txt
pre-commit install
pre-commit run --all-files   # optional: run once over everything
```

### Tests

Unit tests live in `tests/` and run offline (no Home Assistant required) — they mock the
clients. Run them with `pytest`. Please add/extend tests for backend logic you change.

- **Python:** Black (line length 120), isort (black profile), flake8. All functions need type hints and docstrings.
- **Logs in English:** all log output (`logger.*` and job/progress logs) must be English. User-facing UI strings are localized via `translations/ui/*.json`.
- **Translations:** edit `translations/ui/<lang>.json` directly (de, en, es, fr) — there is no external translation service.
- **Commits:** use [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat:`, `fix:`, `chore:`, `docs:`).

## Pull request checklist

The PR template lists the full checklist. In short:

1. Branch off `main`.
2. **PR title is a Conventional Commit** (`feat:`, `fix:`, `chore:` …) — it becomes the
   squash commit and drives the version bump (see [Versioning](#versioning)). A check enforces this.
3. Lint passes (flake8 / black / isort) and JSON files are valid.
4. Frontend changes include a rebuilt CSS (`npm run build:css`) and pass the `visual` check.
5. Describe how you tested the change (and which integration: Z2M / Matter / ZHA).
6. Open the PR against **`main`** with `Closes #<issue>`, then **enable auto-merge**.

## Issue & PR process

Issues and PRs are kept tidy with a few automated rules:

- **Every PR must reference an issue** — put `Closes #123` (or `Fixes`/`Resolves`) in the
  description. PRs without a linked issue get a `needs issue` label and a failing check.
- **Status labels are exclusive and automatic** — only one is ever set:
  `status: triage` (new) → `status: in progress` (assigned) → `status: in review`
  (a PR links the issue) → `status: done` (closed).
- **Duplicate hint** — new issues get a comment linking possibly-related existing issues.
- **Unclear request?** If a reported issue is ambiguous, we ask the reporter **in the issue**
  and set `status: needs info` rather than guessing — work pauses until they reply.
- **Review routing** — Claude posts an automated review on every (human) PR. External
  contributor PRs additionally request the maintainer's review; routine Renovate PRs don't.
- **SemVer label** — a `semver: {major,minor,patch}` label is set automatically from the PR title.

Typical flow: open an issue (→ `triage`) → assign yourself (→ `in progress`) → open a PR
with `Closes #N` against `main` (→ issue `in review`) → merge closes the issue (→ `done`).

### Where communication happens

- **The issue (English)** is the durable record: clarifying questions to the reporter,
  scope decisions, and the result (the linked PR).
- **The PR (English)** carries the technical detail: what changed, why, how it was tested.

### Lifecycle automation

- **Dependencies:** handled by Renovate via the **Dependency Dashboard** issue — Renovate
  PRs do **not** need a linked issue and are exempt from the issue requirement and the stale bot.
- **Stale issues/PRs:** inactive items are labeled `stale` after 45 days and closed after
  another 14, unless they carry `status: in progress`, `status: blocked` or `pinned`.
- **Branches:** PR branches are deleted automatically on merge. A weekly job removes branches
  already contained in `main` and reports old unmerged ones (those are never auto-deleted).

## Versioning

We follow [SemVer](https://semver.org/) (`MAJOR.MINOR.PATCH`) — and the bump is derived
automatically from the Conventional Commit PR titles, so nobody decides it by hand:

| PR title | Bump |
|---|---|
| `fix: …` | **PATCH** |
| `feat: …` | **MINOR** |
| `feat!: …` or a `BREAKING CHANGE:` footer | **MAJOR** |
| `chore: / docs: / ci: / test: / refactor: / perf: / build:` | no release |

A check enforces the title format and a `semver:*` label is applied automatically.

## Releasing (maintainers)

Releases are **release-driven and automated** by release-please:

1. As releasing commits land on `main`, release-please opens/updates a **release PR** that
   bumps `version` in `config.json` and writes `CHANGELOG.md`.
2. Merge that release PR when you want to ship — it tags `v<version>` and publishes the
   GitHub release. That merge is the only manual step.

No release branch and no back-merges — `main` is the only long-lived branch.

## Reporting bugs / requesting features

Please use the GitHub issue templates (Bug report / Feature request). For general
questions, use [Discussions](https://github.com/Skjall/home-assistant-entity-manager/discussions).
