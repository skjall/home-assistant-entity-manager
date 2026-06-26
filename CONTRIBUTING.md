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

**Flow:** branch off `main` → PR into **`main`** → CI green → **squash-merge**. That's it.

**Releases** are just tags: bump `version` in `config.json` on `main`, and the `Release`
workflow tags `v<version>` + publishes a GitHub release. No separate release branch.

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

1. Branch off `main` and keep it up to date.
2. Lint passes (flake8 / black / isort) and JSON files are valid.
3. Frontend changes include a rebuilt CSS (`npm run build:css`).
4. Describe how you tested the change (and which integration: Z2M / Matter / ZHA).
5. Open the PR against **`main`**.

## Issue & PR process

Issues and PRs are kept tidy with a few automated rules:

- **Every PR must reference an issue** — put `Closes #123` (or `Fixes`/`Resolves`) in the
  description. PRs without a linked issue get a `needs issue` label and a failing check.
- **Status labels are exclusive and automatic** — only one is ever set:
  `status: triage` (new) → `status: in progress` (assigned) → `status: in review`
  (a PR links the issue) → `status: done` (closed).
- **Duplicate hint** — new issues get a comment linking possibly-related existing issues.

Typical flow: open an issue (→ `triage`) → assign yourself (→ `in progress`) → open a PR
with `Closes #N` against `main` (→ issue `in review`) → merge closes the issue (→ `done`).

### Lifecycle automation

- **Dependencies:** handled by Renovate via the **Dependency Dashboard** issue — Renovate
  PRs do **not** need a linked issue and are exempt from the issue requirement and the stale bot.
- **Stale issues/PRs:** inactive items are labeled `stale` after 45 days and closed after
  another 14, unless they carry `status: in progress`, `status: blocked` or `pinned`.
- **Branches:** PR branches are deleted automatically on merge. A weekly job removes branches
  already contained in `main` and reports old unmerged ones (those are never auto-deleted).

## Releasing (maintainers)

1. Bump `version` in `config.json` on `main` (keeps the `-beta` suffix for now).
2. The `Release` workflow tags `v<version>` and publishes a GitHub release automatically.

No release branch and no back-merges — `main` is the only long-lived branch.

## Reporting bugs / requesting features

Please use the GitHub issue templates (Bug report / Feature request). For general
questions, use [Discussions](https://github.com/Skjall/home-assistant-entity-manager/discussions).
