#!/bin/sh
# A task's own detached worktree at a commit (staging-review: .worktrees/<issue>-staging
# at origin/main). Run from the main checkout. route.py types only this short call into
# the pane: macOS drops typed input past 1024 bytes (MAX_CANON) while a new tab's zsh is
# still starting, so oversized commands can lose their done marker.
#
#   detached-worktree.sh <path> <commit> [<main checkout> <pnpm command...>]
#
# Reused across runs, never a main checkout: <path> moves to <commit> only when it is
# itself a worktree root (a stray plain directory would let git find the main checkout
# above it) with no tracked changes; anything else fails. Untracked files stay. With a
# main checkout and a pnpm command, node_modules is linked from the main checkout while
# the lockfiles match, else installed into the worktree, as the project's own worktree
# helper does (--deps link).
set -e
[ $# -ge 2 ] || { echo "usage: $0 <path> <commit> [<main checkout> <pnpm command...>]" >&2; exit 2; }
p=$1 c=$2
shift 2
if [ -e "$p" ]; then
  if ! { [ -f "$p/.git" ] && [ "$(git -C "$p" rev-parse --show-toplevel)" = "$(cd "$p" && pwd -P)" ]; }; then
    echo "error: $p exists but is not its own worktree; inspect it" >&2
    exit 1
  elif [ -n "$(git -C "$p" status --porcelain --untracked-files=no)" ]; then
    echo "error: $p has tracked changes; inspect it" >&2
    exit 1
  fi
  git -C "$p" checkout -q --detach "$c"
else
  git worktree add -q --detach "$p" "$c"
fi
[ $# -ge 2 ] || exit 0
root=$1
shift
cd "$p"
for d in . frontend; do
  [ -f "$d/package.json" ] || continue
  if [ -L "$d/node_modules" ]; then
    cmp -s "$root/$d/pnpm-lock.yaml" "$d/pnpm-lock.yaml" && continue
    rm "$d/node_modules"
  fi
  if [ ! -e "$d/node_modules" ] && [ -d "$root/$d/node_modules" ] && cmp -s "$root/$d/pnpm-lock.yaml" "$d/pnpm-lock.yaml"; then
    ln -s "$root/$d/node_modules" "$d/node_modules"
  else
    "$@" --dir "$d" install --frozen-lockfile
  fi
done
