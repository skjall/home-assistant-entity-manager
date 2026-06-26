<!--
  Thanks for contributing! Branch off `main`, open your PR against `main`.
  Once CI is green it gets squash-merged. Releases are created by bumping the
  version in config.json on main (the Release workflow tags it).
-->

## Description

<!-- What does this PR change and why? -->

## Type of change

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 💥 Breaking change (fix or feature that changes existing behavior)
- [ ] 📝 Documentation / chore

## Related issues

<!-- e.g. Closes #123 -->

## Checklist

- [ ] My branch is up to date with `main`
- [ ] Lint passes locally: `flake8`, `black --check --line-length 120`, `isort --check-only --profile black --line-length 120`, `pytest`
- [ ] New/changed Python functions have type hints and docstrings
- [ ] Log messages are in English (UI strings may be localized via `translations/`)
- [ ] If frontend changed: ran `npm run build:css` (Tailwind purges unused classes)
- [ ] I tested the change (describe how below)

## How was this tested?

<!-- Manual steps, affected integration (Z2M / Matter / ZHA), etc. -->
