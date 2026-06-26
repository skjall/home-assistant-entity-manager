#!/usr/bin/env bash
#
# feature-worktree.sh — create an isolated git worktree + feature branch.
#
# For working on several changes in parallel (e.g. multiple Claude sessions):
# each task gets its own working directory and its own branch off `next-release`,
# so the changes never collide in the main checkout.
#
# Usage:
#   scripts/feature-worktree.sh <short-name> [base-branch]
#
# Examples:
#   scripts/feature-worktree.sh dashboard-export
#   scripts/feature-worktree.sh fix-z2m-timeout fix
#
# Creates:
#   ../<repo>-<slug>      worktree directory (sibling of the repo)
#   feature/<slug>        branch, based on origin/next-release
#
# When done:
#   cd <worktree>
#   git push -u origin <branch>        # then open a PR against next-release
#   cd -                               # back to main checkout
#   git worktree remove ../<repo>-<slug>
#
set -euo pipefail

name="${1:?Usage: feature-worktree.sh <short-name> [type: feature|fix|chore]}"
type="${2:-feature}"

# slugify: lowercase, spaces/underscores -> dashes, strip junk
slug="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]' | tr ' _' '--' | tr -cd 'a-z0-9-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
[ -n "$slug" ] || { echo "error: name produced an empty slug" >&2; exit 1; }

branch="${type}/${slug}"
repo_name="$(basename "$(git rev-parse --show-toplevel)")"
dir="../${repo_name}-${slug}"

git fetch origin --quiet

if git show-ref --quiet "refs/heads/${branch}"; then
  echo "error: branch '${branch}' already exists" >&2
  exit 1
fi

git worktree add "$dir" -b "$branch" origin/next-release

cat <<EOF

✓ Worktree: ${dir}
✓ Branch:   ${branch}  (based on origin/next-release)

Next:
  cd "${dir}"
  # ... make changes, commit ...
  git push -u origin ${branch}        # then open a PR against 'next-release'

Cleanup when merged:
  git worktree remove "${dir}"
EOF
