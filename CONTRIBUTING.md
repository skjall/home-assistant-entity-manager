# Contributing

Thanks for your interest in improving the **Home Assistant Entity Manager** add-on!
This document describes the branching model, how to set up a development environment,
and the conventions we follow.

> This add-on is **beta software**. Contributions, bug reports and ideas are very welcome.

## Branching model

| Branch | Purpose |
|---|---|
| `main` | **Release branch.** Always deployable. Each push that changes `config.json` creates a tagged release. Never push directly. |
| `next-release` | **Integration branch.** All feature/fix branches are merged here first; CI runs on every push. |
| `feature/*`, `fix/*`, `chore/*` | Your working branches, created from `next-release`. |

**Flow:** `feature/*` → PR into **`next-release`** → (once stable) `next-release` → `main` → release.

> ⚠️ **Always target `next-release` with your Pull Request, not `main`.**

## Getting started

### Prerequisites

- Node.js (for the frontend build) and `npm`
- Docker (to build/run the add-on)
- Python 3.12+ (for linting locally)

### Setup

```bash
git clone https://github.com/Skjall/home-assistant-entity-manager.git
cd home-assistant-entity-manager
git checkout next-release
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
#     branch: feature/dashboard-export (based on origin/next-release)

cd ../home-assistant-entity-manager-dashboard-export
# ... make changes, commit ...
git push -u origin feature/dashboard-export      # open a PR against next-release
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
```

- **Python:** Black (line length 120), isort (black profile), flake8. All functions need type hints and docstrings.
- **Logs in English:** all log output (`logger.*` and job/progress logs) must be English. User-facing UI strings are localized via `translations/ui/*.json`.
- **Translations:** edit `translations/ui/<lang>.json` directly (de, en, es, fr) — there is no external translation service.
- **Commits:** use [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat:`, `fix:`, `chore:`, `docs:`).

## Pull request checklist

The PR template lists the full checklist. In short:

1. Branch off `next-release` and keep it up to date.
2. Lint passes (flake8 / black / isort) and JSON files are valid.
3. Frontend changes include a rebuilt CSS (`npm run build:css`).
4. Describe how you tested the change (and which integration: Z2M / Matter / ZHA).
5. Open the PR against **`next-release`**.

## Releasing (maintainers)

1. Merge `next-release` into `main`.
2. Bump `version` in `config.json` (keeps the `-beta` suffix for now).
3. The `Release` workflow tags `v<version>` and publishes a GitHub release automatically.

## Reporting bugs / requesting features

Please use the GitHub issue templates (Bug report / Feature request). For general
questions, use [Discussions](https://github.com/Skjall/home-assistant-entity-manager/discussions).
