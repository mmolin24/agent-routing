#!/usr/bin/env bash
# Copy into web-app/scripts/issue-worktree.sh; invoked from the main checkout.
# Minimal helper example. Existing worktrees/branches need inspection, not reset.
set -euo pipefail
[ "$#" -eq 4 ] || { echo 'usage: issue-worktree.sh ISSUE COMMIT link|install|none link|none' >&2; exit 2; }
issue=$1
commit=$2
deps=$3
env_mode=$4
[[ "$issue" =~ ^[A-Za-z]{2,10}-[0-9]+$ && "$commit" =~ ^[0-9a-f]{40}$ ]] || exit 2
case "$deps" in link|install|none) ;; *) exit 2 ;; esac
case "$env_mode" in link|none) ;; *) exit 2 ;; esac
repo=$(git rev-parse --show-toplevel)
worktree="$repo/.worktrees/$issue"
branch="feature/$issue"
if [ -e "$worktree" ] || [ -L "$worktree" ] || git show-ref --verify --quiet "refs/heads/$branch"; then
    echo "Worktree or branch already exists; inspect $worktree and $branch before continuing." >&2
    exit 1
fi
mkdir -p "$repo/.worktrees"
git worktree add -b "$branch" "$worktree" "$commit"
case "$deps" in
    link) [ ! -d "$repo/node_modules" ] || ln -s "$repo/node_modules" "$worktree/node_modules" ;;
    install) (cd "$worktree" && pnpm install --frozen-lockfile) ;;
esac
if [ "$env_mode" = link ] && [ -f "$repo/.env" ]; then
    ln -s "$repo/.env" "$worktree/.env"
fi
[ "$(git -C "$worktree" rev-parse HEAD)" = "$commit" ]
